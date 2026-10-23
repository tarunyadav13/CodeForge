"""Annex A resolver for ``tools.set_policy_resolver`` (rule_registry candidates, not text chunks).

Member 2's tools call ``resolver(candidates, as_of_date, programme, batch)`` when several rule_registry rows apply to
one parameter. Each candidate is a rule row joined with its source document (keys ``rule_id``, ``operator``,
``value``, ``source_doc_id``, ``source_section``, ``source_authority_level``, ``source_effective_from``,
``source_supersedes`` ...). The tools have already filtered by the rule's own dates and scope. This applies the same
order as ``rag.precedence`` (supersession by level 1-2, authority, recency) to the SOURCE documents and returns
``{"winning": candidate | None, "conflicts_detected": [...], "reason": str}``. It never guesses: an unresolved tie
returns ``winning=None`` and the tools report ``rule_conflict`` (``conflict_flagged`` in the API).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

INFORMATIONAL_LEVEL = 5
SUPERSEDING_LEVELS = (1, 2)


def _supersedes(text: str) -> list[tuple[str, Optional[str]]]:
    out = []
    for part in (p.strip() for p in (text or "").split(";")):
        if part:
            doc, _, clause = part.partition("#")
            out.append((doc.strip(), clause.strip() or None))
    return out


def _clause_matches(section: str, clause: Optional[str]) -> bool:
    section = str(section or "")
    return clause is None or section == clause or section.startswith(clause + ".")


def _is_superseded(c: dict[str, Any], pool: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for d in pool:
        if d["source_doc_id"] == c["source_doc_id"] or d.get("source_authority_level") not in SUPERSEDING_LEVELS:
            continue
        for target, clause in _supersedes(d.get("source_supersedes") or ""):
            if target == c["source_doc_id"] and _clause_matches(c.get("source_section"), clause):
                return d
    return None


def _entry(outcome: str, step: str, explanation: str, winning: list[dict[str, Any]], overridden: list[dict[str, Any]]) -> dict[str, Any]:
    brief = lambda c: {"rule_id": c["rule_id"], "doc_id": c["source_doc_id"], "section": c.get("source_section"),  # noqa: E731
                       "authority_level": c.get("source_authority_level"), "value": f"{c['operator']} {c['value']}"}
    return {
        "outcome": outcome, "step": step, "explanation": explanation, "resolved": outcome == "resolved", "status": outcome,
        "doc_ids": sorted({c["source_doc_id"] for c in winning + overridden}),
        "winning": [brief(c) for c in winning], "overridden": [brief(c) for c in overridden],
    }


def resolve_rule_conflict(candidates: list[dict[str, Any]], as_of_date: date, programme: Any = None, batch: Any = None) -> dict[str, Any]:
    conflicts: list[dict[str, Any]] = []
    usable = [c for c in candidates if isinstance(c.get("source_authority_level"), int) and c.get("source_effective_from")]
    if not usable:
        return {"winning": None, "conflicts_detected": [], "reason": "candidate rules lack source authority or effective date"}
    asof = as_of_date.isoformat() if isinstance(as_of_date, date) else str(as_of_date)
    current = [c for c in usable if str(c["source_effective_from"]) <= asof]  # a rule whose source is not yet effective is not current
    if not current:
        return {"winning": None, "conflicts_detected": [], "reason": "no candidate's source document is effective on the as-of date"}

    remaining = []
    for c in current:  # step 2: explicit supersession by an applicable level 1-2 source
        by = _is_superseded(c, current)
        if by is None:
            remaining.append(c)
        else:
            conflicts.append(_entry("resolved", "2 (supersession)",
                                    f"{by['source_doc_id']} supersedes {c['source_doc_id']}"
                                    + (f"#{c['source_section']}" if c.get("source_section") else ""), [by], [c]))
    if not remaining:
        return {"winning": None, "conflicts_detected": conflicts, "reason": "every candidate is superseded"}

    official = [c for c in remaining if c["source_authority_level"] < INFORMATIONAL_LEVEL]
    if not official:
        return {"winning": None, "conflicts_detected": conflicts, "reason": "only level 5 (informational) sources remain"}
    best = min(c["source_authority_level"] for c in official)  # step 3: authority
    top = [c for c in official if c["source_authority_level"] == best]
    beaten = [c for c in remaining if c not in top]
    if beaten:
        conflicts.append(_entry("resolved", "3 (authority)", f"{top[0]['source_doc_id']} has the highest authority (level {best})", top, beaten))
    newest = max(str(c["source_effective_from"]) for c in top)  # step 4: recency
    latest = [c for c in top if str(c["source_effective_from"]) == newest]
    older = [c for c in top if c not in latest]
    if older:
        conflicts.append(_entry("resolved", "4 (recency)", f"{latest[0]['source_doc_id']} is the most recent at the same authority", latest, older))
    values = {(c["operator"], str(c["value"]).strip()) for c in latest}
    if len(values) > 1:  # step 5: unresolved - never pick
        conflicts.append(_entry("unresolved", "5 (unresolved)",
                                "Same authority and effective date but different values; contact the issuing office.", latest, []))
        return {"winning": None, "conflicts_detected": conflicts,
                "reason": "rules from documents of equal authority and date disagree: " + ", ".join(sorted(c["rule_id"] for c in latest))}
    winner = sorted(latest, key=lambda c: c["rule_id"])[0]
    return {"winning": winner, "conflicts_detected": conflicts, "reason": f"{winner['source_doc_id']} prevails (Annex A)"}
