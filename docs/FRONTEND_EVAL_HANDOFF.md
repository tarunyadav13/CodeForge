# Frontend / evaluation integration handoff (Member 3 to the main integration session)

Prepared 2026-10-23 from branch `m3-ui` at `3f871d8` (working tree clean when written). This file
is new and uncommitted. Nothing in `api/`, `graph/`, `rag/`, `db/`, `tools/` or `scripts/` was
touched; none of those exist on this branch.

**Verified here:** the UI, client, runner and tests against mocked HTTP.
**Not verified:** every interaction with the real FastAPI app, because `api/main.py`,
`api/models.py`, `graph/`, `rag/`, `db/`, `tools/`, `scripts/load_students.py` and `requirements.txt`
are absent from this checkout. The contract below is the one quoted in
`TEAM_HANDOFF_HCL_Hackathon.md` section 8, not a file I could read.

## 1. Entry points and addresses

| Item | Value |
|---|---|
| Streamlit entry point | `ui/app.py` (run from the repository root) |
| UI address | http://localhost:8501 (Streamlit default) |
| Expected API base URL | no default since the deployment change: set `API_BASE_URL` (Streamlit secret or env var; e.g. `http://127.0.0.1:8000` locally); also editable in the UI sidebar |
| HTTP code | only `ui/api_client.py` talks HTTP; the UI and `eval/runner.py` both use it |
| CORS | not needed: Streamlit calls the API from its server process, not from the browser |
| Timeouts | connect 5 s; `/ask` read 120 s (`ASK_TIMEOUT_SECONDS`); `/ingest` read 300 s (`INGEST_TIMEOUT_SECONDS`); health/sources/audit read 15 s |

## 2. Requests the frontend sends

| Call | Request | Notes |
|---|---|---|
| `GET /health` | none | Shown as a four-component strip |
| `POST /ask` | JSON `{"question": str, "as_of_date": "YYYY-MM-DD"}`; `as_of_date` only when the sidebar toggle is on. Header `X-Student-Id: S1234` only when an ID is typed | No identity in the body or question text |
| `GET /sources` | none | Sources tab |
| `GET /audit/{trace_id}` | none; the client rejects empty IDs and IDs containing `/` | Audit tab; 404 is shown as "No audit record found" |
| `POST /ingest` | multipart: field `file` (bytes, filename, content type from the upload) and field `metadata` (JSON string) | See metadata below |

`metadata` JSON has exactly the 14 `SourceRecord` fields, so `extra="forbid"` is satisfied:
`doc_id, title, issuer, authority_level (int 1-5), doc_type (regulation|circular|notice|faq|handbook|unofficial), version, effective_from, effective_to (null unless set), supersedes (""), scope_programmes ("ALL" default), scope_batches ("ALL" default), provenance, retrieved_on, synthetic ("Y"|"N")`. Dates are ISO strings. The upload widget accepts `pdf, txt, md, docx, html`; the backend must handle whichever it supports.

## 3. Responses the frontend requires

**`/ask` (`AskResponse`).** Hard errors (shown as an error, no answer displayed): non-JSON, non-object,
missing/non-string `trace_id`, `answer`, `answer_type`; `answer_type` outside the six values;
`as_of_date` missing or not `YYYY-MM-DD`. Soft problems (answer shown plus a "Contract warnings" line):
malformed list fields, `not_found` whose text is not exactly
`I could not find this information in the authorised university sources.`, `conflict_flagged` with an
empty `conflicts_detected`, `retrieved_fact`/`conflict_flagged` without citations.

| Field | Used for |
|---|---|
| `answer_type` | badge colour: retrieved_fact green, calculated blue, not_found gray, clarification_needed orange, refused red, conflict_flagged violet |
| `citations[]` | table: title, doc_id, section/page, version, effective_from. Items need `doc_id` and `title` |
| `tools_invoked[]` | `tool`, `input{}`, `output{}` tables |
| `applied_rules[]` | `rule_id`, `value`, `source_doc_id` (all three required, else ignored with a warning) |
| `conflicts_detected[]` | free-form dicts. Summary uses `summary`/`description`/`reason`/`detail`, else `doc_ids`/`sources`/`documents`. Unresolved if `answer_type` is `conflict_flagged`, or `resolved: false`, or `status: "unresolved"` |
| `explanation`, `trace_id`, `as_of_date` | caption, copyable trace ID, trace panel |

Answer, explanation and conflict text are treated as untrusted: markdown images are removed and links
are shown as text (`ui/formatting.py: safe_text`).

**`/health`.** `status` in ok|degraded|down and `api, vector_store, sqlite, llm` each `{status, detail}` with
status in ok|degraded|down|not_configured. Missing components show as "unknown".

**`/sources`.** A list of `SourceRecord`, or `{"sources": [...]}`. Anything else is an error.

**`/audit/{id}`.** A JSON object (displayed raw). The runner reads `sources_retrieved` (items with `doc_id`),
`llm_calls`, `tokens`, `latency_ms`.

**`/ingest`.** JSON object with `doc_id` and integer `chunks_indexed` (and `status`). HTTP 501 is shown as "not implemented".

## 4. Identity-header handling

* The ID comes only from the sidebar field. It is stripped and upper-cased, sent only as `X-Student-Id`, and never added to the question.
* A value that is not `S` + 4 digits triggers a sidebar warning but is **still sent** (so the backend can refuse it). Judge IDs `S9000-S9999` are accepted by the UI.
* Empty field means no header. The backend must then refuse or ask for clarification on personal questions (eval EV-023 accepts `refused` or `clarification_needed`).
* A student ID typed inside a question must be ignored by the backend (EV-021, 022, 028). The UI cannot enforce this.
* The UI labels this as prototype identity, not authentication.

## 5. Mismatches and open points against the contract

1. **Contract not readable here.** The mirrored-constants test (`test_mirrored_constants_match_frozen_contract`) is skipped because `api/models.py` is absent. Run it first after the merge; it checks the not-found text, header name, six answer types and the four health components against the real models.
2. **`/sources` shape** is not in the frozen models; the UI accepts two shapes. Please state which.
3. **`/ingest` field names** (`file`, `metadata`) are my assumption from "multipart file + metadata JSON".
4. **Audit item shapes.** The contract says `sources_retrieved: list[dict]` and `tools_invoked: list[dict]` with no keys; the runner assumes `doc_id` in each source item. If absent, retrieval hit rate cannot be computed. `llm_calls`/`tokens` left at 0 are reported N/A (as "not instrumented").
5. **Stricter than the contract on purpose.** `Citation.section/page/version/effective_from` are optional in the models, but brief R2 requires them, so the runner fails a `retrieved_fact` whose citation lacks section/page, version or effective_from. The UI just shows a dash.
6. **Tool `input` should include `student_id`** so the runner can prove no tool ran for another student. If it does not, the cross-student leak check has nothing to inspect (reported N/A, not as a pass).
7. **Refusal transport.** The UI/runner expect refusals as HTTP 200 with `answer_type: "refused"`. The runner also tolerates 401/403 when `refused` is expected; the UI shows those as a plain error, not a "Refused" badge.
8. **Question length.** `AskRequest.question` max 2000 chars is not enforced client-side; a 422 is shown as an error.
9. **`conflicts_detected` items** are free-form; the UI prefers `doc_ids` plus `resolved: bool` or `status`.
10. **Real-document metadata is incomplete.** `data/source_register.csv` has blank `effective_from` for `UGC-FIRSTDEG-2003`, `MSRUAS-ACADREG-2022`, `IITM-INTMA-ORD`, which `SourceRecord` rejects. Fill them or drop those rows before bulk ingest. Open team decisions are in `data/README.md`.
11. Foundation stub per the handoff: `/ingest` returns 501 and `/sources` is empty; the UI reports both honestly.

## 6. Evaluation

* Dataset `eval/eval_set.jsonl`: 30 cases (policy 5, unanswerable 4, conflict/version 5, personal tool 7, cross-student 3, multi-step 3, clarification 1, prompt injection 2). 18 `ready`, 12 `pending` (need Member 2's seed data; pending value checks are skipped, never passed). Placeholder IDs `S1001-S1003`, `CS201`, `CS202`.
* Corpus the cases refer to: `data/source_register.csv` (3 real PDFs + 6 `SYN-*` fixtures in `data/documents/`). Ingest them before a live run.
* Runner `eval/runner.py` (exit 0 all pass, 1 any FAIL/ERROR, 2 invalid dataset). Results JSON records `mode` (`live` or `mock`).

```powershell
python eval/runner.py --validate-only                 # dataset check only
python eval/runner.py --mock                          # MOCK-ONLY canned replay, no API; proves nothing about the assistant
python eval/runner.py --audit                         # LIVE against API_BASE_URL (adds retrieval hit rate, llm calls, tokens)
python eval/runner.py --audit --ids EV-005,EV-021     # subset
python eval/runner.py --audit --base-url http://localhost:8000 --output eval\results\live_run.json
```

* Metrics: answer_type, content, citation, abstention, false abstention, exact not_found text, tool correctness, cross-student leaks, retrieval hit rate, latency p50/p95 (live only), llm calls, tokens. Unavailable values are N/A with a reason, never 0. Errored cases count as wrong in answer_type accuracy and print a warning.
* Live metrics: **none exist.**

## 7. Demo flow (all BLOCKED ON BACKEND; full table in `docs/demo_script.md`)

1. Cited policy answer (EV-001, MSRUAS 75% and 10% condonation). 2. Tool calculation as `S1001` (EV-015). 3. `not_found` (EV-005). 4. Conflict resolution: 80% via `SYN-CIRC-ATT-80`, FAQ ignored, and `conflict_flagged` for the fee (EV-009, 013, 012). 5. Cross-student refusal (EV-021, 022). 6. Live `/ingest`. 7. `scripts/load_students.py` with a new CSV. 8. Unseen question after ingest. Then Audit tab for a trace ID.

## 8. Dependencies and Windows startup

Python 3.11.9 was used. UI side needs `streamlit` (1.66.1 installed here; `>=1.40` is the stated floor, but `st.badge` is used when available with a fallback), `requests` (2.34.2), `pytest` (9.1.1). No Ollama/Chroma on the UI side. `requirements.txt` does not exist on this branch; whoever owns it should include those three.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
pip install "streamlit>=1.40" requests pytest          # until requirements.txt exists
# Terminal 1 (backend branch needed; command from the team handoff, unverified here):
uvicorn api.main:app --reload --port 8000
# Terminal 2:
streamlit run ui/app.py
# Live checks once the API is up:
$env:M3_LIVE_API = "1"; python -m pytest -q tests/test_m3_live_api.py
python eval/runner.py --audit
```

Live test env vars: `M3_LIVE_STUDENT_ID` (default `S1001`), `M3_LIVE_OTHER_STUDENT_ID` (default `S1002`); use real seed IDs.

## 9. Actual test results (run 2026-10-23, `.venv\Scripts\python.exe`)

```
python -m pytest -q          -> 141 passed, 7 skipped in 8.97s
python eval/runner.py --validate-only -> Dataset OK: 30 cases
python eval/runner.py --mock          -> 30 pass, 0 fail, 0 error (MOCK-ONLY, by construction)
```

Collected per file: api_client 43, dataset_audit 8, eval 24, live_api 6 (skipped), runner_audit 10,
sample_audit 4, source_register 24, ui_render 29 (the 7th skip is the `api/models.py` contract-sync test).
Mock metrics: all rates 1.0 / leaks 0 by construction; retrieval hit rate, latency, llm calls, tokens N/A.

## 10. Checklist for the integration session

1. Merge or copy `api/`, `graph/`, `rag/`, `db/`, `tools/`, `scripts/`, `requirements.txt` onto a branch with `ui/`, `eval/`, `data/`.
2. `python -m pytest -q`; confirm the contract-sync skip becomes a pass.
3. Start API and UI; Refresh status in the sidebar.
4. Decide `/sources` and `/ingest` shapes (section 5, items 2-3); fill or drop the three blank `effective_from` rows.
5. Ingest `data/documents/` per the register; load seed data; replace placeholder IDs in `eval/eval_set.jsonl` and flip pending cases to `ready`.
6. `$env:M3_LIVE_API = "1"; python -m pytest -q tests/test_m3_live_api.py`, then `python eval/runner.py --audit`; report only numbers from the results file.
7. Rehearse the eight demo steps.
