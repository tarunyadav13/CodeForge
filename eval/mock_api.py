"""MOCK-ONLY stand-in for the API, used by `python eval/runner.py --mock`.

This is NOT the backend and NOT a model of it. It replays, for each case in the dataset, a canned
response that matches the case's own expectations (an "oracle"). Running the runner against it
proves only that the dataset parses, the HTTP/contract layer works and the scoring code behaves.
It says nothing about retrieval, precedence, tools, security or latency of the real system.

Deliberate limits so no number is invented:
  * /audit/{id} always returns 404, so retrieval hit rate, LLM calls and tokens stay N/A.
  * Latency is measured on in-process calls and is reported N/A in mock mode by the runner.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOT_FOUND = "I could not find this information in the authorised university sources."


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.reason = status, body, "mock"
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _register() -> dict:
    path = ROOT / "data" / "source_register.csv"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {r["doc_id"]: r for r in csv.DictReader(f)}


class MockSession:
    """Quacks like requests.Session for ApiClient; answers /ask from the dataset's expectations."""

    def __init__(self, cases: list[dict]):
        self.cases = {(c["question"], c.get("student_id") or "", c.get("as_of_date") or ""): c for c in cases}
        self.register = _register()
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if method == "POST" and url.endswith("/ask"):
            key = (kwargs["json"]["question"], kwargs.get("headers", {}).get("X-Student-Id", ""),
                   kwargs["json"].get("as_of_date", ""))
            case = self.cases.get(key)
            if case is None:
                return _Resp(422, {"detail": "mock: question not in dataset"})
            return _Resp(200, self._answer(case, len(self.calls)))
        return _Resp(404, {"detail": "mock backend: only POST /ask is replayed"})

    def _answer(self, case: dict, n: int) -> dict:
        atype = case["expected_answer_type"][0]
        body = {"trace_id": f"mock-{n:04d}", "answer_type": atype, "citations": [], "tools_invoked": [],
                "applied_rules": [], "conflicts_detected": [], "explanation": "MOCK-ONLY canned response",
                "as_of_date": case.get("as_of_date") or "2026-10-23"}
        if atype == "not_found":
            body["answer"] = NOT_FOUND
            return body
        if atype in ("refused", "clarification_needed"):
            body["answer"] = ("MOCK: request cannot be answered for this identity." if atype == "refused"
                              else "MOCK: please clarify.")
            return body
        body["answer"] = "MOCK answer: " + ", ".join(case.get("expected_answer_contains") or ["(no expected content)"])
        for doc_id in case.get("expected_citation_doc_ids") or []:
            reg = self.register.get(doc_id, {})
            body["citations"].append({"doc_id": doc_id, "title": reg.get("title", doc_id), "section": "MOCK",
                                      "version": reg.get("version") or "MOCK",
                                      "effective_from": reg.get("effective_from") or "2000-01-01"})
        if case.get("requires_citations") and not body["citations"]:
            body["citations"].append({"doc_id": "MOCK-DOC", "title": "mock", "section": "MOCK",
                                      "version": "MOCK", "effective_from": "2000-01-01"})
        sid = case.get("student_id")
        for tool in case.get("expected_tools") or []:
            out = dict((case.get("expected_tool_outputs") or {}).get(tool, {}))
            body["tools_invoked"].append({"tool": tool, "input": {"student_id": sid} if sid else {}, "output": out})
        if case.get("requires_applied_rules"):
            body["applied_rules"].append({"rule_id": "MOCK-RULE", "value": "MOCK", "source_doc_id": "MOCK-DOC"})
        if case.get("requires_conflicts"):
            body["conflicts_detected"].append({"doc_ids": case.get("expected_citation_doc_ids") or [], "status": "mock"})
        return body
