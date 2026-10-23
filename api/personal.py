"""Interim deterministic router for PERSONAL questions (Member 2).

Used by api/main.py only when Member 1's LangGraph workflow (graph.workflow.run_graph) is not
installed. It maps a first-person question onto the real tools in tools/registry.py (through
call_tool, i.e. through the authorization boundary) and builds the answer from the tools'
own validated output. No LLM is involved and nothing is invented: wording comes from the
tool "reason" strings, thresholds from rule_registry.

It returns None for anything that is not clearly a personal data/eligibility question
(policy facts, procedures, small talk) so those go to retrieval (Member 1) instead.
When Member 1's graph is available it replaces this module for all routing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from api.models import NOT_FOUND_MESSAGE
from db import data_access as da
from tools.registry import call_tool

_FIRST_PERSON = re.compile(r"\b(my|mine|me|i|i'm|i am|i've|i have|am i|can i|do i|will i|should i|would i)\b", re.I)
_PROCEDURAL = re.compile(r"\b(how (do|can|should|to|does)|what (is|are) the (process|procedure|steps?|rules?|minimum|maximum|"
                         r"requirements?|criteria|policy|deadline|fees?|last date)|when (is|are|do)|where (do|can|is))\b", re.I)
_HYPOTHETICAL = re.compile(r"\b(if i|what if|suppose|assuming|assume|would i|in case i)\b", re.I)

_PLACEMENT = re.compile(r"\b(placements?|campus (recruit|drive|hiring)|recruit(ment|ers?))\b", re.I)
_SUPPLEMENTARY = re.compile(r"\b(supplementary|supply exam|re-?exam|re-?appear)\b", re.I)
_ATTENDANCE = re.compile(r"\b(attendance|classes attended|attended)\b", re.I)
_ELIGIBLE = re.compile(r"\b(eligible|eligibility|allowed|permitted|appear|sit|write|barred|detained|debarred)\b", re.I)
_BACKLOG = re.compile(r"\b(backlogs?|arrears?)\b", re.I)
_RESULT = re.compile(r"\b(results?|marks|grades?|scores?|passed|failed|pass|fail)\b", re.I)
_PROFILE = re.compile(r"\b(cgpa|programme|program|semester|batch|profile)\b", re.I)
_OTHER_STUDENT_ID = re.compile(r"\bS\d{4}\b", re.I)
_OTHER_PERSON = re.compile(r"\b(another|other|someone else'?s?|somebody else'?s?|classmate'?s?|friend'?s?|roommate'?s?|"
                           r"batchmate'?s?|(his|her|their) )\b.{0,40}\b(attendance|marks|results?|cgpa|backlogs?|grades?|records?)\b", re.I)
_SESSION = re.compile(r"\b(\d{4})[-\s/]?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)
_STOP_WORDS = {"engineering"}
_CODE_LIKE = re.compile(r"\b[A-Za-z]{2,4}\d{3}\b")


@dataclass
class Outcome:
    answer_type: str
    answer: str
    explanation: str = ""
    category: str = "personal"
    tools: list[dict[str, Any]] = field(default_factory=list)
    applied_rules: list[dict[str, str]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    precedence_decision: Optional[str] = None
    sources: list[dict[str, Any]] = field(default_factory=list)


def mentions_other_student(question: str, own_id: Optional[str]) -> bool:
    """True when the text asks for someone else's data (another S-number, or 'a classmate's marks')."""
    for found in _OTHER_STUDENT_ID.findall(question):
        if own_id is None or found.upper() != own_id.upper():
            return True
    return bool(_OTHER_PERSON.search(question))


def is_personal(question: str) -> bool:
    if not _FIRST_PERSON.search(question) or _PROCEDURAL.search(question):
        return False
    return bool(_ATTENDANCE.search(question) or _ELIGIBLE.search(question) or _BACKLOG.search(question)
                or _RESULT.search(question) or _PROFILE.search(question) or _PLACEMENT.search(question)
                or _SUPPLEMENTARY.search(question))


def _find_courses(question: str, db_path: Any, programme: Optional[str]) -> list[dict[str, Any]]:
    with da.db_session(db_path, read_only=True) as conn:
        courses = da.list_courses(conn)
    q_low = question.lower()
    q_words = set(re.findall(r"[a-z0-9]+", q_low))
    by_code = [c for c in courses if re.search(rf"\b{re.escape(c['course_code'].lower())}\b", q_low)]
    if by_code:
        return by_code
    hits = []
    for c in courses:
        name = c["course_name"].lower()
        if name in q_low:
            hits.append((2, c))
            continue
        words = {w for w in re.findall(r"[a-z]+", name) if len(w) >= 4 and w not in _STOP_WORDS}
        if words & q_words:
            hits.append((1, c))
    if programme:  # prefer the student's own programme when names overlap across programmes
        own = [(s, c) for s, c in hits if c["programme"] == programme]
        hits = own or hits
    best = max((s for s, _ in hits), default=0)
    return [c for s, c in hits if s == best]


def _citations_from_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out, seen = [], set()
    for r in rules:
        if not r:
            continue
        key = (r["source_doc_id"], r["source_section"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"doc_id": r["source_doc_id"], "title": r.get("source_title") or r["source_doc_id"],
                    "section": r["source_section"], "page": None, "version": r.get("source_version"),
                    "effective_from": r.get("source_effective_from")})
    return out


def _sources_from_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"doc_id": r["source_doc_id"], "section": r["source_section"], "score": None}
            for r in rules if r]


def _collect_rules(output: dict[str, Any]) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for key in ("rule",):
        if output.get(key):
            rules.append(output[key])
    for crit in output.get("criteria", []) or []:
        if crit.get("rule"):
            rules.append(crit["rule"])
    for key in ("simulated", "baseline"):
        if isinstance(output.get(key), dict):
            rules.extend(_collect_rules(output[key]))
    rules.extend(output.get("conflict_candidates", []) or [])
    uniq, seen = [], set()
    for r in rules:
        if r["rule_id"] not in seen:
            seen.add(r["rule_id"])
            uniq.append(r)
    return uniq


def _not_found(explanation: str, category: str, tools: list[dict[str, Any]]) -> Outcome:
    return Outcome("not_found", NOT_FOUND_MESSAGE, explanation, category, tools)


def _clarify(text: str, category: str) -> Outcome:
    return Outcome("clarification_needed", text, "", category)


def _wrap(category: str, call: dict[str, Any], answer: str, explanation: str = "") -> Outcome:
    out = call["output"]
    rules = _collect_rules(out)
    res = out.get("rule_resolution", {}) or {}
    return Outcome("calculated", answer, explanation or answer, category, [call],
                   applied_rules=out.get("applied_rules") or [], citations=_citations_from_rules(rules),
                   precedence_decision=res.get("reason"), sources=_sources_from_rules(rules))


def _conflict_outcome(category: str, call: dict[str, Any]) -> Outcome:
    out = call["output"]
    cands = out.get("conflict_candidates") or []
    listing = "; ".join(f"{c['rule_id']} ({c['source_doc_id']} clause {c['source_section']}: {c['operator']} {c['value']})"
                        for c in cands)
    text = ("Two or more university rules apply to you on this date and the precedence policy cannot choose between "
            f"them: {listing}. Please contact the issuing office to confirm which applies.")
    return Outcome("conflict_flagged", text, out.get("reason", ""), category, [call],
                   citations=_citations_from_rules(cands), conflicts=[{"candidates": [c["rule_id"] for c in cands],
                                                                       "reason": (out.get("rule_resolution") or {}).get("reason")}],
                   precedence_decision=(out.get("rule_resolution") or {}).get("reason"),
                   sources=_sources_from_rules(cands))


def _eligibility_outcome(category: str, call: dict[str, Any]) -> Outcome:
    out = call["output"]
    if call["status"] == "error":
        return _not_found(out["error"]["message"], category, [call])
    if (out.get("rule_resolution") or {}).get("status") == "rule_conflict" or \
            any((c.get("rule_resolution") or {}).get("status") == "rule_conflict" for c in out.get("criteria", []) or []):
        return _conflict_outcome(category, call)
    result = out["result"]
    lead = {"ELIGIBLE": "Yes, you are eligible.", "NOT_ELIGIBLE": "No, you are not eligible.",
            "UNDETERMINED": "I cannot decide this from the available records.",
            "NOT_APPLICABLE": "This does not apply to you."}[result]
    upcoming = out.get("upcoming_rules") or []
    extra = ""
    if upcoming:
        extra = " Upcoming change: " + "; ".join(
            f"{u['rule_id']} ({u['operator']} {u['value']}) takes effect {u['rule_effective_from']}" for u in upcoming) + "."
    return _wrap(category, call, f"{lead} {out['reason']}{extra}", out["reason"])


def answer_personal(question: str, student_id: str, as_of_date: Any, db_path: Any = None) -> Optional[Outcome]:
    """Answer a personal question with real tools, or return None if it is not one."""
    if not is_personal(question):
        return None
    with da.db_session(db_path, read_only=True) as conn:
        me = da.require_student(conn, student_id)
    courses = _find_courses(question, db_path, me["programme"])
    if not courses:  # a course-code-looking token that matches nothing must not fall back to "all courses"
        unknown = [t for t in _CODE_LIKE.findall(question)]
        if unknown:
            return _not_found(f"No course with code {unknown[0].upper()} exists.", "personal_unknown_course", [])

    def run(name: str, args: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return call_tool(name, args or {}, authenticated_student_id=student_id, as_of_date=as_of_date, db_path=db_path)

    def need_one_course(category: str, what: str) -> Optional[Outcome]:
        if len(courses) == 1:
            return None
        if not courses:
            return _clarify(f"Which course do you mean? Please give the course name or code (for example CS201) for {what}.", category)
        names = ", ".join(f"{c['course_code']} ({c['course_name']})" for c in courses)
        return _clarify(f"Your question matches more than one course: {names}. Which one do you mean?", category)

    if _PLACEMENT.search(question):
        if _HYPOTHETICAL.search(question) or _SUPPLEMENTARY.search(question):
            scenario: dict[str, Any] = {"pass_courses": [c["course_code"] for c in courses]}
            if not courses:
                return _clarify("Which course would you pass in this scenario? Please give its name or code.", "personal_what_if")
            call = run("simulate_what_if", {"scenario": scenario, "check": "placement"})
            if call["status"] == "error":
                return _not_found(call["output"]["error"]["message"], "personal_what_if", [call])
            out = call["output"]
            text = (f"Today: {out['baseline_result'].replace('_', ' ').lower()}. "
                    f"Assumptions: {' '.join(out['assumptions'])} "
                    f"Under that scenario: {out['simulated_result'].replace('_', ' ').lower()}. "
                    f"{out['simulated']['reason']} This is a simulation; your official records are unchanged.")
            return _wrap("personal_what_if", call, text, out["simulated"]["reason"])
        return _eligibility_outcome("personal_placement_eligibility", run("check_placement_eligibility"))

    if _SUPPLEMENTARY.search(question) and (_ELIGIBLE.search(question) or _HYPOTHETICAL.search(question)):
        ask = need_one_course("personal_supplementary_eligibility", "the supplementary exam")
        if ask:
            return ask
        return _eligibility_outcome("personal_supplementary_eligibility",
                                    run("check_supplementary_eligibility", {"course_code": courses[0]["course_code"]}))

    if _ATTENDANCE.search(question) and _ELIGIBLE.search(question):
        ask = need_one_course("personal_exam_eligibility", "exam eligibility")
        if ask:
            return ask
        return _eligibility_outcome("personal_exam_eligibility",
                                    run("check_exam_eligibility", {"course_code": courses[0]["course_code"]}))

    if _ELIGIBLE.search(question) and re.search(r"\bexam", question, re.I):
        ask = need_one_course("personal_exam_eligibility", "exam eligibility")
        if ask:
            return ask
        return _eligibility_outcome("personal_exam_eligibility",
                                    run("check_exam_eligibility", {"course_code": courses[0]["course_code"]}))

    if _ATTENDANCE.search(question):
        ask = need_one_course("personal_attendance", "your attendance")
        if ask:
            return ask
        call = run("get_attendance", {"course_code": courses[0]["course_code"]})
        out = call["output"]
        if call["status"] == "error":
            return _not_found(out["error"]["message"], "personal_attendance", [call])
        if out["status"] == "no_record":
            return _not_found(out["message"], "personal_attendance", [call])
        text = (f"Your attendance in {out['course_name']} ({out['course_code']}) is {out['attendance_pct']}% "
                f"({out['classes_attended']} of {out['classes_held']} classes attended).")
        return Outcome("calculated", text, text, "personal_attendance", [call])

    if _BACKLOG.search(question):
        call = run("get_backlogs")
        out = call["output"]
        names = ", ".join(f"{c['course_name']} ({c['course_code']}, {c['latest_result']} in {c['latest_session']})"
                          for c in out["courses"])
        text = f"You have {out['active_backlogs']} active backlog(s)." + (f" Backlog courses from your results: {names}." if names else "")
        if not out["consistent"]:
            text += (f" Note: your stored backlog count ({out['active_backlogs']}) differs from the "
                     f"{out['derived_count']} derived from your results; the stored figure is the official one.")
        return Outcome("calculated", text, text, "personal_backlogs", [call])

    if _RESULT.search(question):
        session = _SESSION.search(question)
        if len(courses) == 1 and session:
            sess = f"{session.group(1)}-{session.group(2)[:3].upper()}"
            call = run("get_exam_result", {"course_code": courses[0]["course_code"], "session": sess})
            out = call["output"]
            if call["status"] == "error" or out["status"] == "no_record":
                return _not_found((out.get("error") or {}).get("message") or out.get("message", ""), "personal_results", [call])
            rows = out["results"]
        else:
            call = run("get_results", {"course_code": courses[0]["course_code"]} if len(courses) == 1 else {})
            out = call["output"]
            if call["status"] == "error":
                return _not_found(out["error"]["message"], "personal_results", [call])
            rows = out["results"]
            if not rows:
                return _not_found("No results are stored for you.", "personal_results", [call])
        lines = [f"{r['course_name']} ({r['course_code']}), {r['exam_session']} {r['exam_type'].lower()}: "
                 f"{r['total_marks']}/{r['max_marks']} - {r['result']}" for r in rows]
        text = "Your results: " + "; ".join(lines) + "."
        return Outcome("calculated", text, text, "personal_results", [call])

    if _PROFILE.search(question):
        call = run("get_student_profile")
        out = call["output"]
        text = (f"You are in {out['programme']}, batch {out['batch_year']}, semester {out['current_semester']}, "
                f"with a CGPA of {out['cgpa']} and {out['active_backlogs']} active backlog(s).")
        return Outcome("calculated", text, text, "personal_profile", [call])
    return None
