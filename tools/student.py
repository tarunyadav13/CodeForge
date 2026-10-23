"""Student-record lookup tools (read-only, deterministic).

Error model (documented contract)
  * Invalid input / unknown student / unknown course  -> raises a db.data_access.DataAccessError
    subclass (InvalidInputError, StudentNotFoundError, CourseNotFoundError). tools.registry.call_tool
    converts these into {"status": "error", "error": {"code", "message"}} for the graph/API.
  * A valid student+course with no stored record       -> returns a dict with status "no_record".
Callers must pass the AUTHENTICATED student id; authorization lives in tools.registry.call_tool.
"""
from __future__ import annotations

from typing import Any, Optional

from db import data_access as da
from tools._common import pct_str, to_fraction  # noqa: F401  (to_fraction re-exported for tests)
from fractions import Fraction


def _attendance_fraction(held: int, attended: int) -> Fraction:
    return Fraction(attended * 100, held)


def get_student_profile(student_id: str, *, db_path: Any = None) -> dict[str, Any]:
    with da.db_session(db_path, read_only=True) as conn:
        s = da.require_student(conn, student_id)
    return {"status": "ok", "student_id": s["student_id"], "full_name": s["full_name"],
            "programme": s["programme"], "batch_year": s["batch_year"],
            "current_semester": s["current_semester"], "cgpa": s["cgpa"],
            "active_backlogs": s["active_backlogs"]}


def get_attendance(student_id: str, course_code: str, *, db_path: Any = None) -> dict[str, Any]:
    """{status, classes_held, classes_attended, attendance_pct, ...}. The percentage is computed
    here on every call and is never stored."""
    with da.db_session(db_path, read_only=True) as conn:
        da.require_student(conn, student_id)
        course = da.require_course(conn, course_code)
        rec = da.get_attendance_record(conn, student_id, course["course_code"])
    base = {"student_id": student_id, "course_code": course["course_code"], "course_name": course["course_name"]}
    if rec is None:
        return {**base, "status": "no_record", "classes_held": None, "classes_attended": None,
                "attendance_pct": None,
                "message": "No attendance record is stored for this student and course."}
    exact = _attendance_fraction(rec["classes_held"], rec["classes_attended"])
    return {**base, "status": "ok", "classes_held": rec["classes_held"],
            "classes_attended": rec["classes_attended"],
            "classes_missed": rec["classes_held"] - rec["classes_attended"],
            "attendance_pct": pct_str(exact), "attendance_pct_exact": f"{exact.numerator}/{exact.denominator}"}


def get_results(student_id: str, course_code: Optional[str] = None, *, db_path: Any = None) -> list[dict[str, Any]]:
    """All result rows (oldest first), optionally for one course. Empty list = none stored."""
    with da.db_session(db_path, read_only=True) as conn:
        da.require_student(conn, student_id)
        if course_code is not None:
            course_code = da.require_course(conn, course_code)["course_code"]
        rows = da.get_results(conn, student_id, course_code)
    return [{k: v for k, v in r.items()} for r in rows]


def get_exam_result(student_id: str, course_code: str, session: str, *, db_path: Any = None) -> dict[str, Any]:
    with da.db_session(db_path, read_only=True) as conn:
        da.require_student(conn, student_id)
        course = da.require_course(conn, course_code)
        rows = da.get_results(conn, student_id, course["course_code"], session)
    if not rows:
        return {"status": "no_record", "student_id": student_id, "course_code": course["course_code"],
                "exam_session": session, "results": [],
                "message": "No result is stored for this student, course and session."}
    return {"status": "ok", "student_id": student_id, "course_code": course["course_code"],
            "exam_session": rows[0]["exam_session"], "results": rows}


def get_backlogs(student_id: str, *, db_path: Any = None) -> dict[str, Any]:
    """Active backlogs. ``active_backlogs`` is the stored Annex C value (the official figure);
    ``courses`` and ``derived_count`` come from the results table, and ``consistent`` says
    whether the two agree."""
    with da.db_session(db_path, read_only=True) as conn:
        s = da.require_student(conn, student_id)
        courses = da.get_backlog_courses(conn, student_id)
    return {"status": "ok", "student_id": student_id, "active_backlogs": s["active_backlogs"],
            "derived_count": len(courses), "consistent": s["active_backlogs"] == len(courses),
            "courses": courses}
