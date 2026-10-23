"""Shared API contract (Pydantic v2) for the University Student Services Assistant.

FROZEN against the official HCLTech brief, Section 6 (API contract) and Annex D.
Member 1 (graph), Member 2 (API/tools) and Member 3 (UI/eval) all code against
these models. Do not rename or remove fields without telling the whole team.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Section 6.2 - the six answer types, exactly as named in the brief.
AnswerType = Literal[
    "retrieved_fact",
    "calculated",
    "not_found",
    "clarification_needed",
    "refused",
    "conflict_flagged",
]

# Section 3 / R3: exact message required when evidence is insufficient.
NOT_FOUND_MESSAGE = "I could not find this information in the authorised university sources."

# Header that carries the student identity (R7). Never read identity from message text.
STUDENT_ID_HEADER = "X-Student-Id"


class AskRequest(BaseModel):
    """POST /ask body. Identity is NOT here - it comes from the X-Student-Id header."""

    question: str = Field(min_length=1, max_length=2000)
    as_of_date: Optional[date] = Field(
        default=None, description="YYYY-MM-DD; the handler defaults it to today."
    )


class Citation(BaseModel):
    doc_id: str
    title: str
    section: Optional[str] = None
    page: Optional[int] = None
    version: Optional[str] = None
    effective_from: Optional[date] = None


class ToolInvocation(BaseModel):
    tool: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)


class AppliedRule(BaseModel):
    rule_id: str
    value: str
    source_doc_id: str


class AskResponse(BaseModel):
    """One response shape for every answer type (R9). Unused lists stay empty."""

    trace_id: str
    answer: str
    answer_type: AnswerType
    citations: list[Citation] = Field(default_factory=list)
    tools_invoked: list[ToolInvocation] = Field(default_factory=list)
    applied_rules: list[AppliedRule] = Field(default_factory=list)
    conflicts_detected: list[dict[str, Any]] = Field(default_factory=list)
    explanation: str = ""
    as_of_date: date


class ComponentStatus(BaseModel):
    status: Literal["ok", "degraded", "down", "not_configured"]
    detail: Optional[str] = None


class HealthResponse(BaseModel):
    """GET /health - status of API, vector store, SQLite and LLM (Section 6)."""

    status: Literal["ok", "degraded", "down"]
    api: ComponentStatus
    vector_store: ComponentStatus
    sqlite: ComponentStatus
    llm: ComponentStatus


class SourceRecord(BaseModel):
    """One Source Register row (Annex B). Same fields are the POST /ingest metadata."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    title: str
    issuer: str
    authority_level: int = Field(ge=1, le=5)
    doc_type: Literal["regulation", "circular", "notice", "faq", "handbook", "unofficial"]
    version: str
    effective_from: date
    effective_to: Optional[date] = None
    supersedes: str = ""
    scope_programmes: str = "ALL"
    scope_batches: str = "ALL"
    provenance: str
    retrieved_on: date
    synthetic: Literal["Y", "N"] = "N"


class IngestResponse(BaseModel):
    doc_id: str
    chunks_indexed: int
    status: str


class AuditRecord(BaseModel):
    """Annex D. Holds no raw question text, to avoid logging personal data (R7)."""

    trace_id: str
    timestamp: datetime
    student_id: Optional[str] = None
    question_category: str
    sources_retrieved: list[dict[str, Any]] = Field(default_factory=list)
    precedence_decision: Optional[str] = None
    tools_invoked: list[dict[str, Any]] = Field(default_factory=list)
    answer_type: AnswerType
    model: str
    llm_calls: int = 0
    tokens: int = 0
    latency_ms: int = 0
