# AI usage disclosure

> Draft. Each member should add their own entries; only the Member 3 entry below is known.

| Who | Tool | Used for | Human review |
|---|---|---|---|
| Member 3 | Claude (Anthropic) via Claude Code | Drafting `ui/`, `eval/`, Member 3 tests, README and `docs/` | _Describe how you reviewed and ran the code_ |
| Member 1 | _TBD_ | _TBD_ | _TBD_ |
| Member 2 | _TBD_ (synthetic data generation prompts must be saved verbatim in `prompts/`) | _TBD_ | _TBD_ |

Runtime AI: the application is designed to use a local Ollama model (planned: `llama3.1:8b`) to
phrase answers from retrieved text and tool outputs. Calculations and eligibility come from Python
tools, not the model.
