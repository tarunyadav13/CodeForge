# Integration handoff: `rag/` (ingestion, retrieval, precedence)

Audience: Member 2 (API wiring) and the LangGraph work. Written by Member 1's side, 2026-10-23.
Nothing here edits `api/main.py` or `api/models.py`; this file only says how to call `rag/`.

## 1. What exists and what was actually verified

| Piece | File | Verified how |
| --- | --- | --- |
| Section-aware chunking | `rag/chunking.py` | offline unit tests |
| Embeddings (real + offline stand-in) | `rag/embeddings.py` | real model: one opt-in run, 4 queries on a 3-document toy corpus; offline: stand-in only |
| Persisted ChromaDB + JSON source register | `rag/store.py` | offline tests, 400-store repro (0 failures after the fix, 8 before) |
| Ingest (`txt`, `md`, text `pdf`) | `rag/ingest.py` | offline tests incl. a hand-built PDF |
| Retrieval + citations | `rag/retriever.py` | offline tests |
| Untrusted-text handling | `rag/safety.py` | offline tests with `MockLLM` only |
| Annex A precedence | `rag/precedence.py` | 60 offline tests + mutation check |

Offline command (no key, no network, no model download):

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest tests/m1 tests/test_foundation.py -q -rs
# last run: 182 passed, 2 skipped (the 2 skips are the opt-in live OpenAI and real-embedder tests)
```

**Not verified** (see section 9): any wiring through FastAPI, the real embedder on real university
documents, retrieval quality numbers, the LLM answering from `format_evidence()` output.

## 2. Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `CHROMA_DIR` | `data/chroma` | Where ChromaDB data and `source_register.json` live (git-ignored). |
| `EMBEDDING_BACKEND` | unset | `hash` selects the offline stand-in. Never use it for real results. |
| `EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | Real embedding model. First use downloads it (~90 MB) into the Hugging Face cache. |
| `RAG_MIN_SCORE` | `0.25` | Cosine-similarity floor. **Provisional and too low** (section 8). |
| `MOCK_LLM` / `OPENAI_*` | see `llm/README.md` | LLM access; unrelated to `rag/`. |

A store is tied to one embedder. Opening it with a different embedder raises `StoreError`.

## 3. Imports and signatures

```python
from rag import (
    VectorStore, Retriever, RetrievedChunk, ingest_document, IngestError, StoreError,
    resolve_precedence, PrecedenceResult, get_embedder, format_evidence, EVIDENCE_RULE,
)

VectorStore(path: str | Path | None = None, embedder: Embedder | None = None)   # embedder is required
VectorStore.list_sources() -> list[SourceRecord]
VectorStore.health() -> tuple[str, str]            # ("ok" | "down", detail)
VectorStore.chunk_count(doc_id: str | None = None) -> int

get_embedder(env: Mapping[str, str] | None = None) -> Embedder

ingest_document(store, metadata: SourceRecord | Mapping[str, Any], *,
                text: str | None = None, data: bytes | None = None, filename: str | None = None,
                max_words: int = 300) -> IngestResponse            # raises IngestError

Retriever(store, top_k: int = 4, min_score: float | None = None)
Retriever.retrieve(query: str, top_k: int | None = None, min_score: float | None = None,
                   doc_ids: list[str] | None = None) -> list[RetrievedChunk]   # best first; [] if none

resolve_precedence(chunks: Sequence[RetrievedChunk], as_of_date: date,
                   programme: str | None = None, batch: int | None = None,
                   register: Iterable[SourceRecord] | None = None, *,
                   same_subject=None, value_of=None) -> PrecedenceResult
```

Create **one** `VectorStore` per process at start-up and share it:

```python
store = VectorStore(embedder=get_embedder())
store.embedder.embed(["warm up"])      # loads the model now, not on the first student's request
retriever = Retriever(store, top_k=4)
```

`PrecedenceResult` fields: `winning`, `conflicted`, `conflicts`, `excluded`, `decision`,
`answer_type_hint`, `missing_superseders`, `flagged_injection`. Helpers: `.conflicts_detected`
(JSON-safe list for `AskResponse`), `.citations()`, `.citation_chunks()`, `.upcoming`,
`.excluded_for(reason)`.

## 4. Endpoint wiring

### POST /ingest (R11: used immediately, no restart)
Multipart: `file` plus a `metadata` JSON string with the Annex B fields (`SourceRecord`).

```python
import json
from fastapi import File, Form, HTTPException, UploadFile

@app.post("/ingest", response_model=IngestResponse)
def ingest(file: UploadFile = File(...), metadata: str = Form(...)):
    try:
        meta = json.loads(metadata)
        return ingest_document(store, meta, data=file.file.read(), filename=file.filename)
    except (json.JSONDecodeError, IngestError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
```

* Status values: `indexed` (new), `replaced` (same `doc_id`, changed content or metadata),
  `unchanged` (identical re-upload; nothing is re-embedded).
* `IngestError` messages are safe to show. Invalid metadata stores nothing.
* The new chunks are queryable on the next `retrieve` call; no restart or cache refresh.
* Keep the endpoint a plain `def` so FastAPI runs it in a worker thread (embedding is CPU-bound).

### GET /sources
```python
return store.list_sources()          # already list[SourceRecord], sorted by doc_id
```
It reads the JSON register, so it also works while embedding is busy.

### GET /health
```python
status, detail = store.health()      # "ok" with "<n> chunks, <m> sources", or "down" with an error class
vector_store = ComponentStatus(status=status, detail=detail)
```
If constructing `VectorStore` itself raises `StoreError` at start-up (wrong embedder, unreadable
register), report `ComponentStatus(status="down", detail=str(exc))` instead of crashing the API.

### POST /ask (policy path)
Personal questions go to Member 2's tools; this is the document path. Student `programme` and
`batch` come from SQLite using the `X-Student-Id` header, never from the question text.
This sketch was **executed** against synthetic fixtures with `MockLLM` (cases: single document,
supersession, future-effective, not_found, equal-authority conflict, scope with and without student
details). The synthesis step used `MockLLM`, not a real model.

```python
def policy_answer(question, as_of, programme=None, batch=None):
    chunks = retriever.retrieve(question)
    r = resolve_precedence(chunks, as_of, programme, batch, register=store.list_sources())
    if r.missing_superseders:               # a superseding document was not retrieved: fetch it
        chunks += retriever.retrieve(question, doc_ids=r.missing_superseders)
        r = resolve_precedence(chunks, as_of, programme, batch, register=store.list_sources())

    audit = dict(sources_retrieved=[c.audit_entry() for c in chunks],
                 precedence_decision=r.decision)            # Annex D; no raw question text

    if r.answer_type_hint == "not_found":
        return AskResponse(answer=NOT_FOUND_MESSAGE, answer_type="not_found", ...)
    if r.answer_type_hint == "clarification_needed":
        return AskResponse(answer="Which programme and admission year are you in? ...",
                           answer_type="clarification_needed", ...)
    if r.answer_type_hint == "conflict_flagged":
        ...                                                  # see section 6
    text = llm.generate(system=EVIDENCE_RULE,
                        user=format_evidence(r.winning) + f"\n\nQuestion: {question}").text
    return AskResponse(answer=text, answer_type="retrieved_fact",
                       citations=r.citations(), conflicts_detected=r.conflicts_detected,
                       explanation=r.decision, ...)
```

Rules that must hold in the wiring:
1. Pass **only** `r.winning` (or, for a conflict, nothing from the LLM) into the prompt. Never pass
   excluded or superseded chunks.
2. `as_of_date = body.as_of_date or date.today()`, passed through unchanged.
3. Put `EVIDENCE_RULE` in the **system** message and `format_evidence(...)` in the **user** message.
4. Never take citations from LLM output; use `r.citations()`.
5. Mention upcoming rules deterministically from `r.upcoming` (`e.chunk.doc_id`,
   `e.chunk.effective_from`), not by asking the LLM.

## 5. Chunks to citations

`RetrievedChunk.to_citation()` returns the frozen `api.models.Citation`:

| Citation field | Source |
| --- | --- |
| `doc_id`, `title`, `version`, `effective_from` | Source Register metadata stored on the chunk |
| `section` | clause label found by the chunker (`"7.2"`); `None` if the text had no numbered heading |
| `page` | 1-based PDF page; `None` for `txt`/`md` |

`r.citations()` returns winners plus both sides of any unresolved conflict, one per chunk. Judges open
the cited page, so keep one citation per chunk actually used. Plain-text documents have no page numbers,
so their citations carry only `section`.

## 6. Returning the non-answer types safely

* **`not_found`** (`r.answer_type_hint == "not_found"`: nothing retrieved above the floor, or everything
  excluded as upcoming/expired/out of scope/superseded/unusable/informational): answer exactly
  `NOT_FOUND_MESSAGE`, empty `citations`. Do not call the LLM.
* **`clarification_needed`** (everything relevant was excluded only because the student's programme or
  batch is unknown or the document's scope is unparseable): ask for programme and admission year, no
  citations, no LLM call. Not for a missing course code; that is the router's decision.
* **`conflict_flagged`**: build the text **in code** from metadata, do not ask the LLM to pick:
  list each side (`r.conflicted`: title, `doc_id`, section, version, `effective_from`), say the sources
  conflict and the policy cannot resolve it, and advise contacting the issuing office. Return
  `citations=r.citations()` (both sides) and `conflicts_detected=r.conflicts_detected`.
* In all three cases still write the audit record (`sources_retrieved`, `precedence_decision`).
* An *resolved* conflict stays `retrieved_fact`; mention which rule applies and why using `r.decision`
  and `r.conflicts_detected`.

## 7. Metadata the stores need, and what happens when it is wrong

`POST /ingest` metadata is the frozen `SourceRecord` (`extra="forbid"`):

| Field | Required | If missing or invalid |
| --- | --- | --- |
| `doc_id`, `title`, `issuer`, `version`, `provenance` | yes | `IngestError` (HTTP 422), nothing stored |
| `authority_level` (1-5), `doc_type`, `effective_from`, `retrieved_on` | yes | `IngestError`, nothing stored |
| `effective_to` | no | `None` = open-ended; before `effective_from` is rejected |
| `supersedes` | no, default `""` | none; format `DOC` or `DOC#clause`, several separated by `;` |
| `scope_programmes`, `scope_batches` | no, default `ALL` | an empty string is treated as *ambiguous* by precedence |
| `synthetic` | no, default `N` | mark `Y` for the 1-2 allowed synthetic documents |
| unknown extra fields | not allowed | `IngestError` |

Scope syntax understood: programmes `ALL`, a prefix such as `B.Tech` (covers `B.Tech CSE`, not
`B.Technology`), or a `;`/`,` list. Batches `ALL`, `2023`, `2023+`, `2021-2023`, or a list. Anything else
makes the document *ambiguous* for that student (never silently applied).

Defence in depth in `resolve_precedence`: a chunk with a missing `effective_from` or an authority outside
1-5 is `unusable`, never wins and never supersedes. Ingest already prevents this; it protects against
hand-edited data.

Chunk `section` comes from numbered headings only (`7.2 Title`, `Clause 7.2`, `3. Title`). **A document
without numbered headings has `section == ""`, so a clause-level supersession such as `REG#7.2` cannot
match it** and the old document stays current. Whole-document supersession (`REG`) always works. Check
the real documents' headings before relying on clause-level `supersedes`.

## 8. Annex A mapping (checked against the brief)

| Annex A step | Behaviour in `resolve_precedence` |
| --- | --- |
| 1 Applicability | effective on `as_of_date` (`effective_from <= date <= effective_to`, end date inclusive) and scope covers the student. Future documents are excluded and listed in `r.upcoming`; expired and out-of-scope are excluded. |
| 2 Supersession | only from applicable documents of authority 1 or 2, taken from metadata (never from text). Clause-level via `DOC#clause`. A level 3-5 or future-effective or out-of-scope superseder is ignored (noted in `r.decision`). Applied even if the superseder was not retrieved, when `register=` is passed (`r.missing_superseders`). |
| 3 Authority | among conflicting provisions the lower level number wins, regardless of date. |
| 4 Recency | same authority: later `effective_from` wins. |
| 5 Unresolved | same authority and same date, different values: both returned in `r.conflicted`, `answer_type_hint == "conflict_flagged"`, explanation says to contact the issuing office. |
| Level 5 | informational only; never wins, never supersedes. |
| Instructions in documents | ignored: authority, dates, scope and supersession come only from metadata. |

The brief's worked example (regulation 75%, circular 80% superseding it, FAQ 65%) resolves to the
circular, with the FAQ noted as overridden by authority (covered by `test_worked_example_...`).

## 9. Known limitations and what has not been run

1. **Conflict detection is a heuristic.** Metadata has no topic/value fields. Chunks of *different*
   documents conflict when they share subject words and quote different amounts in the same unit
   (`%`, days, weeks, months, hours, marks, credits, semesters, attempts, years, rupees). Misses
   qualitative conflicts ("compulsory" vs "optional") and number words ("ten days"); may link unrelated
   clauses that share vocabulary. `same_subject=` / `value_of=` accept better implementations (for
   example backed by `rule_registry`).
2. **A conflict among weakly-related chunks can flip the whole answer to `conflict_flagged`.** Found
   while running the sketch above: with `min_score=0.1`, two unrelated hostel-fee chunks (score 0.24) that
   conflicted with each other turned a clear library-fine answer (score 0.82) into `conflict_flagged`.
   With `min_score >= 0.3` it did not happen. Mitigation until fixed: use a meaningful `min_score` and a
   small `top_k`. Proposed fix (not implemented): only flag a conflict that involves a chunk scoring near
   the best match.
3. **`RAG_MIN_SCORE=0.25` is untuned.** One smoke run with the real model on a 3-document toy corpus: relevant
   hits 0.37-0.72, an off-topic question 0.27. Needs the evaluation set before a value is claimed.
4. **Not run:** FastAPI wiring of any of these calls; the real embedder against real university documents;
   an LLM (real or mock) answering from `format_evidence()` output beyond prompt-assembly checks; the
   evaluation metrics (retrieval hit rate etc.); a multi-process or multi-worker server.
5. **Live OpenAI test:** reported passing by the project owner (1 passed in 6.7 s). Not re-run in this
   hand-off verification.
6. **Single process only.** The source register uses an in-process lock plus atomic file replace. Two
   Uvicorn workers ingesting simultaneously can lose a registration (last write wins). Run one worker.
7. **ChromaDB workaround.** With two collections in one client, ChromaDB 1.5.9 on Windows left the chunk
   collection permanently unqueryable in ~2% of fresh stores. The register therefore lives in
   `source_register.json`, and Chroma holds one collection. Root cause inside ChromaDB not confirmed.
   Stores from before this change (test directories only) are not migrated.
8. **A crash between writing chunks and writing the register** leaves chunks that `GET /sources` does not
   list; re-uploading the document repairs it (no duplicates).
9. **Ingest scope:** `.txt`, `.md`, text-based `.pdf`. No OCR (scanned PDFs are rejected with a clear
   message), no DOCX, tables are kept as text lines, not parsed.
10. **Prompt-injection handling is defence in depth,** tested with `MockLLM` only. It is not proof against a
    real model; the evaluation should include hostile documents.
11. **Heading detection is heuristic.** Unusual numbering (`7(a)`, roman numerals) is not recognised, so
    those documents chunk coarsely and clause-level citations fall back to `section == ""`.

## 10. Next integration steps

1. Member 2: create the shared `store`/`retriever` at start-up; wire `/ingest`, `/sources`, `/health`
   (sections 3-4). Add an API test that ingests a small synthetic text and then retrieves it with no restart.
2. Member 2 + Member 1: define how `programme`/`batch` reach `resolve_precedence` (from the `students` row
   for the header identity; `None` when there is no identity).
3. Member 3: choose the real public documents and fill the Source Register; confirm each document uses
   numbered headings if clause-level `supersedes` is needed; ingest them and review the chunks.
4. Member 1: LangGraph policy path (`retrieve -> precedence -> synthesize -> guard -> audit`) using the
   sketch in section 4; fix limitation 2; tune `min_score` from the evaluation set.
5. Team: evaluation set including at least 3 version/conflict questions and a hostile-document case;
   report only measured numbers.
