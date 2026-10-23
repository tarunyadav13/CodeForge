"""Eligibility tools: exam (attendance), supplementary exam, placement.

All decisions are made in Python with exact fractions from thresholds in rule_registry.
Results are ELIGIBLE / NOT_ELIGIBLE, or UNDETERMINED when the data or a rule is missing or
ambiguous (never a guess). Every result carries the rule provenance (rule_id, threshold,
source document, clause, version, effective dates) and any not-yet-effective rules as
informational "upcoming_rules".

The *_core functions take explicit values so tools.what_if can reuse the exact same logic
on hypothetical inputs without touching the database.
"""
from __future__ import annotations

from datetime import date
from fractions import Fraction
from typing import Any, Optional

from db import data_access as da
from tools._common import (applied_rule_entry, parse_as_of, pct_str, rule_check, rule_provenance,
                           select_rule, to_fraction)

P_ATTENDANCE = "min_attendance_pct"
P_SUPP_ATTEMPTS = "supp_max_attempts"
P_CGPA = "min_cgpa_placement"
P_BACKLOGS = "max_active_backlogs_placement"

_UNDETERMINED = "UNDETERMINED"


def _op_text(rule: dict[str, Any]) -> str:
    return f"{rule['operator']} {rule['value']}"


def _finish(result: str, reason: str, sel: dict[str, Any], **extra: Any) -> dict[str, Any]:
    rule = sel.get("rule")
    out: dict[str, Any] = {
        "status": "ok", "result": result, "reason": reason,
        "rule_id": rule["rule_id"] if rule else None,
        "threshold": _op_text(rule) if rule else None,
        "rule": rule_provenance(rule) if rule else None,
        "applied_rules": [applied_rule_entry(rule)] if rule else [],
        "rule_resolution": {"status": sel["status"], "resolver": sel["resolver"], "reason": sel["reason"],
                            "candidates": [c["rule_id"] for c in sel["candidates"]]},
        "upcoming_rules": sel["upcoming"], "skipped_rules": sel["skipped"],
    }
    if sel.get("conflicts_detected"):
        out["conflicts_detected"] = sel["conflicts_detected"]
    if sel["status"] == "rule_conflict":
        out["conflict_candidates"] = sel["candidates"]
    out.update(extra)
    return out


# ------------------------------------------------------------------------- exam (attendance)
def exam_eligibility_core(conn, student: dict[str, Any], course: dict[str, Any],
                          attendance: Optional[dict[str, Any]], as_of: date) -> dict[str, Any]:
    base = {"student_id": student["student_id"], "course_code": course["course_code"],
            "course_name": course["course_name"], "as_of_date": as_of.isoformat()}
    sel = select_rule(conn, student, P_ATTENDANCE, as_of)
    if course["programme"] != student["programme"]:
        return {**base, **_finish("NOT_APPLICABLE",
                f"{course['course_code']} belongs to {course['programme']}, not to the student's programme "
                f"({student['programme']}).", sel, attendance=None, actual_value=None)}
    if attendance is None:
        return {**base, **_finish(_UNDETERMINED,
                "No attendance record is stored for this student and course, so eligibility cannot be decided.",
                sel, attendance=None, actual_value=None)}
    exact = Fraction(attendance["classes_attended"] * 100, attendance["classes_held"])
    att = {"classes_held": attendance["classes_held"], "classes_attended": attendance["classes_attended"],
           "attendance_pct": pct_str(exact)}
    check = rule_check(sel, exact)
    if check["state"] == "undetermined":
        return {**base, **_finish(_UNDETERMINED, check["reason"], sel, attendance=att, actual_value=att["attendance_pct"])}
    rule = sel["rule"]
    verb = "meets" if check["state"] == "pass" else "does not meet"
    reason = (f"Attendance in {course['course_code']} is {att['attendance_pct']}% "
              f"({att['classes_attended']}/{att['classes_held']} classes); this {verb} the requirement "
              f"{_op_text(rule)}% in {rule['source_doc_id']} clause {rule['source_section']} (rule {rule['rule_id']}).")
    return {**base, **_finish("ELIGIBLE" if check["state"] == "pass" else "NOT_ELIGIBLE", reason, sel,
                              attendance=att, actual_value=att["attendance_pct"])}


def check_exam_eligibility(student_id: str, course_code: str, as_of_date: Any = None, *, db_path: Any = None) -> dict[str, Any]:
    """Eligible to appear in the end-semester exam for ``course_code`` on ``as_of_date``?"""
    as_of = parse_as_of(as_of_date)
    with da.db_session(db_path, read_only=True) as conn:
        student = da.require_student(conn, student_id)
        course = da.require_course(conn, course_code)
        att = da.get_attendance_record(conn, student_id, course["course_code"])
        return exam_eligibility_core(conn, student, course, att, as_of)


# ------------------------------------------------------------------------- supplementary exam
def supplementary_core(conn, student: dict[str, Any], course: dict[str, Any],
                       attempts: list[dict[str, Any]], as_of: date) -> dict[str, Any]:
    base = {"student_id": student["student_id"], "course_code": course["course_code"],
            "course_name": course["course_name"], "as_of_date": as_of.isoformat()}
    sel = select_rule(conn, student, P_SUPP_ATTEMPTS, as_of)
    if not attempts:
        return {**base, **_finish("NOT_ELIGIBLE", f"No exam attempt is recorded for {course['course_code']}, "
                                  "so there is nothing to take a supplementary for.", sel, attempts_so_far=0)}
    latest = attempts[-1]
    supp_so_far = sum(1 for a in attempts if a["exam_type"] == "SUPPLEMENTARY")
    summary = {"latest_result": latest["result"], "latest_session": latest["exam_session"],
               "supplementary_attempts_so_far": supp_so_far, "next_attempt_number": supp_so_far + 1}
    if latest["result"] == "PASS":
        return {**base, **_finish("NOT_ELIGIBLE", f"The latest attempt in {course['course_code']} "
                                  f"({latest['exam_session']}) is a PASS, so no supplementary is needed.", sel, **summary)}
    check = rule_check(sel, Fraction(supp_so_far + 1))
    if check["state"] == "undetermined":
        return {**base, **_finish(_UNDETERMINED, check["reason"], sel, **summary)}
    rule = sel["rule"]
    if check["state"] == "pass":
        reason = (f"The latest attempt in {course['course_code']} ({latest['exam_session']}) is {latest['result']}. "
                  f"The next supplementary would be attempt {supp_so_far + 1}, within the limit "
                  f"{_op_text(rule)} in {rule['source_doc_id']} clause {rule['source_section']} (rule {rule['rule_id']}).")
        return {**base, **_finish("ELIGIBLE", reason, sel, **summary)}
    reason = (f"The latest attempt is {latest['result']} but the next supplementary would be attempt "
              f"{supp_so_far + 1}, which exceeds the limit {_op_text(rule)} in {rule['source_doc_id']} "
              f"clause {rule['source_section']} (rule {rule['rule_id']}).")
    return {**base, **_finish("NOT_ELIGIBLE", reason, sel, **summary)}


def check_supplementary_eligibility(student_id: str, course_code: str, as_of_date: Any = None, *, db_path: Any = None) -> dict[str, Any]:
    """Eligible to apply for the supplementary exam in ``course_code``? Based on the latest attempt
    being FAIL/ABSENT/DETAINED and the attempt limit rule from rule_registry. No other policy is assumed."""
    as_of = parse_as_of(as_of_date)
    with da.db_session(db_path, read_only=True) as conn:
        student = da.require_student(conn, student_id)
        course = da.require_course(conn, course_code)
        attempts = da.get_results(conn, student_id, course["course_code"])
        return supplementary_core(conn, student, course, attempts, as_of)


# ------------------------------------------------------------------------- placement
def placement_core(conn, student: dict[str, Any], cgpa: Any, active_backlogs: int, as_of: date) -> dict[str, Any]:
    base = {"student_id": student["student_id"], "programme": student["programme"],
            "batch_year": student["batch_year"], "as_of_date": as_of.isoformat()}
    inputs = [("CGPA", P_CGPA, to_fraction(cgpa, "cgpa"), cgpa),
              ("Active backlogs", P_BACKLOGS, Fraction(int(active_backlogs)), int(active_backlogs))]
    criteria: list[dict[str, Any]] = []
    sels: list[dict[str, Any]] = []
    for label, param, exact, shown in inputs:
        sel = select_rule(conn, student, param, as_of)
        sels.append(sel)
        check = rule_check(sel, exact)
        entry: dict[str, Any] = {"criterion": label, "parameter": param, "actual_value": shown,
                                 "state": check["state"], "rule_id": sel["rule"]["rule_id"] if sel["rule"] else None,
                                 "threshold": _op_text(sel["rule"]) if sel["rule"] else None,
                                 "rule": rule_provenance(sel["rule"]) if sel["rule"] else None,
                                 "rule_resolution": {"status": sel["status"], "resolver": sel["resolver"],
                                                     "reason": sel["reason"]},
                                 "upcoming_rules": sel["upcoming"]}
        if check["state"] == "undetermined":
            entry["note"] = check["reason"]
        criteria.append(entry)
    states = [c["state"] for c in criteria]
    if "fail" in states:
        result = "NOT_ELIGIBLE"
    elif all(s == "pass" for s in states):
        result = "ELIGIBLE"
    else:
        result = _UNDETERMINED
    parts = []
    for c in criteria:
        if c["state"] == "undetermined":
            parts.append(f"{c['criterion']} {c['actual_value']}: not evaluated ({c['note']})")
        else:
            r = c["rule"]
            parts.append(f"{c['criterion']} {c['actual_value']} {'meets' if c['state'] == 'pass' else 'does not meet'} "
                         f"{c['threshold']} ({r['source_doc_id']} clause {r['source_section']}, rule {c['rule_id']})")
    applied = [applied_rule_entry(s["rule"]) for s in sels if s["rule"]]
    return {**base, "status": "ok", "result": result, "reason": "; ".join(parts) + ".",
            "criteria": criteria, "applied_rules": applied,
            "rule_id": ", ".join(a["rule_id"] for a in applied) or None,
            "upcoming_rules": [u for c in criteria for u in c["upcoming_rules"]],
            "rule_resolution": {"status": "ok" if all(s["status"] == "ok" for s in sels) else "incomplete",
                                "candidates": [c["rule_id"] for s in sels for c in s["candidates"]]}}


def check_placement_eligibility(student_id: str, as_of_date: Any = None, *, db_path: Any = None) -> dict[str, Any]:
    """Placement eligibility from the student's stored CGPA and active_backlogs against the
    placement rules in force on ``as_of_date``."""
    as_of = parse_as_of(as_of_date)
    with da.db_session(db_path, read_only=True) as conn:
        student = da.require_student(conn, student_id)
        out = placement_core(conn, student, student["cgpa"], student["active_backlogs"], as_of)
    out["inputs"] = {"cgpa": student["cgpa"], "active_backlogs": student["active_backlogs"]}
    return out
