"""Unit tests for llm/openai_client.py. No test makes a real API call or needs a key.

Run:  python -m pytest tests/test_llm_client.py -q
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, Optional

import httpx
import openai
import pytest
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm.openai_client import (DEFAULT_MODEL, CORRECTION_MESSAGE, LLMAPIError, LLMClient,  # noqa: E402
                               LLMConfig, LLMConfigError, LLMOutputError, LLMTimeoutError,
                               mock_instance)

SECRET = "sk-test-SECRET-1234567890abcdef"


class Answer(BaseModel):
    answer: str
    confidence: int = Field(ge=0, le=100)
    kind: Literal["fact", "calc"]
    note: Optional[str] = None


def _usage(i=10, o=5):
    return SimpleNamespace(input_tokens=i, output_tokens=o, total_tokens=i + o)


def _ok(parsed=None, text="hello"):
    return SimpleNamespace(output_parsed=parsed, output_text=text, usage=_usage())


class FakeResponses:
    """Scripted fake of client.responses. Each item is a response or an exception to raise."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def _next(self, kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def parse(self, **kwargs):
        return self._next(kwargs)

    def create(self, **kwargs):
        return self._next(kwargs)


def fake_client(script, **cfg):
    responses = FakeResponses(script)
    config = LLMConfig(api_key=SECRET, **cfg)
    return LLMClient(config, sdk_client=SimpleNamespace(responses=responses)), responses


def _status_error(cls, status):
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    resp = httpx.Response(status, request=req, headers={"x-request-id": "r1"})
    return cls(f"Incorrect API key provided: {SECRET}", response=resp, body=None)


GOOD = Answer(answer="80%", confidence=90, kind="fact")


# ----------------------------------------------------------------- configuration
class TestConfig:
    def test_defaults(self, monkeypatch):
        for v in ("OPENAI_MODEL", "OPENAI_TIMEOUT_SECONDS", "OPENAI_MAX_RETRIES", "MOCK_LLM"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", SECRET)
        cfg = LLMConfig.from_env()
        assert cfg.model == DEFAULT_MODEL == "gpt-6-luna"
        assert cfg.timeout_seconds == 30.0 and cfg.sdk_max_retries == 1 and cfg.mock is False

    def test_model_is_configurable(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", SECRET)
        monkeypatch.setenv("OPENAI_MODEL", "my-model")
        monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "12.5")
        cfg = LLMConfig.from_env()
        assert cfg.model == "my-model" and cfg.timeout_seconds == 12.5

    @pytest.mark.parametrize("name,value", [("OPENAI_TIMEOUT_SECONDS", "abc"), ("OPENAI_TIMEOUT_SECONDS", "0"),
                                            ("OPENAI_MAX_RETRIES", "x"), ("OPENAI_MAX_RETRIES", "-1")])
    def test_bad_numeric_settings_rejected(self, monkeypatch, name, value):
        monkeypatch.setenv(name, value)
        with pytest.raises(LLMConfigError):
            LLMConfig.from_env()

    def test_missing_key_fails_clearly_without_mock(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("MOCK_LLM", raising=False)
        with pytest.raises(LLMConfigError, match="OPENAI_API_KEY"):
            LLMClient()

    def test_key_never_in_repr_or_str(self):
        cfg = LLMConfig(api_key=SECRET)
        assert SECRET not in repr(cfg) and SECRET not in str(cfg)


# ----------------------------------------------------------------- mock mode
class TestMock:
    def test_mock_needs_no_key_and_makes_no_sdk_call(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("MOCK_LLM", "true")

        def boom(*a, **k):
            raise AssertionError("OpenAI SDK must not be constructed in mock mode")
        monkeypatch.setattr(openai, "OpenAI", boom)
        client = LLMClient()
        text = client.generate_text("sys", "user")
        structured = client.generate_structured("sys", "user", Answer)
        assert client.mock and text.mock and structured.mock
        assert text.text.startswith("[MOCK]") and text.model == "mock-llm"
        assert isinstance(structured.parsed, Answer)

    @pytest.mark.parametrize("flag", ["true", "TRUE", "1", "yes"])
    def test_mock_flag_spellings(self, monkeypatch, flag):
        monkeypatch.setenv("MOCK_LLM", flag)
        assert LLMConfig.from_env().mock is True

    def test_mock_instance_handles_enums_optionals_nested(self):
        class Inner(BaseModel):
            n: int = Field(ge=3)

        class Outer(BaseModel):
            kind: Literal["a", "b"]
            maybe: Optional[str] = None
            inner: Inner
            items: list[str]

        out = mock_instance(Outer)
        assert out.kind == "a" and out.inner.n == 3 and out.items == []


# ----------------------------------------------------------------- real path (fake SDK)
class TestGenerateText:
    def test_returns_text_usage_and_model(self):
        client, fake = fake_client([_ok(text="Grounded answer")])
        res = client.generate_text("system prompt", "user prompt")
        assert res.text == "Grounded answer" and res.total_tokens == 15 and res.llm_calls == 1
        assert res.model == DEFAULT_MODEL and not res.mock
        sent = fake.calls[0]
        assert sent["model"] == DEFAULT_MODEL
        assert sent["input"][0] == {"role": "system", "content": "system prompt"}
        assert sent["input"][1] == {"role": "user", "content": "user prompt"}

    def test_empty_text_is_an_error(self):
        client, _ = fake_client([SimpleNamespace(output_text="  ", usage=_usage())])
        with pytest.raises(LLMOutputError):
            client.generate_text("s", "u")


class TestStructured:
    def test_success_first_try_uses_responses_parse_with_schema(self):
        client, fake = fake_client([_ok(GOOD)])
        res = client.generate_structured("s", "u", Answer)
        assert res.parsed == GOOD and res.llm_calls == 1 and not res.retried
        assert fake.calls[0]["text_format"] is Answer

    def test_invalid_output_retried_exactly_once_then_succeeds(self):
        try:
            Answer.model_validate({"answer": "x", "confidence": 500, "kind": "fact"})
        except Exception as exc:  # a real pydantic ValidationError, as parse() would raise
            bad = exc
        client, fake = fake_client([bad, _ok(GOOD)])
        res = client.generate_structured("s", "u", Answer)
        assert res.parsed == GOOD and res.retried and res.llm_calls == 2
        assert fake.calls[1]["input"][-1]["content"] == CORRECTION_MESSAGE
        assert len(fake.calls[1]["input"]) == 3  # no growth beyond one correction message

    def test_never_more_than_one_retry(self):
        client, fake = fake_client([_ok(None), _ok(None), _ok(GOOD)])  # third item must stay unused
        with pytest.raises(LLMOutputError, match="after one retry"):
            client.generate_structured("s", "u", Answer)
        assert len(fake.calls) == 2

    def test_refusal_is_not_trusted(self):
        client, _ = fake_client([_ok(None), _ok(None)])
        with pytest.raises(LLMOutputError):
            client.generate_structured("s", "u", Answer)

    def test_truncated_output_is_treated_as_invalid_output_and_retried(self):
        err = openai.LengthFinishReasonError(completion=SimpleNamespace(usage=None))
        client, fake = fake_client([err, _ok(GOOD)])
        assert client.generate_structured("s", "u", Answer).retried
        assert len(fake.calls) == 2

    def test_json_decode_style_value_error_is_retried(self):
        client, fake = fake_client([ValueError("bad json"), _ok(GOOD)])
        assert client.generate_structured("s", "u", Answer).parsed == GOOD

    def test_token_usage_accumulates_across_retry(self):
        client, _ = fake_client([_ok(None), _ok(GOOD)])
        res = client.generate_structured("s", "u", Answer)
        assert res.total_tokens == 30 and res.llm_calls == 2


# ----------------------------------------------------------------- failures
class TestFailures:
    def test_timeout_maps_to_llm_timeout_and_is_not_retried(self):
        err = openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
        client, fake = fake_client([err, _ok(GOOD)], timeout_seconds=7)
        with pytest.raises(LLMTimeoutError, match="7s"):
            client.generate_structured("s", "u", Answer)
        assert len(fake.calls) == 1

    @pytest.mark.parametrize("cls,status", [(openai.AuthenticationError, 401), (openai.RateLimitError, 429),
                                            (openai.NotFoundError, 404), (openai.PermissionDeniedError, 403),
                                            (openai.InternalServerError, 500)])
    def test_api_errors_are_translated_and_never_leak_the_key(self, cls, status):
        client, fake = fake_client([_status_error(cls, status)])
        with pytest.raises(LLMAPIError) as info:
            client.generate_text("s", "u")
        assert info.value.status_code == status
        assert SECRET not in str(info.value) and "sk-" not in str(info.value)
        assert len(fake.calls) == 1

    def test_connection_error(self):
        err = openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
        client, _ = fake_client([err])
        with pytest.raises(LLMAPIError, match="connect"):
            client.generate_structured("s", "u", Answer)

    def test_unexpected_provider_exception_message_is_redacted(self):
        from llm.openai_client import LLMError
        assert SECRET not in str(LLMError(f"boom {SECRET}"))


# ----------------------------------------------------------------- secrecy
def test_prompts_answers_and_key_are_not_logged(caplog):
    client, _ = fake_client([_ok(GOOD)])
    with caplog.at_level(logging.DEBUG, logger="llm"):
        client.generate_structured("PRIVATE-SYSTEM-PROMPT", "PRIVATE-USER-QUESTION", Answer)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "PRIVATE" not in logged and SECRET not in logged and "80%" not in logged
    assert "model=" in logged  # metadata only
