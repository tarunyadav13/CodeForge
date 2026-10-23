"""Offline tests for the OpenAI client using a FAKE SDK object (no API key, no network).

Where a test needs real SDK classes (exceptions, signatures) it uses the installed
``openai`` package; those tests are skipped if the package is absent.
"""
import inspect
import logging
import re
from types import SimpleNamespace
from typing import Optional

import pytest
from pydantic import BaseModel

from llm import LLMAPIError, LLMConfigError, LLMOutputError, LLMTimeoutError
from llm.openai_client import DEFAULT_MODEL, OpenAIClient

openai = pytest.importorskip("openai")
try:  # openai>=3 ships its HTTP layer as `httpx2`; older versions use `httpx`
    import httpx
except ImportError:
    httpx = pytest.importorskip("httpx2")

URL = "https://api.openai.com/v1/responses"
SECRET = "sk-proj-SECRETKEY1234567890abcdef"


class Synthesis(BaseModel):
    answer: str
    cited_chunk_ids: list[str] = []
    note: Optional[str] = None


# ------------------------------------------------------------------ fakes
class FakeResponses:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeModels:
    def __init__(self, outcome=None):
        self.outcome = outcome
        self.requested = []

    def retrieve(self, model):
        self.requested.append(model)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return SimpleNamespace(id=model)


class FakeSDK:
    def __init__(self, outcomes=(), models=None):
        self.responses = FakeResponses(outcomes)
        self.models = models or FakeModels()


def resp(text="hello", *, status="completed", usage=(10, 5), rid="resp_1", reason=None, refusal=None, error=None):
    output = []
    if refusal is not None:
        output = [SimpleNamespace(type="message", content=[SimpleNamespace(type="refusal", refusal=refusal)])]
    return SimpleNamespace(
        id=rid,
        status=status,
        output_text=text,
        output=output,
        usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1], total_tokens=usage[0] + usage[1]),
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        error=SimpleNamespace(message=error) if error else None,
    )


def client_with(*outcomes, **kwargs):
    sdk = FakeSDK(outcomes)
    return OpenAIClient(sdk_client=sdk, **kwargs), sdk


def status_error(cls, code, message="boom"):
    request = httpx.Request("POST", URL)
    return cls(message, response=httpx.Response(code, request=request), body=None)


# ------------------------------------------------------------------ plain text
def test_plain_text_success_maps_usage_and_request_shape():
    llm, sdk = client_with(resp("The answer.", usage=(120, 30)))
    result = llm.generate(system="SYSTEM", user="USER")

    assert result.text == "The answer." and result.parsed is None
    assert (result.provider, result.model) == ("openai", DEFAULT_MODEL)
    assert (result.usage.input_tokens, result.usage.output_tokens, result.usage.total_tokens) == (120, 30, 150)
    assert result.llm_calls == 1 and result.response_id == "resp_1"

    (request,) = sdk.responses.requests
    assert request["model"] == DEFAULT_MODEL
    assert request["instructions"] == "SYSTEM"
    assert request["input"] == [{"role": "user", "content": "USER"}]
    assert request["store"] is False
    assert request["reasoning"] == {"effort": "low"}
    assert request["max_output_tokens"] == 2000
    assert "temperature" not in request and "text" not in request


def test_reasoning_effort_can_be_omitted_for_non_reasoning_models():
    llm, sdk = client_with(resp(), reasoning_effort=None, model="some-model")
    llm.generate(system="s", user="u")
    assert "reasoning" not in sdk.responses.requests[0]
    assert sdk.responses.requests[0]["model"] == "some-model"


def test_empty_text_is_retried_once_then_fails():
    llm, sdk = client_with(resp(""), resp("   "))
    with pytest.raises(LLMOutputError) as info:
        llm.generate(system="s", user="u")
    assert info.value.kind == "empty"
    assert len(sdk.responses.requests) == 2
    assert info.value.llm_calls == 2 and info.value.usage.total_tokens == 30


# ------------------------------------------------------------------ structured output
def test_structured_request_uses_strict_json_schema():
    llm, sdk = client_with(resp('{"answer": "ok", "cited_chunk_ids": ["c1"], "note": null}'))
    result = llm.generate(system="s", user="u", response_model=Synthesis)

    assert result.parsed == Synthesis(answer="ok", cited_chunk_ids=["c1"])
    fmt = sdk.responses.requests[0]["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", fmt["name"])
    schema = fmt["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"answer", "cited_chunk_ids", "note"}


def test_invalid_output_gets_exactly_one_corrective_retry_and_usage_is_summed():
    bad = resp('{"answer": 1}', usage=(10, 5), rid="r1")
    good = resp('{"answer": "fixed"}', usage=(12, 6), rid="r2")
    llm, sdk = client_with(bad, good)

    result = llm.generate(system="s", user="the question", response_model=Synthesis)

    assert result.parsed.answer == "fixed"
    assert result.llm_calls == 2 and len(sdk.responses.requests) == 2
    assert (result.usage.input_tokens, result.usage.output_tokens, result.usage.total_tokens) == (22, 11, 33)
    assert result.response_id == "r2"
    second_input = sdk.responses.requests[1]["input"]
    assert second_input[0] == {"role": "user", "content": "the question"}
    assert "could not be used" in second_input[1]["content"] and "answer: string_type" in second_input[1]["content"]
    assert '{"answer": 1}' not in str(second_input)  # the bad output is not echoed back


def test_invalid_twice_raises_and_never_makes_a_third_call():
    llm, sdk = client_with(resp("not json"), resp("still not json"), resp('{"answer": "never reached"}'))
    with pytest.raises(LLMOutputError) as info:
        llm.generate(system="s", user="u", response_model=Synthesis)
    assert info.value.kind == "invalid"
    assert len(sdk.responses.requests) == 2
    assert info.value.llm_calls == 2 and info.value.usage.total_tokens == 30


def test_refusal_is_terminal_not_retried():
    llm, sdk = client_with(resp("", refusal="I can't help with that."))
    with pytest.raises(LLMOutputError) as info:
        llm.generate(system="s", user="u", response_model=Synthesis)
    assert info.value.kind == "refusal" and len(sdk.responses.requests) == 1


def test_incomplete_response_is_terminal_and_mentions_token_setting():
    llm, sdk = client_with(resp('{"answer": "cut', status="incomplete", reason="max_output_tokens", usage=(50, 2000)))
    with pytest.raises(LLMOutputError) as info:
        llm.generate(system="s", user="u", response_model=Synthesis)
    assert info.value.kind == "incomplete" and "OPENAI_MAX_OUTPUT_TOKENS" in str(info.value)
    assert len(sdk.responses.requests) == 1
    assert info.value.usage.output_tokens == 2000 and info.value.llm_calls == 1


def test_failed_response_status_raises_api_error():
    llm, _ = client_with(resp("", status="failed", error="upstream problem"))
    with pytest.raises(LLMAPIError) as info:
        llm.generate(system="s", user="u")
    assert info.value.kind == "response_failed" and info.value.detail == "upstream problem"


# ------------------------------------------------------------------ API / transport errors
def test_timeout_maps_to_timeout_error_without_retry():
    llm, sdk = client_with(openai.APITimeoutError(request=httpx.Request("POST", URL)))
    with pytest.raises(LLMTimeoutError) as info:
        llm.generate(system="s", user="u")
    assert info.value.kind == "timeout" and len(sdk.responses.requests) == 1 and info.value.llm_calls == 1


@pytest.mark.parametrize(
    "exc, kind, status",
    [
        (status_error(openai.AuthenticationError, 401), "auth", 401),
        (status_error(openai.PermissionDeniedError, 403), "auth", 403),
        (status_error(openai.RateLimitError, 429), "rate_limit", 429),
        (status_error(openai.BadRequestError, 400), "bad_request", 400),
        (status_error(openai.NotFoundError, 404), "not_found", 404),
        (status_error(openai.InternalServerError, 500), "server", 500),
        (openai.APIConnectionError(request=httpx.Request("POST", URL)), "connection", None),
    ],
)
def test_sdk_errors_map_to_controlled_errors(exc, kind, status):
    llm, sdk = client_with(exc)
    with pytest.raises(LLMAPIError) as info:
        llm.generate(system="s", user="u")
    assert info.value.kind == kind and info.value.status_code == status
    assert len(sdk.responses.requests) == 1  # API errors are never retried by this client


def test_api_key_in_error_text_never_leaks(caplog):
    caplog.set_level(logging.DEBUG, logger="llm")
    exc = status_error(openai.AuthenticationError, 401, f"Incorrect API key provided: {SECRET}.")
    llm, _ = client_with(exc)
    with pytest.raises(LLMAPIError) as info:
        llm.generate(system="s", user="u")
    leaked = [str(info.value), repr(info.value), info.value.detail, caplog.text]
    assert all("SECRETKEY" not in text for text in leaked)


def test_unexpected_programming_errors_are_not_swallowed():
    llm, _ = client_with(RuntimeError("bug in our code"))
    with pytest.raises(RuntimeError):
        llm.generate(system="s", user="u")


def test_prompts_and_answers_are_never_logged(caplog):
    caplog.set_level(logging.DEBUG, logger="llm")
    llm, _ = client_with(resp("SENSITIVE-ANSWER"), resp("", refusal="no"))
    llm.generate(system="SENSITIVE-SYSTEM", user="SENSITIVE-PROMPT")
    with pytest.raises(LLMOutputError):
        llm.generate(system="SENSITIVE-SYSTEM", user="SENSITIVE-PROMPT")
    assert "llm_call" in caplog.text and "llm_failure" in caplog.text
    assert "SENSITIVE" not in caplog.text


# ------------------------------------------------------------------ health
def test_health_ok_only_after_a_successful_request():
    sdk = FakeSDK(models=FakeModels())
    health = OpenAIClient(sdk_client=sdk).health()
    assert health.status == "ok" and sdk.models.requested == [DEFAULT_MODEL]


def test_health_down_on_error_and_never_raises():
    sdk = FakeSDK(models=FakeModels(status_error(openai.AuthenticationError, 401, f"bad {SECRET}")))
    health = OpenAIClient(sdk_client=sdk).health()
    assert health.status == "down" and "auth" in health.detail and "SECRETKEY" not in health.detail


def test_mock_health_never_claims_the_model_is_running():
    from llm import MockLLM

    assert MockLLM().health().status == "mock"


# ------------------------------------------------------------------ real SDK, no network
def test_from_env_defaults_and_overrides_with_real_sdk_object():
    default = OpenAIClient.from_env({"OPENAI_API_KEY": "sk-test-123456"})
    assert default.model == DEFAULT_MODEL
    assert default._sdk.timeout == 30.0 and default._sdk.max_retries == 1

    custom = OpenAIClient.from_env(
        {
            "OPENAI_API_KEY": "sk-test-123456",
            "OPENAI_MODEL": "gpt-5.4-mini",
            "OPENAI_TIMEOUT_S": "12.5",
            "OPENAI_MAX_RETRIES": "0",
            "OPENAI_MAX_OUTPUT_TOKENS": "500",
            "OPENAI_REASONING_EFFORT": "OFF",
        }
    )
    assert custom.model == "gpt-5.4-mini" and custom._sdk.timeout == 12.5 and custom._sdk.max_retries == 0
    assert custom._max_output_tokens == 500 and custom._reasoning_effort is None


@pytest.mark.parametrize(
    "env, name",
    [
        ({"OPENAI_TIMEOUT_S": "abc"}, "OPENAI_TIMEOUT_S"),
        ({"OPENAI_TIMEOUT_S": "0"}, "OPENAI_TIMEOUT_S"),
        ({"OPENAI_MAX_RETRIES": "-1"}, "OPENAI_MAX_RETRIES"),
        ({"OPENAI_MAX_OUTPUT_TOKENS": "10"}, "OPENAI_MAX_OUTPUT_TOKENS"),
        ({"OPENAI_REASONING_EFFORT": "turbo"}, "OPENAI_REASONING_EFFORT"),
    ],
)
def test_invalid_env_values_raise_config_error_naming_the_variable(env, name):
    with pytest.raises(LLMConfigError) as info:
        OpenAIClient.from_env({"OPENAI_API_KEY": "sk-test-123456", **env})
    assert name in str(info.value)


def test_client_does_not_keep_the_api_key():
    client = OpenAIClient(api_key=SECRET)
    assert SECRET not in repr(client)
    assert all(value != SECRET for value in vars(client).values())


def test_request_parameters_exist_on_the_real_sdk_signature():
    """Catches typos / SDK drift: every kwarg we send must be a real responses.create parameter."""
    llm, sdk = client_with(resp('{"answer": "ok"}'))
    llm.generate(system="s", user="u", response_model=Synthesis)
    sent = set(sdk.responses.requests[0])

    real = openai.OpenAI(api_key="sk-test-123456")
    accepted = set(inspect.signature(real.responses.create).parameters)
    assert sent <= accepted, f"unknown parameters: {sent - accepted}"
    assert "retrieve" in dir(real.models)
