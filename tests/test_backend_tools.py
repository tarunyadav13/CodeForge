"""Deterministic student tools: calculations, boundaries, rule provenance, what-if, authorization."""
from __future__ import annotations

import hashlib
from fractions import Fraction

import pytest

from _m2_helpers import *  # noqa: F401,F403
from _m2_helpers import AS_OF, corrupt, raw_conn
from db import data_access as da
import tools
from tools import _common
from tools.registry import AccessDeniedError, TOOLS, UnknownToolError, call_tool, tool_specs


@pytest.fixture(autouse=True)
def _no_resolver():
    _common.set_policy_resolver(None)
    yield
    _common.set_policy_resolver(None)


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def add_rule(db, rule_id, parameter, operator, value, *, programmes="ALL", batches="ALL", frm="2024-07-01", to=None,
             doc="FIX-ACAD-REG-2024", section="9"):
    with da.db_session(db) as conn:
        da.upsert_rule(conn, dict(rule_id=rule_id, description=f"{rule_id} test rule", parameter=parameter, operator=operator, value=value,
                                  scope_programmes=programmes, scope_batches=batches, effective_from=frm, effective_to=to,
                                  source_doc_id=doc, source_section=section))


# ------------------------------------------------------------------ lookups
class TestAttendance:
    def test_percentage_is_computed_from_counts(self, loaded_db):
        r = tools.get_attendance("S1001", "CS201", db_path=loaded_db)
        assert (r["classes_held"], r["classes_attended"], r["attendance_pct"], r["classes_missed"]) == (40, 32, 80.0, 8)

    @pytest.mark.parametrize("sid,expected", [("S1002", 77.5), ("S1003", 75.0), ("S1004", 72.5), ("S1005", 80.0), ("S1006", 77.78)])
    def test_boundary_percentages(self, loaded_db, sid, expected):
        assert tools.get_attendance(sid, "CS201", db_path=loaded_db)["attendance_pct"] == expected

    def test_course_code_is_case_insensitive(self, loaded_db):
        assert tools.get_attendance("S1001", "cs201", db_path=loaded_db)["course_code"] == "CS201"

    def test_missing_record_is_reported_not_invented(self, loaded_db):
        r = tools.get_attendance("S1009", "CS201", db_path=loaded_db)
        assert r["status"] == "no_record" and r["attendance_pct"] is None and r["classes_held"] is None

    def test_unknown_student_and_course_raise_controlled_errors(self, loaded_db):
        with pytest.raises(da.StudentNotFoundError):
            tools.get_attendance("S4242", "CS201", db_path=loaded_db)
        with pytest.raises(da.CourseNotFoundError):
            tools.get_attendance("S1001", "ZZ999", db_path=loaded_db)

    @pytest.mark.parametrize("sid,course", [("1001", "CS201"), ("S1001", ""), ("S1001", "CS201;DROP"), (None, "CS201"), ("S1001", None)])
    def test_invalid_inputs_raise_invalid_input(self, loaded_db, sid, course):
        with pytest.raises(da.InvalidInputError):
            tools.get_attendance(sid, course, db_path=loaded_db)

    def test_percentage_follows_the_stored_counts(self, db_copy):
        with da.db_session(db_copy) as conn:
            conn.execute("UPDATE attendance SET classes_attended=36, classes_held=45 WHERE student_id='S1001' AND course_code='CS201'")
        assert tools.get_attendance("S1001", "CS201", db_path=db_copy)["attendance_pct"] == 80.0
        with da.db_session(db_copy) as conn:
            conn.execute("UPDATE attendance SET classes_attended=1, classes_held=3 WHERE student_id='S1001' AND course_code='CS201'")
        assert tools.get_attendance("S1001", "CS201", db_path=db_copy)["attendance_pct"] == 33.33


class TestResultsAndBacklogs:
    def test_results_for_all_courses_and_one_course(self, loaded_db):
        assert len(tools.get_results("S1019", db_path=loaded_db)) == 5
        rows = tools.get_results("S1019", "MA202", db_path=loaded_db)
        assert [r["result"] for r in rows] == ["FAIL"] * 4 and rows[0]["exam_type"] == "REGULAR"

    def test_no_results_is_an_empty_list(self, loaded_db):
        assert tools.get_results("S1013", db_path=loaded_db) == []

    def test_exam_result_by_session(self, loaded_db):
        r = tools.get_exam_result("S1007", "MA201", "2026-MAY", db_path=loaded_db)
        assert r["status"] == "ok" and r["results"][0]["total_marks"] == 39 and r["results"][0]["result"] == "FAIL"
        assert tools.get_exam_result("S1007", "MA201", "2030-MAY", db_path=loaded_db)["status"] == "no_record"
        assert tools.get_exam_result("S1007", "MA201", "2026-may", db_path=loaded_db)["status"] == "ok"

    def test_absent_and_detained_results_are_visible(self, loaded_db):
        assert tools.get_exam_result("S1010", "CS202", "2026-MAY", db_path=loaded_db)["results"][0]["result"] == "ABSENT"
        assert tools.get_exam_result("S1022", "EC202", "2026-MAY", db_path=loaded_db)["results"][0]["result"] == "DETAINED"

    def test_backlogs(self, loaded_db):
        r = tools.get_backlogs("S1008", db_path=loaded_db)
        assert r["active_backlogs"] == 3 and r["derived_count"] == 3 and r["consistent"]
        assert {c["course_code"] for c in r["courses"]} == {"CS201", "MA201", "CS202"}
        assert tools.get_backlogs("S1001", db_path=loaded_db)["courses"] == []

    def test_inconsistent_backlog_data_is_flagged_not_hidden(self, db_copy):
        with da.db_session(db_copy) as conn:
            conn.execute("UPDATE students SET active_backlogs=5 WHERE student_id='S1001'")
        r = tools.get_backlogs("S1001", db_path=db_copy)
        assert r["active_backlogs"] == 5 and r["derived_count"] == 0 and r["consistent"] is False

    def test_profile(self, loaded_db):
        p = tools.get_student_profile("S1011", db_path=loaded_db)
        assert p["cgpa"] == 6.5 and p["programme"] == "B.Tech CSE" and p["batch_year"] == 2025


# ------------------------------------------------------------------ exam eligibility
class TestExamEligibility:
    @pytest.mark.parametrize("sid,course,result", [("S1001", "CS201", "ELIGIBLE"), ("S1002", "CS201", "NOT_ELIGIBLE"),
                                                   ("S1003", "CS201", "NOT_ELIGIBLE"), ("S1004", "CS201", "NOT_ELIGIBLE"),
                                                   ("S1005", "CS201", "ELIGIBLE"), ("S1006", "CS201", "NOT_ELIGIBLE"),
                                                   ("S1020", "EC201", "ELIGIBLE"), ("S1021", "EC201", "NOT_ELIGIBLE"),
                                                   ("S1030", "EC201", "ELIGIBLE"), ("S1029", "EC201", "NOT_ELIGIBLE")])
    def test_boundaries_under_current_rule_80(self, loaded_db, sid, course, result):
        r = tools.check_exam_eligibility(sid, course, AS_OF, db_path=loaded_db)
        assert r["result"] == result and r["rule_id"] == "ATT-MIN-02" and r["threshold"] == ">= 80"

    def test_result_carries_full_provenance(self, loaded_db):
        r = tools.check_exam_eligibility("S1002", "CS201", AS_OF, db_path=loaded_db)
        assert r["rule"]["source_doc_id"] == "FIX-ACAD-CIRC-2026-08" and r["rule"]["source_section"] == "1"
        assert r["rule"]["source_version"] == "1.0" and r["rule"]["source_effective_from"] == "2026-08-01"
        assert r["applied_rules"] == [{"rule_id": "ATT-MIN-02", "value": ">= 80", "source_doc_id": "FIX-ACAD-CIRC-2026-08"}]
        assert r["actual_value"] == 77.5 and "77.5%" in r["reason"] and "clause 1" in r["reason"]
        assert r["attendance"] == {"classes_held": 40, "classes_attended": 31, "attendance_pct": 77.5}

    def test_old_rule_applies_before_the_circular_and_new_one_is_listed_as_upcoming(self, loaded_db):
        r = tools.check_exam_eligibility("S1003", "CS201", "2026-06-01", db_path=loaded_db)  # 75% exactly
        assert r["result"] == "ELIGIBLE" and r["rule_id"] == "ATT-MIN-01" and r["threshold"] == ">= 75"
        assert [u["rule_id"] for u in r["upcoming_rules"]] == ["ATT-MIN-02"]
        assert r["upcoming_rules"][0]["rule_effective_from"] == "2026-08-01"

    @pytest.mark.parametrize("day,rule", [("2026-07-31", "ATT-MIN-01"), ("2026-08-01", "ATT-MIN-02")])
    def test_circular_takes_effect_exactly_on_its_effective_date(self, loaded_db, day, rule):
        assert tools.check_exam_eligibility("S1003", "CS201", day, db_path=loaded_db)["rule_id"] == rule

    def test_no_rule_in_force_means_undetermined_not_a_guess(self, loaded_db):
        r = tools.check_exam_eligibility("S1001", "CS201", "2023-01-01", db_path=loaded_db)
        assert r["result"] == "UNDETERMINED" and r["rule_id"] is None and "No rule" in r["reason"]

    def test_missing_attendance_is_undetermined(self, loaded_db):
        r = tools.check_exam_eligibility("S1009", "CS201", AS_OF, db_path=loaded_db)
        assert r["result"] == "UNDETERMINED" and "No attendance record" in r["reason"]
        assert tools.check_exam_eligibility("S1028", "EC201", AS_OF, db_path=loaded_db)["result"] == "UNDETERMINED"

    def test_course_from_another_programme_is_not_applicable(self, loaded_db):
        assert tools.check_exam_eligibility("S1001", "EC201", AS_OF, db_path=loaded_db)["result"] == "NOT_APPLICABLE"

    def test_threshold_is_read_from_the_registry_not_hardcoded(self, db_copy):
        assert tools.check_exam_eligibility("S1002", "CS201", AS_OF, db_path=db_copy)["result"] == "NOT_ELIGIBLE"
        corrupt(db_copy, "UPDATE rule_registry SET value='75' WHERE rule_id='ATT-MIN-02'")
        r = tools.check_exam_eligibility("S1002", "CS201", AS_OF, db_path=db_copy)
        assert r["result"] == "ELIGIBLE" and r["threshold"] == ">= 75"

    def test_operator_comes_from_the_registry_too(self, db_copy):
        corrupt(db_copy, "UPDATE rule_registry SET operator='>' WHERE rule_id='ATT-MIN-02'")
        assert tools.check_exam_eligibility("S1001", "CS201", AS_OF, db_path=db_copy)["result"] == "NOT_ELIGIBLE"  # 80 is not > 80

    def test_a_new_circular_changes_the_answer_without_code_changes(self, db_copy):
        with da.db_session(db_copy) as conn:
            da.supersede_rule(conn, "ATT-MIN-02", dict(rule_id="ATT-MIN-03", description="85", parameter="min_attendance_pct", operator=">=",
                                                       value="85", effective_from="2026-11-01", source_doc_id="FIX-ACAD-CIRC-2026-08", source_section="2"))
        assert tools.check_exam_eligibility("S1001", "CS201", "2026-10-31", db_path=db_copy)["result"] == "ELIGIBLE"
        r = tools.check_exam_eligibility("S1001", "CS201", "2026-11-01", db_path=db_copy)
        assert r["result"] == "NOT_ELIGIBLE" and r["rule_id"] == "ATT-MIN-03"

    def test_scope_filters_apply(self, db_copy):
        corrupt(db_copy, "UPDATE rule_registry SET scope_programmes='B.Tech ECE' WHERE rule_id='ATT-MIN-02'")
        assert tools.check_exam_eligibility("S1001", "CS201", AS_OF, db_path=db_copy)["result"] == "UNDETERMINED"   # CSE: no rule
        assert tools.check_exam_eligibility("S1020", "EC201", AS_OF, db_path=db_copy)["result"] == "ELIGIBLE"       # ECE: rule applies
        corrupt(db_copy, "UPDATE rule_registry SET scope_programmes='ALL', scope_batches='2025+' WHERE rule_id='ATT-MIN-02'")
        assert tools.check_exam_eligibility("S1001", "CS201", AS_OF, db_path=db_copy)["result"] == "UNDETERMINED"   # batch 2024: excluded

    def test_unparseable_rule_value_is_undetermined_with_a_reason(self, db_copy):
        corrupt(db_copy, "UPDATE rule_registry SET value='eighty' WHERE rule_id='ATT-MIN-02'")
        r = tools.check_exam_eligibility("S1001", "CS201", AS_OF, db_path=db_copy)
        assert r["result"] == "UNDETERMINED" and "cannot be evaluated" in r["reason"]

    def test_between_operator(self, db_copy):
        corrupt(db_copy, "UPDATE rule_registry SET operator='between', value='75,90' WHERE rule_id='ATT-MIN-02'")
        assert tools.check_exam_eligibility("S1003", "CS201", AS_OF, db_path=db_copy)["result"] == "ELIGIBLE"
        assert tools.check_exam_eligibility("S1030", "EC201", AS_OF, db_path=db_copy)["result"] == "NOT_ELIGIBLE"

    def test_as_of_date_accepts_date_and_string_and_rejects_garbage(self, loaded_db):
        from datetime import date
        assert tools.check_exam_eligibility("S1001", "CS201", date(2026, 10, 6), db_path=loaded_db)["as_of_date"] == AS_OF
        with pytest.raises(da.InvalidInputError):
            tools.check_exam_eligibility("S1001", "CS201", "06-10-2026", db_path=loaded_db)

    def test_exact_arithmetic_has_no_float_error(self):
        assert _common.evaluate(Fraction(29 * 100, 40), ">=", "72.5") is True
        assert _common.evaluate(Fraction(7249, 100), ">=", "72.5") is False
        assert _common.evaluate(Fraction(1, 3), "<", "0.3333333333") is False


# ------------------------------------------------------------------ rule conflicts and the Member 1 resolver hook
class TestRuleConflictBoundary:
    def overlapping(self, db):
        add_rule(db, "ATT-OLD-OPEN", "min_attendance_pct", ">=", "75", frm="2024-07-01", to=None, doc="FIX-ACAD-REG-2024", section="7.2")
        corrupt(db, "UPDATE rule_registry SET effective_to=NULL WHERE rule_id='ATT-MIN-01'")

    def test_two_applicable_rules_without_a_resolver_are_reported_not_guessed(self, db_copy):
        self.overlapping(db_copy)
        r = tools.check_exam_eligibility("S1001", "CS201", AS_OF, db_path=db_copy)
        assert r["result"] == "UNDETERMINED" and r["rule_resolution"]["status"] == "rule_conflict"
        assert {c["rule_id"] for c in r["conflict_candidates"]} == {"ATT-MIN-01", "ATT-MIN-02", "ATT-OLD-OPEN"}

    def test_registered_resolver_decides(self, db_copy):
        self.overlapping(db_copy)
        seen = {}

        def resolver(candidates, as_of, programme, batch):
            seen.update(n=len(candidates), as_of=as_of.isoformat(), programme=programme, batch=batch)
            return {"winning": next(c for c in candidates if c["rule_id"] == "ATT-MIN-02"), "conflicts_detected": [{"note": "x"}],
                    "reason": "circular supersedes regulation (step 2)"}
        _common.set_policy_resolver(resolver)
        r = tools.check_exam_eligibility("S1002", "CS201", AS_OF, db_path=db_copy)
        assert r["rule_id"] == "ATT-MIN-02" and r["result"] == "NOT_ELIGIBLE"
        assert r["rule_resolution"]["resolver"] == "external_resolver" and "supersedes" in r["rule_resolution"]["reason"]
        assert r["conflicts_detected"] == [{"note": "x"}]
        assert seen == dict(n=3, as_of=AS_OF, programme="B.Tech CSE", batch=2024)

    @pytest.mark.parametrize("bad", [lambda *a: {"winning": None, "reason": "unresolved"},
                                     lambda *a: {"winning": {"rule_id": "NOT-A-CANDIDATE"}},
                                     lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))])
    def test_resolver_that_cannot_choose_or_crashes_never_becomes_a_silent_pick(self, db_copy, bad):
        self.overlapping(db_copy)
        _common.set_policy_resolver(bad)
        r = tools.check_exam_eligibility("S1001", "CS201", AS_OF, db_path=db_copy)
        assert r["result"] == "UNDETERMINED" and r["rule_resolution"]["status"] == "rule_conflict"


# ------------------------------------------------------------------ supplementary
class TestSupplementary:
    def test_failed_course_with_attempts_left(self, loaded_db):
        r = tools.check_supplementary_eligibility("S1007", "MA201", AS_OF, db_path=loaded_db)
        assert r["result"] == "ELIGIBLE" and r["rule_id"] == "SUPP-ATT-01" and r["next_attempt_number"] == 1

    def test_attempt_limit_reached(self, loaded_db):
        r = tools.check_supplementary_eligibility("S1019", "MA202", AS_OF, db_path=loaded_db)
        assert r["result"] == "NOT_ELIGIBLE" and r["supplementary_attempts_so_far"] == 3 and "exceeds the limit" in r["reason"]

    def test_third_attempt_is_still_allowed(self, db_copy):
        corrupt(db_copy, "DELETE FROM results WHERE student_id='S1019' AND exam_session='2026-MAY'")
        r = tools.check_supplementary_eligibility("S1019", "MA202", AS_OF, db_path=db_copy)
        assert r["result"] == "ELIGIBLE" and r["next_attempt_number"] == 3

    def test_passed_course_needs_no_supplementary(self, loaded_db):
        r = tools.check_supplementary_eligibility("S1007", "CS201", AS_OF, db_path=loaded_db)
        assert r["result"] == "NOT_ELIGIBLE" and "PASS" in r["reason"]

    def test_absent_counts_as_a_backlog_attempt(self, loaded_db):
        assert tools.check_supplementary_eligibility("S1010", "CS202", AS_OF, db_path=loaded_db)["result"] == "ELIGIBLE"

    def test_no_attempt_at_all(self, loaded_db):
        assert tools.check_supplementary_eligibility("S1013", "CS201", AS_OF, db_path=loaded_db)["result"] == "NOT_ELIGIBLE"

    def test_without_an_attempt_limit_rule_the_answer_is_undetermined(self, db_copy):
        corrupt(db_copy, "DELETE FROM rule_registry WHERE rule_id='SUPP-ATT-01'")
        assert tools.check_supplementary_eligibility("S1007", "MA201", AS_OF, db_path=db_copy)["result"] == "UNDETERMINED"


# ------------------------------------------------------------------ placement
class TestPlacement:
    def test_cgpa_exactly_at_cutoff_is_eligible(self, loaded_db):
        r = tools.check_placement_eligibility("S1011", AS_OF, db_path=loaded_db)
        assert r["result"] == "ELIGIBLE" and r["inputs"] == {"cgpa": 6.5, "active_backlogs": 0}
        assert [c["state"] for c in r["criteria"]] == ["pass", "pass"]

    def test_one_hundredth_below_cutoff_is_not_eligible(self, loaded_db):
        r = tools.check_placement_eligibility("S1012", AS_OF, db_path=loaded_db)
        assert r["result"] == "NOT_ELIGIBLE" and r["criteria"][0]["state"] == "fail" and "does not meet" in r["reason"]

    def test_backlog_blocks_even_when_cgpa_is_fine(self, loaded_db):
        r = tools.check_placement_eligibility("S1023", AS_OF, db_path=loaded_db)  # CGPA exactly 6.5, one backlog
        assert r["result"] == "NOT_ELIGIBLE" and [c["state"] for c in r["criteria"]] == ["pass", "fail"]

    def test_provenance_for_every_criterion(self, loaded_db):
        r = tools.check_placement_eligibility("S1011", AS_OF, db_path=loaded_db)
        assert {a["rule_id"] for a in r["applied_rules"]} == {"PLC-CGPA-01", "PLC-BKLG-01"}
        assert {c["rule"]["source_doc_id"] for c in r["criteria"]} == {"FIX-PLACEMENT-2024"}
        assert r["criteria"][0]["rule"]["source_section"] == "3.1" and "clause 3.1" in r["reason"]

    def test_missing_rule_makes_it_undetermined_unless_another_criterion_already_fails(self, db_copy):
        corrupt(db_copy, "DELETE FROM rule_registry WHERE rule_id='PLC-BKLG-01'")
        r = tools.check_placement_eligibility("S1011", AS_OF, db_path=db_copy)
        assert r["result"] == "UNDETERMINED" and "not evaluated" in r["reason"]
        assert tools.check_placement_eligibility("S1012", AS_OF, db_path=db_copy)["result"] == "NOT_ELIGIBLE"

    def test_cutoff_is_read_from_the_registry(self, db_copy):
        corrupt(db_copy, "UPDATE rule_registry SET value='7.00' WHERE rule_id='PLC-CGPA-01'")
        assert tools.check_placement_eligibility("S1011", AS_OF, db_path=db_copy)["result"] == "NOT_ELIGIBLE"

    def test_upcoming_placement_rule_is_informational_only(self, db_copy):
        add_rule(db_copy, "PLC-CGPA-02", "min_cgpa_placement", ">=", "7.00", frm="2027-01-01", doc="FIX-PLACEMENT-2024", section="3.1a")
        r = tools.check_placement_eligibility("S1011", AS_OF, db_path=db_copy)
        assert r["result"] == "ELIGIBLE" and [u["rule_id"] for u in r["upcoming_rules"]] == ["PLC-CGPA-02"]


# ------------------------------------------------------------------ what-if
class TestWhatIf:
    def test_passing_the_backlog_course_flips_placement(self, loaded_db):
        r = tools.simulate_what_if("S1023", {"pass_courses": ["MA202"]}, "placement", AS_OF, db_path=loaded_db)
        assert (r["baseline_result"], r["simulated_result"], r["outcome_changes"]) == ("NOT_ELIGIBLE", "ELIGIBLE", True)
        assert any("MA202" in a and "reducing active backlogs" in a for a in r["assumptions"])
        assert any("CGPA is assumed unchanged" in a for a in r["assumptions"])
        assert r["simulation"] is True and r["modifies_records"] is False

    def test_passing_one_of_three_backlogs_is_not_enough(self, loaded_db):
        r = tools.simulate_what_if("S1008", {"pass_courses": ["CS201"]}, "placement", AS_OF, db_path=loaded_db)
        assert r["simulated_result"] == "NOT_ELIGIBLE" and r["simulated"]["inputs" if False else "criteria"][1]["actual_value"] == 2

    def test_passing_a_course_that_is_not_a_backlog_changes_nothing_and_says_so(self, loaded_db):
        r = tools.simulate_what_if("S1011", {"pass_courses": ["CS201"]}, "placement", AS_OF, db_path=loaded_db)
        assert r["outcome_changes"] is False and any("not currently a backlog" in a for a in r["assumptions"])

    def test_cgpa_and_attendance_scenarios(self, loaded_db):
        r = tools.simulate_what_if("S1012", {"cgpa": 6.5}, "placement", AS_OF, db_path=loaded_db)
        assert (r["baseline_result"], r["simulated_result"]) == ("NOT_ELIGIBLE", "ELIGIBLE")
        r = tools.simulate_what_if("S1002", {"attendance": {"CS201": {"classes_held": 40, "classes_attended": 32}}}, "exam:CS201", AS_OF, db_path=loaded_db)
        assert (r["baseline_result"], r["simulated_result"]) == ("NOT_ELIGIBLE", "ELIGIBLE") and "32/40" in " ".join(r["assumptions"])

    def test_supplementary_what_if(self, loaded_db):
        r = tools.simulate_what_if("S1007", {"pass_courses": ["MA201"]}, "supplementary:MA201", AS_OF, db_path=loaded_db)
        assert (r["baseline_result"], r["simulated_result"]) == ("ELIGIBLE", "NOT_ELIGIBLE")

    def test_the_database_is_never_modified(self, db_copy):
        before = sha(db_copy)
        tools.simulate_what_if("S1023", {"pass_courses": ["MA202"], "cgpa": 9.9}, "placement", AS_OF, db_path=db_copy)
        tools.simulate_what_if("S1002", {"attendance": {"CS201": {"classes_held": 40, "classes_attended": 40}}}, "exam:CS201", AS_OF, db_path=db_copy)
        assert sha(db_copy) == before
        assert tools.get_backlogs("S1023", db_path=db_copy)["active_backlogs"] == 1
        assert tools.get_attendance("S1002", "CS201", db_path=db_copy)["classes_attended"] == 31

    @pytest.mark.parametrize("scenario,check", [({"hack": 1}, "placement"), ("pass", "placement"), ({}, "bogus"), ({}, "exam:"), ({}, ""),
                                                ({"attendance": {"CS201": {"classes_held": 0, "classes_attended": 0}}}, "exam:CS201"),
                                                ({"attendance": {"CS201": {"classes_held": 10, "classes_attended": 11}}}, "exam:CS201"),
                                                ({"attendance": {"CS201": {"classes_held": "x"}}}, "exam:CS201"),
                                                ({"pass_courses": ["ZZ999"]}, "placement"), ({"cgpa": "high"}, "placement")])
    def test_invalid_scenarios_are_rejected(self, loaded_db, scenario, check):
        with pytest.raises(da.DataAccessError):
            tools.simulate_what_if("S1011", scenario, check, AS_OF, db_path=loaded_db)


# ------------------------------------------------------------------ authorization boundary
class TestCallToolBoundary:
    def test_acts_on_the_authenticated_student_only(self, loaded_db):
        c = call_tool("get_attendance", {"course_code": "CS201"}, authenticated_student_id="S1002", db_path=loaded_db)
        assert c["output"]["classes_attended"] == 31 and c["input"] == {"student_id": "S1002", "course_code": "CS201"}

    @pytest.mark.parametrize("claimed", ["S1002", "s1002", " S1002 ", "S9999"])
    def test_other_student_id_in_arguments_is_denied(self, loaded_db, claimed):
        with pytest.raises(AccessDeniedError):
            call_tool("get_attendance", {"course_code": "CS201", "student_id": claimed}, authenticated_student_id="S1001", db_path=loaded_db)

    def test_same_student_id_in_arguments_is_harmless(self, loaded_db):
        c = call_tool("get_attendance", {"course_code": "CS201", "student_id": "S1001"}, authenticated_student_id="S1001", db_path=loaded_db)
        assert c["status"] == "ok"

    @pytest.mark.parametrize("tool,args", [(n, {"course_code": "CS201", "session": "2026-MAY", "scenario": {}}) for n in TOOLS])
    def test_every_tool_refuses_another_students_id(self, loaded_db, tool, args):
        with pytest.raises(AccessDeniedError):
            call_tool(tool, {**args, "student_id": "S1002"}, authenticated_student_id="S1001", db_path=loaded_db)

    def test_unknown_tool_and_bad_arguments(self, loaded_db):
        with pytest.raises(UnknownToolError):
            call_tool("run_sql", {"query": "SELECT * FROM students"}, authenticated_student_id="S1001", db_path=loaded_db)
        assert call_tool("get_attendance", {}, authenticated_student_id="S1001", db_path=loaded_db)["output"]["error"]["code"] == "invalid_input"
        c = call_tool("get_attendance", {"course_code": "CS201", "sql": "x"}, authenticated_student_id="S1001", db_path=loaded_db)
        assert c["status"] == "error" and "unexpected argument" in c["output"]["error"]["message"]
        assert "sql" not in c["input"]

    def test_tool_errors_are_structured_not_raised(self, loaded_db):
        c = call_tool("get_attendance", {"course_code": "ZZ999"}, authenticated_student_id="S1001", db_path=loaded_db)
        assert c["status"] == "error" and c["output"]["error"] == {"code": "course_not_found", "message": "No course with that code exists"}
        c = call_tool("get_student_profile", {}, authenticated_student_id="S4242", db_path=loaded_db)
        assert c["output"]["error"]["code"] == "student_not_found"

    def test_sql_injection_in_arguments_is_just_invalid_input(self, loaded_db):
        c = call_tool("get_attendance", {"course_code": "CS201' OR '1'='1"}, authenticated_student_id="S1001", db_path=loaded_db)
        assert c["output"]["error"]["code"] == "invalid_input"
        assert tools.get_backlogs("S1001", db_path=loaded_db)["status"] == "ok"

    def test_list_results_are_wrapped_uniformly(self, loaded_db):
        c = call_tool("get_results", {}, authenticated_student_id="S1019", db_path=loaded_db)
        assert c["output"]["count"] == 5 and c["output"]["status"] == "ok"

    def test_as_of_date_default_and_per_call_override(self, loaded_db):
        c = call_tool("check_exam_eligibility", {"course_code": "CS201"}, authenticated_student_id="S1003", as_of_date="2026-06-01", db_path=loaded_db)
        assert c["output"]["rule_id"] == "ATT-MIN-01" and c["input"]["as_of_date"] == "2026-06-01"
        c = call_tool("check_exam_eligibility", {"course_code": "CS201", "as_of_date": "2026-10-06"}, authenticated_student_id="S1003",
                      as_of_date="2026-06-01", db_path=loaded_db)
        assert c["output"]["rule_id"] == "ATT-MIN-02"
        c = call_tool("check_exam_eligibility", {"course_code": "CS201", "as_of_date": "nonsense"}, authenticated_student_id="S1003", db_path=loaded_db)
        assert c["output"]["error"]["code"] == "invalid_input"

    def test_tool_specs_never_expose_a_student_id_parameter(self):
        specs = tool_specs()
        assert {s["name"] for s in specs} == set(TOOLS) and all(s["type"] == "function" for s in specs)
        for s in specs:
            assert "student_id" not in s["parameters"]["properties"] and s["parameters"]["additionalProperties"] is False
        att = next(s for s in specs if s["name"] == "get_attendance")
        assert att["parameters"]["required"] == ["course_code"]
        assert "as_of_date" in next(s for s in specs if s["name"] == "check_placement_eligibility")["parameters"]["properties"]

    def test_there_is_no_generic_sql_tool(self):
        assert not [n for n in TOOLS if "sql" in n.lower() or "query" in n.lower()]

    def test_tools_use_read_only_connections(self, loaded_db, monkeypatch):
        modes = []
        real = da.connect
        monkeypatch.setattr(da, "connect", lambda p=None, *, read_only=False: (modes.append(read_only), real(p, read_only=read_only))[1])
        for name, args in [("get_student_profile", {}), ("get_attendance", {"course_code": "CS201"}), ("check_placement_eligibility", {}),
                           ("simulate_what_if", {"scenario": {"cgpa": 8}})]:
            call_tool(name, args, authenticated_student_id="S1001", db_path=loaded_db)
        assert modes and all(modes)
