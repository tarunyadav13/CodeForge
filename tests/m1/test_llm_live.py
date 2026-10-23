"""OPT-IN live check against the real OpenAI API. Skipped by default (costs a few tokens).

Windows PowerShell:
    $env:OPENAI_API_KEY = "<your key>"      # set in your session; never commit it
    $env:RUN_LIVE_LLM = "1"
    python -m pytest tests/m1/test_llm_live.py -q -s
"""
import os

import pytest
from pydantic import BaseModel

from llm import get_llm

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_LLM") != "1" or not os.environ.get("OPENAI_API_KEY"),
        reason="set RUN_LIVE_LLM=1 and OPENAI_API_KEY to run real OpenAI calls",
    ),
]


class Pong(BaseModel):
    word: str


def test_live_health_text_and_structured_output():
    llm = get_llm()
    assert llm.provider == "openai"

    assert llm.health().status == "ok"

    text = llm.generate(system="Be brief.", user="Reply with the single word: pong")
    assert text.text.strip() and text.usage.total_tokens > 0 and text.llm_calls >= 1

    structured = llm.generate(
        system="Return JSON only.", user="Put the word pong in the field 'word'.", response_model=Pong
    )
    assert isinstance(structured.parsed, Pong) and structured.parsed.word
    print(f"model={structured.model} latency_ms={structured.latency_ms} tokens={structured.usage.total_tokens}")
