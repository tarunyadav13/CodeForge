"""HTTP client for the Student Services API, used by the Streamlit UI and the eval runner.

Mirrors the frozen contract in api/models.py without importing it, so the UI can run
as a separate process (and on a separate machine) from the API. Every call returns an
ApiResult; nothing here ever invents an answer when the API fails.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

import requests

# --- Mirrored constants from api/models.py (tests check they still match) ---------------
ANSWER_TYPES = (
    "retrieved_fact",
    "calculated",
    "not_found",
    "clarification_needed",
    "refused",
    "conflict_flagged",
)
NOT_FOUND_MESSAGE = "I could not find this information in the authorised university sources."
STUDENT_ID_HEADER = "X-Student-Id"
COMPONENT_STATUSES = ("ok", "degraded", "down", "not_configured")
HEALTH_COMPONENTS = ("api", "vector_store", "sqlite", "llm")

# Annex C: student_id is 'S' + 4 digits.
STUDENT_ID_PATTERN = re.compile(r"^S\d{4}$")

# POST /ingest multipart field names (handoff: "multipart file + metadata JSON").
INGEST_FILE_FIELD = "file"
INGEST_METADATA_FIELD = "metadata"

# There is deliberately NO default URL: "localhost" on a hosted UI means the UI's own server, so a default would silently
# point a deployed app at nothing. Configure it (see get_api_base_url); locally: $env:API_BASE_URL = "http://127.0.0.1:8000".
NOT_CONFIGURED_MESSAGE = (
    "The API base URL is not configured. Set API_BASE_URL (Streamlit Community Cloud: App settings -> Secrets -> "
    'API_BASE_URL = "https://your-api.example.com"; locally: $env:API_BASE_URL = "http://127.0.0.1:8000") '
    "or type it into the sidebar."
)
# (connect, read) seconds. /ask runs retrieval + a local LLM, so it gets a long read timeout.
ASK_TIMEOUT = (5.0, float(os.getenv("ASK_TIMEOUT_SECONDS", "120")))
SHORT_TIMEOUT = (5.0, 15.0)
INGEST_TIMEOUT = (5.0, float(os.getenv("INGEST_TIMEOUT_SECONDS", "300")))


def get_api_base_url() -> str:
    """Streamlit secret ``API_BASE_URL``, else the ``API_BASE_URL`` environment variable, else "" (not configured).

    Streamlit Community Cloud also exports top-level secrets as environment variables; the explicit secrets lookup
    keeps this working when the module runs under Streamlit with a secrets file only.
    """
    try:
        import streamlit as st

        value = st.secrets.get("API_BASE_URL")  # raises if no secrets file exists
    except Exception:  # noqa: BLE001 - no Streamlit, or no secrets configured: fall through to the environment
        value = None
    return str(value or os.getenv("API_BASE_URL") or "").strip().rstrip("/")


class ResponseFormatError(ValueError):
    """The API answered, but not in the shape the contract promises."""


@dataclass
class ApiResult:
    ok: bool
    data: Any = None
    error: Optional[str] = None
    status_code: Optional[int] = None
    latency_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)


def is_valid_student_id(student_id: str) -> bool:
    return bool(STUDENT_ID_PATTERN.match(student_id or ""))


# --- Response validation -----------------------------------------------------------------

def _require_str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ResponseFormatError(f"field '{key}' is missing or not a string")
    return value


def _list_of_dicts(data: dict, key: str, warnings: list[str]) -> list[dict]:
    value = data.get(key)
    if value is None:
        if key in data:
            warnings.append(f"'{key}' was null; treated as empty")
        return []
    if not isinstance(value, list):
        warnings.append(f"'{key}' is not a list; ignored")
        return []
    items = [v for v in value if isinstance(v, dict)]
    if len(items) != len(value):
        warnings.append(f"'{key}' contained {len(value) - len(items)} non-object item(s); ignored")
    return items


def validate_ask_response(data: Any) -> tuple[dict, list[str]]:
    """Check an /ask body against AskResponse. Returns (normalised copy, warnings).

    Required fields (trace_id, answer, answer_type, as_of_date) raise ResponseFormatError.
    Optional list fields that are missing or malformed become empty lists plus a warning,
    so the UI can still show the answer while flagging the contract problem.
    """
    if not isinstance(data, dict):
        raise ResponseFormatError("response body is not a JSON object")
    warnings: list[str] = []
    out: dict[str, Any] = {
        "trace_id": _require_str(data, "trace_id"),
        "answer": _require_str(data, "answer"),
        "answer_type": _require_str(data, "answer_type"),
    }
    if out["answer_type"] not in ANSWER_TYPES:
        raise ResponseFormatError(f"unknown answer_type '{out['answer_type']}'")
    as_of = data.get("as_of_date")
    try:
        date.fromisoformat(as_of)
    except (TypeError, ValueError):
        raise ResponseFormatError("field 'as_of_date' is missing or not YYYY-MM-DD") from None
    out["as_of_date"] = as_of

    citations = []
    for c in _list_of_dicts(data, "citations", warnings):
        if not isinstance(c.get("doc_id"), str) or not isinstance(c.get("title"), str):
            warnings.append("a citation without doc_id/title was ignored")
            continue
        citations.append(c)
    out["citations"] = citations

    tools = []
    for t in _list_of_dicts(data, "tools_invoked", warnings):
        if not isinstance(t.get("tool"), str):
            warnings.append("a tool invocation without a tool name was ignored")
            continue
        tools.append({
            "tool": t["tool"],
            "input": t.get("input") if isinstance(t.get("input"), dict) else {},
            "output": t.get("output") if isinstance(t.get("output"), dict) else {},
        })
    out["tools_invoked"] = tools

    rules = []
    for r in _list_of_dicts(data, "applied_rules", warnings):
        if not all(k in r for k in ("rule_id", "value", "source_doc_id")):
            warnings.append("an applied rule missing rule_id/value/source_doc_id was ignored")
            continue
        rules.append(r)
    out["applied_rules"] = rules

    out["conflicts_detected"] = _list_of_dicts(data, "conflicts_detected", warnings)

    explanation = data.get("explanation", "")
    if not isinstance(explanation, str):
        warnings.append("'explanation' is not a string; ignored")
        explanation = ""
    out["explanation"] = explanation

    if out["answer_type"] == "not_found" and out["answer"] != NOT_FOUND_MESSAGE:
        warnings.append("answer_type is not_found but the answer is not the exact required message")
    if out["answer_type"] == "conflict_flagged" and not out["conflicts_detected"]:
        warnings.append("answer_type is conflict_flagged but no conflicts were listed")
    return out, warnings


def validate_health_response(data: Any) -> tuple[dict, list[str]]:
    if not isinstance(data, dict):
        raise ResponseFormatError("health body is not a JSON object")
    warnings: list[str] = []
    overall = data.get("status")
    if overall not in ("ok", "degraded", "down"):
        warnings.append(f"unexpected overall status {overall!r}")
        overall = "unknown"
    components = {}
    for name in HEALTH_COMPONENTS:
        comp = data.get(name)
        if not isinstance(comp, dict) or comp.get("status") not in COMPONENT_STATUSES:
            components[name] = {"status": "unknown", "detail": "missing from /health response"}
            warnings.append(f"component '{name}' missing or malformed")
        else:
            components[name] = {"status": comp["status"], "detail": comp.get("detail")}
    return {"status": overall, **components}, warnings


def normalise_sources(data: Any) -> list[dict]:
    """GET /sources: accept a bare list of SourceRecord or {"sources": [...]}."""
    if isinstance(data, dict) and isinstance(data.get("sources"), list):
        data = data["sources"]
    if not isinstance(data, list):
        raise ResponseFormatError("sources body is not a list")
    return [s for s in data if isinstance(s, dict)]


# --- HTTP --------------------------------------------------------------------------------

def _error_detail(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or "").strip()[:300] or resp.reason or ""
    if isinstance(body, dict) and "detail" in body:
        detail = body["detail"]
        return detail if isinstance(detail, str) else json.dumps(detail)[:300]
    return json.dumps(body)[:300]


class ApiClient:
    def __init__(self, base_url: Optional[str] = None, session: Optional[Any] = None):
        self.base_url = (base_url or get_api_base_url()).rstrip("/")
        self.session = session or requests.Session()

    def _request(self, method: str, path: str, timeout, **kwargs) -> tuple[ApiResult, Any]:
        """Do the HTTP call. Returns (result, parsed_json). result.ok is False on any failure."""
        if not self.base_url:
            return ApiResult(False, error=NOT_CONFIGURED_MESSAGE), None
        if not self.base_url.startswith(("http://", "https://")):
            return ApiResult(False, error=f"The API base URL {self.base_url!r} must start with http:// or https://."), None
        url = f"{self.base_url}{path}"
        start = time.perf_counter()
        try:
            resp = self.session.request(method, url, timeout=timeout, **kwargs)
        except requests.exceptions.Timeout:
            ms = (time.perf_counter() - start) * 1000
            return ApiResult(False, error=f"The API did not respond within the timeout ({url}).", latency_ms=ms), None
        except requests.exceptions.ConnectionError:
            ms = (time.perf_counter() - start) * 1000
            hint = (" 'localhost' means the machine running this UI; on a hosted UI use the API's public https URL."
                    if any(h in self.base_url for h in ("localhost", "127.0.0.1")) else "")
            return ApiResult(False, error=f"Cannot reach the API at {self.base_url}. Is the API running?{hint}", latency_ms=ms), None
        except requests.exceptions.RequestException as exc:
            ms = (time.perf_counter() - start) * 1000
            return ApiResult(False, error=f"Request failed: {exc.__class__.__name__}", latency_ms=ms), None
        ms = (time.perf_counter() - start) * 1000
        if resp.status_code >= 400:
            detail = _error_detail(resp)
            return ApiResult(False, error=f"HTTP {resp.status_code}: {detail}", status_code=resp.status_code, latency_ms=ms), None
        try:
            body = resp.json()
        except ValueError:
            return ApiResult(False, error="The API returned a response that is not valid JSON.", status_code=resp.status_code, latency_ms=ms), None
        return ApiResult(True, data=body, status_code=resp.status_code, latency_ms=ms), body

    def ask(self, question: str, student_id: Optional[str] = None, as_of_date: Optional[date] = None) -> ApiResult:
        payload: dict[str, Any] = {"question": question}
        if as_of_date is not None:
            payload["as_of_date"] = as_of_date.isoformat() if isinstance(as_of_date, date) else str(as_of_date)
        headers = {STUDENT_ID_HEADER: student_id} if student_id else {}
        result, body = self._request("POST", "/ask", ASK_TIMEOUT, json=payload, headers=headers)
        if not result.ok:
            return result
        try:
            result.data, result.warnings = validate_ask_response(body)
        except ResponseFormatError as exc:
            return ApiResult(False, error=f"Malformed /ask response: {exc}", status_code=result.status_code, latency_ms=result.latency_ms)
        return result

    def health(self) -> ApiResult:
        result, body = self._request("GET", "/health", SHORT_TIMEOUT)
        if not result.ok:
            return result
        try:
            result.data, result.warnings = validate_health_response(body)
        except ResponseFormatError as exc:
            return ApiResult(False, error=str(exc), status_code=result.status_code, latency_ms=result.latency_ms)
        return result

    def sources(self) -> ApiResult:
        result, body = self._request("GET", "/sources", SHORT_TIMEOUT)
        if not result.ok:
            return result
        try:
            result.data = normalise_sources(body)
        except ResponseFormatError as exc:
            return ApiResult(False, error=str(exc), status_code=result.status_code, latency_ms=result.latency_ms)
        return result

    def audit(self, trace_id: str) -> ApiResult:
        trace_id = trace_id.strip()
        if not trace_id or "/" in trace_id:
            return ApiResult(False, error="Enter a single trace ID.")
        result, body = self._request("GET", f"/audit/{requests.utils.quote(trace_id)}", SHORT_TIMEOUT)
        if not result.ok and result.status_code == 404:
            result.error = f"No audit record found for trace ID {trace_id}."
        elif result.ok and not isinstance(body, dict):
            return ApiResult(False, error="Audit response is not a JSON object.", status_code=result.status_code)
        return result

    def ingest(self, filename: str, content: bytes, metadata: dict, content_type: str = "application/octet-stream") -> ApiResult:
        files = {INGEST_FILE_FIELD: (filename, content, content_type)}
        form = {INGEST_METADATA_FIELD: json.dumps(metadata, default=str)}
        result, body = self._request("POST", "/ingest", INGEST_TIMEOUT, files=files, data=form)
        if not result.ok:
            if result.status_code == 501:
                result.error = "Ingestion is not implemented by the backend yet (HTTP 501)."
            return result
        if not isinstance(body, dict) or not isinstance(body.get("chunks_indexed"), int) or "doc_id" not in body:
            return ApiResult(False, error="Malformed /ingest response (expected doc_id, chunks_indexed, status).", status_code=result.status_code)
        return result
