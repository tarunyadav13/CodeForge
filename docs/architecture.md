# Architecture

Status key: **built** = in this repository and tested; **planned** = agreed in the team handoff,
owned by another member, not yet verified here.

| Layer | Path | Owner | Status |
|---|---|---|---|
| Streamlit UI | `ui/app.py`, `ui/api_client.py`, `ui/formatting.py` | M3 | built |
| Evaluation | `eval/eval_set.jsonl`, `eval/runner.py`, `eval/mock_api.py` (mock only) | M3 | built, mock-tested |
| Policy corpus + Source Register | `data/source_register.csv`, `data/documents/` | M3 (with M1) | built; real-doc dates unresolved |
| API contract | `api/models.py` (frozen) | all | in foundation code, not on `main` yet |
| FastAPI app | `api/main.py` | M2 | foundation mock (not on `main` yet) |
| Graph, retrieval, precedence, LLM | `graph/`, `rag/`, `llm/` | M1 | planned |
| SQLite schema, tools, loaders | `db/`, `tools/`, `scripts/` | M2 | planned |

## Request flow

1. The UI sends `POST /ask` with JSON `{question, as_of_date?}` and header `X-Student-Id` (only if
   the user entered one). The question text is sent unchanged; the UI never puts the ID in it.
2. *(planned)* `api/main.py` calls `run_graph(question, student_id, as_of_date)`.
3. *(planned)* Policy path: ChromaDB top-k → Annex A precedence (applicability → supersession →
   authority → recency → unresolved = `conflict_flagged`) → Ollama answer from retrieved text only.
4. *(planned)* Personal path: header ID must exist; tools (`get_attendance`, `get_results`,
   `get_backlogs`, `check_exam_eligibility`, `check_placement_eligibility`) read SQLite and
   `rule_registry`; the LLM only explains.
5. One `AskResponse`: `trace_id, answer, answer_type, citations, tools_invoked, applied_rules,
   conflicts_detected, explanation, as_of_date`; an audit record (Annex D) is stored per trace.

## UI client design

`ui/api_client.py` is the only code that talks HTTP; the UI and the eval runner both use it, so
they validate responses identically. It mirrors the frozen contract's constants instead of
importing `api/models.py`, so the UI can run on a machine without the backend code; a test
fails if the mirrored constants drift from `api/models.py`. Any transport error, HTTP error,
non-JSON body or missing required field becomes an error result. There is no code path that
shows an answer the API did not return.
