# llm/ — OpenAI client (Member 1, Milestone A)

Wraps the official `openai` Python SDK (Responses API). Replaces the Ollama client in the handoff.
The organizers allow any model or stack; record that in the root README / AI-usage disclosure.

## Settings (environment variables, never committed)

| Variable | Default | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | none | Required unless `MOCK_LLM=true`. Never printed, logged or put in errors. |
| `OPENAI_MODEL` | `gpt-6-luna` | Any Responses-API model that supports Structured Outputs (e.g. `gpt-6-astra` for quality). |
| `OPENAI_TIMEOUT_SECONDS` | `30` | Per-request timeout. |
| `OPENAI_MAX_RETRIES` | `1` | SDK retries for transient network/429/5xx. Separate from the one invalid-output retry. |
| `MOCK_LLM` | unset | `true` -> no network, no key, deterministic output (use for tests/CI). |

PowerShell:

```powershell
$env:OPENAI_API_KEY = "<your key>"      # this session only; do not write it into any file
$env:OPENAI_MODEL   = "gpt-6-luna"
$env:MOCK_LLM       = "true"            # offline mode
pip install "openai>=2,<4" "pydantic>=2"
```

## Use

```python
from llm import LLMClient
client = LLMClient()                                   # reads the environment
r = client.generate_text(system, user)                 # -> LLMResult(text, model, llm_calls, *_tokens, latency_ms)
s = client.generate_structured(system, user, MySchema) # -> StructuredResult(parsed: MySchema, ...)
```

Audit fields for `AuditRecord`: `client.model_name`, `result.llm_calls`, `result.total_tokens`, `result.latency_ms`.

## Behaviour
* Invalid structured output (bad JSON, schema violation, refusal, truncation): **one** corrective retry, then `LLMOutputError`.
* Timeout -> `LLMTimeoutError`; HTTP/API failure -> `LLMAPIError(status_code)`; missing/invalid config -> `LLMConfigError`. None are retried here.
* Error messages are sanitised (SDK text can contain partial keys). Prompts and answers are never logged.

## Tests
`python -m pytest tests/test_llm_client.py -q` (32 tests; no network, no key).

## Not verified
Nothing here has been run against the real OpenAI API (no key was available when written).
Do one live smoke call with your key before the demo and confirm the chosen model accepts `text_format`.
