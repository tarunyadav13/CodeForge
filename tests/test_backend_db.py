"""Schema, constraints, data-access layer, rule scoping and persistent audit."""
from __future__ import annotations

import sqlite3

import pytest

from _m2_helpers import *  # noqa: F401,F403  (fixtures)
from _m2_helpers import corrupt, raw_conn
from db import data_access as da

ANNEX_C = {
    "students": ["student_id", "full_name", "programme", "batch_year", "current_semester", "cgpa", "active_backlogs"],
    "courses": ["course_code", "course_name", "programme", "semester", "credits"],
    "attendance": ["student_id", "course_code", "classes_held", "classes_attended"],
    "results": ["student_id", "course_code", "exam_session", "exam_type", "internal_marks", "external_marks",
                "total_marks", "max_marks", "result"],
    "rule_registry": ["rule_id", "description", "parameter", "operator", "value", "scope_programmes", "scope_batches",
                      "effective_from", "effective_to", "source_doc_id", "source_section"],
}


@pytest.fixture()
def empty_db(tmp_path):
    path = tmp_path / "empty.db"
    da.init_db(path)
    return path


def cols(path, table):
    conn = raw_conn(path)
    out = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    conn.close()
    return out


class TestSchema:
    def test_annex_c_columns_exact_and_in_order(self, empty_db):
        for table, expected in ANNEX_C.items():
            assert cols(empty_db, table)[:len(expected)] == expected, table

    def test_extra_required_tables_exist(self, empty_db):
        conn = raw_conn(empty_db)
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"source_register", "audit"} <= names
        conn.close()

    def test_attendance_percentage_is_never_stored(self, empty_db):
        assert not [c for c in cols(empty_db, "attendance") if "pct" in c or "percent" in c]

    def test_init_is_repeatable_and_keeps_data(self, empty_db):
        with da.db_session(empty_db) as conn:
            da.upsert_course(conn, dict(course_code="CS101", course_name="Intro", programme="B.Tech CSE", semester=1, credits=3))
        da.init_db(empty_db)
        da.init_db(empty_db)
        with da.db_session(empty_db, read_only=True) as conn:
            assert da.get_course(conn, "CS101")["course_name"] == "Intro"

    def test_foreign_keys_pragma_on_every_connection(self, empty_db):
        with da.db_session(empty_db) as conn:
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with da.db_session(empty_db, read_only=True) as conn:
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_indexes_created(self, empty_db):
        conn = raw_conn(empty_db)
        idx = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"idx_results_student", "idx_rules_param_dates", "idx_audit_student"} <= idx
        conn.close()

    def test_database_path_comes_from_environment(self, tmp_path, monkeypatch):
        target = tmp_path / "from_env.db"
        monkeypatch.setenv(da.DB_ENV_VAR, str(target))
        assert da.resolve_db_path() == target
        da.init_db()
        assert target.exists()


BAD_ROWS = [
    ("student id format", "INSERT INTO students VALUES ('X1','A B','B.Tech CSE',2024,5,8.5,0)"),
    ("student id too short", "INSERT INTO students VALUES ('S123','A B','B.Tech CSE',2024,5,8.5,0)"),
    ("semester 11", "INSERT INTO students VALUES ('S1002','A B','B.Tech CSE',2024,11,8.5,0)"),
    ("semester 0", "INSERT INTO students VALUES ('S1002','A B','B.Tech CSE',2024,0,8.5,0)"),
    ("cgpa 10.5", "INSERT INTO students VALUES ('S1002','A B','B.Tech CSE',2024,5,10.5,0)"),
    ("cgpa negative", "INSERT INTO students VALUES ('S1002','A B','B.Tech CSE',2024,5,-0.1,0)"),
    ("negative backlogs", "INSERT INTO students VALUES ('S1002','A B','B.Tech CSE',2024,5,8.5,-1)"),
    ("blank name", "INSERT INTO students VALUES ('S1002','  ','B.Tech CSE',2024,5,8.5,0)"),
]


class TestConstraints:
    @pytest.mark.parametrize("label,sql", BAD_ROWS, ids=[b[0] for b in BAD_ROWS])
    def test_invalid_student_rejected(self, empty_db, label, sql):
        conn = raw_conn(empty_db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
        conn.close()

    def _base(self, path):
        conn = raw_conn(path)
        conn.execute("INSERT INTO students VALUES ('S1001','A B','B.Tech CSE',2024,5,8.5,0)")
        conn.execute("INSERT INTO courses VALUES ('CS201','DS','B.Tech CSE',3,4)")
        return conn

    @pytest.mark.parametrize("sql", [
        "INSERT INTO attendance VALUES ('S1001','CS201',10,11)",    # attended > held
        "INSERT INTO attendance VALUES ('S1001','CS201',0,0)",      # held must be > 0
        "INSERT INTO attendance VALUES ('S1001','CS201',10,-1)",    # negative attended
        "INSERT INTO attendance VALUES ('S9999','CS201',10,5)",     # unknown student (FK)
        "INSERT INTO attendance VALUES ('S1001','NOPE1',10,5)",     # unknown course (FK)
    ])
    def test_invalid_attendance_rejected(self, empty_db, sql):
        conn = self._base(empty_db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
        conn.close()

    @pytest.mark.parametrize("sql", [
        "INSERT INTO results VALUES ('S1001','CS201','2026-MAY','REGULAR',20,30,60,100,'PASS')",   # total mismatch
        "INSERT INTO results VALUES ('S1001','CS201','2026-MAY','MAKEUP',20,30,50,100,'PASS')",     # bad exam_type
        "INSERT INTO results VALUES ('S1001','CS201','2026-MAY','REGULAR',20,30,50,100,'MAYBE')",   # bad result
        "INSERT INTO results VALUES ('S1001','CS201','2026-MAY','REGULAR',60,60,120,100,'PASS')",   # total > max
        "INSERT INTO results VALUES ('S1001','CS201','2026-MAY','REGULAR',20,30,50,0,'PASS')",      # max must be > 0
    ])
    def test_invalid_result_rejected(self, empty_db, sql):
        conn = self._base(empty_db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
        conn.close()

    def test_rule_requires_registered_source_and_valid_values(self, empty_db):
        conn = raw_conn(empty_db)
        with pytest.raises(sqlite3.IntegrityError):  # source_doc_id must exist in the Source Register
            conn.execute("INSERT INTO rule_registry VALUES ('R1','d','p','>=','75','ALL','ALL','2024-01-01',NULL,'NOSRC','1')")
        conn.execute("INSERT INTO source_register (doc_id,title,issuer,authority_level,doc_type,version,effective_from,provenance,retrieved_on) "
                     "VALUES ('D1','t','i',1,'regulation','1','2024-01-01','p','2026-01-01')")
        with pytest.raises(sqlite3.IntegrityError):  # bad operator
            conn.execute("INSERT INTO rule_registry VALUES ('R1','d','p','~=','75','ALL','ALL','2024-01-01',NULL,'D1','1')")
        with pytest.raises(sqlite3.IntegrityError):  # effective_to before effective_from
            conn.execute("INSERT INTO rule_registry VALUES ('R1','d','p','>=','75','ALL','ALL','2024-01-01','2023-01-01','D1','1')")
        with pytest.raises(sqlite3.IntegrityError):  # impossible date
            conn.execute("INSERT INTO rule_registry VALUES ('R1','d','p','>=','75','ALL','ALL','2024-13-45',NULL,'D1','1')")
        conn.execute("INSERT INTO rule_registry VALUES ('R1','d','p','>=','75','ALL','ALL','2024-01-01',NULL,'D1','7.2')")
        conn.close()

    def test_source_register_validates_authority_and_type(self, empty_db):
        conn = raw_conn(empty_db)
        for level, kind in ((0, "regulation"), (6, "regulation"), (1, "memo")):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO source_register (doc_id,title,issuer,authority_level,doc_type,version,effective_from,provenance,retrieved_on) "
                             "VALUES ('D1','t','i',?,?,'1','2024-01-01','p','2026-01-01')", (level, kind))
        conn.close()

    def test_judge_reserved_ids_are_accepted_by_the_schema(self, empty_db):
        """Judges load their own S9xxx / JDG* data through the same schema."""
        conn = raw_conn(empty_db)
        conn.execute("INSERT INTO students VALUES ('S9001','J D','B.Tech CSE',2024,5,8.5,0)")
        conn.execute("INSERT INTO courses VALUES ('JDG101','Judge Course','B.Tech CSE',3,4)")
        conn.close()

    def test_cascade_delete_does_not_leave_orphans(self, loaded_db, db_copy):
        conn = raw_conn(db_copy)
        conn.execute("DELETE FROM students WHERE student_id='S1001'")
        assert conn.execute("SELECT COUNT(*) FROM attendance WHERE student_id='S1001'").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()


class TestSqlSafety:
    @pytest.mark.parametrize("evil", ["'; DROP TABLE students;--", "S1001' OR '1'='1", "S1001 UNION SELECT * FROM students"])
    def test_malicious_student_ids_are_rejected_before_sql(self, loaded_db, evil):
        with da.db_session(loaded_db, read_only=True) as conn:
            with pytest.raises(da.InvalidInputError):
                da.get_student(conn, evil)
            assert da.student_exists(conn, evil) is False

    @pytest.mark.parametrize("evil", ["CS201'; DROP TABLE courses;--", "x' OR '1'='1", "CS201 OR 1=1"])
    def test_malicious_course_codes_are_rejected(self, loaded_db, evil):
        with da.db_session(loaded_db, read_only=True) as conn:
            with pytest.raises(da.InvalidInputError):
                da.get_course(conn, evil)
            with pytest.raises(da.InvalidInputError):
                da.get_results(conn, "S1001", evil)

    def test_text_parameters_are_bound_not_interpolated(self, loaded_db):
        evil = "2026-MAY' OR '1'='1"  # valid-looking free text, must be treated as a literal
        with da.db_session(loaded_db, read_only=True) as conn:
            assert da.get_results(conn, "S1001", exam_session=evil) == []
            assert da.list_courses(conn, "B.Tech CSE' OR '1'='1") == []
            assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 36

    def test_read_only_connection_cannot_write(self, loaded_db):
        with da.db_session(loaded_db, read_only=True) as conn:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("UPDATE students SET cgpa = 10 WHERE student_id='S1001'")

    def test_read_only_on_missing_file_is_a_controlled_error(self, tmp_path):
        with pytest.raises(da.DatabaseUnavailableError):
            da.connect(tmp_path / "nope.db", read_only=True)


class TestQueries:
    def test_student_course_attendance_results(self, loaded_db):
        with da.db_session(loaded_db, read_only=True) as conn:
            assert da.get_student(conn, "S1001")["programme"] == "B.Tech CSE"
            assert da.get_student(conn, "S4242") is None
            with pytest.raises(da.StudentNotFoundError):
                da.require_student(conn, "S4242")
            assert da.get_course(conn, "cs201")["course_code"] == "CS201"  # case-insensitive
            with pytest.raises(da.CourseNotFoundError):
                da.require_course(conn, "ZZ999")
            assert da.get_attendance_record(conn, "S1001", "CS201")["classes_attended"] == 32
            assert da.get_attendance_record(conn, "S1009", "CS201") is None
            rows = da.get_results(conn, "S1019", "MA202")
            assert [(r["exam_session"], r["exam_type"]) for r in rows] == [
                ("2024-DEC", "REGULAR"), ("2025-MAY", "SUPPLEMENTARY"), ("2025-DEC", "SUPPLEMENTARY"), ("2026-MAY", "SUPPLEMENTARY")]

    def test_backlog_courses_use_latest_attempt(self, loaded_db):
        with da.db_session(loaded_db, read_only=True) as conn:
            assert {b["course_code"] for b in da.get_backlog_courses(conn, "S1008")} == {"CS201", "MA201", "CS202"}
            assert da.get_backlog_courses(conn, "S1001") == []

    def test_latest_pass_clears_an_earlier_fail(self, db_copy):
        with da.db_session(db_copy) as conn:
            da.upsert_result(conn, dict(student_id="S1007", course_code="MA201", exam_session="2026-DEC", exam_type="SUPPLEMENTARY",
                                        internal_marks=20, external_marks=30, total_marks=50, max_marks=100, result="PASS"))
            assert da.get_backlog_courses(conn, "S1007") == []

    def test_session_sort_key_orders_chronologically(self):
        keys = sorted(["2026-MAY", "2025-DEC", "2026-DEC", "2026-JAN"], key=da.session_sort_key)
        assert keys == ["2025-DEC", "2026-JAN", "2026-MAY", "2026-DEC"]


class TestScopesAndRules:
    @pytest.mark.parametrize("programme,scope,expected", [
        ("B.Tech CSE", "ALL", True), ("B.Tech CSE", "B.Tech", True), ("B.Tech CSE", "M.Tech", False),
        ("B.Tech CSE", "CSE;ECE", True), ("B.Tech CSE", "b.tech cse", True), ("B.Tech CSE", "B.Tech ECE", False),
        ("M.Tech CSE", "B.Tech", False)])
    def test_programme_scope(self, programme, scope, expected):
        assert da.programme_in_scope(programme, scope) is expected

    @pytest.mark.parametrize("year,scope,expected", [
        (2024, "ALL", True), (2024, "2023+", True), (2022, "2023+", False), (2023, "2023", True), (2024, "2023", False),
        (2024, "2022-2024", True), (2025, "2022-2024", False), (2020, "2022-2024,2019", False), (2019, "2022-2024,2019", True)])
    def test_batch_scope(self, year, scope, expected):
        assert da.batch_in_scope(year, scope) is expected

    @pytest.mark.parametrize("bad", ["", "2023++", "abc", "2024-2022"])
    def test_bad_batch_scope_is_a_controlled_error(self, bad):
        with pytest.raises(da.InvalidScopeError):
            da.batch_in_scope(2024, bad)

    def test_effective_date_boundaries(self, loaded_db):
        with da.db_session(loaded_db, read_only=True) as conn:
            ids = lambda d: [r["rule_id"] for r in da.get_applicable_rules(conn, "B.Tech CSE", 2024, d, "min_attendance_pct")]
            assert ids("2024-06-30") == []                  # before anything took effect
            assert ids("2024-07-01") == ["ATT-MIN-01"]      # first day in force
            assert ids("2026-07-31") == ["ATT-MIN-01"]      # last day of the old rule
            assert ids("2026-08-01") == ["ATT-MIN-02"]      # circular takes over
            upcoming = da.get_upcoming_rules(conn, "B.Tech CSE", 2024, "2026-06-01", "min_attendance_pct")
            assert [r["rule_id"] for r in upcoming] == ["ATT-MIN-02"]

    def test_rules_carry_source_provenance(self, loaded_db):
        with da.db_session(loaded_db, read_only=True) as conn:
            rule = da.get_applicable_rules(conn, "B.Tech CSE", 2024, "2026-10-06", "min_attendance_pct")[0]
        assert rule["source_doc_id"] == "FIX-ACAD-CIRC-2026-08" and rule["source_section"] == "1"
        assert rule["source_version"] == "1.0" and rule["source_authority_level"] == 2

    def test_programme_and_batch_scoped_rules_are_filtered(self, db_copy):
        with da.db_session(db_copy) as conn:
            da.upsert_rule(conn, dict(rule_id="X-1", description="ECE only", parameter="x_param", operator=">=", value="1",
                                      scope_programmes="B.Tech ECE", scope_batches="2025+", effective_from="2024-07-01",
                                      effective_to=None, source_doc_id="FIX-ACAD-REG-2024", source_section="1"))
            hit = lambda prog, year: [r["rule_id"] for r in da.get_applicable_rules(conn, prog, year, "2026-10-06", "x_param")]
            assert hit("B.Tech ECE", 2025) == ["X-1"]
            assert hit("B.Tech CSE", 2025) == []
            assert hit("B.Tech ECE", 2024) == []

    def test_unparseable_scope_is_skipped_and_reported_not_applied(self, db_copy):
        with da.db_session(db_copy) as conn:
            da.upsert_rule(conn, dict(rule_id="X-BAD", description="d", parameter="x_param", operator=">=", value="1",
                                      scope_programmes="ALL", scope_batches="soon", effective_from="2024-07-01",
                                      effective_to=None, source_doc_id="FIX-ACAD-REG-2024", source_section="1"))
            found = da.get_applicable_rules_detailed(conn, "B.Tech CSE", 2024, "2026-10-06", "x_param")
        assert found["rules"] == [] and found["skipped"][0]["rule_id"] == "X-BAD"

    def test_supersede_rule_closes_old_and_opens_new(self, db_copy):
        with da.db_session(db_copy) as conn:
            da.supersede_rule(conn, "ATT-MIN-02", dict(rule_id="ATT-MIN-03", description="85 now", parameter="min_attendance_pct",
                                                       operator=">=", value="85", scope_programmes="ALL", scope_batches="ALL",
                                                       effective_from="2027-01-01", effective_to=None,
                                                       source_doc_id="FIX-ACAD-CIRC-2026-08", source_section="2"))
            old = conn.execute("SELECT effective_to FROM rule_registry WHERE rule_id='ATT-MIN-02'").fetchone()[0]
            ids = lambda d: [r["rule_id"] for r in da.get_applicable_rules(conn, "B.Tech CSE", 2024, d, "min_attendance_pct")]
            assert old == "2026-12-31"
            assert ids("2026-12-31") == ["ATT-MIN-02"] and ids("2027-01-01") == ["ATT-MIN-03"]
            with pytest.raises(da.InvalidInputError):
                da.supersede_rule(conn, "ATT-MIN-03", dict(rule_id="ATT-MIN-04", description="d", parameter="min_attendance_pct",
                                                           operator=">=", value="90", effective_from="2026-01-01",
                                                           source_doc_id="FIX-ACAD-CIRC-2026-08", source_section="3"))


class TestSourceRegister:
    def test_register_upsert_and_list(self, empty_db):
        rec = dict(doc_id="D1", title="T", issuer="I", authority_level=2, doc_type="circular", version="1.0",
                   effective_from="2026-08-01", effective_to=None, supersedes="", scope_programmes="ALL",
                   scope_batches="ALL", provenance="test", retrieved_on="2026-10-05", synthetic="Y")
        with da.db_session(empty_db) as conn:
            da.register_source(conn, rec, content_sha256="abc", chunks_indexed=3)
            da.register_source(conn, {**rec, "title": "T2"})  # update keeps hash/chunks
        with da.db_session(empty_db, read_only=True) as conn:
            row = da.get_source(conn, "D1")
        assert row["title"] == "T2" and row["content_sha256"] == "abc" and row["chunks_indexed"] == 3
        assert len(da.list_sources(raw_conn(empty_db))) == 1


class TestAudit:
    REC = dict(trace_id="abc123def456", student_id="S1001", question_category="personal_attendance",
               sources_retrieved=[{"doc_id": "D1", "section": "7.2", "score": 0.8}], precedence_decision="x",
               tools_invoked=[{"tool": "get_student_profile", "output": {"full_name": "Secret Name", "cgpa": 8}}],
               answer_type="calculated", model="gpt-x", llm_calls=2, tokens=100, latency_ms=50,
               applied_rules=[{"rule_id": "R", "value": ">= 1", "source_doc_id": "D1"}], conflicts_detected=[],
               citations=[], as_of_date="2026-10-06")

    def test_round_trip_survives_reconnect(self, empty_db):
        with da.db_session(empty_db) as conn:
            da.write_audit(conn, self.REC)
        with da.db_session(empty_db, read_only=True) as conn:  # a brand-new connection
            rec = da.get_audit(conn, "abc123def456")
        assert rec["answer_type"] == "calculated" and rec["sources_retrieved"][0]["doc_id"] == "D1"
        assert rec["llm_calls"] == 2 and rec["tokens"] == 100 and rec["student_id"] == "S1001"
        assert rec["applied_rules"][0]["rule_id"] == "R"

    def test_personal_name_is_redacted_from_logged_tool_output(self, empty_db):
        with da.db_session(empty_db) as conn:
            da.write_audit(conn, self.REC)
            raw = conn.execute("SELECT tools_invoked FROM audit").fetchone()[0]
        assert "Secret Name" not in raw and "cgpa" in raw

    @pytest.mark.parametrize("key", ["question", "question_text", "raw_question", "prompt"])
    def test_raw_question_text_is_refused(self, empty_db, key):
        with da.db_session(empty_db) as conn:
            with pytest.raises(da.InvalidInputError):
                da.write_audit(conn, {**self.REC, key: "what is my attendance"})

    def test_audit_outlives_student_rows_and_rejects_bad_answer_type(self, empty_db):
        with da.db_session(empty_db) as conn:
            da.write_audit(conn, self.REC)  # student S1001 does not exist: no FK on purpose
            with pytest.raises(sqlite3.IntegrityError):
                da.write_audit(conn, {**self.REC, "trace_id": "zzz", "answer_type": "guess"})

    def test_missing_trace_is_none_and_blank_is_invalid(self, empty_db):
        with da.db_session(empty_db, read_only=True) as conn:
            assert da.get_audit(conn, "nope") is None
            with pytest.raises(da.InvalidInputError):
                da.get_audit(conn, "  ")

    def test_trace_ids_are_unique(self):
        assert len({da.new_trace_id() for _ in range(500)}) == 500
