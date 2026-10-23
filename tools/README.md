# Deterministic student tools (Member 2)

Everything authoritative is computed here in Python/SQL with exact fractions. The LLM only chooses tools and
explains their output. Tools use read-only database connections.

## Calling tools safely (for Member 1's graph)
```python
from tools import call_tool, tool_specs, AccessDeniedError
result = call_tool("check_exam_eligibility", {"course_code": "CS201"},
                   authenticated_student_id=student_id,   # from the validated X-Student-Id header, never from text
                   as_of_date=as_of_date)
# -> {"tool", "input", "output", "status": "ok"|"error"|..., "ms"}
```
* The tool always acts on `authenticated_student_id`. A different `student_id` inside the arguments raises
  `AccessDeniedError` (map it to `answer_type="refused"`). `tool_specs()` gives OpenAI function schemas with **no**
  `student_id` parameter, so the model cannot name a student. There is no SQL tool.
* Errors come back as `output = {"status": "error", "error": {"code", "message"}}` (`invalid_input`, `student_not_found`,
  `course_not_found`). Unknown tools raise `UnknownToolError`.
* Build `ToolInvocation`s from `{"tool", "input", "output"}`; `output["applied_rules"]` already has the
  `AppliedRule` shape; `output["rule"]` / `output["criteria"][i]["rule"]` hold full provenance for citations
  (`source_doc_id`, `source_section`, `source_title`, `source_version`, `source_effective_from`).

## Tools
| Tool | Arguments | Result |
|---|---|---|
| `get_student_profile` | | programme, batch_year, current_semester, cgpa, active_backlogs |
| `get_attendance` | course_code | `status` ok/no_record, classes_held, classes_attended, classes_missed, attendance_pct (computed) |
| `get_results` | course_code? | list of result rows, oldest first (wrapped as `{"results", "count"}` by `call_tool`) |
| `get_exam_result` | course_code, session | `status` ok/no_record, rows for that session |
| `get_backlogs` | | `active_backlogs` (stored, official), `derived_count` + `courses` (from results), `consistent` |
| `check_exam_eligibility` | course_code, as_of_date | `result` ELIGIBLE / NOT_ELIGIBLE / UNDETERMINED / NOT_APPLICABLE, `rule_id`, `threshold`, `actual_value`, `attendance`, `reason`, `rule` (provenance), `applied_rules`, `upcoming_rules`, `rule_resolution` |
| `check_supplementary_eligibility` | course_code, as_of_date | same shape; basis = latest attempt FAIL/ABSENT/DETAINED and the `supp_max_attempts` rule |
| `check_placement_eligibility` | as_of_date | `result`, per-criterion `criteria` (CGPA, backlogs) each with rule provenance |
| `simulate_what_if` | scenario, check, as_of_date | `baseline`, `simulated`, `assumptions`, `outcome_changes`; never writes |

`scenario` keys: `pass_courses` (backlog courses assumed passed), `cgpa`, `attendance` (`{course: {classes_held, classes_attended}}`);
`check`: `placement`, `exam:<course>` or `supplementary:<course>`.

## Rule parameters read from `rule_registry`
`min_attendance_pct`, `pass_marks_pct` (validator), `supp_max_attempts`, `min_cgpa_placement`, `max_active_backlogs_placement`.
Operators `>= <= > < == != between` are applied exactly as stored.

## Integration boundary with Member 1: policy precedence
When more than one rule is applicable for a parameter the tools do not choose. Register Member 1's resolver:
```python
from tools import set_policy_resolver
set_policy_resolver(resolve_fn)   # (candidates: list[dict], as_of: date, programme: str, batch: int)
                                  #   -> {"winning": <one candidate dict>|None, "conflicts_detected": [...], "reason": str}
```
Candidates are `rule_registry` rows joined with their source document (`source_authority_level`, `source_supersedes`,
`source_effective_from`...). Without a resolver, or if it returns no winner or raises, the result is `UNDETERMINED` with
`rule_resolution.status == "rule_conflict"` and `conflict_candidates`; the API turns that into `conflict_flagged`.

Run `python -m pytest tests/test_backend_tools.py -q`.
