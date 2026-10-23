"""SQLite data-access layer (Member 2).

Rules this module enforces
--------------------------
* Every statement is parameterised. User-supplied text is never concatenated
  into SQL (table/column names are fixed literals in this file).
* Foreign keys are switched on for every connection.
* Tools open READ-ONLY connections (``connect(read_only=True)``), so a tool
  or a what-if simulation physically cannot modify official records.
* Audit rows never hold raw question text, and ``full_name`` is stripped from
  logged tool outputs.

Database location: ``STUDENT_DB_PATH`` environment variable, else
``<repo>/data/student_services.db``.

Initialise (repeatable and safe)::

    python -m db.data_access --init            # PowerShell: same command
    python -m db.data_access --init --db C:\\tmp\\test.db
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Union

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DB_ENV_VAR = "STUDENT_DB_PATH"
DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "student_services.db"

STUDENT_ID_RE = re.compile(r"^S\d{4}$")
COURSE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{1,19}$")
DOC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._#\-]{0,63}$")

BACKLOG_RESULTS = ("FAIL", "ABSENT", "DETAINED")

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


# --------------------------------------------------------------------------
# Errors (controlled, documented). ``code`` is stable and machine-readable.
# --------------------------------------------------------------------------
class DataAccessError(Exception):
    code = "data_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class InvalidInputError(DataAccessError):
    code = "invalid_input"


class StudentNotFoundError(DataAccessError):
    code = "student_not_found"


class CourseNotFoundError(DataAccessError):
    code = "course_not_found"


class DatabaseUnavailableError(DataAccessError):
    code = "database_unavailable"


class InvalidScopeError(DataAccessError):
    code = "invalid_scope"


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------
def resolve_db_path(db_path: Union[str, os.PathLike, None] = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    env = os.environ.get(DB_ENV_VAR)
    return Path(env) if env else DEFAULT_DB_PATH


def connect(db_path: Union[str, os.PathLike, None] = None, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a connection with foreign keys ON and dict-like rows.

    ``read_only=True`` opens the file with SQLite's ``mode=ro``: any write
    raises ``sqlite3.OperationalError``. The database must already exist.
    """
    path = resolve_db_path(db_path)
    try:
        if read_only:
            if not path.exists():
                raise DatabaseUnavailableError(f"Database file not found: {path.name}")
            uri = path.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=10)
        else:
            if str(path) != ":memory:":
                path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), timeout=10)
    except sqlite3.Error as exc:
        raise DatabaseUnavailableError(f"Cannot open database: {exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_session(db_path: Union[str, os.PathLike, None] = None, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
    """Open, yield, then commit (or roll back on error) and always close."""
    conn = connect(db_path, read_only=read_only)
    try:
        yield conn
        if not read_only:
            conn.commit()
    except BaseException:
        if not read_only:
            conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Union[str, os.PathLike, None] = None) -> Path:
    """Create the schema if missing. Idempotent: safe to call on every start."""
    path = resolve_db_path(db_path)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()
    return path


REQUIRED_TABLES = ("students", "courses", "attendance", "results", "rule_registry",
                   "source_register", "audit")


def database_status(db_path: Union[str, os.PathLike, None] = None) -> tuple[bool, str]:
    """Probe for /health. Returns (ok, detail)."""
    try:
        with db_session(db_path, read_only=True) as conn:
            conn.execute("SELECT 1").fetchone()
            names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [t for t in REQUIRED_TABLES if t not in names]
            if missing:
                return False, f"missing tables: {', '.join(missing)}"
            n = conn.execute("SELECT COUNT(*) AS n FROM students").fetchone()["n"]
            return True, f"{n} students loaded"
    except DatabaseUnavailableError as exc:
        return False, exc.message
    except sqlite3.Error as exc:
        return False, f"sqlite error: {exc}"


# --------------------------------------------------------------------------
# Validation / normalisation helpers
# --------------------------------------------------------------------------
def validate_student_id(student_id: Any) -> str:
    if not isinstance(student_id, str) or not STUDENT_ID_RE.match(student_id.strip()):
        raise InvalidInputError("student_id must be 'S' followed by exactly four digits")
    return student_id.strip()


def validate_course_code(course_code: Any) -> str:
    if not isinstance(course_code, str) or not COURSE_CODE_RE.match(course_code.strip()):
        raise InvalidInputError("course_code must be 2-20 letters, digits, '-' or '_'")
    return course_code.strip()


def coerce_date(value: Any, field: str = "date") -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise InvalidInputError(f"{field} must be a date in YYYY-MM-DD format")


def session_sort_key(session: str) -> tuple[int, int, str]:
    """Chronological key for sessions like '2026-MAY' (unknown formats sort first)."""
    m = re.match(r"^\s*(\d{4})[-_ /]?([A-Za-z]{3,9})\s*$", session or "")
    if m and m.group(2)[:3].upper() in _MONTHS:
        return (int(m.group(1)), _MONTHS[m.group(2)[:3].upper()], session)
    return (0, 0, session or "")


def _row(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


# --------------------------------------------------------------------------
# Students / courses
# --------------------------------------------------------------------------
def get_student(conn: sqlite3.Connection, student_id: str) -> Optional[dict[str, Any]]:
    sid = validate_student_id(student_id)
    return _row(conn.execute("SELECT * FROM students WHERE student_id = ?", (sid,)).fetchone())


def student_exists(conn: sqlite3.Connection, student_id: str) -> bool:
    try:
        return get_student(conn, student_id) is not None
    except InvalidInputError:
        return False


def require_student(conn: sqlite3.Connection, student_id: str) -> dict[str, Any]:
    student = get_student(conn, student_id)
    if student is None:
        raise StudentNotFoundError("No student with that ID exists")
    return student


def list_students(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM students ORDER BY student_id")]


def get_course(conn: sqlite3.Connection, course_code: str) -> Optional[dict[str, Any]]:
    code = validate_course_code(course_code)
    return _row(conn.execute(
        "SELECT * FROM courses WHERE course_code = ? COLLATE NOCASE", (code,)).fetchone())


def require_course(conn: sqlite3.Connection, course_code: str) -> dict[str, Any]:
    course = get_course(conn, course_code)
    if course is None:
        raise CourseNotFoundError("No course with that code exists")
    return course


def list_courses(conn: sqlite3.Connection, programme: Optional[str] = None) -> list[dict[str, Any]]:
    if programme is None:
        rows = conn.execute("SELECT * FROM courses ORDER BY course_code")
    else:
        rows = conn.execute("SELECT * FROM courses WHERE programme = ? ORDER BY course_code", (programme,))
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# Attendance / results / backlogs
# --------------------------------------------------------------------------
def get_attendance_record(conn: sqlite3.Connection, student_id: str, course_code: str) -> Optional[dict[str, Any]]:
    sid = validate_student_id(student_id)
    code = validate_course_code(course_code)
    return _row(conn.execute(
        """SELECT a.student_id, a.course_code, c.course_name, a.classes_held, a.classes_attended
           FROM attendance a JOIN courses c ON c.course_code = a.course_code
           WHERE a.student_id = ? AND a.course_code = ? COLLATE NOCASE""",
        (sid, code)).fetchone())


def list_attendance(conn: sqlite3.Connection, student_id: str) -> list[dict[str, Any]]:
    sid = validate_student_id(student_id)
    return [dict(r) for r in conn.execute(
        """SELECT a.student_id, a.course_code, c.course_name, a.classes_held, a.classes_attended
           FROM attendance a JOIN courses c ON c.course_code = a.course_code
           WHERE a.student_id = ? ORDER BY a.course_code""", (sid,))]


def get_results(conn: sqlite3.Connection, student_id: str, course_code: Optional[str] = None,
                exam_session: Optional[str] = None) -> list[dict[str, Any]]:
    """Result rows for one student, oldest session first (REGULAR before SUPPLEMENTARY)."""
    sid = validate_student_id(student_id)
    sql = ("SELECT r.*, c.course_name FROM results r JOIN courses c ON c.course_code = r.course_code "
           "WHERE r.student_id = ?")
    params: list[Any] = [sid]
    if course_code is not None:
        sql += " AND r.course_code = ? COLLATE NOCASE"
        params.append(validate_course_code(course_code))
    if exam_session is not None:
        if not isinstance(exam_session, str) or not exam_session.strip():
            raise InvalidInputError("exam_session must be a non-empty string")
        sql += " AND r.exam_session = ? COLLATE NOCASE"
        params.append(exam_session.strip())
    rows = [dict(r) for r in conn.execute(sql, params)]
    rows.sort(key=lambda r: (r["course_code"], session_sort_key(r["exam_session"]),
                             0 if r["exam_type"] == "REGULAR" else 1))
    return rows


def get_backlog_courses(conn: sqlite3.Connection, student_id: str) -> list[dict[str, Any]]:
    """Courses whose most recent attempt is FAIL/ABSENT/DETAINED (derived from results)."""
    rows = get_results(conn, student_id)
    by_course: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_course.setdefault(r["course_code"], []).append(r)
    out: list[dict[str, Any]] = []
    for code, attempts in sorted(by_course.items()):
        latest = attempts[-1]  # rows already chronologically sorted per course
        if latest["result"] in BACKLOG_RESULTS:
            out.append({
                "course_code": code,
                "course_name": latest["course_name"],
                "latest_result": latest["result"],
                "latest_session": latest["exam_session"],
                "latest_exam_type": latest["exam_type"],
                "attempts": len(attempts),
                "supplementary_attempts": sum(1 for a in attempts if a["exam_type"] == "SUPPLEMENTARY"),
            })
    return out


# --------------------------------------------------------------------------
# Rule scopes (programme / batch) and the rule registry
# --------------------------------------------------------------------------
def programme_in_scope(programme: str, scope: str) -> bool:
    """'ALL' or a comma/semicolon list. A token matches whole words, so 'B.Tech'
    covers 'B.Tech CSE' but not 'M.Tech CSE'."""
    if scope is None or not str(scope).strip():
        raise InvalidScopeError("scope_programmes is empty")
    tokens = [t.strip() for t in re.split(r"[;,]", str(scope)) if t.strip()]
    if not tokens:
        raise InvalidScopeError("scope_programmes is empty")
    if any(t.upper() == "ALL" for t in tokens):
        return True
    padded = f" {' '.join(programme.split()).casefold()} "
    return any(f" {' '.join(t.split()).casefold()} " in padded for t in tokens)


_BATCH_TOKEN = re.compile(r"^(\d{4})(\+|-(\d{4}))?$")


def parse_batch_scope(scope: str) -> Optional[list[tuple[int, Optional[int]]]]:
    """None means ALL; otherwise a list of inclusive (low, high) year ranges
    (high None = open-ended, i.e. 'YYYY+')."""
    if scope is None or not str(scope).strip():
        raise InvalidScopeError("scope_batches is empty")
    ranges: list[tuple[int, Optional[int]]] = []
    for tok in (t.strip() for t in re.split(r"[;,]", str(scope)) if t.strip()):
        if tok.upper() == "ALL":
            return None
        m = _BATCH_TOKEN.match(tok)
        if not m:
            raise InvalidScopeError(f"unrecognised batch scope token '{tok}' (use ALL, 2023, 2023+, 2022-2024)")
        lo = int(m.group(1))
        if m.group(2) is None:
            ranges.append((lo, lo))
        elif m.group(2) == "+":
            ranges.append((lo, None))
        else:
            hi = int(m.group(3))
            if hi < lo:
                raise InvalidScopeError(f"batch range '{tok}' ends before it starts")
            ranges.append((lo, hi))
    if not ranges:
        raise InvalidScopeError("scope_batches is empty")
    return ranges


def batch_in_scope(batch_year: int, scope: str) -> bool:
    ranges = parse_batch_scope(scope)
    if ranges is None:
        return True
    return any(lo <= batch_year and (hi is None or batch_year <= hi) for lo, hi in ranges)


_RULE_SELECT = """
SELECT r.rule_id, r.description, r.parameter, r.operator, r.value,
       r.scope_programmes, r.scope_batches, r.effective_from, r.effective_to,
       r.source_doc_id, r.source_section,
       s.title AS source_title, s.version AS source_version,
       s.authority_level AS source_authority_level, s.doc_type AS source_doc_type,
       s.effective_from AS source_effective_from, s.effective_to AS source_effective_to,
       s.supersedes AS source_supersedes, s.synthetic AS source_synthetic
FROM rule_registry r JOIN source_register s ON s.doc_id = r.source_doc_id
"""


def get_applicable_rules_detailed(conn: sqlite3.Connection, programme: str, batch_year: int,
                                  as_of_date: Any, parameter: Optional[str] = None) -> dict[str, Any]:
    """Rules in force on ``as_of_date`` whose programme AND batch scope cover the student.

    Date filtering is done in SQL (effective_from <= date AND effective_to empty or >= date),
    scope filtering in Python. Rules with an unparsable scope are not applied; they are
    reported in ``skipped`` so the caller can surface them instead of failing silently.
    Returns {"rules": [...], "skipped": [{"rule_id", "reason"}]}.
    """
    d = coerce_date(as_of_date, "as_of_date").isoformat()
    sql = _RULE_SELECT + " WHERE r.effective_from <= ? AND (r.effective_to IS NULL OR r.effective_to >= ?)"
    params: list[Any] = [d, d]
    if parameter is not None:
        sql += " AND r.parameter = ?"
        params.append(parameter)
    sql += " ORDER BY r.effective_from DESC, r.rule_id"
    rules: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for row in conn.execute(sql, params):
        rule = dict(row)
        try:
            if programme_in_scope(programme, rule["scope_programmes"]) and \
                    batch_in_scope(int(batch_year), rule["scope_batches"]):
                rules.append(rule)
        except InvalidScopeError as exc:
            skipped.append({"rule_id": rule["rule_id"], "reason": exc.message})
    return {"rules": rules, "skipped": skipped}


def get_applicable_rules(conn: sqlite3.Connection, programme: str, batch_year: int,
                         as_of_date: Any, parameter: Optional[str] = None) -> list[dict[str, Any]]:
    return get_applicable_rules_detailed(conn, programme, batch_year, as_of_date, parameter)["rules"]


def get_upcoming_rules(conn: sqlite3.Connection, programme: str, batch_year: int,
                       as_of_date: Any, parameter: Optional[str] = None) -> list[dict[str, Any]]:
    """Rules that are NOT yet effective on ``as_of_date`` (informational only, Annex A step 1)."""
    d = coerce_date(as_of_date, "as_of_date").isoformat()
    sql = _RULE_SELECT + " WHERE r.effective_from > ?"
    params: list[Any] = [d]
    if parameter is not None:
        sql += " AND r.parameter = ?"
        params.append(parameter)
    sql += " ORDER BY r.effective_from, r.rule_id"
    out = []
    for row in conn.execute(sql, params):
        rule = dict(row)
        try:
            if programme_in_scope(programme, rule["scope_programmes"]) and \
                    batch_in_scope(int(batch_year), rule["scope_batches"]):
                out.append(rule)
        except InvalidScopeError:
            continue
    return out


def _iso_or_none(value: Any, field: str) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return coerce_date(value, field).isoformat()


def upsert_rule(conn: sqlite3.Connection, rule: Mapping[str, Any]) -> None:
    """Insert or update one rule_registry row (source_doc_id must already be registered)."""
    conn.execute(
        """INSERT INTO rule_registry (rule_id, description, parameter, operator, value,
               scope_programmes, scope_batches, effective_from, effective_to, source_doc_id, source_section)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(rule_id) DO UPDATE SET
               description=excluded.description, parameter=excluded.parameter,
               operator=excluded.operator, value=excluded.value,
               scope_programmes=excluded.scope_programmes, scope_batches=excluded.scope_batches,
               effective_from=excluded.effective_from, effective_to=excluded.effective_to,
               source_doc_id=excluded.source_doc_id, source_section=excluded.source_section""",
        (rule["rule_id"], rule["description"], rule["parameter"], rule["operator"], str(rule["value"]),
         rule.get("scope_programmes") or "ALL", rule.get("scope_batches") or "ALL",
         _iso_or_none(rule["effective_from"], "effective_from"),
         _iso_or_none(rule.get("effective_to"), "effective_to"),
         rule["source_doc_id"], rule["source_section"]))


def supersede_rule(conn: sqlite3.Connection, old_rule_id: str, new_rule: Mapping[str, Any]) -> None:
    """How a new circular changes a rule: close the old row the day before the new rule
    takes effect, then insert the new row linked to its own source clause. Atomic when
    called inside one transaction (db_session)."""
    old = conn.execute("SELECT effective_from, effective_to FROM rule_registry WHERE rule_id = ?",
                       (old_rule_id,)).fetchone()
    if old is None:
        raise InvalidInputError(f"rule '{old_rule_id}' does not exist")
    new_from = coerce_date(new_rule["effective_from"], "effective_from")
    if new_from.isoformat() <= old["effective_from"]:
        raise InvalidInputError("new rule must take effect after the rule it replaces")
    close_on = (new_from - timedelta(days=1)).isoformat()
    if old["effective_to"] is None or old["effective_to"] > close_on:
        conn.execute("UPDATE rule_registry SET effective_to = ? WHERE rule_id = ?", (close_on, old_rule_id))
    upsert_rule(conn, new_rule)


# --------------------------------------------------------------------------
# Row upserts used by scripts/load_students.py (UPSERT, never INSERT OR REPLACE,
# so ON DELETE CASCADE can never wipe attendance/results during a reload).
# --------------------------------------------------------------------------
def upsert_student(conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
    conn.execute(
        """INSERT INTO students (student_id, full_name, programme, batch_year, current_semester, cgpa, active_backlogs)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(student_id) DO UPDATE SET full_name=excluded.full_name, programme=excluded.programme,
               batch_year=excluded.batch_year, current_semester=excluded.current_semester,
               cgpa=excluded.cgpa, active_backlogs=excluded.active_backlogs""",
        (row["student_id"], row["full_name"], row["programme"], row["batch_year"],
         row["current_semester"], row["cgpa"], row["active_backlogs"]))


def upsert_course(conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
    conn.execute(
        """INSERT INTO courses (course_code, course_name, programme, semester, credits)
           VALUES (?,?,?,?,?)
           ON CONFLICT(course_code) DO UPDATE SET course_name=excluded.course_name,
               programme=excluded.programme, semester=excluded.semester, credits=excluded.credits""",
        (row["course_code"], row["course_name"], row["programme"], row["semester"], row["credits"]))


def upsert_attendance(conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
    conn.execute(
        """INSERT INTO attendance (student_id, course_code, classes_held, classes_attended)
           VALUES (?,?,?,?)
           ON CONFLICT(student_id, course_code) DO UPDATE SET
               classes_held=excluded.classes_held, classes_attended=excluded.classes_attended""",
        (row["student_id"], row["course_code"], row["classes_held"], row["classes_attended"]))


def upsert_result(conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
    conn.execute(
        """INSERT INTO results (student_id, course_code, exam_session, exam_type, internal_marks,
               external_marks, total_marks, max_marks, result)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(student_id, course_code, exam_session, exam_type) DO UPDATE SET
               internal_marks=excluded.internal_marks, external_marks=excluded.external_marks,
               total_marks=excluded.total_marks, max_marks=excluded.max_marks, result=excluded.result""",
        (row["student_id"], row["course_code"], row["exam_session"], row["exam_type"],
         row["internal_marks"], row["external_marks"], row["total_marks"], row["max_marks"], row["result"]))


# --------------------------------------------------------------------------
# Source register
# --------------------------------------------------------------------------
def register_source(conn: sqlite3.Connection, record: Any, *, content_sha256: Optional[str] = None,
                    chunks_indexed: Optional[int] = None) -> None:
    """Upsert one Source Register row (dict or SourceRecord)."""
    data = record.model_dump(mode="json") if hasattr(record, "model_dump") else dict(record)
    conn.execute(
        """INSERT INTO source_register (doc_id, title, issuer, authority_level, doc_type, version,
               effective_from, effective_to, supersedes, scope_programmes, scope_batches,
               provenance, retrieved_on, synthetic, content_sha256, chunks_indexed)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(doc_id) DO UPDATE SET
               title=excluded.title, issuer=excluded.issuer, authority_level=excluded.authority_level,
               doc_type=excluded.doc_type, version=excluded.version,
               effective_from=excluded.effective_from, effective_to=excluded.effective_to,
               supersedes=excluded.supersedes, scope_programmes=excluded.scope_programmes,
               scope_batches=excluded.scope_batches, provenance=excluded.provenance,
               retrieved_on=excluded.retrieved_on, synthetic=excluded.synthetic,
               content_sha256=COALESCE(excluded.content_sha256, source_register.content_sha256),
               chunks_indexed=COALESCE(excluded.chunks_indexed, source_register.chunks_indexed)""",
        (data["doc_id"], data["title"], data["issuer"], int(data["authority_level"]), data["doc_type"],
         str(data["version"]), _iso_or_none(data["effective_from"], "effective_from"),
         _iso_or_none(data.get("effective_to"), "effective_to"), data.get("supersedes") or "",
         data.get("scope_programmes") or "ALL", data.get("scope_batches") or "ALL",
         data["provenance"], _iso_or_none(data["retrieved_on"], "retrieved_on"),
         data.get("synthetic") or "N", content_sha256, chunks_indexed))


def get_source(conn: sqlite3.Connection, doc_id: str) -> Optional[dict[str, Any]]:
    return _row(conn.execute("SELECT * FROM source_register WHERE doc_id = ?", (doc_id,)).fetchone())


def list_sources(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM source_register ORDER BY doc_id")]


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------
_FORBIDDEN_AUDIT_KEYS = {"question", "question_text", "raw_question", "prompt", "message"}
_REDACT_KEYS = {"full_name"}


def new_trace_id() -> str:
    return uuid.uuid4().hex[:12]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def redact_for_audit(value: Any) -> Any:
    """Drop personal fields (full_name) from logged tool inputs/outputs, recursively."""
    if isinstance(value, dict):
        return {k: redact_for_audit(v) for k, v in value.items() if k not in _REDACT_KEYS}
    if isinstance(value, (list, tuple)):
        return [redact_for_audit(v) for v in value]
    return value


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))


def write_audit(conn: sqlite3.Connection, record: Mapping[str, Any]) -> None:
    """Persist one audit record. Raw question text is rejected by design (R7)."""
    bad = _FORBIDDEN_AUDIT_KEYS.intersection(record.keys())
    if bad:
        raise InvalidInputError(f"audit records must not contain raw question text (got {sorted(bad)})")
    ts = record.get("timestamp") or utc_now_iso()
    if isinstance(ts, datetime):
        ts = ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts.tzinfo else ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    as_of = record.get("as_of_date")
    conn.execute(
        """INSERT INTO audit (trace_id, timestamp, student_id, question_category, sources_retrieved,
               precedence_decision, tools_invoked, answer_type, model, llm_calls, tokens, latency_ms,
               applied_rules, conflicts_detected, citations, as_of_date)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (record["trace_id"], ts, record.get("student_id"), record["question_category"],
         _dumps(redact_for_audit(record.get("sources_retrieved") or [])),
         record.get("precedence_decision"),
         _dumps(redact_for_audit(record.get("tools_invoked") or [])),
         record["answer_type"], record.get("model") or "none",
         int(record.get("llm_calls") or 0), int(record.get("tokens") or 0), int(record.get("latency_ms") or 0),
         _dumps(redact_for_audit(record.get("applied_rules") or [])),
         _dumps(redact_for_audit(record.get("conflicts_detected") or [])),
         _dumps(redact_for_audit(record.get("citations") or [])),
         as_of.isoformat() if isinstance(as_of, date) else as_of))


_AUDIT_JSON_COLUMNS = ("sources_retrieved", "tools_invoked", "applied_rules", "conflicts_detected", "citations")


def get_audit(conn: sqlite3.Connection, trace_id: str) -> Optional[dict[str, Any]]:
    if not isinstance(trace_id, str) or not trace_id.strip():
        raise InvalidInputError("trace_id must be a non-empty string")
    row = conn.execute("SELECT * FROM audit WHERE trace_id = ?", (trace_id.strip(),)).fetchone()
    if row is None:
        return None
    rec = dict(row)
    for col in _AUDIT_JSON_COLUMNS:
        rec[col] = json.loads(rec[col]) if rec[col] else []
    return rec


# --------------------------------------------------------------------------
def _main() -> int:
    parser = argparse.ArgumentParser(description="Initialise the student-services SQLite database.")
    parser.add_argument("--init", action="store_true", help="create tables if they do not exist")
    parser.add_argument("--db", help=f"database path (default: ${DB_ENV_VAR} or data/student_services.db)")
    args = parser.parse_args()
    if not args.init:
        parser.print_help()
        return 2
    path = init_db(args.db)
    ok, detail = database_status(path)
    print(f"Initialised {path} -> {'OK' if ok else 'PROBLEM'}: {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
