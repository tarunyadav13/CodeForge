"""Shared fakes for Member 3's mocked HTTP tests (no network, no Ollama)."""
from __future__ import annotations

import json

import requests

NOT_FOUND = "I could not find this information in the authorised university sources."


def ask_body(answer_type="retrieved_fact", **overrides):
    body = {
        "trace_id": "trace-123",
        "answer": "Minimum attendance is 75%.",
        "answer_type": answer_type,
        "citations": [],
        "tools_invoked": [],
        "applied_rules": [],
        "conflicts_detected": [],
        "explanation": "",
        "as_of_date": "2026-10-01",
    }
    body.update(overrides)
    return body


CITATION = {"doc_id": "DOC-REG-01", "title": "Academic Regulations", "section": "4.2", "page": 12,
            "version": "2024.1", "effective_from": "2024-07-01"}
TOOL = {"tool": "get_attendance", "input": {"student_id": "S1001", "course_code": "CS201"},
        "output": {"classes_held": 50, "classes_attended": 37, "attendance_pct": 74.0}}
RULE = {"rule_id": "R-ATT-01", "value": "75", "source_doc_id": "DOC-REG-01"}
HEALTH_OK = {"status": "ok", **{k: {"status": "ok", "detail": None} for k in ("api", "vector_store", "sqlite", "llm")}}


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")
        self.reason = "fake"

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Records requests; `responder(method, url, kwargs)` returns a FakeResponse or raises."""

    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responder(method, url, kwargs)


def raising(exc):
    def responder(method, url, kwargs):
        raise exc
    return responder


def static(status=200, body=None, text=None):
    return lambda method, url, kwargs: FakeResponse(status, body, text)


ConnectionErr = requests.exceptions.ConnectionError
TimeoutErr = requests.exceptions.ReadTimeout
