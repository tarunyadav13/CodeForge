"""CSV layout shared by gen_data.py, load_students.py and validate.py.

One CSV per table, named ``<table>.csv``, with exactly the Annex C column names
(plus Annex B for source_register). Extra columns in a CSV are ignored.
"""
from __future__ import annotations

from typing import Any, Mapping

# Load order respects foreign keys: source_register -> courses -> students ->
# rule_registry -> attendance -> results.
TABLE_ORDER = ("source_register", "courses", "students", "rule_registry", "attendance", "results")

COLUMNS: dict[str, tuple[str, ...]] = {
    "source_register": ("doc_id", "title", "issuer", "authority_level", "doc_type", "version",
                        "effective_from", "effective_to", "supersedes", "scope_programmes",
                        "scope_batches", "provenance", "retrieved_on", "synthetic"),
    "courses": ("course_code", "course_name", "programme", "semester", "credits"),
    "students": ("student_id", "full_name", "programme", "batch_year", "current_semester",
                 "cgpa", "active_backlogs"),
    "rule_registry": ("rule_id", "description", "parameter", "operator", "value", "scope_programmes",
                      "scope_batches", "effective_from", "effective_to", "source_doc_id",
                      "source_section"),
    "attendance": ("student_id", "course_code", "classes_held", "classes_attended"),
    "results": ("student_id", "course_code", "exam_session", "exam_type", "internal_marks",
                "external_marks", "total_marks", "max_marks", "result"),
}

KEY_COLUMNS: dict[str, tuple[str, ...]] = {
    "source_register": ("doc_id",),
    "courses": ("course_code",),
    "students": ("student_id",),
    "rule_registry": ("rule_id",),
    "attendance": ("student_id", "course_code"),
    "results": ("student_id", "course_code", "exam_session", "exam_type"),
}

INT_COLUMNS = {
    "authority_level", "semester", "credits", "batch_year", "current_semester", "active_backlogs",
    "classes_held", "classes_attended", "internal_marks", "external_marks", "total_marks", "max_marks",
}
REAL_COLUMNS = {"cgpa"}
# Blank allowed -> stored as NULL
NULLABLE_COLUMNS = {"effective_to"}
# Blank allowed -> default value
DEFAULTS = {"supersedes": "", "scope_programmes": "ALL", "scope_batches": "ALL", "synthetic": "N"}


def normalise_header(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def convert_row(table: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    """Convert one CSV row (all strings) to typed values. Raises ValueError with a
    short message that never echoes personal data other than the offending column."""
    out: dict[str, Any] = {}
    for col in COLUMNS[table]:
        value = raw.get(col)
        text = value.strip() if isinstance(value, str) else value
        if text is None or text == "":
            if col in NULLABLE_COLUMNS:
                out[col] = None
                continue
            if col in DEFAULTS:
                out[col] = DEFAULTS[col]
                continue
            raise ValueError(f"column '{col}' is required")
        if col in INT_COLUMNS:
            try:
                out[col] = int(text)
            except (TypeError, ValueError):
                raise ValueError(f"column '{col}' must be a whole number") from None
        elif col in REAL_COLUMNS:
            try:
                out[col] = float(text)
            except (TypeError, ValueError):
                raise ValueError(f"column '{col}' must be a number") from None
        else:
            out[col] = text
    return out
