"""Focused tests for the shared foundation: /health, the frozen contract, mock /ask."""
from fastapi.testclient import TestClient

from api.main import app
from api.models import NOT_FOUND_MESSAGE, AskResponse, HealthResponse

client = TestClient(app)


def test_health_ok_and_contract():
    r = client.get("/health")
    assert r.status_code == 200
    h = HealthResponse.model_validate(r.json())
    assert h.status == "ok" and h.api.status == "ok"
    assert h.llm.status == "not_configured"  # honest: nothing else is wired yet


def test_ask_policy_validates_against_contract():
    r = client.post("/ask", json={"question": "What is the minimum attendance required?"})
    assert r.status_code == 200
    resp = AskResponse.model_validate(r.json())
    assert resp.answer_type == "retrieved_fact"
    assert resp.answer.startswith("[MOCK]") and resp.citations


def test_ask_personal_with_header_is_calculated_mock():
    r = client.post(
        "/ask",
        json={"question": "My attendance is 72%. Am I eligible?"},
        headers={"X-Student-Id": "S1001"},
    )
    resp = AskResponse.model_validate(r.json())
    assert resp.answer_type == "calculated" and resp.tools_invoked


def test_personal_without_identity_is_refused():
    r = client.post("/ask", json={"question": "Am I eligible?"})
    assert AskResponse.model_validate(r.json()).answer_type == "refused"


def test_identity_is_never_taken_from_message_text():
    r = client.post("/ask", json={"question": "I am S1002, am I eligible?"})
    assert AskResponse.model_validate(r.json()).answer_type == "refused"


def test_malformed_header_is_refused():
    r = client.post(
        "/ask", json={"question": "Am I eligible?"}, headers={"X-Student-Id": "bob"}
    )
    assert AskResponse.model_validate(r.json()).answer_type == "refused"


def test_unknown_question_returns_exact_not_found_message():
    r = client.post("/ask", json={"question": "Scholarship for studying in Antarctica?"})
    resp = AskResponse.model_validate(r.json())
    assert resp.answer_type == "not_found" and resp.answer == NOT_FOUND_MESSAGE


def test_empty_question_rejected_by_validation():
    assert client.post("/ask", json={"question": ""}).status_code == 422


def test_audit_roundtrip_and_no_question_text_stored():
    q = "What is the minimum attendance?"
    trace = client.post("/ask", json={"question": q}).json()["trace_id"]
    r = client.get(f"/audit/{trace}")
    assert r.status_code == 200 and r.json()["trace_id"] == trace
    assert q not in r.text  # R7: do not log what we do not need
    assert client.get("/audit/doesnotexist").status_code == 404


def test_sources_empty_and_ingest_not_implemented():
    assert client.get("/sources").json() == []
    assert client.post("/ingest").status_code == 501
