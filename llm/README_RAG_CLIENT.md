# llm/ — LLM access (Member 1)

One small interface, two implementations:

| Mode | When | Network |
| --- | --- | --- |
| `MockLLM` | `MOCK_LLM=true` | never; no key, no `openai` package needed |
| `OpenAIClient` | otherwise | OpenAI **Responses API** via the official `openai` SDK |

```python
from llm import get_llm
from pydantic import BaseModel

class Synthesis(BaseModel):
    answer: str
    used_chunk_ids: list[str]

llm = get_llm()                                   # MockLLM or OpenAIClient, from env
r = llm.generate(system="...", user="...", response_model=Synthesis)
r.parsed          # validated Synthesis instance (None when no response_model)
r.model, r.usage, r.latency_ms, r.llm_calls   # feed the audit record (brief R10)
llm.health()      # "ok" only after a real request succeeded; "mock" in mock mode
```

The LLM only writes wording. Evidence goes in through `user`; **citations come from the
retrieved chunks, never from model output** (brief R1/R2/R5).

## Setup (Windows PowerShell, per session; never commit the key)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install openai pydantic pytest          # `openai` is only needed for real calls

$env:OPENAI_API_KEY = "<paste key here>"    # set in YOUR shell only
python -m pytest tests/m1 -q                # offline tests (no key, no network)

$env:RUN_LIVE_LLM = "1"                     # optional: 1 real call, a few tokens
python -m pytest tests/m1/test_llm_live.py -q -s
```

Offline mode for everything else: `$env:MOCK_LLM = "true"`.

## Settings (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENAI_API_KEY` | — (required for real calls) | Read once and handed to the SDK. Never stored, printed or logged. |
| `OPENAI_MODEL` | `gpt-6-luna` | Any Responses-API model that supports Structured Outputs. |
| `OPENAI_TIMEOUT_S` | `30` | Per-request timeout in seconds. |
| `OPENAI_MAX_RETRIES` | `1` | SDK retries for connection errors, 429 and 5xx. Not output retries. |
| `OPENAI_MAX_OUTPUT_TOKENS` | `2000` | Includes hidden reasoning tokens; raise it if you see `incomplete`. |
| `OPENAI_REASONING_EFFORT` | `low` | `none/low/medium/high/xhigh/max`, or `off` to omit (for non-reasoning models). |
| `MOCK_LLM` | unset | `true` → `MockLLM`. An explicit switch; real failures never turn into mock answers. |

### Why `gpt-6-luna`
From OpenAI's model pages (checked 2026-10-23): it is the efficient tier, labelled "Default",
supports the Responses API and Structured Outputs, and costs $0.10 / $0.50 per 1M input /
output tokens. `gpt-5.4-mini` ($0.75 / $4.50) is a verified alternative:
`$env:OPENAI_MODEL = "gpt-5.4-mini"`. The Free tier is not supported for `gpt-6-luna`, so the
account needs paid credit. Model lineups change: if a model ID is rejected you will get
`LLMAPIError(kind="not_found" | "bad_request")`; pick another from the models page.

## Behaviour

* **One request per call.** If the *output* is unusable (empty, or JSON not matching
  `response_model`) there is exactly **one** corrective retry. Refusals, truncated
  (`incomplete`) responses and API/timeout errors are never retried by this client.
* **Errors are controlled and safe.** `LLMConfigError`, `LLMTimeoutError`, `LLMAPIError`,
  `LLMOutputError`; `str(error)` never contains a key or prompt text. `error.usage` and
  `error.llm_calls` say what was spent, so the audit record can still be written.
* `store=False` (no response storage at OpenAI), no `temperature` (reasoning models reject it).
* Logs contain provider, model, status, token counts and latency only.

## Data note (for the README disclosure owned by Member 3)
In OpenAI mode the retrieved document chunks and tool results in the prompt are sent to
OpenAI. The brief requires public documents and synthetic students only, so no real student
data may be present. The official brief's technology table lists local Ollama as the primary
LLM and a cloud LLM as a disclosed, config-switched fallback; the team lead states the
organizers allow any model/stack. Record that confirmation in the README disclosure.

## Not verified yet
Everything above is covered by offline tests with a fake SDK object, plus the real SDK's
exception classes and parameter names. **No real OpenAI request has been made** from this
code yet: run the opt-in live test above once with your key.
