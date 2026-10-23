"""Offline tests for the provider-neutral layer: MockLLM, factory, error hygiene."""
import sys
from typing import Literal, Optional

import pytest
from pydantic import BaseModel

from llm import MockLLM, get_llm, is_mock_enabled
from llm.client import LLMConfigError, LLMOutputError
from llm.client import MOCK_TEXT, scrub


class Inner(BaseModel):
    doc_id: str
    page: Optional[int] = None


class Example(BaseModel):
    answer: str
    count: int
    ratio: float
    flag: bool
    kind: Literal["policy", "personal"]
    tags: list[str]
    inner: Inner
    note: Optional[str] = None


def test_mock_switch_values():
    for value in ("1", "true", "TRUE", " yes ", "on"):
        assert is_mock_enabled({"MOCK_LLM": value})
    for value in ("", "0", "false", "no", "off", "maybe"):
        assert not is_mock_enabled({"MOCK_LLM": value})
    assert not is_mock_enabled({})


def test_get_llm_mock_needs_no_key_and_no_openai_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", None)  # `import openai` now raises ImportError
    llm = get_llm({"MOCK_LLM": "true"})  # no OPENAI_API_KEY either
    assert isinstance(llm, MockLLM)
    assert (llm.provider, llm.model) == ("mock", "MOCK")


def test_mock_text_is_labelled_and_makes_no_network_call():
    result = MockLLM().generate(system="s", user="u")  # conftest fails the test on any connect()
    assert result.text == MOCK_TEXT and result.text.startswith("[MOCK LLM]")
    assert result.parsed is None
    assert (result.provider, result.model) == ("mock", "MOCK")
    assert result.usage.total_tokens == 0 and result.llm_calls == 1


def test_mock_records_calls_for_assertions():
    llm = MockLLM()
    llm.generate(system="SYS", user="USER")
    assert llm.calls == [{"system": "SYS", "user": "USER", "response_model": None}]


def test_mock_builds_valid_structured_example():
    result = MockLLM().generate(system="s", user="u", response_model=Example)
    assert isinstance(result.parsed, Example)
    assert result.parsed.kind == "policy" and result.parsed.tags == [] and result.parsed.note is None
    assert result.parsed.inner.doc_id == "[MOCK]" and result.parsed.flag is False
    assert Example.model_validate_json(result.text) == result.parsed


@pytest.mark.parametrize(
    "scripted",
    [
        '{"doc_id": "D1", "page": 3}',  # JSON text
        {"doc_id": "D1", "page": 3},    # dict
        Inner(doc_id="D1", page=3),     # instance
    ],
)
def test_mock_responder_can_script_structured_output(scripted):
    llm = MockLLM(responder=lambda system, user, model: scripted)
    result = llm.generate(system="s", user="u", response_model=Inner)
    assert result.parsed == Inner(doc_id="D1", page=3)


def test_mock_responder_can_script_plain_text():
    llm = MockLLM(responder=lambda s, u, m: "scripted answer")
    assert llm.generate(system="s", user="u").text == "scripted answer"


def test_mock_invalid_scripted_output_raises_controlled_error_without_values():
    llm = MockLLM(responder=lambda s, u, m: {"doc_id": 5, "page": "not-a-number SECRET-VALUE"})
    with pytest.raises(LLMOutputError) as info:
        llm.generate(system="s", user="u", response_model=Inner)
    assert info.value.kind == "invalid"
    assert "SECRET-VALUE" not in str(info.value) and "SECRET-VALUE" not in info.value.detail


def test_get_llm_without_key_raises_config_error_and_does_not_fall_back_to_mock():
    with pytest.raises(LLMConfigError) as info:
        get_llm({})  # MOCK_LLM unset, no key
    assert "OPENAI_API_KEY" in str(info.value)
    assert info.value.kind == "config"


def test_get_llm_blank_key_is_rejected():
    with pytest.raises(LLMConfigError):
        get_llm({"OPENAI_API_KEY": "   "})


def test_get_llm_reports_missing_openai_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", None)
    with pytest.raises(LLMConfigError) as info:
        get_llm({"OPENAI_API_KEY": "sk-test-123456"})
    assert "pip install openai" in str(info.value)


def test_scrub_redacts_keys_and_bearer_tokens_and_truncates():
    text = "bad key sk-proj-ABCDEF1234567890 and Bearer abc.def-123 end"
    cleaned = scrub(text)
    assert "ABCDEF" not in cleaned and "abc.def" not in cleaned and cleaned.count("[REDACTED]") == 2
    assert len(scrub("x" * 5000)) < 400
