"""Synthetic data generation, judge CSV loader and validator."""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from _m2_helpers import *  # noqa: F401,F403
from _m2_helpers import corrupt, raw_conn
from db import data_access as da
from scripts import gen_data, validate
from scripts.csv_schema import COLUMNS
from scripts.load_students import format_report, load_directory, main as load_main


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path, table, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS[table])
        w.writeheader()
        w.writerows(rows)


# ------------------------------------------------------------------ generator
class TestGenerator:
    def test_same_seed_gives_byte_identical_files(self, tmp_path):
        hashes = []
        for name in ("a", "b"):
            out = tmp_path / name
            gen_data.write_dataset(gen_data.build_dataset(1234), out, seed=1234, llm_report=None)
            hashes.append({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.glob("*.csv"))})
        assert hashes[0] == hashes[1]

    def test_different_seed_changes_random_parts_not_edge_cases(self, tmp_path):
        a = gen_data.build_dataset(1)
        b = gen_data.build_dataset(2)
        assert [s["full_name"] for s in a["students"]] != [s["full_name"] for s in b["students"]]
        fixed = lambda d: {k: v for k, v in d["edge_case_students"].items() if not k.endswith("_random")}
        assert fixed(a) == fixed(b)  # deliberate edge cases never move; only chance-tagged ones may
        att = lambda d: {(r["student_id"], r["course_code"]): (r["classes_held"], r["classes_attended"]) for r in d["attendance"]}
        assert att(a)["S1001", "CS201"] == att(b)["S1001", "CS201"] == (40, 32)

    def test_meets_brief_minimums(self, gen_dir):
        out, report = gen_dir
        students, courses = read_csv(out / "students.csv"), read_csv(out / "courses.csv")
        assert len(students) >= 30 and len(courses) >= 6
        assert len({s["programme"] for s in students}) >= 2 and len({s["batch_year"] for s in students}) >= 2
        assert report["counts"]["students"] == len(students) == 36

    def test_no_judge_reserved_ids_and_no_duplicates(self, gen_dir):
        out, _ = gen_dir
        ids = [s["student_id"] for s in read_csv(out / "students.csv")]
        assert len(set(ids)) == len(ids) and not [i for i in ids if i.startswith("S9")]
        codes = [c["course_code"] for c in read_csv(out / "courses.csv")]
        assert len(set(codes)) == len(codes) and not [c for c in codes if c.upper().startswith("JDG")]
        names = [s["full_name"].casefold() for s in read_csv(out / "students.csv")]
        assert len(set(names)) == len(names)

    def test_every_source_row_is_marked_synthetic(self, gen_dir):
        assert {r["synthetic"] for r in read_csv(gen_dir[0] / "source_register.csv")} == {"Y"}

    def test_every_rule_traces_to_a_source_row(self, gen_dir):
        out, _ = gen_dir
        docs = {r["doc_id"] for r in read_csv(out / "source_register.csv")}
        rules = read_csv(out / "rule_registry.csv")
        assert rules and all(r["source_doc_id"] in docs and r["source_section"] for r in rules)

    def test_documented_edge_cases_hold_exactly(self, gen_dir):
        out, report = gen_dir
        att = {(r["student_id"], r["course_code"]): (int(r["classes_held"]), int(r["classes_attended"])) for r in read_csv(out / "attendance.csv")}
        assert att["S1001", "CS201"] == (40, 32) and att["S1002", "CS201"] == (40, 31)      # 80% exactly / one class below
        assert att["S1003", "CS201"] == (40, 30) and att["S1004", "CS201"] == (40, 29)      # 75% exactly / one below
        assert not [k for k in att if k[0] == "S1028"] and ("S1009", "CS201") not in att  # missing attendance
        res = read_csv(out / "results.csv")
        assert not [r for r in res if r["student_id"] == "S1013"]                         # no result records
        s1007 = [r for r in res if r["student_id"] == "S1007" and r["course_code"] == "MA201"][0]
        assert (s1007["result"], s1007["total_marks"]) == ("FAIL", "39")                  # one mark below a 40 pass mark
        assert {r["result"] for r in res if r["student_id"] == "S1010"} >= {"ABSENT"}
        assert {r["result"] for r in res if r["student_id"] == "S1022"} >= {"DETAINED"}
        cg = {s["student_id"]: float(s["cgpa"]) for s in read_csv(out / "students.csv")}
        assert cg["S1011"] == 6.5 and cg["S1012"] == 6.49
        assert report["edge_case_students"]["multiple_active_backlogs_3"] == ["S1008"]

    def test_stored_backlog_counts_match_results(self, loaded_db):
        with da.db_session(loaded_db, read_only=True) as conn:
            for s in da.list_students(conn):
                assert len(da.get_backlog_courses(conn, s["student_id"])) == s["active_backlogs"], s["student_id"]

    def test_prompts_are_saved_verbatim_and_used(self):
        system, user = gen_data.render_prompts()
        assert "synthetic data generator" in system and "<<GROUPS>>" not in user
        assert 'programme "B.Tech CSE", batch_year 2024: 9 students' in user
        assert (gen_data.PROMPT_DIR / "student_generation_user.txt").read_text().count("<<GROUPS>>") == 1


class FakeLLM:
    """Stands in for llm.LLMClient (no network)."""
    mock = False
    model_name = "fake-model"

    def __init__(self, payloads=None, error=None):
        self.payloads, self.error, self.calls = list(payloads or []), error, 0

    def generate_structured(self, system, user, schema):
        self.calls += 1
        if self.error:
            raise self.error
        return SimpleNamespace(parsed=schema.model_validate({"students": self.payloads.pop(0)}), llm_calls=1, total_tokens=100)


def draft_rows(prog, batch, n, start=0, bad=False):
    first, last = ["Aanya", "Bimal", "Chetan", "Devika", "Eshwar", "Farah", "Gopal", "Harini", "Indra", "Jaya"], ["Kumar", "Lal", "Menon"]
    rows = []
    for i in range(n):
        rows.append({"full_name": f"{first[(start + i) % 10]} {last[(start + i) // 10 % 3]}", "programme": prog, "batch_year": batch,
                     "cgpa": 7.0 + (i % 5) * 0.1})
    return rows


class TestLLMDrafting:
    def test_accepts_valid_rows_and_rejects_bad_ones(self):
        rows = draft_rows("B.Tech CSE", 2024, 9)
        rows += [{"full_name": "X", "programme": "B.Tech CSE", "batch_year": 2024, "cgpa": 7.0},          # name too short
                 {"full_name": "Bad Cgpa", "programme": "B.Tech CSE", "batch_year": 2024, "cgpa": 11.0},  # out of range
                 {"full_name": "Wrong Prog", "programme": "B.Arch", "batch_year": 2024, "cgpa": 7.0},     # unknown programme
                 {"full_name": "Numb3r Name", "programme": "B.Tech CSE", "batch_year": 2024, "cgpa": 7.0},
                 {"full_name": "Aanya Kumar", "programme": "B.Tech CSE", "batch_year": 2024, "cgpa": 7.0}]  # duplicate of row 1
        fake = FakeLLM([rows])
        accepted, report = gen_data.draft_with_llm(client=fake, max_calls=1)
        assert len(accepted[("B.Tech CSE", 2024)]) == 9
        assert report["rows_accepted"] == 9 and report["rows_rejected"] == 5 and report["llm_calls"] == 1
        assert report["model"] == "fake-model" and len(report["prompt_sha256"]) == 64

    def test_llm_rows_are_used_and_gaps_filled_deterministically(self):
        accepted, _ = gen_data.draft_with_llm(client=FakeLLM([draft_rows("B.Tech CSE", 2024, 9)]), max_calls=1)
        ds = gen_data.build_dataset(7, accepted)
        assert ds["llm_rows_used"] == 9 and len(ds["students"]) == 36
        assert ds["students"][0]["full_name"] == "Aanya Kumar"
        assert ds["students"][0]["student_id"] == "S1001"
        assert len({s["full_name"].casefold() for s in ds["students"]}) == 36

    def test_boundary_cgpa_from_the_llm_never_collides_with_edge_students(self):
        rows = draft_rows("B.Tech CSE", 2025, 9)
        rows[0]["cgpa"] = 6.5
        accepted, _ = gen_data.draft_with_llm(client=FakeLLM([rows]), max_calls=1)
        ds = gen_data.build_dataset(7, accepted)
        at_cutoff = [s["student_id"] for s in ds["students"] if s["cgpa"] == 6.5]
        assert set(at_cutoff) == {"S1011", "S1023"}

    def test_llm_failure_is_recorded_and_does_not_raise(self):
        from llm.openai_client import LLMAPIError
        fake = FakeLLM(error=LLMAPIError("rate limited", 429))
        accepted, report = gen_data.draft_with_llm(client=fake, max_calls=3)
        assert fake.calls == 1  # api errors are not retried
        assert report["rows_accepted"] == 0 and "llm_api_error" in report["call_errors"][0]
        assert len(gen_data.build_dataset(7, accepted)["students"]) == 36

    def test_mock_client_yields_deterministic_dataset(self, monkeypatch):
        from llm.openai_client import LLMClient, LLMConfig
        client = LLMClient(LLMConfig(mock=True))
        accepted, report = gen_data.draft_with_llm(client=client, max_calls=2)
        assert report["mode"] == "openai-mock" and report["rows_accepted"] == 0
        assert gen_data.build_dataset(5, accepted)["students"] == gen_data.build_dataset(5)["students"]

    def test_cli_openai_mode_without_key_fails_cleanly(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("MOCK_LLM", raising=False)
        assert gen_data.main(["--out", str(tmp_path), "--mode", "openai"]) == 2
        assert "OPENAI_API_KEY" in capsys.readouterr().err
        assert not list(tmp_path.glob("*.csv"))


# ------------------------------------------------------------------ loader
class TestLoader:
    def test_load_twice_is_idempotent(self, gen_dir, tmp_path):
        db = tmp_path / "x.db"
        first = load_directory(gen_dir[0], db)
        second = load_directory(gen_dir[0], db)
        assert first.ok and second.ok and first.rows_loaded == second.rows_loaded == 268
        with da.db_session(db, read_only=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 36
            assert conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0] == 139

    def test_reload_updates_changed_rows_without_wiping_children(self, gen_dir, tmp_path):
        d = tmp_path / "csv"
        shutil.copytree(gen_dir[0], d)
        db = tmp_path / "x.db"
        load_directory(d, db)
        rows = read_csv(d / "students.csv")
        rows[0]["cgpa"] = "9.99"
        write_csv(d / "students.csv", "students", rows)
        load_directory(d / "..", db) if False else load_directory(d, db)
        with da.db_session(db, read_only=True) as conn:
            assert da.get_student(conn, "S1001")["cgpa"] == 9.99
            assert da.get_attendance_record(conn, "S1001", "CS201") is not None  # ON DELETE CASCADE never fired

    def test_judge_style_csv_with_reserved_ids_loads(self, tmp_path):
        d = tmp_path / "judge"
        d.mkdir()
        write_csv(d / "courses.csv", "courses", [dict(course_code="JDG101", course_name="Judge Course", programme="B.Tech CSE", semester=3, credits=4)])
        write_csv(d / "students.csv", "students", [dict(student_id="S9001", full_name="Test Judge", programme="B.Tech CSE", batch_year=2024,
                                                          current_semester=5, cgpa=7.5, active_backlogs=0)])
        write_csv(d / "attendance.csv", "attendance", [dict(student_id="S9001", course_code="JDG101", classes_held=20, classes_attended=15)])
        rep = load_directory(d, tmp_path / "j.db")
        assert rep.ok and rep.rows_loaded == 3

    def test_headers_are_case_insensitive_and_bom_tolerated(self, tmp_path):
        d = tmp_path / "bom"
        d.mkdir()
        (d / "Students.csv").write_bytes("﻿Student_ID,Full Name,Programme,Batch_Year,Current_Semester,CGPA,Active_Backlogs\n"
                                         "S2001,Anu Rao,B.Tech CSE,2024,5,7.5,0\n".encode("utf-8"))
        rep = load_directory(d, tmp_path / "b.db")
        assert rep.ok, format_report(rep)

    def test_bad_rows_are_reported_with_line_numbers_and_good_rows_still_load(self, tmp_path):
        d = tmp_path / "bad"
        d.mkdir()
        write_csv(d / "courses.csv", "courses", [dict(course_code="CS201", course_name="DS", programme="B.Tech CSE", semester=3, credits=4)])
        write_csv(d / "students.csv", "students", [
            dict(student_id="S3001", full_name="Good One", programme="B.Tech CSE", batch_year=2024, current_semester=5, cgpa=7.0, active_backlogs=0),
            dict(student_id="BAD", full_name="Bad Id", programme="B.Tech CSE", batch_year=2024, current_semester=5, cgpa=7.0, active_backlogs=0),
            dict(student_id="S3003", full_name="Bad Cgpa", programme="B.Tech CSE", batch_year=2024, current_semester=5, cgpa=11, active_backlogs=0),
            dict(student_id="S3004", full_name="Not Number", programme="B.Tech CSE", batch_year=2024, current_semester="five", cgpa=7, active_backlogs=0),
            dict(student_id="S3001", full_name="Duplicate", programme="B.Tech CSE", batch_year=2024, current_semester=5, cgpa=7.0, active_backlogs=0)])
        write_csv(d / "attendance.csv", "attendance", [
            dict(student_id="S3001", course_code="CS201", classes_held=10, classes_attended=11),   # attended > held
            dict(student_id="S3001", course_code="NOPE", classes_held=10, classes_attended=5),     # unknown course
            dict(student_id="S3001", course_code="CS201", classes_held=10, classes_attended=9)])   # fine
        rep = load_directory(d, tmp_path / "bad.db")
        assert not rep.ok
        t = {x.table: x for x in rep.tables}
        assert t["students"].rows_loaded == 1 and [ln for ln, _ in t["students"].rejected] == [3, 4, 5, 6]
        assert "duplicate key" in t["students"].rejected[-1][1]
        assert t["attendance"].rows_loaded == 1 and [ln for ln, _ in t["attendance"].rejected] == [2, 3]
        assert "Bad Id" not in format_report(rep)  # rejected values (personal data) are not echoed back

    def test_atomic_mode_writes_nothing_when_anything_is_rejected(self, tmp_path):
        d = tmp_path / "atomic"
        d.mkdir()
        write_csv(d / "students.csv", "students", [
            dict(student_id="S3001", full_name="Good One", programme="B.Tech CSE", batch_year=2024, current_semester=5, cgpa=7.0, active_backlogs=0),
            dict(student_id="S3002", full_name="Bad Sem", programme="B.Tech CSE", batch_year=2024, current_semester=99, cgpa=7.0, active_backlogs=0)])
        rep = load_directory(d, tmp_path / "a.db", atomic=True)
        assert rep.rolled_back and not rep.ok
        with da.db_session(tmp_path / "a.db", read_only=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 0

    def test_missing_required_column_rejects_the_file(self, tmp_path):
        d = tmp_path / "col"
        d.mkdir()
        (d / "students.csv").write_text("student_id,full_name\nS1,x\n")
        rep = load_directory(d, tmp_path / "c.db")
        assert rep.tables[2].file_error and "missing required column" in rep.tables[2].file_error

    def test_cli_exit_codes(self, gen_dir, tmp_path, capsys):
        db = tmp_path / "cli.db"
        assert load_main(["--dir", str(gen_dir[0]), "--db", str(db)]) == 0
        empty = tmp_path / "empty"
        empty.mkdir()
        assert load_main(["--dir", str(empty), "--db", str(db)]) == 2
        assert load_main(["--dir", str(tmp_path / "missing"), "--db", str(db)]) == 2

    def test_loader_cannot_be_used_for_sql_injection(self, tmp_path):
        d = tmp_path / "inj"
        d.mkdir()
        write_csv(d / "students.csv", "students", [dict(student_id="S4001", full_name="x'); DROP TABLE students;--", programme="B.Tech CSE",
                                                          batch_year=2024, current_semester=5, cgpa=7, active_backlogs=0)])
        rep = load_directory(d, tmp_path / "i.db")
        assert rep.ok
        with da.db_session(tmp_path / "i.db", read_only=True) as conn:
            assert da.get_student(conn, "S4001")["full_name"].startswith("x'); DROP")
            assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 1


# ------------------------------------------------------------------ validator
def run_validate(db, **kw):
    conn = raw_conn(db)
    try:
        return validate.validate_connection(conn, **kw)
    finally:
        conn.close()


def messages(rep, severity="ERROR"):
    return " | ".join(f.message for f in rep.findings if f.severity == severity)


class TestValidator:
    def test_generated_data_passes_with_no_findings(self, loaded_db):
        rep = run_validate(loaded_db)
        assert rep.ok and not rep.findings, messages(rep) + messages(rep, "WARN")
        assert rep.counts["students"] == 36 and len(rep.checks_run) == 9
        for case in ("attendance exactly at threshold", "attendance one class below threshold", "failed course just below pass mark",
                     "ABSENT result", "DETAINED result", "multiple backlogs (>=2)", "CGPA exactly at placement cut-off"):
            assert rep.counts["edge_cases"][case], case

    def test_planted_marks_total_mismatch_is_caught(self, db_copy):
        corrupt(db_copy, "UPDATE results SET total_marks = 99 WHERE student_id='S1001' AND course_code=(SELECT course_code FROM results WHERE student_id='S1001' LIMIT 1)")
        assert "internal+external" in messages(run_validate(db_copy))

    def test_planted_attended_over_held_is_caught(self, db_copy):
        corrupt(db_copy, "UPDATE attendance SET classes_attended = classes_held + 5 WHERE student_id='S1001' AND course_code='CS201'")
        assert "classes_attended" in messages(run_validate(db_copy))

    def test_planted_result_status_inconsistent_with_marks(self, db_copy):
        corrupt(db_copy, "UPDATE results SET result='FAIL' WHERE student_id='S1001' AND result='PASS'")
        assert "meets the 40% pass mark" in messages(run_validate(db_copy))
        corrupt(db_copy, "UPDATE results SET result='PASS' WHERE student_id='S1007' AND course_code='MA201'")
        assert "below the 40% pass mark" in messages(run_validate(db_copy))

    def test_absent_with_marks_is_caught(self, db_copy):
        corrupt(db_copy, "UPDATE results SET total_marks=50, internal_marks=20, external_marks=30 WHERE result='ABSENT'")
        assert "ABSENT must have 0 marks" in messages(run_validate(db_copy))

    def test_reserved_ids_are_errors_in_our_data_but_allowed_for_judges(self, db_copy):
        corrupt(db_copy, "UPDATE students SET student_id='S9001' WHERE student_id='S1036'")
        corrupt(db_copy, "UPDATE courses SET course_code='JDG201' WHERE course_code='MA202'")
        assert "judge-reserved" in messages(run_validate(db_copy)) and "JDG" in messages(run_validate(db_copy))
        assert "judge-reserved" not in messages(run_validate(db_copy, allow_reserved=True))

    def test_bad_student_values_caught(self, db_copy):
        corrupt(db_copy, "UPDATE students SET cgpa=11, current_semester=12, student_id='S12' WHERE student_id='S1005'")
        m = messages(run_validate(db_copy))
        assert "outside 0-10" in m and "outside 1-10" in m and "not S + 4 digits" in m

    def test_orphan_rows_and_foreign_keys_caught(self, db_copy):
        corrupt(db_copy, "INSERT INTO attendance VALUES ('S7777','CS201',10,5)")
        m = messages(run_validate(db_copy))
        assert "unknown student" in m and "violates a foreign key" in m

    def test_cross_programme_attendance_is_caught(self, db_copy):
        corrupt(db_copy, "INSERT INTO attendance VALUES ('S1001','EC201',10,5)")
        assert "course belongs to B.Tech ECE" in messages(run_validate(db_copy))

    def test_rule_without_source_or_clause_or_numeric_value(self, db_copy):
        corrupt(db_copy, "UPDATE rule_registry SET source_doc_id='GHOST' WHERE rule_id='PLC-CGPA-01'")
        corrupt(db_copy, "UPDATE rule_registry SET source_section=' ' WHERE rule_id='PLC-BKLG-01'")
        corrupt(db_copy, "UPDATE rule_registry SET value='many' WHERE rule_id='PASS-MARK-01'")
        m = messages(run_validate(db_copy))
        assert "not in source_register" in m and "no source_section" in m and "not numeric" in m

    def test_rule_with_bad_scope_or_dates(self, db_copy):
        corrupt(db_copy, "UPDATE rule_registry SET scope_batches='whenever' WHERE rule_id='SUPP-ATT-01'")
        corrupt(db_copy, "UPDATE rule_registry SET effective_to='2000-01-01' WHERE rule_id='ATT-MIN-02'")
        m = messages(run_validate(db_copy))
        assert "unrecognised batch scope" in m and "effective_to is before effective_from" in m

    def test_overlapping_rules_are_warned(self, db_copy):
        corrupt(db_copy, "UPDATE rule_registry SET effective_to=NULL WHERE rule_id='ATT-MIN-01'")
        rep = run_validate(db_copy)
        assert "overlap" in messages(rep, "WARN")

    def test_inconsistent_backlog_count_is_a_warning(self, db_copy):
        corrupt(db_copy, "UPDATE students SET active_backlogs=4 WHERE student_id='S1001'")
        rep = run_validate(db_copy)
        assert rep.ok and "S1001" in messages(rep, "WARN")

    @pytest.mark.parametrize("sql,missing", [
        ("DELETE FROM results WHERE result='ABSENT'", "ABSENT result"),
        ("DELETE FROM results WHERE result='DETAINED'", "DETAINED result"),
        ("UPDATE students SET cgpa=cgpa+0.5 WHERE cgpa=6.5", "CGPA exactly at placement cut-off"),
        ("UPDATE attendance SET classes_attended=classes_attended-1 WHERE classes_attended*100 = 80*classes_held", "attendance exactly at threshold"),
    ])
    def test_missing_required_edge_case_is_an_error(self, db_copy, sql, missing):
        corrupt(db_copy, sql)
        assert f"required edge case missing: {missing}" in messages(run_validate(db_copy))

    def test_too_small_dataset_is_an_error(self, db_copy):
        corrupt(db_copy, "DELETE FROM students WHERE student_id > 'S1020'")
        assert "only 20 students" in messages(run_validate(db_copy))

    def test_thresholds_come_from_the_registry_not_from_the_validator(self, db_copy):
        """Change the attendance rule to 60%: the 'exactly at threshold' case must now be found at 60%."""
        corrupt(db_copy, "UPDATE rule_registry SET value='60' WHERE rule_id='ATT-MIN-02'")
        rep = run_validate(db_copy)
        assert "S1001" not in rep.counts["edge_cases"].get("attendance exactly at threshold", [])

    def test_cli_exit_codes_and_report(self, loaded_db, db_copy, capsys):
        assert validate.main(["--db", str(loaded_db)]) == 0
        out = capsys.readouterr().out
        assert "RESULT: PASSED" in out and "0 error(s)" in out
        corrupt(db_copy, "UPDATE results SET total_marks = total_marks + 1 WHERE rowid = 1")
        assert validate.main(["--db", str(db_copy)]) == 1
        assert "RESULT: FAILED" in capsys.readouterr().out
        assert validate.main(["--db", str(loaded_db.parent / "missing.db")]) == 2
        assert validate.main(["--db", str(loaded_db), "--as-of-date", "06/10/2026"]) == 2

    def test_cli_validates_a_csv_directory_and_reports_loader_rejections(self, gen_dir, tmp_path, capsys):
        assert validate.main(["--dir", str(gen_dir[0])]) == 0
        bad = tmp_path / "bad"
        shutil.copytree(gen_dir[0], bad)
        rows = read_csv(bad / "attendance.csv")
        rows[0]["classes_attended"] = str(int(rows[0]["classes_held"]) + 3)
        write_csv(bad / "attendance.csv", "attendance", rows)
        assert validate.main(["--dir", str(bad)]) == 1
        assert "attendance.csv line 2" in capsys.readouterr().out
