"""FastAPI application (Member 2): identity, /ask, /ingest, /health, /audit, /sources.

Run:   uvicorn api.main:app --reload --port 8000

Trust model
* Student identity comes ONLY from the X-Student-Id header, validated against the students
  table. Student ids written in the question text are never used for authorization; a question
  that asks for another student's data is refused before any tool runs.
* Every refusal is an ordinary AskResponse with answer_type "refused" (HTTP 200).
* Raw question text is never stored or logged. The audit table holds categories, ids, tool
  inputs/outputs (minus full_name), rules, timing.

Integration hooks (so Member 1 plugs in without editing this file)
* graph.workflow.run_graph(question, student_id|None, as_of_date) -> AskResponse | dict | (AskResponse, audit_extras)
* rag.ingest.ingest_document(file_path, metadata: dict) -> {"chunks_indexed": int, "status": str}
* rag.retriever.health() -> {"status": "ok"|"degraded"|"down", "detail": str}
When a hook is absent, behaviour is honest: policy questions get HTTP 503, /ingest gets HTTP 501,
the vector store reports not_configured. Nothing is faked.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from pydantic import ValidationError

from api import personal
from api.models import (NOT_FOUND_MESSAGE, STUDENT_ID_HEADER, AskRequest, AskResponse, ComponentStatus,
                        HealthResponse, IngestResponse, SourceRecord)
from db import data_access as da

log = logging.getLogger("api")
ROOT = Path(__file__).resolve().parents[1]
STUDENT_ID_RE = re.compile(r"^S\d{4}$")
ALLOWED_UPLOAD_SUFFIXES = {".pdf", ".txt", ".md"}  # what rag.ingest can read; docx/html would only fail later
DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
SOURCE_FIELDS = tuple(SourceRecord.model_fields)
_AUTO = object()

REFUSAL_NO_IDENTITY = ("This question is about your personal records, but no student identity was provided. "
                       "Please sign in so your student ID can be sent in the X-Student-Id header.")
REFUSAL_BAD_IDENTITY = "The student identity provided is not valid, so I cannot access any student records."
REFUSAL_OTHER_STUDENT = ("I can only share your own records. I cannot provide another student's attendance, "
                         "results or other personal data.")


def _try_import(dotted: str, attr: str) -> tuple[Optional[Callable[..., Any]], str]:
    try:
        return getattr(importlib.import_module(dotted), attr), "available"
    except ImportError as exc:
        return None, f"{dotted} is not installed ({exc.__class__.__name__})"
    except AttributeError:
        return None, f"{dotted} has no '{attr}'"
    except Exception as exc:  # a broken module must not take the whole API down
        log.error("failed to import %s.%s: %s", dotted, attr, type(exc).__name__)
        return None, f"{dotted} failed to import ({type(exc).__name__})"


def create_app(db_path: Any = None, *, graph_runner: Any = _AUTO, ingest_handler: Any = _AUTO,
               vector_health: Any = _AUTO, upload_dir: Any = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        da.init_db(app.state.db_path)  # idempotent
        yield

    app = FastAPI(title="University Student Services Assistant", version="1.0", lifespan=lifespan)
    app.state.db_path = db_path
    app.state.upload_dir = Path(upload_dir) if upload_dir else ROOT / "data" / "uploads"
    hooks: dict[str, tuple[Optional[Callable[..., Any]], str]] = {}
    hooks["graph"] = _try_import("graph.workflow", "run_graph") if graph_runner is _AUTO else \
        (graph_runner, "injected" if graph_runner else "disabled")
    hooks["ingest"] = _try_import("rag.service", "ingest_file") if ingest_handler is _AUTO else \
        (ingest_handler, "injected" if ingest_handler else "disabled")
    hooks["vector"] = _try_import("rag.service", "health") if vector_health is _AUTO else \
        (vector_health, "injected" if vector_health else "disabled")
    # "already ingested" must mean the vector store really holds the chunks, not just a register row; only wired
    # when the real ingestion hook is in use (tests that inject a handler keep the hash-only behaviour)
    hooks["has_chunks"] = _try_import("rag.service", "has_document") if ingest_handler is _AUTO else (None, "not used")
    app.state.hooks = hooks

    def dbp() -> Any:
        return app.state.db_path

    # ------------------------------------------------------------------ identity
    def resolve_identity(header_value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """-> (student_id, problem). problem is None when identity is absent or valid."""
        if header_value is None or not header_value.strip():
            return None, None
        sid = header_value.strip()
        if not STUDENT_ID_RE.match(sid):
            return None, "bad_format"
        with da.db_session(dbp(), read_only=True) as conn:
            if da.get_student(conn, sid) is None:
                return None, "unknown_student"
        return sid, None

    # ------------------------------------------------------------------ audit
    def persist_audit(resp: AskResponse, *, student_id: Optional[str], category: str, started: float,
                      sources: Optional[list[dict[str, Any]]] = None, precedence: Optional[str] = None,
                      model: str = "none (deterministic tools)", llm_calls: int = 0, tokens: int = 0) -> None:
        tools = [{"tool": t.tool, "status": (t.output or {}).get("status", "ok"), "input": t.input, "output": t.output}
                 for t in resp.tools_invoked]
        try:
            with da.db_session(dbp()) as conn:
                da.write_audit(conn, {
                    "trace_id": resp.trace_id, "student_id": student_id, "question_category": category,
                    "sources_retrieved": sources or [], "precedence_decision": precedence, "tools_invoked": tools,
                    "answer_type": resp.answer_type, "model": model, "llm_calls": llm_calls, "tokens": tokens,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "applied_rules": [r.model_dump() for r in resp.applied_rules],
                    "conflicts_detected": resp.conflicts_detected,
                    "citations": [c.model_dump(mode="json") for c in resp.citations],
                    "as_of_date": resp.as_of_date})
        except (da.DataAccessError, Exception) as exc:  # fail closed: an unauditable answer is not returned
            log.error("audit write failed trace=%s err=%s", resp.trace_id, type(exc).__name__)
            raise HTTPException(status_code=503, detail="Audit storage is unavailable; the answer was withheld.") from None

    def refused(trace_id: str, text: str, as_of: date, category: str, started: float,
                student_id: Optional[str] = None) -> AskResponse:
        resp = AskResponse(trace_id=trace_id, answer=text, answer_type="refused", explanation=text, as_of_date=as_of)
        persist_audit(resp, student_id=student_id, category=category, started=started)
        return resp

    # ------------------------------------------------------------------ /ask
    @app.post("/ask", response_model=AskResponse)
    def ask(body: AskRequest, x_student_id: Optional[str] = Header(default=None, alias=STUDENT_ID_HEADER)) -> AskResponse:
        started = time.perf_counter()
        trace_id = da.new_trace_id()
        as_of = body.as_of_date or date.today()
        question = body.question

        try:
            student_id, problem = resolve_identity(x_student_id)
        except da.DatabaseUnavailableError:
            raise HTTPException(status_code=503, detail="Student database is unavailable.") from None
        if problem:
            return refused(trace_id, REFUSAL_BAD_IDENTITY, as_of, f"refused_identity_{problem}", started)
        if personal.mentions_other_student(question, student_id):
            return refused(trace_id, REFUSAL_OTHER_STUDENT, as_of, "refused_cross_student", started, student_id)

        runner = app.state.hooks["graph"][0]
        if runner is not None:  # Member 1's workflow owns routing, retrieval and explanation
            return run_graph_path(runner, question, student_id, as_of, trace_id, started)

        # --- interim path: deterministic tools only, no LLM, no retrieval ---------------------------
        if not personal.is_personal(question):
            raise HTTPException(status_code=503, detail=(
                "Policy questions need the retrieval workflow, which is not installed on this server "
                f"({app.state.hooks['graph'][1]}). No answer was generated."))
        if student_id is None:
            return refused(trace_id, REFUSAL_NO_IDENTITY, as_of, "refused_no_identity", started)
        try:
            outcome = personal.answer_personal(question, student_id, as_of, dbp())
        except da.DatabaseUnavailableError:
            raise HTTPException(status_code=503, detail="Student database is unavailable.") from None
        if outcome is None:
            raise HTTPException(status_code=503, detail="This question needs the retrieval workflow, which is not installed.")
        resp = AskResponse(
            trace_id=trace_id, answer=outcome.answer, answer_type=outcome.answer_type, citations=outcome.citations,
            tools_invoked=[{"tool": t["tool"], "input": t["input"], "output": t["output"]} for t in outcome.tools],
            applied_rules=outcome.applied_rules, conflicts_detected=outcome.conflicts,
            explanation=outcome.explanation, as_of_date=as_of)
        persist_audit(resp, student_id=student_id, category=outcome.category, started=started,
                      sources=outcome.sources, precedence=outcome.precedence_decision)
        return resp

    def run_graph_path(runner: Callable[..., Any], question: str, student_id: Optional[str], as_of: date,
                       trace_id: str, started: float) -> AskResponse:
        try:
            raw = runner(question, student_id, as_of)
            extras: dict[str, Any] = {}
            if isinstance(raw, tuple):
                raw, extras = raw[0], dict(raw[1] or {})
            resp = raw if isinstance(raw, AskResponse) else AskResponse.model_validate(raw)
        except (ValidationError, Exception) as exc:
            log.error("graph failed trace=%s err=%s", trace_id, type(exc).__name__)
            raise HTTPException(status_code=502, detail="The answer workflow failed; no answer was generated.") from None
        with da.db_session(dbp(), read_only=True) as conn:
            already = da.get_audit(conn, resp.trace_id) is not None  # the graph may audit itself
        if not already:
            persist_audit(resp, student_id=student_id, category=str(extras.get("question_category", "unclassified")),
                          started=started, sources=extras.get("sources_retrieved"),
                          precedence=extras.get("precedence_decision"), model=str(extras.get("model", "unknown")),
                          llm_calls=int(extras.get("llm_calls", 0)), tokens=int(extras.get("tokens", 0)))
        return resp

    # ------------------------------------------------------------------ /health
    def llm_status(live: bool) -> ComponentStatus:
        try:
            from llm.openai_client import LLMClient, LLMConfig, LLMError
        except ImportError:
            return ComponentStatus(status="not_configured", detail="llm package is not installed")
        try:
            cfg = LLMConfig.from_env()
        except LLMError as exc:
            return ComponentStatus(status="down", detail=f"invalid LLM settings: {exc}")
        if cfg.mock:
            return ComponentStatus(status="degraded", detail="MOCK_LLM=true: offline mock, no real model is used")
        if not cfg.api_key:
            return ComponentStatus(status="not_configured", detail="OPENAI_API_KEY is not set")
        if not live:
            return ComponentStatus(status="degraded", detail=(
                f"configuration only (key present, model {cfg.model}); live connectivity NOT verified - "
                "call GET /health?live=true to test"))
        try:
            res = LLMClient(cfg).generate_text("Reply with the single word OK.", "ping", max_output_tokens=16)
            return ComponentStatus(status="ok", detail=f"live request succeeded (model {res.model}, {res.latency_ms} ms)")
        except LLMError as exc:
            return ComponentStatus(status="down", detail=f"live request failed: {exc.code}")

    @app.get("/health", response_model=HealthResponse)
    def health(live: bool = False) -> HealthResponse:
        ok, detail = da.database_status(dbp())
        sqlite = ComponentStatus(status="ok" if ok else "down", detail=detail)
        probe, why = app.state.hooks["vector"]
        if probe is None:
            vector = ComponentStatus(status="not_configured", detail=why)
        else:
            try:
                r = probe()
                vector = ComponentStatus(status=r.get("status", "down"), detail=r.get("detail"))
            except Exception as exc:
                vector = ComponentStatus(status="down", detail=f"vector store probe failed ({type(exc).__name__})")
        llm = llm_status(live)
        parts = [sqlite, vector, llm]
        if sqlite.status == "down":
            overall = "down"
        elif all(p.status == "ok" for p in parts):
            overall = "ok"
        else:
            overall = "degraded"
        return HealthResponse(status=overall, api=ComponentStatus(status="ok", detail="serving requests"),
                              sqlite=sqlite, vector_store=vector, llm=llm)

    # ------------------------------------------------------------------ /audit
    @app.get("/audit/{trace_id}")
    def audit(trace_id: str, x_student_id: Optional[str] = Header(default=None, alias=STUDENT_ID_HEADER),
              x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token")) -> JSONResponse:
        """Owner-only for records tied to a student (header must match, or a valid X-Admin-Token).
        Records with no student (anonymous questions) are readable with the trace id. A record the
        caller may not see is reported as 404 so its existence is not leaked."""
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", trace_id):
            raise HTTPException(status_code=404, detail="No such audit record")
        with da.db_session(dbp(), read_only=True) as conn:
            rec = da.get_audit(conn, trace_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="No such audit record")
        admin = os.environ.get("AUDIT_ADMIN_TOKEN", "")
        is_admin = bool(admin) and x_admin_token is not None and hmac.compare_digest(admin, x_admin_token)
        if rec["student_id"] is not None and not is_admin and (x_student_id or "").strip() != rec["student_id"]:
            raise HTTPException(status_code=404, detail="No such audit record")
        return JSONResponse(rec)

    # ------------------------------------------------------------------ /sources
    @app.get("/sources", response_model=list[SourceRecord])
    def sources() -> list[SourceRecord]:
        with da.db_session(dbp(), read_only=True) as conn:
            rows = da.list_sources(conn)
        return [SourceRecord(**{k: r[k] for k in SOURCE_FIELDS}) for r in rows]

    # ------------------------------------------------------------------ /ingest
    @app.post("/ingest", response_model=IngestResponse)
    async def ingest(request: Request) -> IngestResponse:
        """Multipart: a file part ('file') and a 'metadata' part holding the Annex B JSON."""
        handler, why = app.state.hooks["ingest"]
        if handler is None:
            raise HTTPException(status_code=501, detail=f"Ingestion pipeline is not available ({why}); nothing was ingested.")
        form = await request.form()
        meta_part = form.get("metadata")
        upload = form.get("file")
        if upload is None:
            upload = next((v for k, v in form.multi_items() if k != "metadata" and hasattr(v, "read")), None)
        if meta_part is None or upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=422, detail="Send multipart form data with a 'file' part and a 'metadata' JSON part.")
        try:
            meta_text = (await meta_part.read()).decode("utf-8") if hasattr(meta_part, "read") else str(meta_part)
            record = SourceRecord.model_validate(json.loads(meta_text))
        except (ValueError, UnicodeDecodeError, ValidationError) as exc:
            detail = ([{"field": ".".join(map(str, e["loc"])), "problem": e["msg"]} for e in exc.errors()]
                      if isinstance(exc, ValidationError) else "metadata is not valid JSON")
            raise HTTPException(status_code=422, detail={"metadata_errors": detail}) from None
        filename = os.path.basename(upload.filename or "")
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_UPLOAD_SUFFIXES:
            raise HTTPException(status_code=415, detail=f"Unsupported file type '{suffix}'. Allowed: {sorted(ALLOWED_UPLOAD_SUFFIXES)}")
        limit = int(os.environ.get("INGEST_MAX_BYTES", DEFAULT_MAX_UPLOAD_BYTES))
        data = await upload.read(limit + 1)
        if not data:
            raise HTTPException(status_code=422, detail="The uploaded file is empty.")
        if len(data) > limit:
            raise HTTPException(status_code=413, detail=f"File is larger than {limit} bytes.")
        sha = hashlib.sha256(data).hexdigest()
        with da.db_session(dbp(), read_only=True) as conn:
            existing = da.get_source(conn, record.doc_id)
        has_chunks = app.state.hooks["has_chunks"][0]
        indexed = has_chunks(record.doc_id) if has_chunks is not None else True
        if existing and existing.get("content_sha256") == sha and (existing.get("chunks_indexed") or 0) > 0 and indexed:
            return IngestResponse(doc_id=record.doc_id, chunks_indexed=existing["chunks_indexed"], status="already_ingested")
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "upload"
        target_dir = app.state.upload_dir / sha[:16]
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / safe
        path.write_bytes(data)
        try:
            result = await run_in_threadpool(handler, str(path), record.model_dump(mode="json"))
            chunks = int(result["chunks_indexed"])
            status = str(result.get("status", "ingested"))
        except ValueError as exc:  # rag.ingest.IngestError: unreadable / empty / unsupported document
            raise HTTPException(status_code=422, detail=f"Document could not be ingested: {exc}") from None
        except Exception as exc:
            log.error("ingest handler failed doc=%s err=%s", record.doc_id, type(exc).__name__)
            raise HTTPException(status_code=500, detail="Ingestion failed; the document was not registered.") from None
        if chunks < 1:
            raise HTTPException(status_code=422, detail=f"No text could be indexed from this file (status: {status}).")
        try:
            with da.db_session(dbp()) as conn:
                da.register_source(conn, record, content_sha256=sha, chunks_indexed=chunks)
        except (da.DataAccessError, Exception) as exc:
            log.error("source registration failed doc=%s err=%s", record.doc_id, type(exc).__name__)
            raise HTTPException(status_code=500, detail="Document indexed but the Source Register update failed.") from None
        return IngestResponse(doc_id=record.doc_id, chunks_indexed=chunks, status=status)

    @app.exception_handler(da.DatabaseUnavailableError)
    async def _db_down(_: Request, __: da.DatabaseUnavailableError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "Student database is unavailable."})

    return app


app = create_app()
