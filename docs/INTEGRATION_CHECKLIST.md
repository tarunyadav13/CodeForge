# Integration checklist (Members 1, 2, 3)

Companion to `docs/INTEGRATION_HANDOFF.md` (signatures and code sketches). This file is the
to-do and test order. Every path, command and variable below was checked against the repository
on 2026-10-23. Anything not listed as "passed" has **not** been run.

## 0. Snapshot of the repository today

* Branch `main`, one commit (`Create app`). Everything else is **untracked** (29 entries): foundation
  (`api/`, `ui/`, `tests/test_foundation.py`, `.gitignore`), `llm/`, `rag/`, `requirements.txt`, `docs/`,
  `tests/m1/`. Nothing is staged or committed.
* `api/main.py` and `ui/app.py` are the foundation versions: **neither imports `llm` or `rag` yet.**
  Current live behaviour (checked with FastAPI's `TestClient`, offline):
  `GET /health` returns `vector_store`/`sqlite`/`llm` as `not_configured`; `GET /sources` returns `[]`;
  `POST /ingest` returns `501`; `POST /ask` returns a `[MOCK]` answer citing `SYNTHETIC-MOCK-DOC`.
* Missing on purpose or not yet created (do not reference them as if they exist): `db/`, `tools/`,
  `graph/`, `eval/`, `scripts/` (so no `load_students.py`, `gen_data.py`, `validate.py`, `eval/runner.py`),
  `README.md`, `data/`, `test_students/`, `prompts/`, `.env`/`.env.example`.
* `app` at the repo root is a tracked 2-byte placeholder from the first commit (the team handoff calls it 1 byte);
  leave it.

## 1. What Member 1 has implemented and how it is called

| Capability | Module | Call |
| --- | --- | --- |
| LLM access (OpenAI Responses API, offline mock) | `llm/` | `from llm import get_llm; get_llm().generate(system=..., user=..., response_model=None)` |
| Persisted vector store + source register | `rag/store.py` | `VectorStore(embedder=get_embedder())` |
| Ingest `.txt`/`.md`/text `.pdf` with Annex B metadata | `rag/ingest.py` | `ingest_document(store, metadata, data=bytes, filename=str)` |
| Top-k retrieval with citation metadata | `rag/retriever.py` | `Retriever(store).retrieve(question)` then `chunk.to_citation()` |
| Annex A precedence and conflicts | `rag/precedence.py` | `resolve_precedence(chunks, as_of_date, programme, batch, register=store.list_sources())` |
| Untrusted-text handling | `rag/safety.py` | `EVIDENCE_RULE` (system) + `format_evidence(r.winning)` (user) |

Exact signatures, error types and a wiring sketch for each endpoint: `docs/INTEGRATION_HANDOFF.md`
sections 3-6. Not built: the LangGraph workflow (`graph/`), the router, synthesis/guard nodes, and the
audit writer that produces `AuditRecord` (the retrieval side supplies `sources_retrieved` via
`chunk.audit_entry()` and `precedence_decision` via `result.decision`).

## 2. Member 2: what must be connected to FastAPI (`api/main.py` is theirs)

1. Create one shared `store` and `retriever` at start-up (guard `StoreError` -> report `down`, do not crash).
2. `POST /ingest`: multipart `file` + `metadata` (JSON string of `SourceRecord`) -> `ingest_document`.
   `IngestError`/bad JSON -> HTTP 422. Remove the `status_code=501`. Keep it a plain `def`.
3. `GET /sources`: `return store.list_sources()`.
4. `GET /health`: `store.health()` for `vector_store`; their own probe for `sqlite`; `llm` from
   `get_llm().health()`. **Mapping to agree:** `LLMHealth.status` is `ok | down | mock`, while
   `ComponentStatus.status` is `ok | degraded | down | not_configured`. Suggested: `ok->ok`, `down->down`,
   `mock->degraded` with detail `mock mode`. Update the stale detail text "Ollama not wired yet".
5. `POST /ask`: replace the mock branches. Policy path: retrieve -> `resolve_precedence` -> build the response from
   `answer_type_hint` (handoff section 4/6). Personal path: their SQLite tools; the student's `programme` and
   `batch` come from the `students` row for the `X-Student-Id` header and are passed to `resolve_precedence`.
6. Persistent audit (`AuditRecord`, `GET /audit/{trace_id}`): store `sources_retrieved`, `precedence_decision`,
   `model`, `llm_calls`, `tokens`, `latency_ms` (from `LLMResult`), and no raw question text.
7. Their own pieces (not Member 1's): `db/schema.sql`, tools, `rule_registry`, authorization, data scripts.

## 3. Member 3: what must be connected to Streamlit and evaluation

1. **UI (`ui/app.py`)** already renders `answer_type`, `answer`, `explanation`, `citations`, `tools_invoked`,
   `conflicts_detected`, `trace_id`, and the four `/health` components, and sends `X-Student-Id` and `as_of_date`.
   It needs no change to work with the real backend. Worth adding: show `answer_type == "conflict_flagged"` and
   `clarification_needed` states clearly, and an upcoming-change note. `conflicts_detected` is shown as raw JSON
   (shape in section 4).
2. **Documents and Source Register:** choose the real public documents and fill `data/source_register.csv`
   (the file does not exist yet). Each row must match `SourceRecord` field-for-field so it can be sent as
   `/ingest` metadata. Use numbered headings (`7.2 Title`) in documents if clause-level `supersedes` is needed
   (handoff section 7). Mark synthetic conflict documents `synthetic: Y`.
3. **Ingest the documents** through `POST /ingest` once Member 2 has wired it, then read `GET /sources`.
4. **Evaluation set and runner** (`eval/` does not exist): include the version/conflict cases and at least one
   hostile-document case; record retrieval hit rate using `chunk.doc_id`/`chunk.section`; tune `RAG_MIN_SCORE` from
   it. Report only measured numbers.
5. **README / disclosures:** the OpenAI-vs-Ollama disclosure text is in `llm/README.md` ("Data note") and
   `docs/INTEGRATION_HANDOFF.md`; the setup section proposed earlier is Member 3's to merge.

## 4. Shared fields and interfaces that must agree

Frozen in `api/models.py` (SHA-256 `59239fff33d17db19886f9c4d369c42bb9a78991dd9e96160121c7b8460140a8`,
identical to `foundation_code.zip`). Do not change without all three members.

| Interface | Agreement |
| --- | --- |
| Header | `X-Student-Id` only; format `S` + 4 digits; never read from the question text. Judge range `S9000-S9999` and course codes `JDG*` are reserved. |
| `AskRequest` | `question` (1-2000 chars), optional `as_of_date`; handler defaults to `date.today()`. |
| `AskResponse` | `trace_id, answer, answer_type, citations, tools_invoked, applied_rules, conflicts_detected, explanation, as_of_date`. |
| `answer_type` | `retrieved_fact, calculated, not_found, clarification_needed, refused, conflict_flagged`. Retrieval gives `PrecedenceResult.answer_type_hint` = `retrieved_fact | conflict_flagged | clarification_needed | not_found`; `calculated` and `refused` come from the tool/auth side. |
| Not-found text | exactly `NOT_FOUND_MESSAGE` from `api.models`. |
| `Citation` | `doc_id, title, section, page, version, effective_from`; built only by `RetrievedChunk.to_citation()` / `PrecedenceResult.citations()`. `section` is `None` when a document has no numbered headings; `page` is `None` for txt/md. |
| `conflicts_detected` item | `{outcome: resolved|unresolved, step: "3 (authority)"|"4 (recency)"|"5 (unresolved)", explanation, winning: [member], overridden: [member]}`; member = `{doc_id, section, version, authority_level, effective_from (ISO str), values}`. |
| `SourceRecord` (Annex B / `/ingest` metadata) | `doc_id, title, issuer, authority_level 1-5, doc_type regulation|circular|notice|faq|handbook|unofficial, version, effective_from, effective_to?, supersedes ("DOC" or "DOC#clause", `;`-separated), scope_programmes (ALL | prefix | list), scope_batches (ALL | 2023 | 2023+ | 2021-2023 | list), provenance, retrieved_on, synthetic Y|N`. Extra fields are rejected. |
| `IngestResponse` | `doc_id, chunks_indexed, status` with status `indexed | replaced | unchanged`. |
| `HealthResponse` | `status, api, vector_store, sqlite, llm`, each a `ComponentStatus`. See the `llm` mapping in section 2. |
| `AuditRecord.sources_retrieved` | list of `{doc_id, section, score}` (Annex D) = `[c.audit_entry() for c in chunks]`. |
| Student scope inputs | `programme` (string, e.g. `B.Tech CSE`) and `batch` (int admission year) from `students.programme` / `students.batch_year`. `None` when no identity. |
| Source Register file formats | CSV deliverable (Member 3) and the store's internal `source_register.json` are different things; the JSON is written only by `ingest_document`. |

## 5. Environment variables (only those the code actually reads)

| Variable | Read in | Default | Notes |
| --- | --- | --- | --- |
| `OPENAI_API_KEY` | `llm/openai_client.py` | none | Set in your own shell only. Never print or commit. |
| `OPENAI_MODEL` | same | `gpt-6-luna` | |
| `OPENAI_TIMEOUT_S`, `OPENAI_MAX_RETRIES`, `OPENAI_MAX_OUTPUT_TOKENS`, `OPENAI_REASONING_EFFORT` | same | see `llm/README.md` | |
| `MOCK_LLM` | `llm/client.py` | unset | `1`, `true`, `yes` or `on` selects `MockLLM` (no key, no network). |
| `CHROMA_DIR` | `rag/store.py` | `data/chroma` | Chroma data and `source_register.json`; git-ignored. |
| `EMBEDDING_BACKEND` | `rag/embeddings.py` | unset | `hash` = offline stand-in for tests only. |
| `EMBEDDING_MODEL` | same | `sentence-transformers/all-MiniLM-L6-v2` | |
| `RAG_MIN_SCORE` | `rag/retriever.py` | `0.25` | Uncalibrated (section 8). |
| `API_URL` | `ui/app.py` | `http://127.0.0.1:8000` | UI -> API address. |
| `RUN_LIVE_LLM`, `RUN_LIVE_EMBEDDINGS` | tests only | unset | `1` enables the opt-in live tests. |

There is no `.env` loader in the code; set variables in the shell (`$env:NAME = "value"`).

## 6. Local Windows startup (PowerShell 5.1, no Docker)

```powershell
cd C:\Users\singh\Desktop\HACKATHON\CodeForge     # or your clone
python -m venv .venv
.\.venv\Scripts\Activate.ps1                       # if blocked: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
pip install -r requirements.txt

python -m pytest tests/m1 tests/test_foundation.py -q -rs     # offline suite, no key/network

# Terminal 1: API (today this still serves the mock /ask; it does not use rag/ yet)
uvicorn api.main:app --reload --port 8000
# Terminal 2: UI
.\.venv\Scripts\Activate.ps1
streamlit run ui/app.py
```

* Offline everything: `$env:MOCK_LLM = "true"`; add `$env:EMBEDDING_BACKEND = "hash"` only for throwaway runs.
* Real LLM: `$env:OPENAI_API_KEY = "<key>"` in your own shell, then
  `$env:RUN_LIVE_LLM = "1"; python -m pytest tests/m1/test_llm_live.py -q -s`.
* Real embeddings, first use only (downloads the model once; needs internet):
  `$env:RUN_LIVE_EMBEDDINGS = "1"; python -m pytest tests/m1/test_rag_real_embedder.py -q -s`.
  Hugging Face prints a harmless symlink warning on this machine.
* `uvicorn` 0.54.0 and `streamlit` 1.66.1 executables exist in `.venv\Scripts`. **I did not start the API or the UI
  server in this check**; only the API's behaviour through `TestClient` was exercised.
* Windows PowerShell 5.1 has no `Invoke-RestMethod -Form`; for `/ingest` use `curl.exe` (curl 8.21.0 is installed):
  ```powershell
  curl.exe -X POST http://127.0.0.1:8000/ingest -F "file=@docs\sample.txt" -F "metadata=<meta.json"
  ```
  where `docs\sample.txt` and `meta.json` are placeholders you create (neither exists in the repo; `meta.json` holds the
  `SourceRecord` JSON). This is the intended request shape and has **not** been run; it cannot work until Member 2
  wires `/ingest` (today it returns 501).

## 7. Test order (do them in this order; tick as you go)

| # | Check | How | Status |
| --- | --- | --- | --- |
| 1 | Environment | `pip install -r requirements.txt`, `pip check` | **Passed** (pip check clean, dry-run install ok) |
| 2 | Offline unit suite | `python -m pytest tests/m1 tests/test_foundation.py -q -rs` | **Passed**: 182 passed, 2 skipped |
| 3 | Live LLM | `RUN_LIVE_LLM=1` live test | **Reported passed by project owner** (1 passed, 6.7 s); not re-run here |
| 4 | Real embedder smoke | `RUN_LIVE_EMBEDDINGS=1` test | **Passed once** earlier (toy corpus); skipped by default |
| 5 | Ingestion via API | `curl.exe` to `/ingest`, then `GET /sources` | **Outstanding** (needs Member 2) |
| 6 | Used immediately, no restart | ingest, then `/ask` about it in the same server run | **Outstanding** |
| 7 | Restart persistence | stop/start the API, `GET /sources`, ask again, no re-ingest | **Outstanding** over HTTP (passed at store level offline) |
| 8 | Retrieval quality | eval set hit rate with the real embedder on real documents | **Outstanding** (Member 3 + 1) |
| 9 | Policy questions end to end | cited answer, `not_found`, partial evidence | **Outstanding** |
| 10 | Student tools | `get_attendance`, `check_exam_eligibility`, other-student refusal | **Outstanding** (Member 2) |
| 11 | Citations | open each cited section/page and confirm it supports the answer | **Outstanding**; mapping tested offline |
| 12 | Conflicts and versions | worked example, future-effective, equal-authority tie, via the API | **Outstanding** via API; logic passed offline (60 tests) |
| 13 | Hostile document | ingest a document containing instructions; confirm they are ignored | **Outstanding** with a real LLM; offline only with `MockLLM` |
| 14 | Audit records | `GET /audit/{trace_id}` for 3 answer types | **Outstanding** (persistent audit not built) |
| 15 | `/health` | all four components truthful | **Outstanding** |
| 16 | UI | badges, citations, conflict and clarification states against the live API | **Outstanding** |
| 17 | Judge rehearsal | ingest an unseen document live; load unseen students; unseen questions | **Outstanding** (loader script does not exist yet) |

## 8. Offline test results, per file (run today)

| File | Result |
| --- | --- |
| `tests/test_foundation.py` | 10 passed |
| `tests/m1/test_llm_mock.py` | 14 passed |
| `tests/m1/test_llm_openai.py` | 31 passed (fake SDK, no real call) |
| `tests/m1/test_rag.py` | 67 passed |
| `tests/m1/test_precedence.py` | 60 passed |
| `tests/m1/test_llm_live.py` | 1 skipped (opt-in) |
| `tests/m1/test_rag_real_embedder.py` | 1 skipped (opt-in) |
| **Combined** `python -m pytest tests/m1 tests/test_foundation.py -q -rs` | **182 passed, 2 skipped** |

## 9. Known limitations

1. **False-conflict issue (open, deliberately not fixed yet).** A conflict among weakly-related retrieved chunks can
   flip the whole answer to `conflict_flagged` (example: two unrelated hostel-fee chunks scoring 0.24 overrode a clear
   0.82 library-fine match at `min_score=0.1`). Does not occur at `min_score >= 0.3` in that example. Mitigation until
   fixed: a meaningful `min_score` and small `top_k`.
2. **Retrieval threshold uncalibrated.** `RAG_MIN_SCORE=0.25` is provisional; one 4-query smoke run showed relevant
   hits at 0.37-0.72 and an off-topic query at 0.27. No retrieval-quality numbers exist.
3. Conflict detection is a text heuristic (shared subject words + different amounts in the same unit); misses
   qualitative conflicts and number words.
4. Clause-level supersession needs numbered headings in the cited document.
5. Single-process only (source register lock is in-process); run one Uvicorn worker.
6. ChromaDB 1.5.9 two-collection bug worked around by the JSON register; root cause unconfirmed.
7. No OCR, no DOCX; tables kept as text.
8. Prompt-injection handling is tested with `MockLLM` only.
9. Brief's stack lists Ollama as primary LLM and a disclosed cloud fallback; this project uses OpenAI with a mock mode.
   The disclosure text is Member 3's README item.

## 10. Next actions

* **Member 1 (me), after your go-ahead:** fix the false-conflict issue; LangGraph policy path
  (`retrieve -> precedence -> synthesize -> guard -> audit`); calibrate `RAG_MIN_SCORE` with Member 3's evaluation set.
* **Member 2:** wire `/ingest`, `/sources`, `/health` and the policy branch of `/ask` (section 2); add an API test that
  ingests text and retrieves it without a restart; persistent audit; agree the LLM health mapping and the
  `programme`/`batch` hand-off.
* **Member 3:** pick the real documents and Source Register rows (numbered headings if possible); evaluation set and
  runner; UI states for conflict and clarification; merge the setup and disclosure text into `README.md`.
* **All three:** review this checklist, commit only on your own branches (`m1-rag`, `m2-backend`, `m3-ui`) after
  approval, and announce any `api/models.py` change before pushing.
