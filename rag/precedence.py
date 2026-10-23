"""Source Precedence Policy (brief Annex A, requirement R4) as a plain, deterministic function.

``resolve_precedence`` takes retrieved chunks and decides which of them may be treated as
the current rule on ``as_of_date``. It never calls an LLM and never lets document TEXT
decide anything about authority, dates, scope or supersession: those come only from the
Source Register metadata stored with each chunk. Order, per Annex A.2:

  1. Applicability   effective on as_of_date and scope covers the student. Future-effective
                     documents are excluded but reported as upcoming; expired ones too.
  2. Supersession    a document that explicitly supersedes another (``supersedes`` metadata,
                     ``DOC`` or ``DOC#clause``) replaces it, only if issued at level 1-2.
  3. Authority       among conflicting provisions the higher authority (lower number) wins,
                     regardless of date. Level 5 is informational and never wins.
  4. Recency         same authority: the later effective_from wins.
  5. Unresolved      otherwise report the conflict and cite both; never pick silently.

KNOWN GAP: the metadata has no topic/parameter/value fields, so "do these two provisions
conflict" is inferred from chunk text by a heuristic: chunks of DIFFERENT documents conflict
when they share enough subject words and quote different amounts in the same unit
(80% vs 75%, 10 days vs 15 days). Pass ``same_subject`` / ``value_of`` to plug in something
better (e.g. the rule_registry). Explicit supersession, authority and dates do not depend
on that heuristic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Iterable, Optional, Sequence

from api.models import Citation, SourceRecord
from rag.retriever import RetrievedChunk

INFORMATIONAL_LEVEL = 5
# A disagreement only counts as a conflict if AT LEAST ONE of the two chunks scores within this margin of the
# best-matching chunk, i.e. it bears on the question. Two weak, unrelated chunks that merely disagree with each
# other no longer flip a clear answer to conflict_flagged. (Requiring BOTH to be close was tried and rejected: a
# relevant older regulation can score well below the circular that supersedes it.) 0.15 is a first setting,
# checked only on synthetic fixtures and a few smoke queries, not tuned on an evaluation set.
DEFAULT_RELEVANCE_MARGIN = 0.15
SUPERSEDING_LEVELS = (1, 2)

# ---- text -> comparable quantities (heuristic, see module docstring) ---------------------
_UNIT_NAMES = {
    "%": "%", "percent": "%", "percentage": "%", "per cent": "%",
    "day": "days", "days": "days", "week": "weeks", "weeks": "weeks",
    "month": "months", "months": "months", "hour": "hours", "hours": "hours",
    "mark": "marks", "marks": "marks", "credit": "credits", "credits": "credits",
    "semester": "semesters", "semesters": "semesters", "attempt": "attempts",
    "attempts": "attempts", "year": "years", "years": "years",
}
_QTY_AFTER = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*(%|percentage|percent|per\s+cent|days?|weeks?|months?|hours?|"
    r"marks?|credits?|semesters?|attempts?|years?)(?![A-Za-z])",
    re.IGNORECASE,
)
_QTY_RUPEES = re.compile(r"(?:\brs\.?|\binr\b|₹)\s*(\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)
_QTY_RUPEES_WORD = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*rupees", re.IGNORECASE)
_WORD = re.compile(r"[a-z]{3,}")
_STOP = frozenset(
    "the and for with that this from have has are was were will shall must may per each any all "
    "not can who which their been than then such only into upon also other there these those being "
    "student students university effective its our your you but may least within before after "
    "yes".split()
)


def _num(text: str) -> str:
    return f"{float(text.replace(',', '')):g}"


def quantities(text: str) -> dict[str, frozenset[str]]:
    """Amounts with a unit found in ``text``: ``{"%": {"80"}, "days": {"10"}}``.

    Bare numbers (clause numbers, years, dates) are ignored on purpose; number words
    ("ten days") are not understood.
    """
    found: dict[str, set[str]] = {}
    for m in _QTY_AFTER.finditer(text):
        unit = _UNIT_NAMES[re.sub(r"\s+", " ", m.group(2).lower())]
        found.setdefault(unit, set()).add(_num(m.group(1)))
    for rx in (_QTY_RUPEES, _QTY_RUPEES_WORD):
        for m in rx.finditer(text):
            found.setdefault("rupees", set()).add(_num(m.group(1)))
    return {u: frozenset(v) for u, v in found.items()}


def _subject_words(text: str) -> frozenset[str]:
    words = (w[:-1] if w.endswith("s") and len(w) > 4 else w for w in _WORD.findall(text.lower()))
    return frozenset(w for w in words if w not in _STOP and w not in _UNIT_NAMES)


def default_same_subject(a: RetrievedChunk, b: RetrievedChunk) -> bool:
    """Enough shared subject words (>= 2 and >= 30% of the shorter chunk's words)."""
    wa, wb = _subject_words(a.text), _subject_words(b.text)
    shared = wa & wb
    return len(shared) >= 2 and len(shared) / max(1, min(len(wa), len(wb))) >= 0.3


def _differ(a: dict[str, frozenset[str]], b: dict[str, frozenset[str]]) -> bool:
    """True when some unit appears in both and the quoted amounts are disjoint."""
    return any(a[u].isdisjoint(b[u]) for u in a.keys() & b.keys())


# ---- scope and applicability -----------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _entries(scope: str) -> list[str]:
    return [e for e in (x.strip() for x in re.split(r"[;,]", scope)) if e]


def _programme_in_scope(scope: str, programme: Optional[str]) -> Optional[bool]:
    """True / False, or None when it cannot be decided (missing or unknown)."""
    entries = _entries(scope)
    if not entries:
        return None
    if any(_norm(e) == "all" for e in entries):
        return True
    if not programme or not programme.strip():
        return None
    p = _norm(programme)
    for e in map(_norm, entries):
        if p == e or (p.startswith(e) and p[len(e)] in " .-/("):  # "B.Tech" covers "B.Tech CSE"
            return True
    return False


def _batch_in_scope(scope: str, batch: Optional[int]) -> Optional[bool]:
    entries = _entries(scope)
    if not entries:
        return None
    if any(e.lower() == "all" for e in entries):
        return True
    if batch is None:
        return None
    matched = unparsed = False
    for e in entries:
        if m := re.fullmatch(r"(\d{4})\+", e):
            matched |= batch >= int(m.group(1))
        elif m := re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", e):
            matched |= int(m.group(1)) <= batch <= int(m.group(2))
        elif re.fullmatch(r"\d{4}", e):
            matched |= batch == int(e)
        else:
            unparsed = True
    return True if matched else (None if unparsed else False)


def _applicability(
    authority: Optional[int],
    eff_from: Optional[date],
    eff_to: Optional[date],
    scope_programmes: str,
    scope_batches: str,
    as_of: date,
    programme: Optional[str],
    batch: Optional[int],
) -> tuple[str, str]:
    """(state, detail); state is current|upcoming|expired|out_of_scope|ambiguous|unusable."""
    if not isinstance(authority, int) or not 1 <= authority <= 5 or eff_from is None:
        return "unusable", "missing or invalid authority_level / effective_from"
    prog = _programme_in_scope(scope_programmes or "", programme)
    bat = _batch_in_scope(scope_batches or "", batch)
    if prog is False or bat is False:
        return "out_of_scope", "programme or batch scope does not cover the student"
    if eff_from > as_of:
        return "upcoming", f"not effective until {eff_from.isoformat()}"
    if eff_to is not None and eff_to < as_of:
        return "expired", f"ceased to be effective on {eff_to.isoformat()}"
    if prog is None or bat is None:
        return "ambiguous", "scope cannot be matched (student programme/batch unknown or scope unparseable)"
    return "current", ""


# ---- results -------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Excluded:
    """A chunk that may not be treated as the current rule, and why."""

    chunk: RetrievedChunk
    reason: str  # superseded|upcoming|expired|out_of_scope|ambiguous|unusable|informational|overridden
    detail: str


@dataclass(frozen=True)
class ConflictRecord:
    outcome: str  # "resolved" | "unresolved"
    step: str  # "3 (authority)", "4 (recency)", "5 (unresolved)"
    explanation: str
    winning: tuple[dict[str, Any], ...]
    overridden: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe, for ``AskResponse.conflicts_detected``."""
        return {
            "outcome": self.outcome, "step": self.step, "explanation": self.explanation,
            "winning": list(self.winning), "overridden": list(self.overridden),
            # aliases the Streamlit UI reads (docs/FRONTEND_EVAL_HANDOFF.md): doc_ids + resolved/status
            "doc_ids": sorted({m["doc_id"] for m in (*self.winning, *self.overridden)}),
            "resolved": self.outcome == "resolved", "status": self.outcome, "summary": self.explanation,
        }


@dataclass(frozen=True)
class PrecedenceResult:
    winning: list[RetrievedChunk]  # safe to answer from; best first
    conflicted: list[RetrievedChunk]  # unresolved conflict: cite ALL of these, do not pick
    conflicts: list[ConflictRecord]
    excluded: list[Excluded]
    decision: str  # for AuditRecord.precedence_decision
    answer_type_hint: str  # conflict_flagged | retrieved_fact | clarification_needed | not_found
    missing_superseders: list[str]  # applicable superseding doc_ids that were not retrieved
    flagged_injection: list[str]  # chunk_ids whose text looked like instructions (audit only)

    @property
    def conflicts_detected(self) -> list[dict[str, Any]]:
        return [c.as_dict() for c in self.conflicts]

    def excluded_for(self, reason: str) -> list[Excluded]:
        return [e for e in self.excluded if e.reason == reason]

    @property
    def upcoming(self) -> list[Excluded]:
        """Not-yet-effective rules, to mention as upcoming changes (R4)."""
        return self.excluded_for("upcoming")

    def citation_chunks(self) -> list[RetrievedChunk]:
        """Chunks to cite: the winners, plus every side of an unresolved conflict."""
        seen: set[str] = set()
        out = []
        for c in [*self.winning, *self.conflicted]:
            if c.chunk_id not in seen:
                seen.add(c.chunk_id)
                out.append(c)
        return out

    def citations(self) -> list[Citation]:
        return [c.to_citation() for c in self.citation_chunks()]


def _member(c: RetrievedChunk, qty: dict[str, frozenset[str]]) -> dict[str, Any]:
    return {
        "doc_id": c.doc_id, "section": c.section, "version": c.version,
        "authority_level": c.authority_level,
        "effective_from": c.effective_from.isoformat() if c.effective_from else None,
        "values": {u: sorted(v) for u, v in qty.items()},
    }


def _label(c: RetrievedChunk) -> str:
    return f"{c.doc_id}#{c.section}" if c.section else c.doc_id


def _parse_supersedes(text: str) -> list[tuple[str, Optional[str]]]:
    out = []
    for part in (p.strip() for p in (text or "").split(";")):
        if not part:
            continue
        doc, _, clause = part.partition("#")
        if doc.strip():
            out.append((doc.strip(), clause.strip() or None))
    return out


def _clause_matches(section: str, clause: Optional[str]) -> bool:
    return clause is None or section == clause or section.startswith(clause + ".")


# ---- the resolver --------------------------------------------------------------------------------
def resolve_precedence(
    chunks: Sequence[RetrievedChunk],
    as_of_date: date,
    programme: Optional[str] = None,
    batch: Optional[int] = None,
    register: Optional[Iterable[SourceRecord]] = None,
    *,
    same_subject: Optional[Callable[[RetrievedChunk, RetrievedChunk], bool]] = None,
    value_of: Optional[Callable[[str], dict[str, frozenset[str]]]] = None,
    relevance_margin: Optional[float] = DEFAULT_RELEVANCE_MARGIN,
) -> PrecedenceResult:
    """Apply Annex A to ``chunks`` for a student (``programme``/``batch``; None if unknown).

    ``relevance_margin``: a disagreement only counts as a conflict if at least one of the two chunks scores within
    this margin of the best match (``None`` disables the check).

    ``register`` (``VectorStore.list_sources()``) lets supersession be applied even when the
    superseding document's own chunks were not retrieved; such documents are reported in
    ``missing_superseders`` so the caller can retrieve them with ``doc_ids=``.
    """
    same_subject = same_subject or default_same_subject
    value_of = value_of or quantities
    notes: list[str] = []
    excluded: list[Excluded] = []

    unique: dict[str, RetrievedChunk] = {}
    for c in chunks:
        unique.setdefault(c.chunk_id, c)
    flagged = [c.chunk_id for c in unique.values() if c.injection_suspected]

    # Step 1: applicability
    candidates: list[RetrievedChunk] = []
    for c in unique.values():
        state, detail = _applicability(
            c.authority_level, c.effective_from, c.effective_to, c.scope_programmes,
            c.scope_batches, as_of_date, programme, batch,
        )
        if state == "current":
            candidates.append(c)
        else:
            excluded.append(Excluded(c, state, detail))
            if state == "upcoming":
                notes.append(f"{c.doc_id} is not yet effective ({detail}); upcoming change")

    # Step 2: explicit supersession, only by applicable level 1-2 documents
    sources: dict[str, tuple[int, str]] = {}  # doc_id -> (authority, supersedes), applicable only
    for c in candidates:
        sources[c.doc_id] = (c.authority_level, c.supersedes)
    from_register: set[str] = set()
    for r in register or ():
        state, _ = _applicability(
            r.authority_level, r.effective_from, r.effective_to, r.scope_programmes,
            r.scope_batches, as_of_date, programme, batch,
        )
        if state == "current" and r.doc_id not in sources:
            sources[r.doc_id] = (r.authority_level, r.supersedes)
            from_register.add(r.doc_id)
    superseders: dict[str, list[tuple[str, Optional[str]]]] = {}
    for doc_id, (level, supersedes) in sources.items():
        for target, clause in _parse_supersedes(supersedes):
            if target == doc_id:
                continue
            if level not in SUPERSEDING_LEVELS:
                notes.append(f"{doc_id} (level {level}) claims to supersede {target}; ignored, only level 1-2 can")
                continue
            superseders.setdefault(target, []).append((doc_id, clause))

    current: list[RetrievedChunk] = []
    needed: set[str] = set()
    for c in candidates:
        by = next(((d, cl) for d, cl in superseders.get(c.doc_id, []) if _clause_matches(c.section, cl)), None)
        if by is None:
            current.append(c)
            continue
        excluded.append(Excluded(c, "superseded", f"{by[0]} supersedes {c.doc_id}" + (f"#{by[1]}" if by[1] else "")))
        notes.append(f"{by[0]} supersedes {_label(c) if by[1] else c.doc_id} (step 2)")
        if by[0] in from_register:
            needed.add(by[0])
    retrieved_docs = {c.doc_id for c in unique.values()}

    # Relevance baseline: the best chunk that is still CURRENT. A superseded regulation often matches the question best,
    # and measuring against it would wrongly hide a conflict between its replacement and a contradicting lower source.
    best_score = max((c.score for c in current), default=0.0)

    def relevant(c: RetrievedChunk) -> bool:  # None disables the check (every pair may conflict)
        return relevance_margin is None or c.score >= best_score - relevance_margin
    missing = sorted(d for d in needed if d not in retrieved_docs)

    # Level 5 is informational only
    official: list[RetrievedChunk] = []
    for c in current:
        if c.authority_level == INFORMATIONAL_LEVEL:
            excluded.append(Excluded(c, "informational", "level 5 content is informational only"))
        else:
            official.append(c)

    # Steps 3-5: conflicts between provisions of different documents
    qty = {c.chunk_id: value_of(c.text) for c in current}
    nodes = official + [e.chunk for e in excluded if e.reason == "informational"]
    edges: dict[str, set[str]] = {c.chunk_id: set() for c in nodes}
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            if (a.doc_id != b.doc_id and (relevant(a) or relevant(b))
                    and _differ(qty[a.chunk_id], qty[b.chunk_id]) and same_subject(a, b)):
                edges[a.chunk_id].add(b.chunk_id)
                edges[b.chunk_id].add(a.chunk_id)

    by_id = {c.chunk_id: c for c in nodes}
    seen: set[str] = set()
    losers: dict[str, str] = {}  # chunk_id -> reason
    conflicted_ids: set[str] = set()
    conflicts: list[ConflictRecord] = []
    for start in by_id:
        if start in seen or not edges[start]:
            continue
        comp, stack = [], [start]
        while stack:
            cid = stack.pop()
            if cid in seen:
                continue
            seen.add(cid)
            comp.append(cid)
            stack.extend(edges[cid] - seen)
        members = [by_id[i] for i in sorted(comp)]
        contenders = [m for m in members if m.authority_level < INFORMATIONAL_LEVEL]
        if not contenders:
            continue
        best = min(m.authority_level for m in contenders)
        top = [m for m in contenders if m.authority_level == best]
        newest = max(m.effective_from for m in top)  # type: ignore[type-var]
        top = [m for m in top if m.effective_from == newest]
        clash = {m.chunk_id for m in top if edges[m.chunk_id] & {t.chunk_id for t in top}}
        winners = [m for m in top if m.chunk_id not in clash]
        beaten = [m for m in members if m not in top]
        step = "3 (authority)" if any(m.authority_level != best for m in beaten) else "4 (recency)"
        for m in beaten:
            losers[m.chunk_id] = "authority" if m.authority_level != best else "recency"
        if clash:
            conflicted_ids |= clash
            sides = [by_id[i] for i in sorted(clash)]
            conflicts.append(ConflictRecord(
                "unresolved", "5 (unresolved)",
                "Documents of the same authority and effective date give different values ("
                + ", ".join(_label(s) for s in sides)
                + "); the policy cannot choose. Contact the issuing office.",
                tuple(_member(s, qty[s.chunk_id]) for s in sides),
                tuple(_member(m, qty[m.chunk_id]) for m in beaten),
            ))
            notes.append("unresolved conflict between " + " and ".join(_label(s) for s in sides) + " (step 5)")
        if beaten and winners:
            why = "higher authority" if step.startswith("3") else "later effective_from"
            conflicts.append(ConflictRecord(
                "resolved", step,
                f"{', '.join(_label(w) for w in winners)} prevails over "
                f"{', '.join(_label(m) for m in beaten)} ({why}).",
                tuple(_member(w, qty[w.chunk_id]) for w in winners),
                tuple(_member(m, qty[m.chunk_id]) for m in beaten),
            ))
            notes.append(f"{', '.join(_label(w) for w in winners)} prevails over "
                         f"{', '.join(_label(m) for m in beaten)} (step {step[0]})")

    for cid, why in losers.items():
        c = by_id[cid]
        if c.authority_level == INFORMATIONAL_LEVEL:
            continue  # already listed as informational
        excluded.append(Excluded(c, "overridden", f"overridden by {why} (Annex A)"))

    winning = [c for c in official if c.chunk_id not in losers and c.chunk_id not in conflicted_ids]
    winning.sort(key=lambda c: (c.authority_level, -c.effective_from.toordinal(), -c.score))  # type: ignore[union-attr]
    conflicted = sorted((by_id[i] for i in conflicted_ids), key=lambda c: (c.doc_id, c.chunk_id))

    if conflicted:
        hint = "conflict_flagged"
    elif winning:
        hint = "retrieved_fact"
    elif any(e.reason == "ambiguous" for e in excluded):
        hint = "clarification_needed"
    else:
        hint = "not_found"
    ambiguous = sorted({e.chunk.doc_id for e in excluded if e.reason == "ambiguous"})
    if ambiguous:
        notes.append("applicability unclear for " + ", ".join(ambiguous) + " (programme/batch needed)")
    decision = "; ".join(dict.fromkeys(notes)) or "no conflicting provisions"
    return PrecedenceResult(
        winning=winning, conflicted=conflicted, conflicts=conflicts, excluded=excluded,
        decision=decision, answer_type_hint=hint, missing_superseders=missing, flagged_injection=flagged,
    )
