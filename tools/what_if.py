"""What-if simulation. Runs on a READ-ONLY connection and an in-memory overlay, so official
records can never change. It reuses the same eligibility logic as the real tools.

scenario (all keys optional):
    {"pass_courses": ["CS201"],                   # assume these backlog courses are cleared (PASS)
     "cgpa": 7.1,                                 # assume this CGPA
     "attendance": {"CS201": {"classes_held": 40, "classes_attended": 35}}}  # assume these counts
check:
    "placement"                    -> placement eligibility
    "exam:<course_code>"           -> exam (attendance) eligibility for that course
    "supplementary:<course_code>"  -> supplementary eligibility (after the scenario's results)

The output always contains the real baseline, the simulated outcome, and the assumptions made.
"""
from __future__ import annotations

from typing import Any

from db import data_access as da
from tools._common import parse_as_of
from tools.eligibility import exam_eligibility_core, placement_core, supplementary_core

_SCENARIO_KEYS = {"pass_courses", "cgpa", "attendance"}


def simulate_what_if(student_id: str, scenario: dict[str, Any], check: str = "placement",
                     as_of_date: Any = None, *, db_path: Any = None) -> dict[str, Any]:
    if not isinstance(scenario, dict):
        raise da.InvalidInputError("scenario must be an object")
    unknown = set(scenario) - _SCENARIO_KEYS
    if unknown:
        raise da.InvalidInputError(f"unsupported scenario keys: {sorted(unknown)}; allowed: {sorted(_SCENARIO_KEYS)}")
    if not isinstance(check, str) or not check:
        raise da.InvalidInputError("check must be 'placement', 'exam:<course>' or 'supplementary:<course>'")
    as_of = parse_as_of(as_of_date)
    kind, _, target = check.partition(":")
    if kind not in ("placement", "exam", "supplementary") or (kind != "placement" and not target):
        raise da.InvalidInputError("check must be 'placement', 'exam:<course>' or 'supplementary:<course>'")

    assumptions: list[str] = []
    with da.db_session(db_path, read_only=True) as conn:  # read-only: cannot write
        student = da.require_student(conn, student_id)
        backlog_courses = {b["course_code"].casefold(): b for b in da.get_backlog_courses(conn, student_id)}

        # ---- overlay ---------------------------------------------------------------
        cgpa = student["cgpa"]
        backlogs = student["active_backlogs"]
        passed: list[str] = []
        for raw in scenario.get("pass_courses", []) or []:
            course = da.require_course(conn, raw)
            code = course["course_code"]
            if code.casefold() in backlog_courses:
                passed.append(code)
            else:
                assumptions.append(f"{code} is not currently a backlog, so passing it changes nothing.")
        if passed:
            backlogs = max(0, backlogs - len(passed))
            assumptions.append(f"Assumes {', '.join(passed)} is passed, reducing active backlogs by {len(passed)}.")
            if "cgpa" not in scenario:
                assumptions.append("CGPA is assumed unchanged; clearing a backlog may change the real CGPA.")
        if "cgpa" in scenario:
            cgpa = scenario["cgpa"]
            assumptions.append(f"Assumes CGPA of {cgpa}.")

        # ---- baseline (real data) and simulated outcome ----------------------------------
        if kind == "placement":
            baseline = placement_core(conn, student, student["cgpa"], student["active_backlogs"], as_of)
            simulated = placement_core(conn, student, cgpa, backlogs, as_of)
        elif kind == "exam":
            course = da.require_course(conn, target)
            real = da.get_attendance_record(conn, student_id, course["course_code"])
            sim = dict(real) if real else None
            override = (scenario.get("attendance") or {})
            match = next((v for k, v in override.items() if str(k).casefold() == course["course_code"].casefold()), None)
            if match is not None:
                try:
                    held, attended = int(match["classes_held"]), int(match["classes_attended"])
                except (KeyError, TypeError, ValueError):
                    raise da.InvalidInputError("attendance override needs integer classes_held and classes_attended") from None
                if held <= 0 or not 0 <= attended <= held:
                    raise da.InvalidInputError("attendance override must satisfy held > 0 and 0 <= attended <= held")
                sim = {"classes_held": held, "classes_attended": attended}
                assumptions.append(f"Assumes {attended}/{held} classes attended in {course['course_code']}.")
            baseline = exam_eligibility_core(conn, student, course, real, as_of)
            simulated = exam_eligibility_core(conn, student, course, sim, as_of)
        else:
            course = da.require_course(conn, target)
            attempts = da.get_results(conn, student_id, course["course_code"])
            baseline = supplementary_core(conn, student, course, attempts, as_of)
            sim_attempts = list(attempts)
            if course["course_code"] in passed:
                sim_attempts.append({"exam_type": "SUPPLEMENTARY", "result": "PASS", "exam_session": "(simulated)"})
            simulated = supplementary_core(conn, student, course, sim_attempts, as_of)

    return {"status": "ok", "simulation": True, "modifies_records": False, "student_id": student_id,
            "check": check, "as_of_date": as_of.isoformat(), "scenario": scenario,
            "assumptions": assumptions, "baseline": baseline, "simulated": simulated,
            "baseline_result": baseline["result"], "simulated_result": simulated["result"],
            "outcome_changes": baseline["result"] != simulated["result"],
            "applied_rules": simulated.get("applied_rules", [])}
