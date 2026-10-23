"""Pure display helpers for the Streamlit UI (no Streamlit import, so they are unit-testable)."""
from __future__ import annotations

import json
import re
from typing import Any

# label, st.badge colour, one-line meaning shown under the badge
ANSWER_TYPE_DISPLAY: dict[str, tuple[str, str, str]] = {
    "retrieved_fact": ("Retrieved fact", "green", "Answered from the cited university documents."),
    "calculated": ("Calculated", "blue", "Computed by deterministic tools over your student records."),
    "not_found": ("Not found", "gray", "The authorised sources did not contain enough evidence to answer."),
    "clarification_needed": ("Clarification needed", "orange", "The question is ambiguous; please add detail."),
    "refused": ("Refused", "red", "The request was refused (for example, another student's data)."),
    "conflict_flagged": ("Conflict flagged", "violet", "Sources disagree and precedence rules could not resolve it."),
}

STATUS_ICONS = {
    "ok": "🟢",
    "degraded": "🟡",
    "down": "🔴",
    "not_configured": "⚪",
    "unknown": "⚫",
}


_MD_IMAGE = re.compile(r"!\[([^\]]*)\](?:\([^)]*\)|\[[^\]]*\])")
_MD_LINK = re.compile(r"\[([^\]]*)\]\(\s*<?([^)\s>]+)[^)]*\)")


def safe_text(text: str) -> str:
    """Neutralise markdown that could make the browser fetch or open a URL from untrusted text.

    Answers and explanations are derived from document text, which is untrusted (R8). Streamlit does
    not render raw HTML here, but it does render markdown images (fetched automatically, which can
    leak data in the URL) and clickable links. Images are replaced by a placeholder; links are shown
    as text with the target in a code span, so nothing is fetched or hidden behind a label.
    """
    if not isinstance(text, str):
        return str(text)
    text = _MD_IMAGE.sub(lambda m: f"[image not shown: {m.group(1) or 'no description'}]", text)
    return _MD_LINK.sub(lambda m: f"{m.group(1)} (`{m.group(2)}`)", text)


def answer_type_display(answer_type: str) -> tuple[str, str, str]:
    return ANSWER_TYPE_DISPLAY.get(answer_type, (answer_type, "gray", "Unrecognised answer type."))


def citation_location(c: dict) -> str:
    parts = []
    if c.get("section"):
        parts.append(f"Section {c['section']}")
    if c.get("page") is not None:
        parts.append(f"p. {c['page']}")
    return ", ".join(parts) or "Location not given"


def citation_rows(citations: list[dict]) -> list[dict]:
    return [
        {
            "Document": c.get("title", ""),
            "Doc ID": c.get("doc_id", ""),
            "Location": citation_location(c),
            "Version": c.get("version") or "—",
            "Effective from": c.get("effective_from") or "—",
        }
        for c in citations
    ]


def applied_rule_rows(rules: list[dict]) -> list[dict]:
    return [
        {"Rule ID": r.get("rule_id", ""), "Applied value": str(r.get("value", "")), "Source doc": r.get("source_doc_id", "")}
        for r in rules
    ]


def format_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


def kv_rows(data: dict) -> list[dict]:
    return [{"Field": str(k), "Value": format_value(v)} for k, v in data.items()]


def conflict_is_unresolved(conflict: dict, answer_type: str) -> bool:
    """A conflict counts as unresolved if the answer says so, or the record says so itself."""
    if answer_type == "conflict_flagged":
        return True
    resolved = conflict.get("resolved")
    if isinstance(resolved, bool):
        return not resolved
    return str(conflict.get("status", "")).lower() == "unresolved"


def conflict_summary(conflict: dict) -> str:
    """Best-effort one-liner. conflicts_detected items are free-form dicts in the contract."""
    for key in ("summary", "description", "reason", "detail"):
        if isinstance(conflict.get(key), str) and conflict[key]:
            return conflict[key]
    docs = conflict.get("doc_ids") or conflict.get("sources") or conflict.get("documents")
    if isinstance(docs, list) and docs:
        names = [d.get("doc_id", str(d)) if isinstance(d, dict) else str(d) for d in docs]
        return "Conflicting sources: " + ", ".join(names)
    return "Conflict details below."
