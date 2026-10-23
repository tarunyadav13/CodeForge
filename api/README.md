# API (Member 2): `api/main.py`

```powershell
uvicorn api.main:app --reload --port 8000
```
`api/models.py` is the frozen contract and is unchanged. Settings: `STUDENT_DB_PATH`, `AUDIT_ADMIN_TOKEN` (optional),
`INGEST_MAX_BYTES` (default 25 MB), and for the LLM `OPENAI_API_KEY`, `OPENAI_MODEL`, `MOCK_LLM` (see `llm/README.md`).

## Endpoints
| Endpoint | Behaviour |
|---|---|
| `POST /ask` | Header `X-Student-Id` (optional for general questions), body `question`, optional `as_of_date`. Always returns an `AskResponse`; refusals are `answer_type: "refused"` with HTTP 200. |
| `GET /health` | `HealthResponse`. API, SQLite (real probe: tables present, rows counted), vector store (`rag.retriever.health()`; `not_configured` if absent), LLM. A present API key is reported as `degraded` ("configuration only, NOT verified"). `GET /health?live=true` makes one minimal real request (16 output tokens) and reports `ok`/`down`. `MOCK_LLM=true` is shown as `degraded` (mock). |
| `GET /audit/{trace_id}` | Persistent (SQLite). Records tied to a student need the same `X-Student-Id` (or a valid `X-Admin-Token` = `AUDIT_ADMIN_TOKEN`); otherwise 404. Anonymous records are readable by trace id. Returns the `AuditRecord` fields plus `applied_rules`, `conflicts_detected`, `citations`, `as_of_date`. |
| `GET /sources` | Source Register rows as `SourceRecord`. |
| `POST /ingest` | Multipart: `file` + `metadata` (Annex B JSON as a form field or a file part). Validates metadata (422), type (415), size (413), empty (422); skips an unchanged re-upload; calls Member 1's `rag.ingest.ingest_document`; registers the source only after success. **501 if the pipeline is not installed; nothing is registered or faked.** It never edits `rule_registry`. |

curl / PowerShell:
```powershell
curl.exe -s -X POST http://localhost:8000/ask -H "Content-Type: application/json" -H "X-Student-Id: S1002" `
  -d '{"question":"Am I eligible to appear in the end-semester exam for CS201?","as_of_date":"2026-10-06"}'
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/audit/<trace_id> -Headers @{"X-Student-Id"="S1002"}
curl.exe -s -X POST http://localhost:8000/ingest -F "file=@circular.pdf" -F "metadata=<metadata.json"
```

## Identity and security
* Identity = `X-Student-Id` only, validated (`S`+4 digits, must exist). Missing identity on a personal question,
  malformed or unknown ids, and any question naming another student (another `S####`, "a classmate's marks") are refused
  **before** any tool or LLM runs. Tools are additionally locked to the authenticated student (`tools/registry.py`).
* Raw question text is never stored or logged. If the audit write fails the answer is withheld (503).

## Hooks for Member 1 (no edits to `api/main.py` needed)
| Hook | Contract | If absent |
|---|---|---|
| `graph.workflow.run_graph(question, student_id|None, as_of_date) -> AskResponse` (or dict, or `(AskResponse, extras)`) | Called for every question that passed the identity checks. `extras` may carry `question_category`, `model`, `llm_calls`, `tokens`, `sources_retrieved`, `precedence_decision`. The API writes the audit row unless the graph already wrote one for `resp.trace_id` (use `db.data_access.new_trace_id/write_audit`). Failures return 502. | Personal questions use the interim deterministic router (`api/personal.py`, real tools, no LLM); policy questions return **503** (never fabricated). |
| `rag.ingest.ingest_document(file_path, metadata: dict) -> {"chunks_indexed": int, "status": str}` | metadata = Annex B fields | `/ingest` 501 |
| `rag.retriever.health() -> {"status": "ok"|"degraded"|"down", "detail": str}` | | `vector_store: not_configured` |
| `tools.set_policy_resolver(fn)` | see `tools/README.md` | ambiguous rules -> `conflict_flagged` |

## For Member 3 (UI)
Send `X-Student-Id` from the login box. Show `answer_type` badge, `citations`, `tools_invoked`, `applied_rules`.
`refused`/`clarification_needed`/`not_found` are normal 200 responses. Expect 503 (policy workflow missing) and 501
(ingest missing) as JSON `{"detail": ...}`. Three real sample audit records are in `api/sample_audit_records/`.

Run `python -m pytest tests/test_backend_api.py -q`.
