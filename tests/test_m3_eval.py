"""Member 3: evaluation dataset validation and runner scoring with mocked HTTP."""
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import runner  # noqa: E402
from tests.m3_fakes import CITATION, NOT_FOUND, RULE, TOOL, FakeResponse, FakeSession, ask_body  # noqa: E402
from ui.api_client import ApiResult, validate_ask_response  # noqa: E402

DATASET = ROOT / "eval" / "eval_set.jsonl"


# --- dataset ---------------------------------------------------------------------------

def test_dataset_is_valid_jsonl_with_unique_ids():
    lines = [line for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
    objs = [json.loads(line) for line in lines]
    assert len({o["id"] for o in objs}) == len(objs)
    assert runner.load_dataset(DATASET) == objs


def test_dataset_meets_brief_minimums():
    cases = runner.load_dataset(DATASET)
    cats = Counter(c["category"] for c in cases)
    assert len(cases) >= 20
    assert cats["unanswerable"] >= 3
    assert cats["conflict_version"] >= 3
    assert cats["personal_tool"] >= 4
    assert cats["cross_student"] >= 2
    assert cats["multi_step"] >= 2


def test_dataset_avoids_judge_reserved_ids():
    for c in runner.load_dataset(DATASET):
        sid = c.get("student_id")
        assert sid is None or not 9000 <= int(sid[1:]) <= 9999
        assert "JDG" not in json.dumps(c).upper()


def test_dataset_cases_are_documented():
    for c in runner.load_dataset(DATASET):
        assert c["evaluates"].strip()
        assert c["fixture_status"] in ("ready", "pending")
        if c["fixture_status"] == "pending":
            assert c["fixture_requirements"].strip(), c["id"]


@pytest.mark.parametrize("bad, fragment", [
    ('{"id": "X"', "invalid JSON"),
    ('{"id": "X", "category": "c", "question": "q", "expected_answer_type": ["guess"], "evaluates": "e", "fixture_status": "ready"}', "unknown expected_answer_type"),
    ('{"id": "X", "category": "c", "question": "q", "expected_answer_type": ["refused"], "evaluates": "e", "fixture_status": "ready", "student_id": "S9123"}', "judge-reserved"),
    ('{"id": "X", "category": "c", "question": "JDG101?", "expected_answer_type": ["refused"], "evaluates": "e", "fixture_status": "ready"}', "JDG"),
    ('{"id": "X", "category": "c", "question": "q", "expected_answer_type": ["refused"], "evaluates": "e", "fixture_status": "ready"}\n'
     '{"id": "X", "category": "c", "question": "q", "expected_answer_type": ["refused"], "evaluates": "e", "fixture_status": "ready"}', "duplicate id"),
])
def test_invalid_datasets_rejected(tmp_path, bad, fragment):
    p = tmp_path / "bad.jsonl"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(runner.DatasetError, match=fragment):
        runner.load_dataset(p)


# --- scoring ---------------------------------------------------------------------------

def case(**kw):
    base = {"id": "T", "category": "policy_answerable", "question": "q", "expected_answer_type": ["retrieved_fact"],
            "evaluates": "e", "fixture_status": "ready", "student_id": None}
    base.update(kw)
    return base


def ok(body):
    return ApiResult(True, data=validate_ask_response(body)[0], status_code=200, latency_ms=10)


def checks(scored):
    return {c["check"]: c["status"] for c in scored["checks"]}


def test_http_200_with_wrong_type_fails():
    scored = runner.score_case(case(expected_answer_type=["not_found"]), ok(ask_body("retrieved_fact")))
    assert scored["status"] == "FAIL" and checks(scored)["answer_type"] == "fail"


def test_not_found_exact_message_required():
    good = runner.score_case(case(expected_answer_type=["not_found"]), ok(ask_body("not_found", answer=NOT_FOUND)))
    bad = runner.score_case(case(expected_answer_type=["not_found"]), ok(ask_body("not_found", answer=NOT_FOUND[:-1])))
    assert good["status"] == "PASS"
    assert bad["status"] == "FAIL" and checks(bad)["not_found_exact_message"] == "fail"


def test_citation_checks():
    c = case(requires_citations=True, expected_citation_doc_ids=["DOC-REG-01"])
    assert runner.score_case(c, ok(ask_body(citations=[CITATION])))["status"] == "PASS"
    assert checks(runner.score_case(c, ok(ask_body())))["citations_present"] == "fail"
    partial = dict(CITATION, version=None)
    assert checks(runner.score_case(c, ok(ask_body(citations=[partial]))))["citation_metadata_complete"] == "fail"
    other = dict(CITATION, doc_id="OTHER")
    assert checks(runner.score_case(c, ok(ask_body(citations=[other]))))["citation_doc_ids"] == "fail"


def test_pending_fixture_checks_are_skipped_not_passed():
    c = case(fixture_status="pending", expected_citation_doc_ids=["SYN-X"],
             expected_tools=["get_attendance"], expected_tool_outputs={"get_attendance": {"attendance_pct": 75.0}},
             expected_answer_type=["calculated"], student_id="S1001")
    scored = runner.score_case(c, ok(ask_body("calculated", tools_invoked=[TOOL])))
    assert checks(scored)["citation_doc_ids"] == "skipped"
    assert checks(scored)["tool_output:get_attendance"] == "skipped"
    assert checks(scored)["tools_invoked"] == "pass"


def test_tool_and_rule_checks():
    c = case(expected_answer_type=["calculated"], student_id="S1001", expected_tools=["check_exam_eligibility"],
             requires_applied_rules=True)
    scored = runner.score_case(c, ok(ask_body("calculated", tools_invoked=[TOOL])))
    assert checks(scored)["tools_invoked"] == "fail" and checks(scored)["applied_rules_present"] == "fail"
    tool = {"tool": "check_exam_eligibility", "input": {"student_id": "S1001"}, "output": {"result": "ELIGIBLE"}}
    c["expected_tool_outputs"] = {"check_exam_eligibility": {"result": "ELIGIBLE"}}
    assert runner.score_case(c, ok(ask_body("calculated", tools_invoked=[tool], applied_rules=[RULE])))["status"] == "PASS"


def test_cross_student_leak_detected():
    c = case(category="cross_student", expected_answer_type=["refused", "calculated"], student_id="S1001",
             forbidden_tool_student_ids=["S1002"])
    leak = dict(TOOL, input={"student_id": "S1002", "course_code": "CS201"})
    scored = runner.score_case(c, ok(ask_body("calculated", tools_invoked=[leak])))
    assert scored["status"] == "FAIL" and checks(scored)["no_cross_student_tools"] == "fail"
    assert runner.score_case(c, ok(ask_body("refused")))["status"] == "PASS"


def test_tools_without_header_is_a_leak():
    c = case(category="cross_student", expected_answer_type=["refused", "calculated"], student_id=None)
    assert runner.score_case(c, ok(ask_body("calculated", tools_invoked=[TOOL])))["status"] == "FAIL"


def test_http_403_counts_as_refusal_only_when_refusal_expected():
    denied = ApiResult(False, error="HTTP 403: forbidden", status_code=403)
    assert runner.score_case(case(expected_answer_type=["refused"]), denied)["status"] == "PASS"
    assert runner.score_case(case(expected_answer_type=["calculated"]), denied)["status"] == "ERROR"


def test_forbidden_strings_use_boundaries():
    c = case(forbidden_strings=["0%"])
    assert runner.score_case(c, ok(ask_body(answer="It is 74.0% and 70%")))["status"] == "PASS"
    assert runner.score_case(c, ok(ask_body(answer="Requirement is 0% now")))["status"] == "FAIL"


def test_transport_error_is_error():
    scored = runner.score_case(case(), ApiResult(False, error="Cannot reach the API"))
    assert scored["status"] == "ERROR"


# --- end-to-end runner with a fake HTTP session --------------------------------------------

def fake_backend(method, url, kwargs):
    if url.endswith("/ask"):
        q = kwargs["json"]["question"]
        if "parking" in q:
            return FakeResponse(200, ask_body("not_found", answer=NOT_FOUND, trace_id="t-nf"))
        return FakeResponse(200, ask_body("retrieved_fact", citations=[CITATION], trace_id="t-ok"))
    if "/audit/" in url:
        return FakeResponse(200, {"trace_id": url.rsplit("/", 1)[1], "sources_retrieved": [{"doc_id": "DOC-REG-01"}],
                                  "llm_calls": 1, "tokens": 120, "latency_ms": 5})
    return FakeResponse(404, {"detail": "nope"})


def write_dataset(tmp_path, cases):
    p = tmp_path / "set.jsonl"
    p.write_text("\n".join(json.dumps(c) for c in cases), encoding="utf-8")
    return p


def test_runner_end_to_end_report(tmp_path, capsys):
    ds = write_dataset(tmp_path, [
        case(id="A", question="What is the attendance rule?", requires_citations=True, expected_citation_doc_ids=["DOC-REG-01"]),
        case(id="B", category="unanswerable", question="parking fee?", expected_answer_type=["not_found"]),
        case(id="C", category="unanswerable", question="cricket?", expected_answer_type=["not_found"]),
    ])
    out = tmp_path / "out.json"
    session = FakeSession(fake_backend)
    code = runner.main(["--dataset", str(ds), "--output", str(out), "--audit", "--base-url", "http://api.test"], session=session)
    report = json.loads(out.read_text())
    s = report["summary"]
    assert code == 1  # C fails: answered instead of abstaining
    assert (s["pass"], s["fail"], s["error"]) == (2, 1, 0)
    m = s["metrics"]
    assert m["abstention_accuracy"]["value"] == 0.5
    assert m["citation_correctness"]["value"] == 1.0
    assert m["retrieval_hit_rate"]["value"] == 1.0
    assert m["llm_calls_total"]["value"] == 3 and m["tokens_total"]["value"] == 360
    assert m["latency_p50_ms"]["value"] is not None
    assert report["run"]["dataset_sha256"]
    ask_calls = [c for c in session.calls if c["url"].endswith("/ask")]
    assert all(c["headers"] == {} for c in ask_calls)
    assert "Cases 3: 2 pass, 1 fail" in capsys.readouterr().out


def test_runner_sends_header_and_date(tmp_path):
    ds = write_dataset(tmp_path, [case(id="P", student_id="S1001", as_of_date="2025-01-15")])
    session = FakeSession(fake_backend)
    runner.main(["--dataset", str(ds), "--output", str(tmp_path / "o.json"), "--base-url", "http://api.test"], session=session)
    call = session.calls[0]
    assert call["headers"] == {"X-Student-Id": "S1001"}
    assert call["json"]["as_of_date"] == "2025-01-15"


def test_runner_metrics_na_without_audit(tmp_path):
    ds = write_dataset(tmp_path, [case(id="A")])
    out = tmp_path / "o.json"
    assert runner.main(["--dataset", str(ds), "--output", str(out), "--base-url", "http://api.test"],
                       session=FakeSession(fake_backend)) == 0
    m = json.loads(out.read_text())["summary"]["metrics"]
    for name in ("retrieval_hit_rate", "llm_calls_total", "tokens_total"):
        assert m[name]["value"] is None and m[name]["note"]


def test_runner_api_down_exits_nonzero(tmp_path):
    from tests.m3_fakes import ConnectionErr, raising
    ds = write_dataset(tmp_path, [case(id="A")])
    out = tmp_path / "o.json"
    code = runner.main(["--dataset", str(ds), "--output", str(out), "--base-url", "http://api.test"],
                       session=FakeSession(raising(ConnectionErr())))
    assert code == 1
    assert json.loads(out.read_text())["summary"]["error"] == 1


def test_runner_invalid_dataset_exit_2(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text("{nope", encoding="utf-8")
    assert runner.main(["--dataset", str(p), "--validate-only"]) == 2
