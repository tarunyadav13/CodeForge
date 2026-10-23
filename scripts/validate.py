"""Validate the student database (schema constraints, logic, rules, required edge cases).

    python scripts/validate.py --db data/student_services.db
    python scripts/validate.py --dir data/synthetic            # loads into a temp DB first
    python scripts/validate.py --db data/student_services.db --allow-reserved   # judge data

Severity
  ERROR  a requirement or logical constraint is violated -> exit code 1
  WARN   suspicious but not forbidden (e.g. stored backlog count differs from results)
Exit code: 0 = no errors, 1 = errors found, 2 = could not run (bad path, unreadable DB).

Thresholds used by the edge-case checks come from rule_registry (as of --as-of-date),
never from constants here. Reserved judge IDs (S9000-S9999, JDG*) are errors in OUR data
and allowed with --allow-reserved (judges load their own data in that range).
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import data_access as da  # noqa: E402

MIN_STUDENTS, MIN_PROGRAMMES, MIN_BATCHES, MIN_COURSES = 30, 2, 2, 6
RESERVED_STUDENT = re.compile(r"^S9\d{3}$")
DEFAULT_AS_OF = "2026-10-06"
NUMERIC_PARAMS = {"min_attendance_pct", "supp_max_attempts", "pass_marks_pct",
                  "min_cgpa_placement", "max_active_backlogs_placement"}


@dataclass
class Finding:
    severity: str  # ERROR | WARN
    check: str
    message: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)
    counts: dict[str, Any] = field(default_factory=dict)

    def add(self, severity: str, check: str, message: str) -> None:
        self.findings.append(Finding(severity, check, message))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "ERROR"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "WARN"]

    @property
    def ok(self) -> bool:
        return not self.errors


def _backlog_count(conn: sqlite3.Connection, student_id: str) -> Optional[int]:
    """Backlogs derived from results, or None when the id is malformed (reported elsewhere)."""
    try:
        return len(da.get_backlog_courses(conn, student_id))
    except da.InvalidInputError:
        return None


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def _threshold(conn: sqlite3.Connection, parameter: str, programme: str, batch: int, as_of: str) -> Optional[float]:
    rules = da.get_applicable_rules(conn, programme, batch, as_of, parameter)
    if not rules:
        return None
    try:
        return float(rules[0]["value"])  # newest effective_from first
    except ValueError:
        return None


def validate_connection(conn: sqlite3.Connection, *, as_of_date: str = DEFAULT_AS_OF,
                        allow_reserved: bool = False) -> Report:
    rep = Report()
    c = rep.checks_run

    students = _rows(conn, "SELECT * FROM students")
    courses = _rows(conn, "SELECT * FROM courses")
    attendance = _rows(conn, "SELECT * FROM attendance")
    results = _rows(conn, "SELECT * FROM results")
    rules = _rows(conn, "SELECT * FROM rule_registry")
    sources = {r["doc_id"] for r in _rows(conn, "SELECT doc_id FROM source_register")}
    by_id = {s["student_id"]: s for s in students}
    course_by_code = {r["course_code"].casefold(): r for r in courses}

    # 1. minimum dataset size ---------------------------------------------------------
    c.append("dataset size (>=30 students, >=2 programmes, >=2 batches, >=6 courses)")
    progs = {s["programme"] for s in students}
    batches = {s["batch_year"] for s in students}
    rep.counts = {"students": len(students), "programmes": len(progs), "batches": len(batches),
                  "courses": len(courses), "attendance_rows": len(attendance),
                  "result_rows": len(results), "rules": len(rules), "sources": len(sources)}
    for label, have, need in (("students", len(students), MIN_STUDENTS), ("programmes", len(progs), MIN_PROGRAMMES),
                              ("batches", len(batches), MIN_BATCHES), ("courses", len(courses), MIN_COURSES)):
        if have < need:
            rep.add("ERROR", "dataset size", f"only {have} {label}, need at least {need}")

    # 2. identifiers --------------------------------------------------------------------
    c.append("identifiers (format, duplicates, reserved judge ranges)")
    seen_codes: dict[str, str] = {}
    for r in courses:
        key = r["course_code"].casefold()
        if key in seen_codes:
            rep.add("ERROR", "identifiers", f"duplicate course code (case-insensitive): {r['course_code']} / {seen_codes[key]}")
        seen_codes[key] = r["course_code"]
        if not allow_reserved and r["course_code"].upper().startswith("JDG"):
            rep.add("ERROR", "identifiers", f"course {r['course_code']} uses the judge-reserved JDG prefix")
    for s in students:
        if not re.fullmatch(r"S\d{4}", s["student_id"]):
            rep.add("ERROR", "identifiers", f"student_id {s['student_id']!r} is not S + 4 digits")
        if not allow_reserved and RESERVED_STUDENT.match(s["student_id"]):
            rep.add("ERROR", "identifiers", f"student_id {s['student_id']} is in the judge-reserved range S9000-S9999")

    # 3. students ----------------------------------------------------------------------
    c.append("student values (semester 1-10, CGPA 0-10, backlogs >= 0)")
    for s in students:
        if not 1 <= s["current_semester"] <= 10:
            rep.add("ERROR", "student values", f"{s['student_id']}: current_semester {s['current_semester']} outside 1-10")
        if not 0 <= s["cgpa"] <= 10:
            rep.add("ERROR", "student values", f"{s['student_id']}: cgpa {s['cgpa']} outside 0-10")
        if s["active_backlogs"] < 0:
            rep.add("ERROR", "student values", f"{s['student_id']}: negative active_backlogs")

    # 4. attendance ----------------------------------------------------------------------
    c.append("attendance (held > 0, 0 <= attended <= held, valid references, programme match)")
    for a in attendance:
        sid, code = a["student_id"], a["course_code"]
        if a["classes_held"] <= 0:
            rep.add("ERROR", "attendance", f"{sid}/{code}: classes_held must be > 0")
        if not 0 <= a["classes_attended"] <= a["classes_held"]:
            rep.add("ERROR", "attendance", f"{sid}/{code}: classes_attended {a['classes_attended']} outside 0..{a['classes_held']}")
        if sid not in by_id:
            rep.add("ERROR", "attendance", f"{sid}/{code}: unknown student")
        course = course_by_code.get(code.casefold())
        if course is None:
            rep.add("ERROR", "attendance", f"{sid}/{code}: unknown course")
        elif sid in by_id and course["programme"] != by_id[sid]["programme"]:
            rep.add("ERROR", "attendance", f"{sid}/{code}: course belongs to {course['programme']}, student is in {by_id[sid]['programme']}")

    # 5. results ---------------------------------------------------------------------------
    c.append("results (marks arithmetic, ranges, status consistency, valid references)")
    for r in results:
        tag = f"{r['student_id']}/{r['course_code']}/{r['exam_session']}/{r['exam_type']}"
        if r["total_marks"] != r["internal_marks"] + r["external_marks"]:
            rep.add("ERROR", "results", f"{tag}: total_marks {r['total_marks']} != internal+external "
                                        f"{r['internal_marks'] + r['external_marks']}")
        if not 0 <= r["total_marks"] <= r["max_marks"]:
            rep.add("ERROR", "results", f"{tag}: total_marks {r['total_marks']} outside 0..{r['max_marks']}")
        if r["student_id"] not in by_id:
            rep.add("ERROR", "results", f"{tag}: unknown student")
            continue
        if r["course_code"].casefold() not in course_by_code:
            rep.add("ERROR", "results", f"{tag}: unknown course")
        st = by_id[r["student_id"]]
        pass_pct = _threshold(conn, "pass_marks_pct", st["programme"], st["batch_year"], as_of_date)
        if r["result"] in ("ABSENT", "DETAINED") and r["total_marks"] != 0:
            rep.add("ERROR", "results", f"{tag}: {r['result']} must have 0 marks, has {r['total_marks']}")
        elif pass_pct is not None and r["result"] in ("PASS", "FAIL"):
            passed = r["total_marks"] * 100 >= pass_pct * r["max_marks"]
            if passed and r["result"] == "FAIL":
                rep.add("ERROR", "results", f"{tag}: FAIL but {r['total_marks']}/{r['max_marks']} meets the {pass_pct:g}% pass mark")
            if not passed and r["result"] == "PASS":
                rep.add("ERROR", "results", f"{tag}: PASS but {r['total_marks']}/{r['max_marks']} is below the {pass_pct:g}% pass mark")
        elif pass_pct is None and r["result"] in ("PASS", "FAIL"):
            rep.add("WARN", "results", f"{tag}: no applicable pass_marks_pct rule, PASS/FAIL consistency not checked")

    # 6. backlog consistency ------------------------------------------------------------------
    c.append("stored active_backlogs vs backlogs derived from results")
    for s in students:
        derived = _backlog_count(conn, s["student_id"])
        if derived is not None and derived != s["active_backlogs"]:
            rep.add("WARN", "backlogs", f"{s['student_id']}: stored active_backlogs={s['active_backlogs']} but results show {derived}")

    # 7. rule registry -----------------------------------------------------------------------------
    c.append("rule_registry (dates, operator, scope, numeric value, source reference)")
    if not rules:
        rep.add("ERROR", "rule_registry", "no rules loaded; eligibility tools have nothing to apply")
    for r in rules:
        rid = r["rule_id"]
        try:
            f = date.fromisoformat(r["effective_from"])
            if r["effective_to"] and date.fromisoformat(r["effective_to"]) < f:
                rep.add("ERROR", "rule_registry", f"{rid}: effective_to is before effective_from")
        except ValueError:
            rep.add("ERROR", "rule_registry", f"{rid}: invalid date")
        if r["source_doc_id"] not in sources:
            rep.add("ERROR", "rule_registry", f"{rid}: source_doc_id {r['source_doc_id']!r} is not in source_register")
        if not (r["source_section"] or "").strip():
            rep.add("ERROR", "rule_registry", f"{rid}: no source_section (every threshold must trace to a clause)")
        try:
            da.parse_batch_scope(r["scope_batches"])
            da.programme_in_scope("x", r["scope_programmes"])
        except da.InvalidScopeError as exc:
            rep.add("ERROR", "rule_registry", f"{rid}: {exc.message}")
        if r["parameter"] in NUMERIC_PARAMS:
            try:
                float(r["value"])
            except ValueError:
                rep.add("ERROR", "rule_registry", f"{rid}: value {r['value']!r} is not numeric for {r['parameter']}")
    by_param: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rules:
        by_param[r["parameter"]].append(r)
    for param, group in by_param.items():
        for i, x in enumerate(group):
            for y in group[i + 1:]:
                if x["scope_programmes"] == y["scope_programmes"] and x["scope_batches"] == y["scope_batches"]:
                    x_end, y_end = x["effective_to"] or "9999-12-31", y["effective_to"] or "9999-12-31"
                    if x["effective_from"] <= y_end and y["effective_from"] <= x_end:
                        rep.add("WARN", "rule_registry", f"{x['rule_id']} and {y['rule_id']} overlap in time for {param} "
                                                         f"(same scope): resolver must pick one")

    # 8. database integrity -------------------------------------------------------------------------------
    c.append("foreign keys and SQLite integrity")
    for row in conn.execute("PRAGMA foreign_key_check").fetchall():
        rep.add("ERROR", "foreign keys", f"row in {row[0]} (rowid {row[1]}) violates a foreign key to {row[2]}")
    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        rep.add("ERROR", "integrity", f"PRAGMA integrity_check: {result}")

    # 9. required edge cases ----------------------------------------------------------------------------------
    c.append("required edge cases present (thresholds read from rule_registry)")
    _edge_cases(conn, rep, students, attendance, results, as_of_date)
    return rep


def _edge_cases(conn, rep: Report, students, attendance, results, as_of: str) -> None:
    by_id = {s["student_id"]: s for s in students}
    found: dict[str, list[str]] = defaultdict(list)
    att_thresholds: set[float] = set()
    for a in attendance:
        s = by_id.get(a["student_id"])
        if not s:
            continue
        thr = _threshold(conn, "min_attendance_pct", s["programme"], s["batch_year"], as_of)
        if thr is None:
            continue
        att_thresholds.add(thr)
        held, att = a["classes_held"], a["classes_attended"]
        if att * 100 == thr * held:
            found["attendance exactly at threshold"].append(a["student_id"])
        # one class below: (attended+1) reaches the threshold but attended does not
        if att * 100 < thr * held <= (att + 1) * 100:
            found["attendance one class below threshold"].append(a["student_id"])
    if not att_thresholds:
        rep.add("WARN", "edge cases", "no applicable min_attendance_pct rule, attendance boundary cases not checked")

    cutoffs = set()
    for s in students:
        cut = _threshold(conn, "min_cgpa_placement", s["programme"], s["batch_year"], as_of)
        if cut is not None:
            cutoffs.add(cut)
            if abs(s["cgpa"] - cut) < 1e-9:
                found["CGPA exactly at placement cut-off"].append(s["student_id"])
    if not cutoffs:
        rep.add("WARN", "edge cases", "no min_cgpa_placement rule, CGPA cut-off case not checked (brief: only if documents have one)")

    for r in results:
        s = by_id.get(r["student_id"])
        pass_pct = _threshold(conn, "pass_marks_pct", s["programme"], s["batch_year"], as_of) if s else None
        if r["result"] == "FAIL" and pass_pct is not None and r["total_marks"] * 100 < pass_pct * r["max_marks"] \
                and (r["total_marks"] + 1) * 100 >= pass_pct * r["max_marks"]:
            found["failed course just below pass mark"].append(r["student_id"])
        if r["result"] == "ABSENT":
            found["ABSENT result"].append(r["student_id"])
        if r["result"] == "DETAINED":
            found["DETAINED result"].append(r["student_id"])
    for s in students:
        if (_backlog_count(conn, s["student_id"]) or 0) >= 2:
            found["multiple backlogs (>=2)"].append(s["student_id"])
    have_results = {r["student_id"] for r in results}
    have_att = {a["student_id"] for a in attendance}
    for s in students:
        if s["student_id"] not in have_results:
            found["student with no result records"].append(s["student_id"])
        if s["student_id"] not in have_att:
            found["student with no attendance records"].append(s["student_id"])

    required = ["attendance exactly at threshold", "attendance one class below threshold",
                "failed course just below pass mark", "ABSENT result", "DETAINED result",
                "multiple backlogs (>=2)"]
    if cutoffs:
        required.append("CGPA exactly at placement cut-off")
    rep.counts["edge_cases"] = {k: sorted(set(v)) for k, v in sorted(found.items())}
    for name in required:
        if not found.get(name):
            rep.add("ERROR", "edge cases", f"required edge case missing: {name}")
    for name in ("student with no result records", "student with no attendance records"):
        if not found.get(name):
            rep.add("WARN", "edge cases", f"no example of: {name}")


def format_report(rep: Report, target: str) -> str:
    lines = [f"Validation of {target}", "-" * 60]
    for k, v in rep.counts.items():
        if k != "edge_cases":
            lines.append(f"  {k:<16} {v}")
    lines.append("Checks run:")
    for name in rep.checks_run:
        lines.append(f"  - {name}")
    if rep.counts.get("edge_cases"):
        lines.append("Edge cases found (student IDs):")
        for k, v in rep.counts["edge_cases"].items():
            lines.append(f"  - {k}: {', '.join(v[:8])}{' ...' if len(v) > 8 else ''}")
    for sev in ("ERROR", "WARN"):
        items = [f for f in rep.findings if f.severity == sev]
        if items:
            lines.append(f"{sev}S ({len(items)}):")
            for f in items[:50]:
                lines.append(f"  [{f.check}] {f.message}")
            if len(items) > 50:
                lines.append(f"  ... {len(items) - 50} more")
    lines.append("-" * 60)
    lines.append(f"RESULT: {'PASSED' if rep.ok else 'FAILED'} - {len(rep.errors)} error(s), {len(rep.warnings)} warning(s)")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Validate the student database.")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--db", help="database path (default: $STUDENT_DB_PATH or data/student_services.db)")
    src.add_argument("--dir", help="validate a CSV directory (loaded into a temporary database first)")
    p.add_argument("--as-of-date", default=DEFAULT_AS_OF, help="date used to pick applicable rules (YYYY-MM-DD)")
    p.add_argument("--allow-reserved", action="store_true", help="permit S9000-S9999 and JDG* (judge data)")
    args = p.parse_args(argv)
    try:
        date.fromisoformat(args.as_of_date)
    except ValueError:
        print("ERROR: --as-of-date must be YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        if args.dir:
            from scripts.load_students import format_report as fmt_load, load_directory
            with tempfile.TemporaryDirectory() as tmp:
                load = load_directory(args.dir, Path(tmp) / "validate.db")
                if load.files_found == 0:
                    print(f"ERROR: no recognised CSV files in {args.dir}", file=sys.stderr)
                    return 2
                if load.rows_rejected:
                    print(fmt_load(load))
                conn = da.connect(Path(tmp) / "validate.db")
                try:
                    rep = validate_connection(conn, as_of_date=args.as_of_date, allow_reserved=args.allow_reserved)
                finally:
                    conn.close()
                for t in load.tables:  # rows the loader refused are validation errors too
                    for line, reason in t.rejected:
                        rep.add("ERROR", "load", f"{t.table}.csv line {line}: {reason}")
                    if t.file_error:
                        rep.add("ERROR", "load", f"{t.table}.csv: {t.file_error}")
            target = args.dir
        else:
            path = da.resolve_db_path(args.db)
            conn = da.connect(path, read_only=True)
            try:
                rep = validate_connection(conn, as_of_date=args.as_of_date, allow_reserved=args.allow_reserved)
            finally:
                conn.close()
            target = str(path)
    except (da.DataAccessError, sqlite3.Error, OSError) as exc:
        print(f"ERROR: cannot validate: {exc}", file=sys.stderr)
        return 2
    print(format_report(rep, target))
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
