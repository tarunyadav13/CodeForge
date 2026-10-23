"""FastAPI app - shared foundation.

Only GET /health is real. POST /ask returns a clearly-labelled MOCK so the UI and
the backend/RAG work can proceed in parallel. Every mock answer is prefixed with
"[MOCK]" and must be replaced by the LangGraph workflow (Member 1 + Member 2).
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException

from api.models import (
    NOT_FOUND_MESSAGE,
    AppliedRule,
    AskRequest,
    AskResponse,
    AuditRecord,
    Citation,
    ComponentStatus,
    HealthResponse,
    IngestResponse,
    SourceRecord,
    ToolInvocation,
)

app = FastAPI(title="University Student Services Assistant", version="0.1.0-foundation")

STUDENT_ID_RE = re.compile(r"^S\d{4}$")  # Annex C: 'S' followed by 4 digits
PERSONAL_HINTS = ("my ", "am i", "i am", "i'm", "am i eligible")
POLICY_HINTS = ("attendance", "eligib", "minimum", "policy", "rule", "supplementary")

# In-memory audit store for the mock only. Real implementation persists to SQLite.
_AUDIT: dict[str, AuditRecord] = {}


def _new_trace_id() -> str:
    return uuid.uuid4().hex[:8]


def _is_personal(question: str) -> bool:
    q = question.lower()
    return any(h in q for h in PERSONAL_HINTS)


def _record_audit(resp: AskResponse, student_id: Optional[str], category: str) -> None:
    _AUDIT[resp.trace_id] = AuditRecord(
        trace_id=resp.trace_id,
        timestamp=datetime.now(timezone.utc),
        student_id=student_id,
        question_category=category,
        tools_invoked=[{"tool": t.tool, "status": "mock"} for t in resp.tools_invoked],
        answer_type=resp.answer_type,
        model="MOCK",
    )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Real endpoint. Components other than the API are not wired yet and say so."""
    return HealthResponse(
        status="ok",
        api=ComponentStatus(status="ok"),
        vector_store=ComponentStatus(status="not_configured", detail="ChromaDB not wired yet"),
        sqlite=ComponentStatus(status="not_configured", detail="SQLite not wired yet"),
        llm=ComponentStatus(status="not_configured", detail="Ollama not wired yet"),
    )


@app.post("/ask", response_model=AskResponse)
def ask(
    body: AskRequest,
    x_student_id: Optional[str] = Header(default=None, alias="X-Student-Id"),
) -> AskResponse:
    """MOCK. Identity comes only from the X-Student-Id header (R7), never from the text."""
    as_of = body.as_of_date or date.today()
    trace_id = _new_trace_id()
    personal = _is_personal(body.question)

    if personal and x_student_id is None:
        resp = AskResponse(
            trace_id=trace_id,
            answer="[MOCK] This question needs your student identity. No X-Student-Id was provided.",
            answer_type="refused",
            explanation="Personal questions require the X-Student-Id request header.",
            as_of_date=as_of,
        )
        category = "personal_no_identity"
    elif personal and not STUDENT_ID_RE.match(x_student_id or ""):
        resp = AskResponse(
            trace_id=trace_id,
            answer="[MOCK] The supplied student identity is not in a valid format.",
            answer_type="refused",
            explanation="X-Student-Id must be 'S' followed by 4 digits.",
            as_of_date=as_of,
        )
        category = "personal_bad_identity"
    elif personal:
        resp = AskResponse(
            trace_id=trace_id,
            answer="[MOCK] Placeholder calculated answer - no real student data was read.",
            answer_type="calculated",
            tools_invoked=[
                ToolInvocation(
                    tool="mock_check_exam_eligibility",
                    input={"course_code": "MOCK101"},
                    output={"result": "MOCK", "note": "not computed"},
                )
            ],
            applied_rules=[
                AppliedRule(rule_id="MOCK-RULE-01", value="MOCK", source_doc_id="SYNTHETIC-MOCK-DOC")
            ],
            explanation="Mock explanation. Real tools will compute this from SQLite and rule_registry.",
            as_of_date=as_of,
        )
        category = "personal_eligibility"
    elif any(h in body.question.lower() for h in POLICY_HINTS):
        resp = AskResponse(
            trace_id=trace_id,
            answer="[MOCK] Placeholder policy answer - no document was retrieved.",
            answer_type="retrieved_fact",
            citations=[
                Citation(
                    doc_id="SYNTHETIC-MOCK-DOC",
                    title="Synthetic placeholder document (not a real regulation)",
                    section="0.0",
                    page=1,
                    version="0",
                    effective_from=date(2000, 1, 1),
                )
            ],
            explanation="Mock. Real answers will be grounded in retrieved text from ChromaDB.",
            as_of_date=as_of,
        )
        category = "policy"
    else:
        resp = AskResponse(
            trace_id=trace_id,
            answer=NOT_FOUND_MESSAGE,
            answer_type="not_found",
            as_of_date=as_of,
        )
        category = "not_found"

    _record_audit(resp, x_student_id, category)
    return resp


@app.post("/ingest", response_model=IngestResponse, status_code=501)
def ingest() -> IngestResponse:
    """Not implemented in the foundation. Owned by Member 1 (indexing) + Member 2 (register)."""
    raise HTTPException(status_code=501, detail="POST /ingest is not implemented yet.")


@app.get("/audit/{trace_id}", response_model=AuditRecord)
def audit(trace_id: str) -> AuditRecord:
    """Returns mock audit records created by this process only."""
    rec = _AUDIT.get(trace_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Unknown trace_id.")
    return rec


@app.get("/sources", response_model=list[SourceRecord])
def sources() -> list[SourceRecord]:
    """Source Register. Empty until ingestion exists."""
    return []
