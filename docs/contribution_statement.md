# Contribution statement

Filled in from the repository, not estimated. Members 1 and 2 add their own rows from their own commits.

| Member | Role | Main contributions | Branch / key commits |
|---|---|---|---|
| Member 1 | AI / RAG / LangGraph | _TBD_ | `m1-rag` |
| Member 2 | Backend / data / security | _TBD_ | `m2-backend` |
| Member 3 | UI / evaluation / integration | See below | `m3-ui`: `fd97a1c` (UI, eval set + runner, tests, docs); later work is uncommitted at the time of writing |

## Member 3 (files under `ui/`, `eval/`, `data/`, `docs/`, `tests/`, `README.md`)

* **UI:** Streamlit chat app (`ui/app.py`), HTTP client with contract validation (`ui/api_client.py`),
  rendering helpers (`ui/formatting.py`): answer-type badges, citations, tool and rule tables,
  conflicts, health strip, Sources / Audit / Ingest tabs, error and missing-backend states.
* **Evaluation:** 30-case dataset (`eval/eval_set.jsonl`), runner with measured-only metrics and
  live/mock labelling (`eval/runner.py`), MOCK-ONLY replay (`eval/mock_api.py`).
* **Corpus:** Source Register (`data/source_register.csv`), three public regulations downloaded with
  provenance (`data/documents/`), six synthetic conflict / injection fixtures
  (`data/documents/synthetic/`), notes on unresolved metadata (`data/README.md`).
* **Tests:** `tests/test_m3_*.py` (mocked HTTP, UI rendering, dataset and register validation,
  runner honesty, sample audit shape); live-API tests are opt-in (`M3_LIVE_API=1`).
* **Docs:** README, architecture, evaluation, known limitations, demo script, integration requests
  and checklist, data card, AI-use disclosure, sample audit records (`docs/sample_audit_records.json`).

Not done by Member 3 and not claimed: anything in `api/`, `graph/`, `rag/`, `db/`, `tools/`,
`scripts/` or `requirements.txt`.
