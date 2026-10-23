"""Member 3: UI API client against mocked HTTP (no running backend needed)."""
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.m3_fakes import (  # noqa: E402
    CITATION, HEALTH_OK, NOT_FOUND, RULE, TOOL, ConnectionErr, FakeSession, TimeoutErr, ask_body, raising, static,
)
from ui import api_client  # noqa: E402
from ui.api_client import ApiClient, ResponseFormatError, validate_ask_response, validate_health_response  # noqa: E402


def make(responder):
    session = FakeSession(responder)
    return ApiClient("http://api.test", session=session), session


# --- request shape ---------------------------------------------------------------------

def test_ask_sends_student_id_header_and_as_of_date():
    client, session = make(static(body=ask_body()))
    result = client.ask("What is my attendance?", "S1001", date(2026, 3, 1))
    call = session.calls[0]
    assert result.ok
    assert call["method"] == "POST" and call["url"] == "http://api.test/ask"
    assert call["headers"] == {"X-Student-Id": "S1001"}
    assert call["json"] == {"question": "What is my attendance?", "as_of_date": "2026-03-01"}
    assert "S1001" not in call["json"]["question"]


def test_ask_without_student_id_or_date_sends_neither():
    client, session = make(static(body=ask_body()))
    client.ask("What is the attendance policy?")
    call = session.calls[0]
    assert call["headers"] == {}
    assert call["json"] == {"question": "What is the attendance policy?"}


def test_ask_uses_a_timeout():
    client, session = make(static(body=ask_body()))
    client.ask("q")
    assert session.calls[0]["timeout"] == api_client.ASK_TIMEOUT


def test_base_url_from_env(monkeypatch):
    monkeypatch.setenv("API_BASE_URL", "http://example:9000/")
    assert ApiClient(session=FakeSession(static())).base_url == "http://example:9000"


# --- failures never produce an answer ---------------------------------------------------

@pytest.mark.parametrize("responder, fragment", [
    (raising(ConnectionErr()), "Cannot reach the API"),
    (raising(TimeoutErr()), "did not respond within the timeout"),
    (static(500, {"detail": "boom"}), "HTTP 500: boom"),
    (static(422, {"detail": [{"loc": ["body", "question"], "msg": "too short"}]}), "HTTP 422"),
    (static(200, None, text="<html>not json</html>"), "not valid JSON"),
    (static(200, {"answer": "hi"}), "Malformed /ask response"),
    (static(200, ask_body(answer_type="made_up")), "unknown answer_type"),
    (static(200, ask_body(as_of_date="yesterday")), "as_of_date"),
    (static(200, ["not", "an", "object"]), "not a JSON object"),
])
def test_ask_failures(responder, fragment):
    client, _ = make(responder)
    result = client.ask("q", "S1001")
    assert not result.ok
    assert result.data is None
    assert fragment in result.error


# --- response validation --------------------------------------------------------------

@pytest.mark.parametrize("answer_type", list(api_client.ANSWER_TYPES))
def test_every_answer_type_is_accepted(answer_type):
    answer = NOT_FOUND if answer_type == "not_found" else "x"
    conflicts = [{"doc_ids": ["A", "B"]}] if answer_type == "conflict_flagged" else []
    data, warnings = validate_ask_response(ask_body(answer_type, answer=answer, conflicts_detected=conflicts))
    assert data["answer_type"] == answer_type
    assert warnings == []


def test_full_response_is_preserved():
    body = ask_body("calculated", citations=[CITATION], tools_invoked=[TOOL], applied_rules=[RULE],
                    conflicts_detected=[{"doc_ids": ["A", "B"], "resolved": True}], explanation="because")
    data, warnings = validate_ask_response(body)
    assert data["citations"] == [CITATION]
    assert data["tools_invoked"] == [TOOL]
    assert data["applied_rules"] == [RULE]
    assert data["conflicts_detected"][0]["resolved"] is True
    assert data["explanation"] == "because"
    assert warnings == []


def test_missing_optional_fields_default_to_empty():
    body = {k: v for k, v in ask_body().items() if k in ("trace_id", "answer", "answer_type", "as_of_date")}
    data, warnings = validate_ask_response(body)
    assert data["citations"] == data["tools_invoked"] == data["applied_rules"] == data["conflicts_detected"] == []
    assert data["explanation"] == ""
    assert warnings == []


def test_malformed_optional_fields_are_dropped_with_warnings():
    body = ask_body(citations="nope", tools_invoked=[{"input": {}}], applied_rules=[{"rule_id": "R1"}],
                    conflicts_detected=None, explanation=5)
    data, warnings = validate_ask_response(body)
    assert data["citations"] == data["tools_invoked"] == data["applied_rules"] == data["conflicts_detected"] == []
    assert len(warnings) == 5


def test_not_found_with_wrong_text_is_flagged():
    _, warnings = validate_ask_response(ask_body("not_found", answer="Sorry, no idea."))
    assert any("exact required message" in w for w in warnings)
    _, warnings = validate_ask_response(ask_body("not_found", answer=NOT_FOUND))
    assert warnings == []


def test_conflict_flagged_without_conflicts_is_flagged():
    _, warnings = validate_ask_response(ask_body("conflict_flagged"))
    assert any("no conflicts" in w for w in warnings)


def test_validate_raises_on_missing_trace_id():
    body = ask_body()
    del body["trace_id"]
    with pytest.raises(ResponseFormatError):
        validate_ask_response(body)


# --- health / sources / audit / ingest --------------------------------------------------

def test_health_ok():
    client, _ = make(static(body=HEALTH_OK))
    result = client.health()
    assert result.ok and result.data["status"] == "ok" and result.warnings == []


def test_health_degraded_and_missing_components():
    body = {"status": "degraded", "api": {"status": "ok"}, "sqlite": {"status": "down", "detail": "no db"},
            "llm": {"status": "bogus"}}
    client, _ = make(static(body=body))
    result = client.health()
    assert result.ok
    assert result.data["sqlite"] == {"status": "down", "detail": "no db"}
    assert result.data["vector_store"]["status"] == "unknown"
    assert result.data["llm"]["status"] == "unknown"
    assert len(result.warnings) == 2


def test_health_unreachable():
    client, _ = make(raising(ConnectionErr()))
    assert not client.health().ok


@pytest.mark.parametrize("body", [[{"doc_id": "D1"}], {"sources": [{"doc_id": "D1"}]}])
def test_sources_list_or_wrapped(body):
    client, _ = make(static(body=body))
    assert client.sources().data == [{"doc_id": "D1"}]


def test_sources_empty():
    client, _ = make(static(body=[]))
    result = client.sources()
    assert result.ok and result.data == []


def test_audit_found_and_missing():
    client, session = make(static(body={"trace_id": "t1", "answer_type": "refused"}))
    assert client.audit(" t1 ").data["trace_id"] == "t1"
    assert session.calls[0]["url"] == "http://api.test/audit/t1"
    client, _ = make(static(404, {"detail": "not found"}))
    result = client.audit("nope")
    assert not result.ok and "No audit record" in result.error


def test_audit_rejects_path_tricks():
    client, session = make(static(body={}))
    assert not client.audit("../health").ok
    assert session.calls == []


def test_ingest_not_implemented():
    client, _ = make(static(501, {"detail": "Not implemented"}))
    result = client.ingest("a.txt", b"x", {"doc_id": "D1"})
    assert not result.ok and "not implemented" in result.error.lower()


def test_ingest_success_sends_multipart_and_metadata_json():
    client, session = make(static(body={"doc_id": "D1", "chunks_indexed": 7, "status": "indexed"}))
    result = client.ingest("a.txt", b"hello", {"doc_id": "D1", "authority_level": 2}, "text/plain")
    call = session.calls[0]
    assert result.ok and result.data["chunks_indexed"] == 7
    assert call["files"]["file"] == ("a.txt", b"hello", "text/plain")
    assert call["data"]["metadata"] == '{"doc_id": "D1", "authority_level": 2}'


def test_ingest_malformed_success_body():
    client, _ = make(static(body={"status": "ok"}))
    assert not client.ingest("a.txt", b"x", {}).ok


# --- identity helpers ------------------------------------------------------------------

@pytest.mark.parametrize("sid, ok", [("S1001", True), ("S9001", True), ("s1001", False), ("S12", False), ("", False), ("1001", False)])
def test_student_id_format(sid, ok):
    # Judge IDs (S9000-S9999) must be accepted by the UI even though our own data avoids them.
    assert api_client.is_valid_student_id(sid) is ok


# --- contract sync ---------------------------------------------------------------------

def test_mirrored_constants_match_frozen_contract():
    models_path = ROOT / "api" / "models.py"
    if not models_path.exists():
        pytest.skip("api/models.py not present in this checkout")
    from api import models
    assert api_client.NOT_FOUND_MESSAGE == models.NOT_FOUND_MESSAGE
    assert api_client.STUDENT_ID_HEADER == models.STUDENT_ID_HEADER
    assert set(api_client.ANSWER_TYPES) == set(models.AnswerType.__args__)
    for name in api_client.HEALTH_COMPONENTS:
        assert name in models.HealthResponse.model_fields
