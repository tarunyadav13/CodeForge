# Backend data layer (Member 2): SQLite, synthetic data, validation

All commands are shown for Windows PowerShell and run from the repo root. Python 3.11+.

```powershell
pip install fastapi uvicorn "pydantic>=2" python-multipart httpx pytest "openai>=2,<4"
python -m db.data_access --init                                   # create tables (safe to repeat)
python scripts/gen_data.py --out data/synthetic                   # deterministic CSVs (seeded)
python scripts/load_students.py --dir data/synthetic              # load CSVs into SQLite
python scripts/validate.py --db data/student_services.db          # integrity + edge-case checks (exit 1 on errors)
python -m pytest tests/test_backend_db.py tests/test_backend_data.py tests/test_backend_tools.py tests/test_backend_api.py -q
```

Database file: `$env:STUDENT_DB_PATH`, default `data/student_services.db` (never commit it).
Judges load their own students with the same loader: `python scripts/load_students.py --dir test_students/`.

## Schema (`db/schema.sql`)
Annex C tables `students`, `courses`, `attendance`, `results`, `rule_registry` with the exact required
column names, plus `source_register` (Annex B), `audit` (Annex D + rules/conflicts/citations) and `schema_meta`.
* CHECK constraints: student id `S`+4 digits, semester 1-10, CGPA 0-10, backlogs >= 0, held > 0,
  0 <= attended <= held, `total = internal + external`, `total <= max`, enumerated `exam_type`/`result`/`doc_type`,
  real calendar dates, `effective_to >= effective_from`.
* Foreign keys are switched on for **every** connection (`db/data_access.py:connect`). `rule_registry.source_doc_id`
  must exist in `source_register`.
* Attendance % is **never stored**; tools compute it with exact fractions.
* Reserved judge IDs (S9000-S9999, `JDG*`) are accepted by the schema/loader (judges use them). Our own data never
  uses them; `validate.py` flags them unless run with `--allow-reserved`.
* Initialisation is idempotent (`CREATE ... IF NOT EXISTS`). Loader uses UPSERT, so reloading never wipes child rows.

## Rule registry: how a rule gets in, and what a new circular does
1. A rule row is written to `rule_registry` (via `rule_registry.csv` + the loader, or `db.data_access.upsert_rule`).
   Every row names a `source_doc_id` (must be in the Source Register) and a `source_section` (the clause).
2. Tools read thresholds **only** from this table, filtered by as-of date (`effective_from <= d` and `effective_to`
   empty or `>= d`), programme scope (`ALL`, `B.Tech`, `CSE;ECE`) and batch scope (`ALL`, `2023`, `2023+`, `2022-2024`).
3. A new circular that changes a threshold: call `db.data_access.supersede_rule(conn, old_rule_id, new_rule)`
   (closes the old row the day before, inserts the new one) or load a new row. No code change; answers change from the
   effective date. Rules not yet effective are reported as `upcoming_rules` (informational), never applied.
4. **Uploading a document through `/ingest` never creates or edits rules.** Executable rules are an explicit, reviewed step.
5. If more than one rule applies to the same parameter, the tools do not guess: they call the policy resolver
   registered with `tools.set_policy_resolver` (Member 1) or report `rule_conflict` (see `tools/README.md`).

## Synthetic data (`scripts/gen_data.py`)
36 students (2 programmes x 2 batches x 9), 8 courses, 139 attendance rows, 76 result rows, 6 rules, 3 synthetic source rows
(`FIX-*`, `synthetic=Y`). Fixture thresholds: attendance 75% (old, to 2026-07-31) then 80% (circular, from 2026-08-01),
pass mark 40%, placement CGPA >= 6.50 and 0 backlogs, supplementary attempts <= 3. These are **test placeholders**, not any
university's real policy; repoint `rule_registry.source_doc_id` at real Source Register rows once real documents are chosen.
Edge-case student IDs are listed in `data/synthetic/generation_report.json` and `DATA_CARD.md`.
`--mode openai` asks the OpenAI client (`llm/openai_client.py`) for names and CGPAs (prompts in `prompts/`), validates every
row, and fills any gap deterministically. It has **not** been run against the real API yet.

## Persistent audit
`audit` rows are written by the API for every `/ask` outcome (including refusals). They hold ids, category, tool inputs
and outputs (minus `full_name`), rules, citations, model, tokens, latency. Raw question text is rejected by
`write_audit` and never stored.

## Known limitations
* Fixture data is simplified (e.g. batch-2025 students are semester 3 but have results in semester-4 courses).
* PASS/FAIL consistency in `validate.py` uses the single `pass_marks_pct` rule; grade tables are not modelled.
* `.gitignore` does not exist yet in the repo: it must exclude `data/student_services.db`, `data/uploads/`, `data/chroma/`, `.env`, `__pycache__/`.
