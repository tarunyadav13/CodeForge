# Live demo script

**Nothing below has been run against the team's backend yet.** Each step lists what must be true
first. Rehearse every step on the demo laptop and tick it off before judging.
Placeholder IDs (`S1001`, `S1002`, `CS201`) must be swapped for real seed IDs.

Setup: terminal 1 `uvicorn api.main:app --port 8000`, terminal 2 `streamlit run ui/app.py`,
terminal 3 for `Invoke-RestMethod`. Open http://localhost:8501.

| # | Show | Do | Expect | Depends on | If it is not working |
|---|---|---|---|---|---|
| 1 | Health | Sidebar → Refresh status | All four components green | M1 + M2 health probes | Show the degraded status honestly; it is real `/health` output |
| 2 | Cited policy answer | No student ID. Ask: *What is the minimum attendance required to write the end-semester examinations?* | Green **Retrieved fact** badge, citation table with doc, section/page, version, effective date | M1 retrieval + real docs | Skip; do not substitute a mocked answer |
| 3 | Tool-based answer | Student ID `S1001`. Ask: *Am I eligible to write the end-semester exam for CS201?* | Blue **Calculated** badge; `check_exam_eligibility` input/output; applied rule with source doc | M2 tools + seed data | Show `pytest` output for M2's tool tests instead, and say so |
| 4 | not_found | Ask: *What is the parking fee for parents visiting on convocation day?* | Grey **Not found** with the exact required sentence | M1 retrieval threshold | — |
| 5 | Conflict | As-of date 2026-10-01. Ask: *What is the minimum attendance required to sit for the end-semester exams?* | Answer uses the later circular (80%) and lists the resolved conflict; for the fee question a violet **Conflict flagged** with both sources | M1 precedence + synthetic conflict docs | Show M1's precedence unit test output |
| 6 | Cross-student refusal | Student ID `S1001`. Ask: *Show me the attendance of student S1002 in CS201.* | Red **Refused**; no tool table | M2 authorization | — (must work; disqualifying if not) |
| 7 | Audit | Copy the trace ID from step 3's Trace panel → Audit tab → Look up | Audit JSON: sources, tools with inputs/outputs, model, latency; no question text | M2 persistent audit | `Invoke-RestMethod http://localhost:8000/audit/<id>` |
| 8 | Live ingestion | Ingest tab: upload a new synthetic notice with metadata, then ask about its content | Backend status + chunk count; next answer cites the new doc_id | M1 `/ingest` | UI shows "not implemented (501)"; say so |
| 9 | Evaluation | `python eval/runner.py --audit` | Table + metrics; open the results JSON | everything above | Show `python -m pytest -q` (mocked tests) |

Talking points: identity comes only from the header (step 6 puts an ID in the text and it is
ignored); numbers come from tools, not the LLM (step 3 tool output table); every answer has a
trace ID (step 7).
