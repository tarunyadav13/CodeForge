"""Member 3: docs/sample_audit_records.json must follow the frozen AuditRecord shape and stay labelled."""
import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = json.loads((ROOT / "docs" / "sample_audit_records.json").read_text(encoding="utf-8"))
# api/models.py AuditRecord (handoff section 8), in order.
FIELDS = ["trace_id", "timestamp", "student_id", "question_category", "sources_retrieved", "precedence_decision",
          "tools_invoked", "answer_type", "model", "llm_calls", "tokens", "latency_ms"]
ANSWER_TYPES = {"retrieved_fact", "calculated", "not_found", "clarification_needed", "refused", "conflict_flagged"}


def test_file_is_labelled_synthetic():
    assert "SYNTHETIC EXAMPLES" in DOC["_label"] and "NOT output of the running backend" in DOC["_label"]
    assert all(r["trace_id"].startswith("EXAMPLE-") for r in DOC["records"])


def test_three_records_cover_required_kinds():
    types = [r["answer_type"] for r in DOC["records"]]
    assert len(types) == 3
    assert "retrieved_fact" in types and "calculated" in types and ({"refused", "not_found"} & set(types))


def test_records_have_exactly_the_audit_fields():
    for r in DOC["records"]:
        assert list(r) == FIELDS, r["trace_id"]
        assert r["answer_type"] in ANSWER_TYPES
        datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
        assert isinstance(r["llm_calls"], int) and isinstance(r["tokens"], int) and isinstance(r["latency_ms"], int)
        assert r["student_id"] is None or re.fullmatch(r"S\d{4}", r["student_id"])


def test_no_question_text_or_reserved_ids():
    blob = json.dumps(DOC["records"]).upper()
    assert "QUESTION\":" not in blob and "JDG" not in blob
    for r in DOC["records"]:
        if r["student_id"]:
            assert not 9000 <= int(r["student_id"][1:]) <= 9999
