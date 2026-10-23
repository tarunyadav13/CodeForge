# Evaluation

## Dataset: `eval/eval_set.jsonl`

30 cases, one JSON object per line. Categories: policy_answerable 5, unanswerable 4,
conflict_version 5, personal_tool 7, cross_student 3, multi_step 3, clarification 1,
prompt_injection 2. Handoff minimums (3 unanswerable, 3 conflict/version, 4 personal via tools,
2 cross-student, 2 multi-step, 20 total) are all met and enforced by `tests/test_m3_eval.py` and
`tests/test_m3_dataset_audit.py`.

| Field | Meaning |
|---|---|
| `id`, `category`, `question`, `evaluates` | What is asked and why |
| `student_id` | Sent as the `X-Student-Id` header (null = no header). Synthetic placeholders; never S9000-S9999 |
| `as_of_date` | Sent in the body when set |
| `expected_answer_type` | List of acceptable answer types |
| `expected_answer_contains` / `forbidden_strings` | Machine-checkable content (word-boundary matching, so `75` does not match `175`) |
| `requires_citations`, `expected_citation_doc_ids` | Citation checks |
| `expected_tools`, `expected_tool_outputs`, `requires_applied_rules` | Tool checks |
| `requires_conflicts` | `conflicts_detected` must be non-empty |
| `forbidden_tool_student_ids` | No tool may run for these IDs |
| `fixture_status` | `ready` or `pending` |
| `fixture_requirements`, `human_review` | Dependencies, and what a person must check by hand |

### Fixture status

* **Ready, 18 cases:** EV-005 to 008, 021 to 023, 027, 028 need no fixtures. EV-001, 004, 009 to 013,
  029 and 030 are tied to documents that now exist in `data/source_register.csv`, with expected
  values read from those documents (the real ones on 2026-10-23, the synthetic ones by construction).
  "Ready" means the expectation is final, **not** that a backend has ever produced it.
* **Pending, 12 cases:** EV-002 and EV-003 (policy corpus coverage not checked: CGPA formula and
  supplementary-exam procedure may be answerable from IITM-INTMA-ORD, left for review) and every
  `personal_tool` and `multi_step` case (need Member 2's seed data).
  `S1001`/`S1002`/`S1003`, `CS201`/`CS202` are placeholders; the exact-threshold student (EV-019)
  must exist in the seed. Pending cases get their value checks **skipped**, never passed.
* EV-007 ("1985 regulations") expects `not_found`. The UGC PDF in the corpus mentions a 1985
  notification only as superseded and does not state its content; if the backend answers from that,
  review the case rather than the system.
* The real documents come from three institutions and disagree (UGC 75%, MSRUAS 75% with 10%
  condonation, IIT Madras 85%). EV-001 and EV-030 therefore name the institution in the question.
  Open team decision: `data/README.md`.

## Runner: `eval/runner.py`

```powershell
python eval/runner.py --validate-only      # dataset check, no HTTP
python eval/runner.py --mock               # MOCK-ONLY replay, no API needed
python eval/runner.py --audit              # LIVE: API_BASE_URL or http://localhost:8000
python eval/runner.py --ids EV-005,EV-021  # subset
```

Per case it records HTTP status, latency, the validated response, contract warnings and each check
(`pass` / `fail` / `skipped` with a reason). Case status is `PASS`, `FAIL` or `ERROR` (transport or
contract failure). HTTP 401/403 counts as a refusal only when `refused` is expected. Exit code: 0 all
pass, 1 any FAIL/ERROR, 2 invalid dataset. Output and JSON carry the run **mode**: `live` or `mock`.

| Metric | How it is computed | Kind |
|---|---|---|
| answer_type_accuracy | answer_type in expected list; errored cases count as wrong | automatic |
| answer_correctness_auto | `expected_answer_contains` + `forbidden_strings` | automatic, where values are known |
| citation_correctness | citations present, metadata complete (section/page, version, effective_from), expected doc_ids | automatic |
| abstention_accuracy | abstention cases that abstained | automatic |
| false_abstention_rate | answerable cases that abstained | automatic |
| not_found_exact_message | not_found answers equal the exact required text | automatic |
| tool_correctness | expected tools invoked, expected outputs matched | automatic |
| cross_student_leaks | tools ran for an ID other than the header; N/A when no such check ran | automatic |
| retrieval_hit_rate | expected doc_id in audit `sources_retrieved` | needs `--audit`, else N/A |
| latency p50 / p95 | client-side round trip of `/ask` | live only; always N/A in mock mode |
| llm_calls_total / tokens_total | sum from audit records | needs `--audit` and non-zero values, else N/A |
| semantic correctness | cases with `human_review` | **human review** |

Unavailable values are `null` with a reason, never zero. If any case errors, the output prints a
warning, because the rate metrics other than answer_type_accuracy exclude errored cases.

## Results

### Live: none

No evaluation has been run against the real backend (not in this checkout). There are no live
numbers, and none are reported.

### MOCK-ONLY run, 2026-10-23 (`eval/results/mock_run_2026-10-23.json`)

Command: `python eval/runner.py --mock --output eval/results/mock_run_2026-10-23.json`.
The mock (`eval/mock_api.py`) replays each case's own expectations, so a perfect score is
guaranteed by construction. **It does not measure the assistant.** It shows only that the dataset
parses, responses pass contract validation, and the scoring code runs end to end.

| Measure | Mock value | Meaning |
|---|---|---|
| cases / pass / fail / error | 30 / 30 / 0 / 0 | pipeline works |
| checks skipped | 1 | EV-019 pending tool-output expectation |
| answer_type, correctness, citation, abstention, tool, not_found-exact | 1.0 | by construction |
| false_abstention_rate, cross_student_leaks | 0.0, 0 | by construction |
| retrieval_hit_rate, latency p50/p95, llm_calls, tokens | N/A | cannot be mocked honestly |

The scoring code's failure detection is tested separately with deliberately bad responses
(`tests/test_m3_runner_audit.py`, `tests/test_m3_eval.py`).
