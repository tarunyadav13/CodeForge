"""API: identity, authorization, /ask on real tools, persistent audit, health, sources, ingest hooks."""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from _m2_helpers import *  # noqa: F401,F403
from _m2_helpers import AS_OF, corrupt, raw_conn
from api import personal
from api.main import create_app
from api.models import NOT_FOUND_MESSAGE, AskResponse, AuditRecord, HealthResponse, SourceRecord
from db import data_access as da
from tools import _common

H = lambda sid: {"X-Student-Id": sid}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ("OPENAI_API_KEY", "MOCK_LLM", "OPENAI_MODEL", "AUDIT_ADMIN_TOKEN", "INGEST_MAX_BYTES"):
        monkeypatch.delenv(v, raising=False)
    _common.set_policy_resolver(None)


@pytest.fixture()
def client(db_copy, tmp_path):
    app = create_app(db_copy, graph_runner=None, ingest_handler=None, vector_health=None, upload_dir=tmp_path / "uploads")
    with TestClient(app) as c:
        c.db = db_copy
        yield c


def make_client(db, tmp_path, **hooks):
    app = create_app(db, upload_dir=tmp_path / "uploads", **{"graph_runner": None, "ingest_handler": None, "vector_health": None, **hooks})
    return TestClient(app)


def ask(c, q, sid=None, **body):
    return c.post("/ask", json={"question": q, "as_of_date": AS_OF, **body}, headers=H(sid) if sid else {})


# ------------------------------------------------------------------ /ask on real tools
class TestAskPersonal:
    def test_attendance_is_calculated_by_the_tool(self, client):
        r = ask(client, "What is my attendance in Data Structures?", "S1002")
        assert r.status_code == 200
        body = AskResponse.model_validate(r.json())  # frozen contract
        assert body.answer_type == "calculated" and "77.5%" in body.answer and "31 of 40" in body.answer
        assert body.tools_invoked[0].tool == "get_attendance" and body.tools_invoked[0].output["classes_attended"] == 31
        assert body.tools_invoked[0].input["student_id"] == "S1002" and body.as_of_date == date(2026, 10, 6)

    @pytest.mark.parametrize("sid,verdict", [("S1001", "Yes, you are eligible"), ("S1002", "No, you are not eligible")])
    def test_exam_eligibility_at_the_boundary(self, client, sid, verdict):
        r = ask(client, "Am I eligible to appear in the end-semester exam for CS201?", sid).json()
        assert r["answer_type"] == "calculated" and r["answer"].startswith(verdict)
        assert r["applied_rules"] == [{"rule_id": "ATT-MIN-02", "value": ">= 80", "source_doc_id": "FIX-ACAD-CIRC-2026-08"}]
        assert r["citations"][0]["doc_id"] == "FIX-ACAD-CIRC-2026-08" and r["citations"][0]["section"] == "1"
        assert r["citations"][0]["version"] == "1.0" and r["citations"][0]["effective_from"] == "2026-08-01"
        assert [t["tool"] for t in r["tools_invoked"]] == ["check_exam_eligibility"]

    def test_old_rule_and_upcoming_change_are_explained_for_an_earlier_date(self, client):
        r = ask(client, "Can I appear for the CS201 exam, is my attendance enough?", "S1003", as_of_date="2026-06-01").json()
        assert r["answer"].startswith("Yes") and r["applied_rules"][0]["rule_id"] == "ATT-MIN-01"
        assert "Upcoming change: ATT-MIN-02" in r["answer"] and "2026-08-01" in r["answer"]

    def test_supplementary_by_course_name(self, client):
        r = ask(client, "Am I eligible for the supplementary exam in Mathematics?", "S1007").json()
        assert r["answer_type"] == "calculated" and r["answer"].startswith("Yes") and r["tools_invoked"][0]["input"]["course_code"] == "MA201"

    def test_multi_step_what_if_states_assumptions_and_changes_nothing(self, client):
        before = open(client.db, "rb").read()
        r = ask(client, "I failed Data Structures. If I pass the supplementary, will I be eligible for placement?", "S1008").json()
        assert r["tools_invoked"][0]["tool"] == "simulate_what_if" and r["tools_invoked"][0]["output"]["modifies_records"] is False
        assert "Assumptions" in r["answer"] and "simulation" in r["answer"] and "CS201" in r["answer"]
        r2 = ask(client, "If I pass the supplementary in Mathematics will I be eligible for placement?", "S1023").json()
        assert "Today: not eligible" in r2["answer"] and "Under that scenario: eligible" in r2["answer"]
        assert sorted(a for a in map(lambda x: x["tool"], r2["tools_invoked"])) == ["simulate_what_if"]
        conn = raw_conn(client.db)
        assert conn.execute("SELECT active_backlogs FROM students WHERE student_id='S1023'").fetchone()[0] == 1
        conn.close()

    def test_placement_backlogs_results_and_profile(self, client):
        assert "Yes, you are eligible" in ask(client, "Am I eligible for placements?", "S1011").json()["answer"]
        assert "No, you are not eligible" in ask(client, "Am I eligible for campus placement?", "S1012").json()["answer"]
        b = ask(client, "How many backlogs do I have?", "S1008").json()
        assert b["answer_type"] == "calculated" and "3 active backlog" in b["answer"]
        assert "39/100 - FAIL" in ask(client, "What was my result in MA201 in 2026 May?", "S1007").json()["answer"]
        assert "6.5" in ask(client, "What is my CGPA?", "S1011").json()["answer"]

    def test_ambiguous_question_asks_for_clarification(self, client):
        r = ask(client, "What is my attendance?", "S1001").json()
        assert r["answer_type"] == "clarification_needed" and "Which course" in r["answer"] and r["tools_invoked"] == []
        r = ask(client, "What is my attendance in systems?", "S1001").json()  # matches several courses
        assert r["answer_type"] == "clarification_needed"

    def test_missing_record_and_unknown_course_return_the_exact_not_found_text(self, client):
        r = ask(client, "What is my attendance in CS201?", "S1009").json()
        assert r["answer_type"] == "not_found" and r["answer"] == NOT_FOUND_MESSAGE and "No attendance record" in r["explanation"]
        r = ask(client, "What are my results in ZZ999?", "S1001").json()
        assert r["answer_type"] == "not_found" and r["answer"] == NOT_FOUND_MESSAGE and "ZZ999" in r["explanation"] and r["tools_invoked"] == []
        r = ask(client, "What is my attendance in Antarctica studies?", "S1001").json()
        assert r["answer_type"] == "clarification_needed"

    def test_conflicting_rules_yield_conflict_flagged_with_both_cited(self, client):
        corrupt(client.db, "UPDATE rule_registry SET effective_to=NULL WHERE rule_id='ATT-MIN-01'")
        r = ask(client, "Am I eligible to appear in the CS201 exam?", "S1001").json()
        assert r["answer_type"] == "conflict_flagged" and "contact the issuing office" in r["answer"]
        assert {c["doc_id"] for c in r["citations"]} == {"FIX-ACAD-REG-2024", "FIX-ACAD-CIRC-2026-08"}
        assert r["conflicts_detected"][0]["candidates"] == ["ATT-MIN-02", "ATT-MIN-01"] or set(r["conflicts_detected"][0]["candidates"]) == {"ATT-MIN-01", "ATT-MIN-02"}

    def test_resolver_hook_resolves_the_same_conflict(self, client):
        corrupt(client.db, "UPDATE rule_registry SET effective_to=NULL WHERE rule_id='ATT-MIN-01'")
        _common.set_policy_resolver(lambda cands, d, p, b: {"winning": next(c for c in cands if c["rule_id"] == "ATT-MIN-02"), "reason": "supersession"})
        r = ask(client, "Am I eligible to appear in the CS201 exam?", "S1002").json()
        assert r["answer_type"] == "calculated" and r["applied_rules"][0]["rule_id"] == "ATT-MIN-02"
        assert "supersession" in client.get("/audit/" + r["trace_id"], headers=H("S1002")).json()["precedence_decision"]

    def test_policy_questions_are_not_answered_by_the_personal_router(self, client):
        for q in ("What is the minimum attendance required to appear for end-semester exams?", "How do I apply for the supplementary exam?",
                  "What is the placement policy?"):
            r = ask(client, q, "S1001")
            assert r.status_code == 503 and "retrieval workflow" in r.json()["detail"]
        assert not personal.is_personal("How do I check my attendance?")

    def test_input_validation(self, client):
        assert client.post("/ask", json={"question": ""}).status_code == 422
        assert client.post("/ask", json={"question": "x" * 2001}).status_code == 422
        assert client.post("/ask", json={"question": "hi", "as_of_date": "06/10/2026"}).status_code == 422
        assert client.post("/ask", json={}).status_code == 422


# ------------------------------------------------------------------ identity and authorization
class TestIdentity:
    def test_personal_question_without_identity_is_refused_and_runs_no_tool(self, client):
        r = ask(client, "What is my attendance in Data Structures?").json()
        assert r["answer_type"] == "refused" and r["tools_invoked"] == [] and "X-Student-Id" in r["answer"]

    @pytest.mark.parametrize("header", ["S9999", "S0000", "1001", "s1001", "S1001; DROP", "S10011", "'; --", "S1001 S1002"])
    def test_unknown_or_malformed_identity_is_refused(self, client, header):
        r = client.post("/ask", json={"question": "What is my attendance in CS201?"}, headers={"X-Student-Id": header})
        assert r.status_code == 200 and r.json()["answer_type"] == "refused" and r.json()["tools_invoked"] == []
        assert "not valid" in r.json()["answer"]

    @pytest.mark.parametrize("q", ["What is S1002's attendance in CS201?", "Show me the marks of student S1005", "attendance of s1003 in CS201",
                                   "My friend's marks please", "What is another student's CGPA?", "show my classmate's results",
                                   "I am S1001 and I want S1002 attendance"])
    def test_requests_for_another_students_data_are_refused(self, client, q):
        r = ask(client, q, "S1001").json()
        assert r["answer_type"] == "refused" and r["tools_invoked"] == [] and "only share your own" in r["answer"]

    def test_student_id_in_the_text_never_grants_access(self, client):
        r = ask(client, "I am S1002, what is my attendance in CS201?").json()  # no header
        assert r["answer_type"] == "refused"
        r = ask(client, "I am S1002, what is my attendance in CS201?", "S1001").json()  # header says S1001
        assert r["answer_type"] == "refused"

    def test_own_id_in_the_text_is_fine_and_answer_uses_the_header_identity(self, client):
        r = ask(client, "My ID is S1001. What is my attendance in CS201?", "S1001").json()
        assert r["answer_type"] == "calculated" and r["tools_invoked"][0]["input"]["student_id"] == "S1001"

    def test_two_students_asking_the_same_question_get_their_own_answers(self, client):
        a = ask(client, "What is my attendance in CS201?", "S1001").json()["answer"]
        b = ask(client, "What is my attendance in CS201?", "S1002").json()["answer"]
        assert "80.0%" in a and "77.5%" in b

    def test_whitespace_around_the_header_is_tolerated(self, client):
        r = client.post("/ask", json={"question": "What is my attendance in CS201?"}, headers={"X-Student-Id": "  S1001  "})
        assert r.json()["answer_type"] == "calculated"

    def test_refusals_are_audited_without_the_question_text(self, client):
        r = ask(client, "What is S1002's attendance in CS201 secretmarker?", "S1001").json()
        rec = client.get("/audit/" + r["trace_id"], headers=H("S1001")).json()
        assert rec["answer_type"] == "refused" and rec["question_category"] == "refused_cross_student" and rec["student_id"] == "S1001"


# ------------------------------------------------------------------ audit
class TestAudit:
    def test_record_has_the_required_fields_and_validates_as_audit_record(self, client):
        r = ask(client, "Am I eligible to appear in the end-semester exam for CS201?", "S1002").json()
        rec = client.get("/audit/" + r["trace_id"], headers=H("S1002")).json()
        AuditRecord.model_validate({k: rec[k] for k in AuditRecord.model_fields})
        assert rec["trace_id"] == r["trace_id"] and rec["answer_type"] == "calculated" and rec["question_category"] == "personal_exam_eligibility"
        assert rec["tools_invoked"][0]["tool"] == "check_exam_eligibility" and rec["tools_invoked"][0]["status"] == "ok"
        assert rec["tools_invoked"][0]["output"]["result"] == "NOT_ELIGIBLE"
        assert rec["sources_retrieved"] == [{"doc_id": "FIX-ACAD-CIRC-2026-08", "section": "1", "score": None}]
        assert rec["applied_rules"][0]["rule_id"] == "ATT-MIN-02" and rec["as_of_date"] == AS_OF
        assert rec["llm_calls"] == 0 and rec["tokens"] == 0 and rec["latency_ms"] >= 0 and "deterministic" in rec["model"]
        assert rec["precedence_decision"] and rec["timestamp"].endswith("Z")

    def test_no_raw_question_text_or_name_is_stored_anywhere(self, client):
        marker = "zebra-unique-marker-42"
        ask(client, f"What is my attendance in Data Structures {marker}?", "S1001")
        ask(client, f"Am I eligible for placement? {marker}", "S1011")
        ask(client, f"What is S1002's marks {marker}", "S1001")
        dump = "\n".join(raw_conn(client.db).iterdump())
        assert marker not in dump
        audit_rows = raw_conn(client.db).execute("SELECT * FROM audit").fetchall()
        name = raw_conn(client.db).execute("SELECT full_name FROM students WHERE student_id='S1011'").fetchone()[0]
        assert audit_rows and name not in json.dumps([dict(r) for r in audit_rows])

    def test_records_persist_across_app_restarts(self, db_copy, tmp_path):
        with make_client(db_copy, tmp_path) as first:
            trace = ask(first, "What is my attendance in CS201?", "S1001").json()["trace_id"]
        with make_client(db_copy, tmp_path) as second:  # a brand-new app instance, same database file
            rec = second.get("/audit/" + trace, headers=H("S1001"))
            assert rec.status_code == 200 and rec.json()["answer_type"] == "calculated"

    def test_access_control(self, client, monkeypatch):
        trace = ask(client, "What is my attendance in CS201?", "S1001").json()["trace_id"]
        assert client.get("/audit/" + trace, headers=H("S1001")).status_code == 200
        assert client.get("/audit/" + trace, headers=H("S1002")).status_code == 404   # another student: indistinguishable from absent
        assert client.get("/audit/" + trace).status_code == 404                        # anonymous
        assert client.get("/audit/" + trace, headers={"X-Admin-Token": "guess"}).status_code == 404
        monkeypatch.setenv("AUDIT_ADMIN_TOKEN", "s3cret-admin")
        assert client.get("/audit/" + trace, headers={"X-Admin-Token": "s3cret-admin"}).status_code == 200
        assert client.get("/audit/" + trace, headers={"X-Admin-Token": "wrong"}).status_code == 404
        assert client.get("/audit/does-not-exist", headers=H("S1001")).status_code == 404
        assert client.get("/audit/..%2F..%2Fetc").status_code == 404

    def test_anonymous_refusal_is_readable_with_its_trace_id(self, client):
        trace = ask(client, "What is my attendance in CS201?").json()["trace_id"]
        rec = client.get("/audit/" + trace)
        assert rec.status_code == 200 and rec.json()["student_id"] is None and rec.json()["question_category"] == "refused_no_identity"

    def test_trace_ids_are_unique_per_response(self, client):
        ids = {ask(client, "What is my attendance in CS201?", "S1001").json()["trace_id"] for _ in range(20)}
        assert len(ids) == 20

    def test_a_failed_audit_write_withholds_the_answer(self, client):
        corrupt(client.db, "DROP TABLE audit")
        r = ask(client, "What is my attendance in CS201?", "S1001")
        assert r.status_code == 503 and "withheld" in r.json()["detail"] and "80.0" not in r.text


# ------------------------------------------------------------------ health
class TestHealth:
    def test_defaults_report_what_is_actually_unavailable(self, client):
        h = client.get("/health").json()
        HealthResponse.model_validate(h)
        assert h["status"] == "degraded" and h["api"]["status"] == "ok"
        assert h["sqlite"] == {"status": "ok", "detail": "36 students loaded"}
        assert h["vector_store"]["status"] == "not_configured" and h["llm"]["status"] == "not_configured"

    def test_api_key_present_is_not_reported_as_available(self, client, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-notreal")
        llm = client.get("/health").json()["llm"]
        assert llm["status"] == "degraded" and "NOT verified" in llm["detail"] and "sk-test" not in json.dumps(llm)

    def test_mock_mode_is_labelled_as_mock(self, client, monkeypatch):
        monkeypatch.setenv("MOCK_LLM", "true")
        llm = client.get("/health").json()["llm"]
        assert llm["status"] == "degraded" and "mock" in llm["detail"].lower()

    def test_live_check_makes_one_real_style_call_and_reports_the_result(self, client, monkeypatch):
        import llm.openai_client as oc
        calls = []

        class Fake:
            def __init__(self, cfg): pass
            def generate_text(self, s, u, max_output_tokens=None):
                calls.append(max_output_tokens)
                return oc.LLMResult("OK", "gpt-6-luna", 1, 1, 1, 2, 12)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-notreal")
        monkeypatch.setattr(oc, "LLMClient", Fake)
        assert calls == [] and client.get("/health").json()["llm"]["status"] == "degraded"   # no live call without ?live=true
        llm = client.get("/health?live=true").json()["llm"]
        assert llm["status"] == "ok" and "live request succeeded" in llm["detail"] and calls == [16]

    def test_live_check_failure_is_down_and_sanitised(self, client, monkeypatch):
        import llm.openai_client as oc

        class Broken:
            def __init__(self, cfg): pass
            def generate_text(self, *a, **k): raise oc.LLMAPIError("The LLM provider rejected the API key (authentication failed)", 401)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-notreal")
        monkeypatch.setattr(oc, "LLMClient", Broken)
        llm = client.get("/health?live=true").json()["llm"]
        assert llm["status"] == "down" and "llm_api_error" in llm["detail"] and "sk-" not in llm["detail"]

    def test_invalid_llm_settings_are_down(self, client, monkeypatch):
        monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "banana")
        assert client.get("/health").json()["llm"]["status"] == "down"

    def test_everything_ok_only_when_every_component_is_ok(self, db_copy, tmp_path, monkeypatch):
        import llm.openai_client as oc
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-notreal")
        monkeypatch.setattr(oc, "LLMClient", lambda cfg: type("F", (), {"generate_text": lambda s, *a, **k: oc.LLMResult("OK", "m", 1, 0, 0, 0, 1)})())
        with make_client(db_copy, tmp_path, vector_health=lambda: {"status": "ok", "detail": "3 docs"}) as c:
            h = c.get("/health?live=true").json()
            assert h["status"] == "ok" and h["vector_store"]["detail"] == "3 docs"

    def test_broken_vector_probe_is_down_not_ok(self, db_copy, tmp_path):
        def boom(): raise RuntimeError("chroma exploded")
        with make_client(db_copy, tmp_path, vector_health=boom) as c:
            assert c.get("/health").json()["vector_store"]["status"] == "down"

    def test_missing_database_is_reported_down(self, tmp_path):
        app = create_app(tmp_path / "nowhere" / "none.db", graph_runner=None, ingest_handler=None, vector_health=None)
        c = TestClient(app)  # no lifespan: the database is never created
        h = c.get("/health").json()
        assert h["status"] == "down" and h["sqlite"]["status"] == "down" and h["api"]["status"] == "ok"

    def test_database_missing_tables_is_down(self, tmp_path):
        path = tmp_path / "bare.db"
        sqlite3.connect(path).close()
        h = TestClient(create_app(path, graph_runner=None, ingest_handler=None, vector_health=None)).get("/health").json()
        assert h["sqlite"]["status"] == "down" and "missing tables" in h["sqlite"]["detail"]


# ------------------------------------------------------------------ sources and ingest
META = {"doc_id": "NEW-CIRC-2026-11", "title": "New Circular", "issuer": "Registrar", "authority_level": 2, "doc_type": "circular",
        "version": "1.0", "effective_from": "2026-11-01", "supersedes": "", "scope_programmes": "ALL", "scope_batches": "ALL",
        "provenance": "test", "retrieved_on": "2026-10-23", "synthetic": "Y"}


def post_ingest(c, meta=META, content=b"Attendance must be 90%.", name="circ.txt", meta_as_file=False):
    files = {"file": (name, content, "text/plain")}
    data = {}
    if meta_as_file:
        files["metadata"] = ("meta.json", json.dumps(meta), "application/json")
    else:
        data["metadata"] = json.dumps(meta)
    return c.post("/ingest", files=files, data=data)


class TestSourcesAndIngest:
    def test_sources_lists_the_register(self, client):
        rows = client.get("/sources").json()
        assert {r["doc_id"] for r in rows} == {"FIX-ACAD-REG-2024", "FIX-ACAD-CIRC-2026-08", "FIX-PLACEMENT-2024"}
        for r in rows:
            SourceRecord.model_validate(r)
        circ = next(r for r in rows if r["doc_id"] == "FIX-ACAD-CIRC-2026-08")
        assert circ["authority_level"] == 2 and circ["supersedes"] == "FIX-ACAD-REG-2024#7.2" and circ["synthetic"] == "Y"

    def test_ingest_is_honestly_unavailable_without_the_pipeline(self, client):
        r = post_ingest(client)
        assert r.status_code == 501 and "nothing was ingested" in r.json()["detail"]
        assert "NEW-CIRC-2026-11" not in {s["doc_id"] for s in client.get("/sources").json()}

    def test_successful_ingest_registers_the_source_and_calls_the_pipeline(self, db_copy, tmp_path):
        seen = {}

        def handler(path, meta):
            seen.update(path=path, meta=meta, text=open(path, "rb").read())
            return {"chunks_indexed": 4, "status": "ingested"}
        with make_client(db_copy, tmp_path, ingest_handler=handler) as c:
            r = post_ingest(c)
            assert r.status_code == 200 and r.json() == {"doc_id": "NEW-CIRC-2026-11", "chunks_indexed": 4, "status": "ingested"}
            assert seen["meta"]["doc_id"] == "NEW-CIRC-2026-11" and seen["meta"]["authority_level"] == 2 and seen["text"] == b"Attendance must be 90%."
            src = {s["doc_id"]: s for s in c.get("/sources").json()}["NEW-CIRC-2026-11"]   # visible immediately, no restart
            assert src["effective_from"] == "2026-11-01" and src["synthetic"] == "Y"
            row = raw_conn(db_copy).execute("SELECT chunks_indexed, content_sha256 FROM source_register WHERE doc_id='NEW-CIRC-2026-11'").fetchone()
            assert row[0] == 4 and len(row[1]) == 64
            assert raw_conn(db_copy).execute("SELECT COUNT(*) FROM rule_registry").fetchone()[0] == 6   # ingest never edits the rule registry

    def test_metadata_may_arrive_as_a_json_file_part(self, db_copy, tmp_path):
        with make_client(db_copy, tmp_path, ingest_handler=lambda p, m: {"chunks_indexed": 2, "status": "ok"}) as c:
            assert post_ingest(c, meta_as_file=True).status_code == 200

    def test_same_file_twice_is_not_reingested(self, db_copy, tmp_path):
        calls = []
        with make_client(db_copy, tmp_path, ingest_handler=lambda p, m: calls.append(1) or {"chunks_indexed": 3, "status": "ingested"}) as c:
            post_ingest(c)
            again = post_ingest(c)
            assert again.json()["status"] == "already_ingested" and again.json()["chunks_indexed"] == 3 and len(calls) == 1
            changed = post_ingest(c, content=b"Attendance must be 95%.")
            assert changed.json()["status"] == "ingested" and len(calls) == 2

    @pytest.mark.parametrize("meta", [{**META, "authority_level": 9}, {**META, "doc_type": "memo"}, {**META, "effective_from": "soon"},
                                      {**META, "surprise": 1}, {k: v for k, v in META.items() if k != "provenance"}])
    def test_invalid_metadata_is_rejected_with_field_errors(self, db_copy, tmp_path, meta):
        with make_client(db_copy, tmp_path, ingest_handler=lambda p, m: pytest.fail("handler must not run")) as c:
            r = post_ingest(c, meta=meta)
            assert r.status_code == 422 and r.json()["detail"]["metadata_errors"]

    def test_metadata_not_json_and_missing_parts(self, db_copy, tmp_path):
        with make_client(db_copy, tmp_path, ingest_handler=lambda p, m: pytest.fail("no")) as c:
            assert c.post("/ingest", files={"file": ("a.txt", b"x")}, data={"metadata": "not json"}).status_code == 422
            assert c.post("/ingest", files={"file": ("a.txt", b"x")}).status_code == 422
            assert c.post("/ingest", data={"metadata": json.dumps(META)}).status_code == 422

    def test_file_validation(self, db_copy, tmp_path, monkeypatch):
        with make_client(db_copy, tmp_path, ingest_handler=lambda p, m: pytest.fail("handler must not run")) as c:
            assert post_ingest(c, name="evil.exe").status_code == 415
            assert post_ingest(c, name="noext").status_code == 415
            assert post_ingest(c, content=b"").status_code == 422
            monkeypatch.setenv("INGEST_MAX_BYTES", "10")
            assert post_ingest(c, content=b"x" * 11).status_code == 413

    def test_path_traversal_in_filename_is_neutralised(self, db_copy, tmp_path):
        seen = {}
        with make_client(db_copy, tmp_path, ingest_handler=lambda p, m: seen.update(p=p) or {"chunks_indexed": 1, "status": "ok"}) as c:
            assert post_ingest(c, name="../../../etc/passwd.txt").status_code == 200
        stored = tmp_path / "uploads"
        assert seen["p"].startswith(str(stored)) and ".." not in seen["p"].replace(str(stored), "")

    def test_handler_failure_registers_nothing(self, db_copy, tmp_path):
        def boom(p, m): raise RuntimeError("embedding model missing")
        with make_client(db_copy, tmp_path, ingest_handler=boom) as c:
            r = post_ingest(c)
            assert r.status_code == 500 and "not registered" in r.json()["detail"] and "embedding" not in r.text
            assert "NEW-CIRC-2026-11" not in {s["doc_id"] for s in c.get("/sources").json()}

    def test_zero_chunks_is_not_reported_as_success(self, db_copy, tmp_path):
        with make_client(db_copy, tmp_path, ingest_handler=lambda p, m: {"chunks_indexed": 0, "status": "empty"}) as c:
            assert post_ingest(c).status_code == 422
            assert "NEW-CIRC-2026-11" not in {s["doc_id"] for s in c.get("/sources").json()}


# ------------------------------------------------------------------ Member 1 graph hook
class TestGraphHook:
    def canned(self, trace="graphtrace001", answer_type="retrieved_fact"):
        return AskResponse(trace_id=trace, answer="80% [ACAD-2026-08 s1]", answer_type=answer_type, as_of_date=date(2026, 10, 6))

    def test_graph_answers_are_returned_and_audited_with_its_extras(self, db_copy, tmp_path):
        calls = []

        def runner(q, sid, as_of):
            calls.append((q, sid, as_of))
            return self.canned(), {"question_category": "policy_fact", "model": "gpt-6-luna", "llm_calls": 2, "tokens": 900,
                                   "sources_retrieved": [{"doc_id": "ACAD-2026-08", "section": "1", "score": 0.8}], "precedence_decision": "step 2"}
        with make_client(db_copy, tmp_path, graph_runner=runner) as c:
            r = ask(c, "What is the minimum attendance?", "S1001").json()
            assert r["trace_id"] == "graphtrace001" and r["answer_type"] == "retrieved_fact"
            assert calls == [("What is the minimum attendance?", "S1001", date(2026, 10, 6))]
            rec = c.get("/audit/graphtrace001", headers=H("S1001")).json()
            assert rec["model"] == "gpt-6-luna" and rec["llm_calls"] == 2 and rec["tokens"] == 900 and rec["question_category"] == "policy_fact"
            assert rec["sources_retrieved"][0]["doc_id"] == "ACAD-2026-08" and rec["precedence_decision"] == "step 2"

    def test_anonymous_questions_reach_the_graph_with_no_student(self, db_copy, tmp_path):
        got = []
        with make_client(db_copy, tmp_path, graph_runner=lambda q, s, d: got.append(s) or self.canned("t2")) as c:
            assert ask(c, "What is the minimum attendance?").status_code == 200 and got == [None]

    def test_graph_never_runs_for_cross_student_or_bad_identity(self, db_copy, tmp_path):
        with make_client(db_copy, tmp_path, graph_runner=lambda *a: pytest.fail("graph must not run")) as c:
            assert ask(c, "What is S1002's attendance?", "S1001").json()["answer_type"] == "refused"
            assert ask(c, "What is my attendance?", "S9999").json()["answer_type"] == "refused"

    def test_graph_that_audits_itself_is_not_audited_twice(self, db_copy, tmp_path):
        def runner(q, sid, as_of):
            with da.db_session(db_copy) as conn:
                da.write_audit(conn, {"trace_id": "selfaudit01", "student_id": sid, "question_category": "x", "answer_type": "not_found", "model": "m"})
            return AskResponse(trace_id="selfaudit01", answer=NOT_FOUND_MESSAGE, answer_type="not_found", as_of_date=date(2026, 10, 6))
        with make_client(db_copy, tmp_path, graph_runner=runner) as c:
            assert ask(c, "Antarctica scholarship?", "S1001").status_code == 200
            assert raw_conn(db_copy).execute("SELECT COUNT(*) FROM audit WHERE trace_id='selfaudit01'").fetchone()[0] == 1

    def test_graph_may_return_a_plain_dict(self, db_copy, tmp_path):
        with make_client(db_copy, tmp_path, graph_runner=lambda q, s, d: self.canned("dicttrace01").model_dump(mode="json")) as c:
            assert ask(c, "What is the fee?", "S1001").json()["trace_id"] == "dicttrace01"

    @pytest.mark.parametrize("bad", [lambda *a: (_ for _ in ()).throw(RuntimeError("secret internal detail")), lambda *a: {"answer": "no type"}])
    def test_graph_failure_or_contract_violation_returns_502_without_leaking(self, db_copy, tmp_path, bad):
        with make_client(db_copy, tmp_path, graph_runner=bad) as c:
            r = ask(c, "What is the fee?", "S1001")
            assert r.status_code == 502 and "secret" not in r.text and "no answer was generated" in r.json()["detail"]
