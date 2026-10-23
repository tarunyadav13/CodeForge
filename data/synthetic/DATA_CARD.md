# Synthetic data card (Annex E), draft from Member 2

| Field | Content |
|---|---|
| Purpose | Exercise attendance/eligibility/placement tools and the conflict-by-date behaviour with deliberate boundary cases. Contains **no real person**. |
| Generator | `scripts/gen_data.py`, seed `20261006`. Default mode is deterministic Python (no LLM): two runs are byte-identical (verified by a test). Optional `--mode openai` drafts names and CGPAs via `llm/openai_client.py` (model from `OPENAI_MODEL`). **The OpenAI mode has not been run against the real API; this dataset was generated deterministically (0 rows LLM-drafted).** |
| Prompts | `prompts/student_generation_system.txt`, `prompts/student_generation_user.txt` (used verbatim by the script; SHA-256 recorded in the report when the LLM mode runs). |
| Schema enforcement | CSV columns = Annex C. Loader type-checks every row; SQLite CHECK/FK constraints; LLM mode: structured-output schema, then per-row Pydantic validation, duplicate-name and quota checks; invalid rows are rejected and counted. |
| Row counts | 36 students (CSE/ECE x batch 2024/2025, 9 each), 8 courses, 139 attendance rows, 76 result rows, 6 rules, 3 source rows (all `synthetic=Y`). |
| Distributions | Attendance %: min 0.0, mean 77.52, max 100.0. CGPA: min 5.25, max 9.32. Pass mark 40/100. |
| Edge cases (IDs) | Attendance exactly 80%: S1001, S1005, S1020. One class below 80%: S1002, S1006, S1021. Exactly 75%: S1003. One below 75%: S1004. Missing attendance: S1009 (CS201), S1028 (all). Failed one mark below pass (39/100): S1007 (MA201). 3 active backlogs: S1008. 2 backlogs: S1025. ABSENT: S1010. DETAINED: S1022. CGPA exactly 6.50: S1011, S1023. CGPA 6.49: S1012. No results: S1013. Supplementary attempts exhausted: S1019. Zero / full attendance: S1029 / S1030. Other students may hit boundaries by chance; see `generation_report.json`. |
| Validation results | `python scripts/validate.py --db ...` -> `RESULT: PASSED - 0 error(s), 0 warning(s)` (output saved in `validation_output.txt`; 9 check groups). Pytest also plants corrupt rows and confirms each is caught. |
| What the LLM got wrong | Not applicable yet: no real LLM run. Fill this in after a real `--mode openai` run from `generation_report.json` (`rejection_reasons`). A test confirms bad rows (short name, CGPA 11, unknown programme, duplicates) are rejected. |
| Known limitations | Rules are test placeholders, not any university's policy. Batch-2025 students are semester 3 but hold semester-4 course results. Names come from small fixed lists. No grade tables, credits-weighted CGPA, or timetable realism. |
