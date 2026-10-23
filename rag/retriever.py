"""Top-k retrieval with citation metadata (brief R1, R2, R3).

Returns real stored chunks only. Citations are built from chunk metadata, never from LLM
output. Weak matches are dropped, so "nothing retrieved" (an empty list) is the signal for
the not_found answer. Applicability, supersession and conflicts (Annex A) are NOT decided
here: every chunk carries authority, effective dates, scope and ``supersedes`` so that
``rag/precedence.py`` can resolve them, and not-yet-effective documents are still returned
so they can be mentioned as upcoming changes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Optional

from api.models import Citation
from rag.store import VectorStore

DEFAULT_TOP_K = 4
# Cosine-similarity floor for all-MiniLM-L6-v2, set from eval/eval_set.jsonl run against the synthetic corpus
# (2026-10-23): top-1 score of the 7 answerable synthetic-corpus cases was 0.60-0.79; of the 4 unanswerable cases,
# the off-topic ones scored 0.24 (cricket) and 0.35 (parking fee), the NEAR-TOPIC ones 0.56 (2031 exam dates) and
# 0.62 (1985 regulation). 0.40 therefore rejects off-topic questions and loses no answerable case, but a score
# floor can NOT reject near-topic unanswerable questions (they overlap the answerable range); the workflow asks the
# LLM whether the evidence answers the question for those. 11 cases, synthetic documents only: the real PDFs are not
# ingested, so retune with RAG_MIN_SCORE once the real corpus and a larger evaluation set exist.
DEFAULT_MIN_SCORE = 0.40


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    text: str
    score: float  # cosine similarity, higher is closer
    doc_id: str
    title: str
    issuer: str
    authority_level: int
    doc_type: str
    version: str
    effective_from: Optional[date]
    effective_to: Optional[date]
    supersedes: str
    scope_programmes: str
    scope_batches: str
    provenance: str
    synthetic: str
    section: str
    page: Optional[int]
    injection_suspected: bool

    def to_citation(self) -> Citation:
        return Citation(
            doc_id=self.doc_id,
            title=self.title,
            section=self.section or None,
            page=self.page,
            version=self.version or None,
            effective_from=self.effective_from,
        )

    def audit_entry(self) -> dict[str, Any]:
        """Annex D ``sources_retrieved`` item."""
        return {"doc_id": self.doc_id, "section": self.section, "score": round(self.score, 4)}


def _date(value: Any) -> Optional[date]:
    return date.fromisoformat(value) if value else None


def _chunk(chunk_id: str, text: str, meta: Mapping[str, Any], distance: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        text=text,
        score=1.0 - float(distance),
        doc_id=meta["doc_id"],
        title=meta["title"],
        issuer=meta["issuer"],
        authority_level=int(meta["authority_level"]),
        doc_type=meta["doc_type"],
        version=meta["version"],
        effective_from=_date(meta["effective_from"]),
        effective_to=_date(meta["effective_to"]),
        supersedes=meta["supersedes"],
        scope_programmes=meta["scope_programmes"],
        scope_batches=meta["scope_batches"],
        provenance=meta["provenance"],
        synthetic=meta["synthetic"],
        section=meta["section"],
        page=int(meta["page"]) or None,
        injection_suspected=bool(meta["injection_suspected"]),
    )


class Retriever:
    def __init__(
        self,
        store: VectorStore,
        top_k: int = DEFAULT_TOP_K,
        min_score: Optional[float] = None,
    ):
        self.store = store
        self.top_k = top_k
        if min_score is None:
            min_score = float(os.environ.get("RAG_MIN_SCORE") or DEFAULT_MIN_SCORE)
        self.min_score = min_score

    def retrieve(
        self,
        query: str,
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
        doc_ids: Optional[list[str]] = None,
    ) -> list[RetrievedChunk]:
        """Best-first chunks scoring at least ``min_score``; ``[]`` when nothing qualifies."""
        if not query or not query.strip():
            return []
        k = top_k if top_k is not None else self.top_k
        floor = self.min_score if min_score is None else min_score
        vector = self.store.embedder.embed([query])[0]
        res = self.store.query(vector, k, doc_ids=doc_ids)
        found = [
            _chunk(cid, doc, meta, dist)
            for cid, doc, meta, dist in zip(
                res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]
            )
        ]
        return sorted((c for c in found if c.score >= floor), key=lambda c: -c.score)
