# AI usage disclosure

Each member adds their own entries. Only Member 3's are filled in, from this repository's work.

| Who | Tool | Used for | Human review |
|---|---|---|---|
| Member 3 | Claude (Anthropic) via Claude Code, with its built-in web fetch and search tools | Drafting `ui/`, `eval/` (dataset, runner, mock API), the Member 3 tests, README and `docs/`; finding and reading the public regulations in `data/`; writing the six synthetic test documents and the three sample audit records | Tests run after each change (latest: see the acceptance report in the conversation / `pytest -q`). Quoted policy values were read from the downloaded PDFs' text. _Member 3: add your own description of what you read, ran and changed by hand._ |
| Member 1 | _TBD_ | _TBD_ | _TBD_ |
| Member 2 | _TBD_ (synthetic data generation prompts must be saved verbatim in `prompts/`) | _TBD_ | _TBD_ |

Claude did not generate any real student data and was not given any. The synthetic policy documents
are labelled as such in their own text and in the Source Register.

Runtime AI: the application is designed to use a local Ollama model (planned: `llama3.1:8b`) to
phrase answers from retrieved text and tool outputs. Calculations and eligibility come from Python
tools, not the model. Not verified here because the backend is absent.
