# Known limitations and blockers

Last updated 2026-10-23.

## Blockers

- **Backend not on `main`.** `main` contains only the placeholder `app` file. The foundation code
  (frozen `api/models.py`, mock `api/main.py`, tests) is in a teammate's working copy. Member 3's
  code was built against the contract quoted in the team handoff and tested with mocked HTTP.
- **No real backend behaviour verified.** Retrieval, precedence, tools, authorization, audit and
  ingestion are owned by Members 1 and 2 and have not been exercised end to end.
- **Eval fixtures pending.** Most eval cases depend on documents and seed data that do not exist
  yet; their value checks are skipped until filled in.
- **Source Register not done.** Choosing ≥3 real public university documents and writing
  `data/source_register.csv` (M3 with M1) is still open.

## Prototype limitations

- `X-Student-Id` is not authentication; any client can claim any ID.
- The UI does not persist chat history across browser refreshes (session state only).
- The `/ingest` field names (`file`, `metadata`) are assumed until the backend implements it.
- Answer correctness beyond substring checks needs human review.
- Latency is measured client-side and includes network and LLM time.
