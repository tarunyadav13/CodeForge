# Evaluation

## Dataset — `eval/eval_set.jsonl`

29 cases, one JSON object per line. Categories: policy_answerable 4, unanswerable 4,
conflict_version 5, personal_tool 7, cross_student 3, multi_step 3, clarification 1,
prompt_injection 2 (brief minimums: 3 / 3 / 4 / 2 / 2 — all met).

| Field | Meaning |
|---|---|
| `id`, `category`, `question`, `evaluates` | What is asked and why |
| `student_id` | Sent as `X-Student-Id` (null = no header). Synthetic placeholders; never S9000–S9999 |
| `as_of_date` | Sent in the body when set |
| `expected_answer_type` | List of acceptable answer types |
| `expected_answer_contains` / `forbidden_strings` | Machine-checkable content (boundary-aware matching) |
| `requires_citations`, `expected_citation_doc_ids` | Citation checks |
| `expected_tools`, `expected_tool_outputs`, `requires_applied_rules` | Tool checks |
| `requires_conflicts` | `conflicts_detected` must be non-empty |
| `forbidden_tool_student_ids` | No tool may run for these IDs |
| `fixture_status` | `ready` or `pending` |
| `fixture_requirements` | What documents / seed rows the case depends on |
| `human_review` | What a person must check by hand |

**Fixture status.** Most cases are `pending`: the policy corpus, synthetic conflict documents and
M2's seed data do not exist yet. `S1001`/`S1002`/`S1003`, `CS201`/`CS202` and the `SYN-*` doc IDs
are placeholders. When fixtures land: replace the placeholders, fill `expected_answer_contains`,
`expected_citation_doc_ids` and `expected_tool_outputs` from the real data, and flip the case to
`ready`. The runner **skips** (never passes) value checks on pending cases.

## Runner — `eval/runner.py`

Per case it records HTTP status, latency, the validated response, contract warnings and each
check (`pass` / `fail` / `skipped` with a reason). Case status is `PASS`, `FAIL` or `ERROR`
(transport or contract failure). HTTP 401/403 counts as a refusal only when `refused` is expected.
Exit code: 0 all pass, 1 any FAIL/ERROR, 2 invalid dataset.

| Metric | How it is computed | Kind |
|---|---|---|
| answer_type_accuracy | answer_type in expected list | automatic |
| answer_correctness_auto | `expected_answer_contains` + `forbidden_strings` | automatic, where values are known |
| citation_correctness | citations present, metadata complete (section/page, version, effective_from), expected doc_ids | automatic |
| abstention_accuracy | abstention cases that abstained | automatic |
| false_abstention_rate | answerable cases that abstained | automatic |
| not_found_exact_message | not_found answers equal the exact required text | automatic |
| tool_correctness | expected tools invoked, expected outputs matched | automatic |
| cross_student_leaks | tools ran for an ID other than the header | automatic |
| retrieval_hit_rate | expected doc_id in audit `sources_retrieved` | needs `--audit`, else N/A |
| latency p50 / p95 | client-side round trip of `/ask` | automatic |
| llm_calls_total / tokens_total | sum from audit records | needs `--audit` and non-zero values, else N/A |
| semantic correctness | cases with `human_review` | **human review** |

Cases count as `needs_human_review` when the expected answer can only be judged by reading it
against the cited page.

## Results

None measured against the team's backend yet. The runner was exercised only against a throwaway
contract mock during development to check that it runs; those numbers are not results and are not
reported.
