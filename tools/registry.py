"""Tool registry and the AUTHORIZATION BOUNDARY between the LLM/graph and student data.

Member 1's graph (or the API) calls ``call_tool(name, arguments, authenticated_student_id=...)``.
* The student the tool acts on is ALWAYS ``authenticated_student_id`` (from the X-Student-Id
  header, validated against the students table by the API). A different ``student_id``
  inside ``arguments`` (e.g. the LLM copied an id from the question text) raises
  AccessDeniedError; it is never silently honoured.
* Only tools listed in TOOLS can be called; arguments are checked against each tool's
  declared parameters. There is no generic SQL tool.
* Tool-level problems (unknown student/course, bad dates...) are returned as
  {"status": "error", "error": {"code", "message"}}; they do not raise.

``tool_specs()`` returns OpenAI-style function schemas WITHOUT a student_id parameter, so
the model cannot even name a student.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from db import data_access as da
from tools import eligibility, student, what_if


class AccessDeniedError(Exception):
    """Raised when a call tries to act on a student other than the authenticated one."""


class UnknownToolError(Exception):
    pass


# name -> (callable, parameters, description). Parameters exclude student_id/db_path/as_of_date handling.
TOOLS: dict[str, dict[str, Any]] = {
    "get_student_profile": dict(fn=student.get_student_profile, params={}, takes_date=False,
        description="The logged-in student's programme, batch, semester, CGPA and stored active backlog count."),
    "get_attendance": dict(fn=student.get_attendance, params={"course_code": "string"}, takes_date=False,
        description="Classes held/attended and the computed attendance percentage for one course."),
    "get_results": dict(fn=student.get_results, params={"course_code": "string?"}, takes_date=False,
        description="The logged-in student's exam results, optionally for one course."),
    "get_exam_result": dict(fn=student.get_exam_result, params={"course_code": "string", "session": "string"}, takes_date=False,
        description="The result for one course in one exam session (for example 2026-MAY)."),
    "get_backlogs": dict(fn=student.get_backlogs, params={}, takes_date=False,
        description="Active backlog count and the backlog courses."),
    "check_exam_eligibility": dict(fn=eligibility.check_exam_eligibility, params={"course_code": "string"}, takes_date=True,
        description="Whether the student may appear in the end-semester exam for a course (attendance rule)."),
    "check_supplementary_eligibility": dict(fn=eligibility.check_supplementary_eligibility, params={"course_code": "string"}, takes_date=True,
        description="Whether the student may apply for the supplementary exam in a course."),
    "check_placement_eligibility": dict(fn=eligibility.check_placement_eligibility, params={}, takes_date=True,
        description="Placement eligibility (CGPA and backlog rules)."),
    "simulate_what_if": dict(fn=what_if.simulate_what_if, params={"scenario": "object", "check": "string?"}, takes_date=True,
        description="Hypothetical eligibility: pass_courses, cgpa or attendance assumptions. Never changes records."),
}

_JSON_TYPES = {"string": "string", "object": "object"}


def tool_specs() -> list[dict[str, Any]]:
    """OpenAI function-tool schemas for the graph. No student_id parameter is exposed."""
    specs = []
    for name, t in TOOLS.items():
        props, required = {}, []
        for pname, ptype in t["params"].items():
            optional = ptype.endswith("?")
            props[pname] = {"type": _JSON_TYPES[ptype.rstrip("?")]}
            if not optional:
                required.append(pname)
        props_all = dict(props)
        if t["takes_date"]:
            props_all["as_of_date"] = {"type": "string", "description": "YYYY-MM-DD; defaults to today"}
        specs.append({"type": "function", "name": name, "description": t["description"],
                      "parameters": {"type": "object", "properties": props_all, "required": required,
                                     "additionalProperties": False}})
    return specs


def call_tool(name: str, arguments: Optional[dict[str, Any]], *, authenticated_student_id: str,
              as_of_date: Any = None, db_path: Any = None) -> dict[str, Any]:
    """Run one tool for the authenticated student. Returns
    {"tool", "input", "output", "status", "ms"} (input never contains personal data beyond ids)."""
    tool = TOOLS.get(name)
    if tool is None:
        raise UnknownToolError(f"unknown tool '{name}'")
    args = dict(arguments or {})
    claimed = args.pop("student_id", None)
    if claimed is not None and str(claimed).strip().upper() != str(authenticated_student_id).strip().upper():
        raise AccessDeniedError("Tools may only act on the authenticated student")
    unexpected = set(args) - set(tool["params"]) - ({"as_of_date"} if tool["takes_date"] else set())
    clean_input = {k: v for k, v in args.items() if k in tool["params"]}
    started = time.perf_counter()
    try:
        if unexpected:
            raise da.InvalidInputError(f"unexpected argument(s): {sorted(unexpected)}")
        for pname, ptype in tool["params"].items():
            if not ptype.endswith("?") and pname not in args:
                raise da.InvalidInputError(f"missing required argument '{pname}'")
        call_kwargs: dict[str, Any] = {k: v for k, v in args.items() if k in tool["params"]}
        if tool["takes_date"]:
            call_kwargs["as_of_date"] = args.get("as_of_date", as_of_date)
        if db_path is not None:
            call_kwargs["db_path"] = db_path
        output = tool["fn"](authenticated_student_id, **call_kwargs)
        if isinstance(output, list):  # get_results returns a list; wrap for a uniform shape
            output = {"status": "ok", "student_id": authenticated_student_id, "results": output, "count": len(output)}
        status = output.get("status", "ok")
        if tool["takes_date"]:
            clean_input["as_of_date"] = da.coerce_date(call_kwargs["as_of_date"] or "", "as_of_date").isoformat() \
                if call_kwargs["as_of_date"] else None
    except da.DataAccessError as exc:
        output = {"status": "error", "error": {"code": exc.code, "message": exc.message}}
        status = "error"
    return {"tool": name, "input": {"student_id": authenticated_student_id, **clean_input}, "output": output,
            "status": status, "ms": int((time.perf_counter() - started) * 1000)}
