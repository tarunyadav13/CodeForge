# Known limitations and blockers

Last updated 2026-10-23.

## Blockers

- **Backend not in this checkout.** `api/main.py`, `api/models.py`, `graph/`, `rag/`, `db/`, `tools/`,
  `scripts/load_students.py` and `requirements.txt` are all missing on `m3-ui` (the foundation code
  lives in a teammate's working copy per the handoff). Member 3's code was built against the
  contract quoted in the handoff and tested with mocked HTTP only. No live check has been run:
  `/health`, `/ask`, `/sources`, `/audit/{id}`, `/ingest`, the student CSV loader and unseen-question
  behaviour are all **BLOCKED ON BACKEND**.
- **Seed data missing.** Personal and multi-step eval cases (12 pending) wait for Member 2's data kit.
- **Metadata gaps in the Source Register.** `effective_from` is blank for the three real documents
  because none states a verifiable date and `SourceRecord` requires one; they cannot be loaded via
  `/ingest` until the team decides. See `data/README.md`.
- **Real documents are from three different institutions** and disagree; the team must decide which
  is "the university" (open decision in `data/README.md`).

## Prototype limitations

- `X-Student-Id` is not authentication; any client can claim any ID.
- Authority levels of the synthetic documents (1 regulation, 2 circular, 3 notice, 5 FAQ) are assumed;
  the brief's Annex A table is not in the repo.
- The real documents were downloaded unmodified from public sites with no stated licence; they may
  need to be removed from the repository if the team does not want to redistribute them.
- UGC regulation 2003 may have been superseded by a later UGC regulation; currency was not checked.
- The UI does not persist chat history across browser refreshes (session state only).
- The `/ingest` field names (`file`, `metadata`) and the shape of `GET /sources` are assumptions.
- Answer correctness beyond substring checks needs human review.
- Latency is measured client-side and includes network and LLM time; always N/A in mock mode.
- Mock evaluation results prove nothing about the assistant (see `docs/evaluation.md`).
