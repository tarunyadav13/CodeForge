"""Member 3: Streamlit UI rendering with mocked HTTP (streamlit.testing AppTest, no backend)."""
import sys
from pathlib import Path

import pytest
import requests
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.m3_fakes import CITATION, HEALTH_OK, NOT_FOUND, RULE, TOOL, FakeResponse, ask_body  # noqa: E402
from ui.formatting import ANSWER_TYPE_DISPLAY, conflict_is_unresolved, conflict_summary, format_value  # noqa: E402

APP = str(ROOT / "ui" / "app.py")


@pytest.fixture
def backend(monkeypatch):
    """Patch requests.Session.request; set .ask / .health to a body or an exception."""
    state = {"ask": ask_body(), "health": HEALTH_OK, "calls": []}

    def fake_request(self, method, url, **kwargs):
        state["calls"].append({"method": method, "url": url, **kwargs})
        key = "ask" if url.endswith("/ask") else "health" if url.endswith("/health") else None
        value = state.get(key)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, FakeResponse):
            return value
        return FakeResponse(200, value)

    monkeypatch.setattr(requests.Session, "request", fake_request)
    return state


def run_app():
    at = AppTest.from_file(APP, default_timeout=20)
    at.run()
    return at


def ask(at, question, student_id=""):
    if student_id:
        at.sidebar.text_input[0].set_value(student_id)
    at.chat_input[0].set_value(question).run()
    return at


def all_text(at):
    parts = []
    for kind in ("title", "markdown", "caption", "error", "warning", "info", "success", "code"):
        parts += [str(e.value) for e in getattr(at, kind)]
    return "\n".join(parts)


def test_app_starts_and_shows_health(backend):
    at = run_app()
    assert not at.exception
    text = all_text(at)
    assert "University Student Services Assistant" in text
    assert "Overall:" in text and "SQLite:" in text


def test_health_failure_is_reported(backend):
    backend["health"] = requests.exceptions.ConnectionError()
    at = run_app()
    assert not at.exception
    assert any("Health check failed" in e.value for e in at.sidebar.error)


def test_degraded_components_shown(backend):
    backend["health"] = {"status": "degraded", "api": {"status": "ok"}, "vector_store": {"status": "not_configured"},
                         "sqlite": {"status": "down", "detail": "db missing"}}
    at = run_app()
    text = all_text(at)
    assert "degraded" in text and "down" in text and "db missing" in text and "unknown" in text


@pytest.mark.parametrize("answer_type", list(ANSWER_TYPE_DISPLAY))
def test_every_answer_type_renders(backend, answer_type):
    answer = NOT_FOUND if answer_type == "not_found" else f"answer for {answer_type}"
    conflicts = [{"doc_ids": ["A", "B"], "reason": "same level"}] if answer_type == "conflict_flagged" else []
    backend["ask"] = ask_body(answer_type, answer=answer, conflicts_detected=conflicts, citations=[CITATION])
    at = ask(run_app(), "question?")
    assert not at.exception
    text = all_text(at)
    assert ANSWER_TYPE_DISPLAY[answer_type][0] in text
    assert answer in text
    assert "trace-123" in text


def test_header_and_as_of_date_sent(backend):
    at = run_app()
    at.sidebar.toggle[0].set_value(True).run()
    at.sidebar.date_input[0].set_value("2025-01-15")
    ask(at, "What is my attendance?", "s1001")
    call = [c for c in backend["calls"] if c["url"].endswith("/ask")][-1]
    assert call["headers"] == {"X-Student-Id": "S1001"}
    assert call["json"] == {"question": "What is my attendance?", "as_of_date": "2025-01-15"}


def test_no_student_id_sends_no_header(backend):
    ask(run_app(), "What is the attendance policy?")
    call = [c for c in backend["calls"] if c["url"].endswith("/ask")][-1]
    assert call["headers"] == {}


def test_citations_tools_rules_rendered(backend):
    backend["ask"] = ask_body("calculated", citations=[CITATION], tools_invoked=[TOOL], applied_rules=[RULE])
    at = ask(run_app(), "Am I eligible for CS201?", "S1001")
    labels = [e.label for e in at.expander]
    assert "Citations (1)" in labels and "Tools invoked (1)" in labels and "Applied rules (1)" in labels
    frames = [df.value for df in at.dataframe]
    flat = "\n".join(f.to_string() for f in frames)
    for expected in ("DOC-REG-01", "Academic Regulations", "Section 4.2, p. 12", "2024.1", "2024-07-01",
                     "get_attendance", "attendance_pct", "74", "R-ATT-01"):
        assert expected in flat or expected in all_text(at), expected


def test_retrieved_fact_without_citations_warns(backend):
    backend["ask"] = ask_body("retrieved_fact", citations=[])
    at = ask(run_app(), "policy?")
    assert any("without citations" in w.value for w in at.warning)


def test_unresolved_conflict_is_visually_distinct(backend):
    backend["ask"] = ask_body("conflict_flagged", conflicts_detected=[{"doc_ids": ["A", "B"]}], citations=[CITATION])
    at = ask(run_app(), "fee?")
    warnings = " ".join(w.value for w in at.warning)
    assert "Unresolved policy conflict" in warnings and "Unresolved:" in warnings


def test_resolved_conflict_shown_as_info(backend):
    backend["ask"] = ask_body("retrieved_fact", citations=[CITATION],
                              conflicts_detected=[{"summary": "Circular overrides regulation", "resolved": True}])
    at = ask(run_app(), "attendance?")
    assert any("Resolved:" in i.value and "Circular overrides" in i.value for i in at.info)


def test_refusal_rendered_as_error(backend):
    backend["ask"] = ask_body("refused", answer="You can only view your own records.")
    at = ask(run_app(), "Show S1002's attendance", "S1001")
    assert any("only view your own records" in e.value for e in at.error)


def test_not_found_exact_text(backend):
    backend["ask"] = ask_body("not_found", answer=NOT_FOUND)
    at = ask(run_app(), "parking fee?")
    assert any(i.value == NOT_FOUND for i in at.info)


@pytest.mark.parametrize("failure, fragment", [
    (requests.exceptions.ConnectionError(), "Cannot reach the API"),
    (requests.exceptions.ReadTimeout(), "timeout"),
    (FakeResponse(500, {"detail": "graph crashed"}), "HTTP 500"),
    (FakeResponse(200, {"answer": "no trace id"}), "Malformed"),
])
def test_api_failure_shows_error_and_no_answer(backend, failure, fragment):
    backend["ask"] = failure
    at = ask(run_app(), "anything")
    assert not at.exception
    errors = [e.value for e in at.error]
    assert any(fragment in e for e in errors), errors
    assert "no trace id" not in all_text(at)


def test_new_conversation_clears_history(backend):
    at = ask(run_app(), "first question")
    assert at.session_state.messages
    next(b for b in at.sidebar.button if b.label == "New conversation").click().run()
    assert at.session_state.messages == []


def test_contract_warnings_surface(backend):
    backend["ask"] = ask_body("not_found", answer="Nothing here")
    at = ask(run_app(), "q")
    assert "exact required message" in all_text(at)


# --- pure helpers -----------------------------------------------------------------------

def test_conflict_helpers():
    assert conflict_is_unresolved({}, "conflict_flagged")
    assert not conflict_is_unresolved({"resolved": True}, "retrieved_fact")
    assert conflict_is_unresolved({"status": "UNRESOLVED"}, "retrieved_fact")
    assert conflict_summary({"sources": [{"doc_id": "A"}, "B"]}) == "Conflicting sources: A, B"
    assert conflict_summary({}) == "Conflict details below."


def test_format_value():
    assert format_value(74.0) == "74" and format_value(74.25) == "74.25" and format_value({"a": 1}) == '{"a": 1}'


# --- untrusted document content (R8) ---------------------------------------------------------

from ui.formatting import safe_text  # noqa: E402


def test_safe_text_removes_images_and_defangs_links():
    out = safe_text("See ![x](https://evil.test/p.png?d=secret) and [click](https://evil.test/a) now")
    assert "![" not in out and "](https://" not in out
    assert "image not shown: x" in out and "click (`https://evil.test/a`)" in out
    assert safe_text("![a][ref]") == "[image not shown: a]"
    assert safe_text("Minimum attendance is 75% (clause 5.8).") == "Minimum attendance is 75% (clause 5.8)."
    assert safe_text(NOT_FOUND) == NOT_FOUND


@pytest.mark.parametrize("answer_type", ["retrieved_fact", "calculated", "conflict_flagged"])
def test_ui_never_renders_markdown_images_from_answers(backend, answer_type):
    evil = "Ok. ![t](https://evil.test/leak.png?q=abc) [more](https://evil.test/x)"
    backend["ask"] = ask_body(answer_type, answer=evil, explanation=evil, citations=[CITATION],
                              conflicts_detected=[{"summary": evil, "doc_ids": ["A", "B"]}])
    at = ask(run_app(), "question?")
    assert not at.exception
    text = all_text(at)
    assert "![" not in text and "](https://evil.test" not in text
    assert "image not shown" in text
