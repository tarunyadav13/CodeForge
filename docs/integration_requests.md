# Integration requests from Member 3

Things the UI and eval runner assume. Each needs a yes/no from the owning member; nothing here
has been changed in their files.

| # | Owner | Request | Why |
|---|---|---|---|
| 1 | M2 (`api/main.py`) | `POST /ingest` multipart field names: `file` and `metadata` (JSON string of `SourceRecord`) | The UI sends these; constants in `ui/api_client.py` |
| 2 | M2 | `GET /sources` returns a JSON list of `SourceRecord` (UI also accepts `{"sources": [...]}`) | Sources tab |
| 3 | M2 | Cross-student requests return HTTP 200 with `answer_type: "refused"` (403 is also tolerated by the runner) | Consistent refusal rendering |
| 4 | M2 | `GET /audit/{trace_id}` returns 404 for unknown IDs | Audit tab message |
| 5 | M1/M2 | Tool `input` includes `student_id` | Lets the runner prove no tool ran for another student |
| 6 | M1 | `conflicts_detected` items include `doc_ids` and either `resolved: bool` or `status` | UI marks unresolved conflicts distinctly |
| 7 | M2 | Audit `llm_calls` and `tokens` populated (not left at 0) | Eval metrics; otherwise reported N/A |
| 8 | M2 | Seed data includes S1001–S1003-style placeholder students and an at-threshold student | Eval fixtures (or tell M3 the real IDs) |
| 9 | whoever owns `requirements.txt` | Include `streamlit>=1.40`, `requests`, `pytest` | UI, runner, tests |
| 10 | M1 + M3 | Synthetic conflict docs `SYN-REG-ATT-75`, `SYN-CIRC-ATT-80`, `SYN-FAQ-ATT-65`, `SYN-CIRC-FEE-A/B`, `SYN-NOTICE-INJ` (synthetic=Y) | Conflict and injection eval cases |
