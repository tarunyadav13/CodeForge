"""Shared helpers for the deterministic tools (Member 2).

Principles
* Every authoritative number is computed here, in Python/SQL, with exact arithmetic
  (fractions), never by an LLM and never with floating-point comparisons.
* Thresholds come from rule_registry rows (filtered by date, programme, batch). Nothing
  about attendance %, pass marks, CGPA cut-offs or backlog limits is hard-coded.
* Tools open READ-ONLY database connections: a tool, and therefore a what-if simulation,
  physically cannot modify official records.

Integration boundary with Member 1 (policy precedence)
-----------------------------------------------------
If more than one rule is applicable for the same parameter, the tools do NOT pick one
themselves. They call a resolver registered with ``set_policy_resolver``:

    resolver(candidates: list[dict], as_of_date: date, programme: str, batch: int) -> dict
        -> {"winning": <one of candidates> | None, "conflicts_detected": [...], "reason": str}

(the same shape as Member 1's ``resolve_precedence``). With no resolver registered, an
ambiguous parameter yields status "rule_conflict" and an UNDETERMINED result carrying
all candidates, so the API can answer conflict_flagged instead of guessing.
"""
from __future__ import annotations

import re
from datetime import date
from fractions import Fraction
from typing import Any, Callable, Optional

from db import data_access as da

PolicyResolver = Callable[[list[dict[str, Any]], date, str, int], dict[str, Any]]
_resolver: Optional[PolicyResolver] = None


def set_policy_resolver(resolver: Optional[PolicyResolver]) -> None:
    """Register Member 1's precedence function (or None to remove it)."""
    global _resolver
    _resolver = resolver


def get_policy_resolver() -> Optional[PolicyResolver]:
    return _resolver


# --------------------------------------------------------------------------- numbers
def to_fraction(value: Any, label: str = "value") -> Fraction:
    try:
        return Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError):
        raise da.InvalidInputError(f"{label} is not a number") from None


def pct_str(value: Fraction, places: int = 2) -> float:
    """Round a Fraction to a float for display only. Decisions use the Fraction itself."""
    scaled = value * (10 ** places)
    rounded = (scaled.numerator * 2 + scaled.denominator) // (scaled.denominator * 2)  # round half up
    return rounded / (10 ** places)


class RuleConfigError(Exception):
    """A rule_registry row cannot be evaluated (bad operator/value)."""


_OPS: dict[str, Callable[[Fraction, Fraction], bool]] = {
    ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
    "<": lambda a, b: a < b, "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
}


def evaluate(actual: Fraction, operator: str, value: str) -> bool:
    """Apply a registry operator exactly. 'between' takes 'low,high' (inclusive)."""
    if operator == "between":
        parts = [p for p in re.split(r"[,;]", str(value)) if p.strip()]
        if len(parts) != 2:
            raise RuleConfigError(f"'between' needs two values, got {value!r}")
        try:
            lo, hi = Fraction(parts[0].strip()), Fraction(parts[1].strip())
        except (ValueError, ZeroDivisionError):
            raise RuleConfigError(f"'between' value {value!r} is not numeric") from None
        return lo <= actual <= hi
    fn = _OPS.get(operator)
    if fn is None:
        raise RuleConfigError(f"unsupported operator {operator!r}")
    try:
        return fn(actual, Fraction(str(value).strip()))
    except (ValueError, ZeroDivisionError):
        raise RuleConfigError(f"rule value {value!r} is not numeric") from None


# --------------------------------------------------------------------------- rules
def rule_provenance(rule: dict[str, Any]) -> dict[str, Any]:
    """Everything the audit/citation needs to trace a threshold to a document clause."""
    return {
        "rule_id": rule["rule_id"], "parameter": rule["parameter"], "operator": rule["operator"],
        "value": rule["value"], "description": rule["description"],
        "scope_programmes": rule["scope_programmes"], "scope_batches": rule["scope_batches"],
        "rule_effective_from": rule["effective_from"], "rule_effective_to": rule["effective_to"],
        "source_doc_id": rule["source_doc_id"], "source_section": rule["source_section"],
        "source_title": rule.get("source_title"), "source_version": rule.get("source_version"),
        "source_authority_level": rule.get("source_authority_level"),
        "source_effective_from": rule.get("source_effective_from"),
        "source_synthetic": rule.get("source_synthetic"),
    }


def applied_rule_entry(rule: dict[str, Any]) -> dict[str, str]:
    """Shape of api.models.AppliedRule."""
    return {"rule_id": rule["rule_id"], "value": f"{rule['operator']} {rule['value']}",
            "source_doc_id": rule["source_doc_id"]}


def select_rule(conn, student: dict[str, Any], parameter: str, as_of: date) -> dict[str, Any]:
    """Find THE rule for ``parameter`` that applies to this student on ``as_of``.

    Returns {"status": "ok"|"no_rule"|"rule_conflict", "rule": row|None, "candidates": [...],
             "skipped": [...], "upcoming": [...], "reason": str, "resolver": str}
    """
    found = da.get_applicable_rules_detailed(conn, student["programme"], student["batch_year"], as_of, parameter)
    candidates, skipped = found["rules"], found["skipped"]
    upcoming = [rule_provenance(r) for r in
                da.get_upcoming_rules(conn, student["programme"], student["batch_year"], as_of, parameter)]
    out: dict[str, Any] = {"status": "no_rule", "rule": None, "candidates": [rule_provenance(r) for r in candidates],
                           "skipped": skipped, "upcoming": upcoming, "resolver": "none", "reason": ""}
    if not candidates:
        out["reason"] = (f"No rule for '{parameter}' is in force on {as_of.isoformat()} for "
                         f"{student['programme']}, batch {student['batch_year']}.")
        return out
    if len(candidates) == 1:
        out.update(status="ok", rule=candidates[0], resolver="single_applicable_rule",
                   reason="Exactly one rule is in force for this student on the as-of date.")
        return out
    resolver = get_policy_resolver()
    if resolver is None:
        ids = ", ".join(c["rule_id"] for c in candidates)
        out.update(status="rule_conflict",
                   reason=f"{len(candidates)} rules ({ids}) are simultaneously applicable and no policy resolver is registered.")
        return out
    try:
        decision = resolver(candidates, as_of, student["programme"], student["batch_year"])
        winner = decision.get("winning") if isinstance(decision, dict) else None
    except Exception as exc:  # a resolver bug must not become a silent guess
        out.update(status="rule_conflict", reason=f"policy resolver failed ({type(exc).__name__})")
        return out
    if winner is None or winner.get("rule_id") not in {c["rule_id"] for c in candidates}:
        out.update(status="rule_conflict", reason=str(decision.get("reason") or "policy resolver could not choose a rule"),
                   resolver="external_resolver")
        out["conflicts_detected"] = list(decision.get("conflicts_detected") or [])
        return out
    chosen = next(c for c in candidates if c["rule_id"] == winner["rule_id"])
    out.update(status="ok", rule=chosen, resolver="external_resolver", reason=str(decision.get("reason") or "resolved by policy resolver"))
    out["conflicts_detected"] = list(decision.get("conflicts_detected") or [])
    return out


def rule_check(rule_sel: dict[str, Any], actual: Fraction) -> dict[str, Any]:
    """Evaluate ``actual`` against a selected rule -> {"state": "pass"|"fail"|"undetermined", ...}."""
    rule = rule_sel["rule"]
    if rule is None:
        return {"state": "undetermined", "reason": rule_sel["reason"], "rule_status": rule_sel["status"]}
    try:
        passed = evaluate(actual, rule["operator"], rule["value"])
    except RuleConfigError as exc:
        return {"state": "undetermined", "reason": f"Rule {rule['rule_id']} cannot be evaluated: {exc}",
                "rule_status": "rule_error"}
    return {"state": "pass" if passed else "fail", "rule_status": "ok"}


def parse_as_of(as_of_date: Any) -> date:
    return da.coerce_date(as_of_date if as_of_date is not None else date.today(), "as_of_date")
