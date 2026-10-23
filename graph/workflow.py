"""Fixed LangGraph workflow behind POST /ask (api/main.py calls ``run_graph``).

    route --refused (instructions inside the question)--> END
      |--personal--> personal --> END   (answered, refused or clarification_needed; never falls back to documents)
      '--policy----> retrieve -> precedence -> synthesize -> guard -> END

One deterministic pipeline, no agents. What each step is allowed to do:

* personal: Member 2's tools via ``api.personal`` (numbers come from SQLite, never from an LLM). The identity is the
  validated X-Student-Id only. No identity on a personal question is refused here, never guessed.
* retrieve / precedence: ``rag`` (ChromaDB, Annex A). Citations come only from retrieved chunk metadata.
* synthesize: the LLM only phrases evidence that survived precedence. No LLM configured, mock mode, an LLM error, or
  a guard failure all fall back to a plain extract of the cited text, so an answer is never invented.
* guard: a policy answer needs citations, and every number in LLM text must occur in the evidence.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from api import personal
from api.models import NOT_FOUND_MESSAGE, AskResponse, Citation
from db import data_access as da
from graph.resolver import resolve_rule_conflict
from llm.client import LLMError, get_llm, is_mock_enabled
from rag import EVIDENCE_RULE, format_evidence, resolve_precedence
from rag.service import get_retriever, get_store

log = logging.getLogger("graph")

TOP_K = int(os.environ.get("RAG_TOP_K") or 6)
EVIDENCE_MARGIN = 0.15  # evidence must score within this of the best winning chunk
MAX_EVIDENCE = 3
MAX_EXTRACT_CHARS = 700

REFUSAL_NO_IDENTITY = ("This question is about your personal records, but no student identity was provided. "
                       "Please sign in so your student ID can be sent in the X-Student-Id header.")
CLARIFY_SCOPE = ("The answer depends on your programme and admission year, and no student identity was provided. "
                 "Please sign in so your student ID can be sent in the X-Student-Id header.")
REFUSAL_INJECTION = ("I can't follow instructions that are written inside a question. Please ask your question about "
                     "university services directly.")
CLARIFY_PERSONAL = ("I couldn't tell which of your records or which rule you mean. Please name the course or the topic, for "
                    "example: 'Am I eligible to appear in the CS201 end-semester exam?'")
# Instructions aimed at the assistant itself, in the USER'S question (deliberately narrower than rag.safety, which screens
# document text: phrases like "act as" are ordinary in student questions).
_QUESTION_INJECTION = re.compile(
    r"(ignore|disregard|forget)\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier|your)\s+(instructions|rules|prompts?)"
    r"|you\s+are\s+now|(system|developer)\s+prompt|(admin|developer|debug|god)\s+mode|jailbreak",
    re.IGNORECASE)

SYNTH_RULES = (EVIDENCE_RULE + " Set answerable=false if the evidence does not actually answer the question (for example "
               "it is about a related topic, a different year or a different document). Otherwise set answerable=true "
               "and answer in 1-3 sentences using only numbers, names and dates that appear in the evidence.")


class Synthesis(BaseModel):
    answerable: bool
    answer: str


class State(TypedDict, total=False):
    question: str
    student_id: Optional[str]
    as_of: date
    personal: bool
    chunks: list
    result: Any  # rag.precedence.PrecedenceResult
    programme: Optional[str]
    batch: Optional[int]
    evidence: list
    draft: Optional[str]
    notes: list
    upcoming_chunks: list
    meta: dict
    response: AskResponse


# ---------------------------------------------------------------------------------------- helpers
def _student_scope(student_id: Optional[str]) -> tuple[Optional[str], Optional[int]]:
    if not student_id:
        return None, None
    try:
        with da.db_session(None, read_only=True) as conn:
            s = da.get_student(conn, student_id)
    except Exception as exc:  # noqa: BLE001 - scope unknown -> precedence reports "ambiguous", never guesses
        log.warning("student scope lookup failed: %s", type(exc).__name__)
        return None, None
    return (s["programme"], int(s["batch_year"])) if s else (None, None)


def _extract(chunks: list) -> str:
    parts = []
    for c in chunks:
        where = f"{c.title} ({c.doc_id})" + (f", section {c.section}" if c.section else "") + (f", page {c.page}" if c.page else "")
        text = " ".join(c.text.split())
        parts.append(f"According to {where}: " + (text if len(text) <= MAX_EXTRACT_CHARS else text[:MAX_EXTRACT_CHARS].rstrip() + "..."))
    return "\n\n".join(parts)


def _numbers(text: str) -> set[str]:
    return {f"{float(n):g}" for n in re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))}


def _upcoming_notes(result: Any, best_score: float) -> tuple[list[str], list]:
    """Not-yet-effective rules that bear on the question (a retrieved but unrelated upcoming document is not mentioned).
    Returns the note texts and the chunks they come from: a document named in an answer must also be cited."""
    seen, notes, chunks = set(), [], []
    for e in sorted(result.upcoming, key=lambda e: -e.chunk.score):
        c = e.chunk
        if c.score >= best_score - EVIDENCE_MARGIN and c.doc_id not in seen:
            seen.add(c.doc_id)
            chunks.append(c)
            notes.append(f"Upcoming change: {c.title} ({c.doc_id}) takes effect on {c.effective_from.isoformat()} and is not yet in force.")
    return notes[:2], chunks[:2]


def _response(state: State, answer: str, answer_type: str, *, citations: Optional[list[Citation]] = None,
              explanation: str = "", conflicts: Optional[list] = None, **meta: Any) -> dict[str, Any]:
    resp = AskResponse(trace_id=da.new_trace_id(), answer=answer, answer_type=answer_type, citations=citations or [],
                       conflicts_detected=conflicts or [], explanation=explanation, as_of_date=state["as_of"])
    return {"response": resp, "meta": meta}


# ---------------------------------------------------------------------------------------- nodes
def route(state: State) -> dict[str, Any]:
    if _QUESTION_INJECTION.search(state["question"]):
        return _response(state, REFUSAL_INJECTION, "refused", explanation=REFUSAL_INJECTION,
                         question_category="refused_prompt_injection", model="none (input screen)")
    return {"personal": personal.is_personal(state["question"])}


def personal_node(state: State) -> dict[str, Any]:
    sid = state.get("student_id")
    if not sid:
        return _response(state, REFUSAL_NO_IDENTITY, "refused", explanation=REFUSAL_NO_IDENTITY,
                         question_category="refused_no_identity", model="none (identity check)")
    outcome = personal.answer_personal(state["question"], sid, state["as_of"])
    if outcome is None:  # a personal question the tools cannot place: ask, do not answer from unrelated documents
        return _response(state, CLARIFY_PERSONAL, "clarification_needed", explanation=CLARIFY_PERSONAL,
                         question_category="personal_unclear", model="none (deterministic tools)")
    resp = AskResponse(
        trace_id=da.new_trace_id(), answer=outcome.answer, answer_type=outcome.answer_type, citations=outcome.citations,
        tools_invoked=[{"tool": t["tool"], "input": t["input"], "output": t["output"]} for t in outcome.tools],
        applied_rules=outcome.applied_rules, conflicts_detected=outcome.conflicts, explanation=outcome.explanation,
        as_of_date=state["as_of"])
    return {"response": resp, "meta": {"question_category": outcome.category, "sources_retrieved": outcome.sources,
                                       "precedence_decision": outcome.precedence_decision, "model": "none (deterministic tools)"}}


def retrieve(state: State) -> dict[str, Any]:
    return {"chunks": get_retriever(top_k=TOP_K).retrieve(state["question"])}


def precedence(state: State) -> dict[str, Any]:
    programme, batch = _student_scope(state.get("student_id"))
    store = get_store()
    register = store.list_sources()
    chunks = list(state["chunks"])
    result = resolve_precedence(chunks, state["as_of"], programme, batch, register=register)
    if result.missing_superseders:  # the document that replaces a retrieved one was not retrieved: fetch and re-resolve
        chunks += get_retriever(top_k=TOP_K).retrieve(state["question"], min_score=0.0, doc_ids=result.missing_superseders)
        result = resolve_precedence(chunks, state["as_of"], programme, batch, register=register)
    return {"chunks": chunks, "result": result, "programme": programme, "batch": batch}


def synthesize(state: State) -> dict[str, Any]:
    result = state["result"]
    hint = result.answer_type_hint
    notes, upcoming_chunks = _upcoming_notes(result, max((c.score for c in state["chunks"]), default=0.0))
    meta: dict[str, Any] = {
        "question_category": f"policy_{hint}", "sources_retrieved": [c.audit_entry() for c in state["chunks"]],
        "precedence_decision": result.decision, "model": "none (no LLM used)", "llm_calls": 0, "tokens": 0}
    if hint == "not_found":
        return _response(state, NOT_FOUND_MESSAGE, "not_found", explanation=result.decision, **meta)
    if hint == "clarification_needed":
        return _response(state, CLARIFY_SCOPE, "clarification_needed", explanation=result.decision, **meta)
    if hint == "conflict_flagged":
        sides = "; ".join(f"{c.title} ({c.doc_id}), section {c.section or '-'}, version {c.version}, effective {c.effective_from}"
                          for c in result.conflicted)
        answer = ("The authorised sources conflict on this and the precedence policy cannot resolve it: " + sides
                  + ". Please contact the issuing office to confirm which applies.")
        return _response(state, answer, "conflict_flagged", citations=[c.to_citation() for c in result.conflicted],
                         explanation=result.decision,
                         conflicts=result.conflicts_detected, **meta)
    # a citation must point at a section or page (brief R2): prefer clause chunks over unlabelled banner/preamble chunks
    pool = [c for c in result.winning if c.section or c.page] or result.winning
    best = max(c.score for c in pool)
    evidence = [c for c in sorted(pool, key=lambda c: -c.score) if c.score >= best - EVIDENCE_MARGIN][:MAX_EVIDENCE]
    draft = None
    unchecked = " No LLM checked that this text answers the question; it is the closest cited text."
    llm_note = "No LLM configured." + unchecked
    if is_mock_enabled():
        llm_note = "MOCK_LLM is on." + unchecked
    else:
        try:
            llm = get_llm()
            res = llm.generate(system=SYNTH_RULES, response_model=Synthesis,
                               user=format_evidence(evidence) + f"\n\nQuestion: {state['question']}")
            meta.update(model=res.model, llm_calls=res.llm_calls, tokens=res.usage.total_tokens)
            if not res.parsed.answerable:  # related text, but it does not answer this question
                return _response(state, NOT_FOUND_MESSAGE, "not_found", **meta, explanation=(
                    f"{result.decision}. The model judged that the retrieved text does not answer the question."))
            draft = res.parsed.answer.strip()
            llm_note = f"Phrased by {res.model} from the cited text."
        except LLMError as exc:  # includes "no API key": fall back, never invent
            llm_note = f"LLM unavailable ({type(exc).__name__})." + unchecked
    state_update = _response(state, "", "retrieved_fact", citations=[c.to_citation() for c in evidence],
                             explanation=f"{result.decision}. {llm_note}", conflicts=result.conflicts_detected, **meta)
    return {**state_update, "evidence": evidence, "draft": draft, "notes": notes, "upcoming_chunks": upcoming_chunks}


def guard(state: State) -> dict[str, Any]:
    resp: AskResponse = state["response"]
    if resp.answer_type != "retrieved_fact":
        return {}
    evidence = state["evidence"]
    draft = state.get("draft")
    grounded = bool(draft) and _numbers(draft) <= _numbers(" ".join(c.text for c in evidence))
    if draft and not grounded:
        resp.explanation += " The model's wording contained figures not found in the cited text, so the cited text is shown instead."
        draft = None
    if not evidence or not resp.citations:  # cannot cite -> cannot answer
        return {"response": AskResponse(trace_id=resp.trace_id, answer=NOT_FOUND_MESSAGE, answer_type="not_found",
                                        explanation="No citable evidence survived the checks.", as_of_date=resp.as_of_date)}
    answer = draft or _extract(evidence)
    if state.get("notes"):
        answer += "\n\n" + " ".join(state["notes"])
        resp.citations += [c.to_citation() for c in state.get("upcoming_chunks", [])]  # named in the answer, so cited
    resp.answer = answer
    return {"response": resp}


def _after_route(state: State) -> str:
    if state.get("response") is not None:  # refused by the input screen
        return END
    return "personal" if state.get("personal") else "retrieve"


def build_graph():
    g = StateGraph(State)
    for name, fn in (("route", route), ("personal", personal_node), ("retrieve", retrieve),
                     ("precedence", precedence), ("synthesize", synthesize), ("guard", guard)):
        g.add_node(name, fn)
    g.add_edge(START, "route")
    g.add_conditional_edges("route", _after_route, {END: END, "personal": "personal", "retrieve": "retrieve"})
    g.add_edge("personal", END)
    g.add_edge("retrieve", "precedence")
    g.add_edge("precedence", "synthesize")
    g.add_edge("synthesize", "guard")
    g.add_edge("guard", END)
    return g.compile()


_GRAPH = build_graph()


def run_graph(question: str, student_id: Optional[str], as_of_date: date) -> tuple[AskResponse, dict[str, Any]]:
    """The hook api/main.py calls. Returns (AskResponse, audit extras)."""
    final = _GRAPH.invoke({"question": question, "student_id": student_id, "as_of": as_of_date})
    return final["response"], dict(final.get("meta") or {})


def register_policy_resolver() -> None:
    """Give Member 2's tools the Annex A resolver for ambiguous rule_registry rows."""
    from tools import set_policy_resolver

    set_policy_resolver(resolve_rule_conflict)


register_policy_resolver()
