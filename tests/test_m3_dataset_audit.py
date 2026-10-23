"""Member 3: deeper checks on eval/eval_set.jsonl than the category minimums (no backend needed)."""
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import runner  # noqa: E402

CASES = runner.load_dataset(ROOT / "eval" / "eval_set.jsonl")
CAT = {c["category"] for c in CASES}


def of(cat):
    return [c for c in CASES if c["category"] == cat]


def test_every_line_is_one_json_object():
    text = (ROOT / "eval" / "eval_set.jsonl").read_text(encoding="utf-8")
    assert text.endswith("\n") and "\n\n" not in text
    assert len(text.splitlines()) == len(CASES)


def test_category_counts_meet_handoff_minimums():
    n = Counter(c["category"] for c in CASES)
    assert len(CASES) >= 20
    assert n["unanswerable"] >= 3 and n["conflict_version"] >= 3 and n["cross_student"] >= 2 and n["multi_step"] >= 2
    tool_cases = [c for c in CASES if c["category"] in ("personal_tool", "multi_step") and c["expected_tools"]]
    assert len([c for c in of("personal_tool") if c["expected_tools"]]) >= 4
    assert len(tool_cases) >= 4


def test_no_duplicate_question_for_the_same_identity_and_date():
    keys = [(c["question"].strip().lower(), c.get("student_id"), c.get("as_of_date")) for c in CASES]
    assert len(keys) == len(set(keys))


def test_unanswerable_cases_expect_only_not_found_and_no_tools_or_citations():
    for c in of("unanswerable"):
        assert c["expected_answer_type"] == ["not_found"], c["id"]
        assert not c["expected_tools"] and not c["requires_citations"], c["id"]


def test_cross_student_cases_use_the_header_and_forbid_other_ids():
    for c in of("cross_student"):
        assert "calculated" not in c["expected_answer_type"] or c["forbidden_tool_student_ids"], c["id"]
        sid = c.get("student_id")
        if sid:
            others = [t for t in c.get("forbidden_tool_student_ids", [])]
            assert sid not in others, c["id"]
    # at least two cases name a *different* student in the text while the header carries another id
    named = [c for c in of("cross_student") if c.get("student_id") and c.get("forbidden_tool_student_ids")]
    assert len(named) >= 2


def test_ready_cases_with_tools_or_citations_have_expectations():
    for c in CASES:
        if c["fixture_status"] != "ready":
            continue
        if c["requires_citations"] and "retrieved_fact" in c["expected_answer_type"]:
            assert c["expected_citation_doc_ids"], f"{c['id']} is ready but has no expected doc_ids"


def test_conflict_case_expects_conflict_listing():
    flagged = [c for c in of("conflict_version") if c["expected_answer_type"] == ["conflict_flagged"]]
    assert flagged and all(len(c["expected_citation_doc_ids"]) >= 2 for c in flagged)
    assert any(c["requires_conflicts"] for c in of("conflict_version"))


def test_dates_are_iso():
    from datetime import date
    for c in CASES:
        if c.get("as_of_date"):
            date.fromisoformat(c["as_of_date"])
