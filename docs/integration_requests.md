# Integration requests and checklist from Member 3

Things the UI and eval runner assume. Each needs a yes/no from the owning member; nothing here
has been changed in their files. Status as of 2026-10-23: **no request has been answered and no live
check has been possible** because the backend is not in the `m3-ui` checkout.

## Requests

| # | Owner | Request | Why |
|---|---|---|---|
| 1 | M2 (`api/main.py`) | `POST /ingest` multipart field names: `file` and `metadata` (JSON string of `SourceRecord`) | The UI sends these; constants in `ui/api_client.py` |
| 2 | M2 | `GET /sources` returns a JSON list of `SourceRecord` (UI also accepts `{"sources": [...]}`) | Sources tab |
| 3 | M2 | Cross-student requests return HTTP 200 with `answer_type: "refused"` (403 is also tolerated by the runner) | Consistent refusal rendering |
| 4 | M2 | `GET /audit/{trace_id}` returns 404 for unknown IDs | Audit tab message |
| 5 | M1/M2 | Tool `input` includes `student_id` | Lets the runner prove no tool ran for another student |
| 6 | M1 | `conflicts_detected` items include `doc_ids` and either `resolved: bool` or `status` | UI marks unresolved conflicts distinctly |
| 7 | M2 | Audit `llm_calls` and `tokens` populated (not left at 0) | Eval metrics; otherwise reported N/A |
| 8 | M2 | Seed data includes S1001 to S1003-style placeholder students and an at-threshold student, or tell M3 the real IDs | Eval fixtures (12 cases pending) |
| 9 | whoever owns `requirements.txt` | Include `streamlit>=1.40`, `requests`, `pytest` | UI, runner, tests |
| 10 | M1 + M3 | Ingest the six synthetic documents (`data/documents/synthetic/`, written, `synthetic=Y`) and the three real PDFs | Conflict, injection and citation cases |
| 11 | Whole team | Fill `effective_from` for the three real documents and decide which institution is "the university" (`data/README.md`) | Register cannot pass `SourceRecord` validation until then |
| 12 | M2 | `audit.sources_retrieved` items carry `doc_id` | Retrieval hit rate in the runner |
| 13 | M2 | rule_registry rows cite these documents; the synthetic values (attendance 75 then 80, backlogs 0, CGPA >= 6.0 for placement) are proposals | Tool answers must trace to a cited clause |

## Integration checklist for Members 1 and 2

Endpoints (fixed by the brief):

| Endpoint | Request | Response the UI/runner require |
|---|---|---|
| `GET /health` | none | `HealthResponse`: `status` and `api`, `vector_store`, `sqlite`, `llm` each `{status, detail}` |
| `POST /ask` | header `X-Student-Id` (optional), JSON `{question, as_of_date?}` | `AskResponse`: `trace_id, answer, answer_type, citations[], tools_invoked[], applied_rules[], conflicts_detected[], explanation, as_of_date`. Citations: `doc_id, title, section or page, version, effective_from`. `not_found` answer must equal the exact sentence |
| `GET /sources` | none | list of `SourceRecord` (Annex B fields) |
| `GET /audit/{trace_id}` | none | `AuditRecord` (see `docs/sample_audit_records.json`, examples only); 404 if unknown |
| `POST /ingest` | multipart `file` + `metadata` JSON | `IngestResponse`: `doc_id, chunks_indexed, status` |

Dependencies and setup the UI/runner need: Python 3.11+, `streamlit`, `requests`, `pytest`; optional
`API_BASE_URL`. No Ollama or ChromaDB on the UI side.

Live test commands (run when the API is up):

```powershell
$env:M3_LIVE_API = "1"; python -m pytest -q tests/test_m3_live_api.py
python eval/runner.py --audit
Invoke-RestMethod http://localhost:8000/health
python scripts/load_students.py --dir test_students/        # M2: not present yet
```

Outstanding blockers (all **BLOCKED ON BACKEND**): `api/main.py`, `api/models.py`, `graph/`, `rag/`,
`db/`, `tools/`, `scripts/load_students.py`, `requirements.txt` absent from this branch; `/ingest`
returns 501 in the foundation stub; `/sources` empty in the stub; seed data missing.
