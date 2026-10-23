"""Member 3: runner honesty checks (mock/live labelling, errors, N/A handling). No backend needed."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import runner  # noqa: E402
from eval.mock_api import MockSession  # noqa: E402
from tests.m3_fakes import NOT_FOUND, FakeResponse, FakeSession, ask_body, raising, ConnectionErr  # noqa: E402
from ui.api_client import ApiResult, validate_ask_response  # noqa: E402

DATASET = ROOT / "eval" / "eval_set.jsonl"


def case(**kw):
    base = {"id": "T", "category": "policy_answerable", "question": "q?", "expected_answer_type": ["retrieved_fact"],
            "evaluates": "x", "fixture_status": "ready"}
    base.update(kw)
    return base


def ok(body):
    data, warnings = validate_ask_response(body)
    return ApiResult(True, data=data, status_code=200, warnings=warnings)


def test_answer_contains_uses_word_boundary():
    c = case(expected_answer_contains=["75"])
    assert runner.score_case(c, ok(ask_body(answer="The minimum is 175 hours")))["status"] == "FAIL"
    assert runner.score_case(c, ok(ask_body(answer="The minimum is 75% of classes")))["status"] == "PASS"


def test_mock_run_is_labelled_in_json_and_stdout(tmp_path, capsys):
    out = tmp_path / "m.json"
    code = runner.main(["--mock", "--output", str(out)])
    report = json.loads(out.read_text(encoding="utf-8"))
    printed = capsys.readouterr().out
    assert code == 0
    assert report["run"]["mode"] == "mock" and "MOCK-ONLY" in report["run"]["mode_note"]
    assert report["summary"]["mode"] == "mock"
    assert printed.count("MOCK-ONLY RUN") >= 2


def test_mock_run_reports_na_for_unmeasurable_metrics(tmp_path):
    out = tmp_path / "m.json"
    runner.main(["--mock", "--audit", "--output", str(out)])
    m = json.loads(out.read_text(encoding="utf-8"))["summary"]["metrics"]
    for name in ("latency_p50_ms", "latency_p95_ms", "retrieval_hit_rate", "llm_calls_total", "tokens_total"):
        assert m[name]["value"] is None and m[name]["note"], name


def test_mock_session_answers_every_dataset_case_with_a_valid_contract():
    cases = runner.load_dataset(DATASET)
    client = runner.ApiClient("http://mock", session=MockSession(cases))
    for c in cases:
        res = runner.run_case(client, c, False)
        assert res["status"] == "PASS", (c["id"], res["checks"])


def test_leak_is_visible_not_hidden_by_other_passes():
    c = case(id="X", category="cross_student", student_id="S1001", expected_answer_type=["refused", "calculated"],
             forbidden_tool_student_ids=["S1002"])
    leaky = ask_body("calculated", tools_invoked=[{"tool": "get_attendance", "input": {"student_id": "S1002"}, "output": {}}])
    scored = runner.score_case(c, ok(leaky))
    assert scored["status"] == "FAIL"
    summary = runner.summarise([dict(_row(c, scored))])
    assert summary["metrics"]["cross_student_leaks"]["value"] == 1


def _row(c, scored):
    return {"id": c["id"], "category": c["category"], "student_id": c.get("student_id"), "as_of_date": None,
            "fixture_status": "ready", "expected_answer_type": c["expected_answer_type"],
            "expected_citation_doc_ids": [], "human_review": None, "http_status": 200, "latency_ms": 5.0,
            "error": None, "response": {}, "contract_warnings": [], **scored}


def test_no_cross_student_case_means_leak_metric_is_na_not_zero():
    c = case()
    scored = runner.score_case(c, ok(ask_body(citations=[{"doc_id": "D", "title": "t", "section": "1", "version": "1", "effective_from": "2024-01-01"}])))
    s = runner.summarise([_row(c, scored)])
    assert s["metrics"]["cross_student_leaks"]["value"] is None


def test_errors_count_against_answer_type_accuracy():
    good = case(id="G")
    bad = case(id="B")
    rows = [_row(good, runner.score_case(good, ok(ask_body()))),
            _row(bad, runner.score_case(bad, ApiResult(False, error="Cannot reach the API")))]
    s = runner.summarise(rows)
    assert s["error"] == 1 and s["metrics"]["answer_type_accuracy"]["value"] == 0.5


def test_malformed_responses_are_errors_not_passes(tmp_path):
    ds = tmp_path / "s.jsonl"
    ds.write_text(json.dumps(case(id="M")) + "\n", encoding="utf-8")
    for bad in (FakeResponse(200, {"answer": "no trace id or type"}), FakeResponse(200, None, text="<html>")):
        out = tmp_path / "o.json"
        code = runner.main(["--dataset", str(ds), "--output", str(out), "--base-url", "http://x"],
                           session=FakeSession(lambda m, u, k, b=bad: b))
        s = json.loads(out.read_text())["summary"]
        assert code == 1 and s["error"] == 1 and s["pass"] == 0


def test_not_found_with_wrong_text_fails():
    c = case(category="unanswerable", expected_answer_type=["not_found"])
    assert runner.score_case(c, ok(ask_body("not_found", answer=NOT_FOUND)))["status"] == "PASS"
    assert runner.score_case(c, ok(ask_body("not_found", answer="Sorry, no idea.")))["status"] == "FAIL"


def test_connection_failure_exit_code_and_banner(tmp_path, capsys):
    ds = tmp_path / "s.jsonl"
    ds.write_text(json.dumps(case(id="M")) + "\n", encoding="utf-8")
    out = tmp_path / "o.json"
    assert runner.main(["--dataset", str(ds), "--output", str(out), "--base-url", "http://x"],
                       session=FakeSession(raising(ConnectionErr()))) == 1
    assert "WARNING: 1 case(s) ERRORED" in capsys.readouterr().out
