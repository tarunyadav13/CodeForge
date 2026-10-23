"""Evaluation runner: sends eval/eval_set.jsonl to a running API and scores the real responses.

Usage:
    python eval/runner.py                         # API_BASE_URL or http://localhost:8000
    python eval/runner.py --base-url http://localhost:8000 --audit --output eval/results/run.json
    python eval/runner.py --ids EV-005,EV-021     # subset
    python eval/runner.py --validate-only         # check the dataset, no HTTP
    python eval/runner.py --mock                  # MOCK-ONLY canned replay; labelled in output and JSON

Every metric comes from actual API responses. A check whose expected value is not known
yet (fixture pending) is reported as skipped, never as passed. Exit code is 1 if any case
FAILs or ERRORs, 2 if the dataset is invalid.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui.api_client import (  # noqa: E402
    ANSWER_TYPES,
    NOT_FOUND_MESSAGE,
    ApiClient,
    get_api_base_url,
    is_valid_student_id,
)

DEFAULT_DATASET = ROOT / "eval" / "eval_set.jsonl"
DEFAULT_OUTPUT_DIR = ROOT / "eval" / "results"
ABSTAIN_TYPES = {"not_found", "refused", "clarification_needed"}
REQUIRED_KEYS = {
    "id": str, "category": str, "question": str, "expected_answer_type": list, "evaluates": str,
    "fixture_status": str,
}
LIST_KEYS = ("expected_answer_contains", "expected_citation_doc_ids", "expected_tools",
             "forbidden_strings", "forbidden_tool_student_ids")


class DatasetError(ValueError):
    pass


# --- Dataset -----------------------------------------------------------------------------

def load_dataset(path: Path) -> list[dict]:
    cases, seen, errors = [], set(), []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"line {lineno}: invalid JSON ({exc.msg})")
                continue
            errors.extend(f"line {lineno}: {e}" for e in validate_case(case))
            cid = case.get("id") if isinstance(case, dict) else None
            if cid in seen:
                errors.append(f"line {lineno}: duplicate id {cid}")
            seen.add(cid)
            cases.append(case)
    if errors:
        raise DatasetError("\n".join(errors))
    return cases


def validate_case(case: Any) -> list[str]:
    if not isinstance(case, dict):
        return ["not a JSON object"]
    errs = []
    for key, typ in REQUIRED_KEYS.items():
        if not isinstance(case.get(key), typ):
            errs.append(f"'{key}' missing or not {typ.__name__}")
    for t in case.get("expected_answer_type") or []:
        if t not in ANSWER_TYPES:
            errs.append(f"unknown expected_answer_type '{t}'")
    if not case.get("expected_answer_type"):
        errs.append("expected_answer_type is empty")
    for key in LIST_KEYS:
        if key in case and not isinstance(case[key], list):
            errs.append(f"'{key}' must be a list")
    if case.get("fixture_status") not in ("ready", "pending"):
        errs.append("fixture_status must be 'ready' or 'pending'")
    sid = case.get("student_id")
    if sid is not None and not is_valid_student_id(sid):
        errs.append(f"student_id '{sid}' is not S+4 digits")
    if sid and 9000 <= int(sid[1:]) <= 9999:
        errs.append(f"student_id '{sid}' is in the judge-reserved range S9000-S9999")
    if "JDG" in json.dumps(case).upper():
        errs.append("uses a judge-reserved JDG course code")
    if case.get("as_of_date") is not None:
        try:
            date.fromisoformat(case["as_of_date"])
        except (TypeError, ValueError):
            errs.append("as_of_date is not YYYY-MM-DD")
    return errs


# --- Scoring -----------------------------------------------------------------------------

def contains_term(text: str, term: str) -> bool:
    """Case-insensitive match that will not find '0%' inside '74.0%' or '70%'."""
    return re.search(r"(?<![\w.])" + re.escape(term), text, re.IGNORECASE) is not None


def _check(name: str, status: str, reason: str = "") -> dict:
    return {"check": name, "status": status, "reason": reason}  # status: pass | fail | skipped


def _tool_student_ids(resp: dict) -> set[str]:
    ids = set()
    for t in resp.get("tools_invoked", []):
        for part in (t.get("input", {}), t.get("output", {})):
            sid = part.get("student_id")
            if isinstance(sid, str):
                ids.add(sid.upper())
    return ids


def score_case(case: dict, result) -> dict:
    """Score one case from an ApiResult. Returns {status, checks, ...}."""
    expected_types = case["expected_answer_type"]
    checks: list[dict] = []

    if not result.ok:
        # A 401/403 is an acceptable refusal when refusal is the expected behaviour.
        if result.status_code in (401, 403) and "refused" in expected_types:
            checks.append(_check("answer_type", "pass", f"HTTP {result.status_code} treated as refusal"))
            checks.append(_check("no_cross_student_tools", "pass", "request rejected before any tool ran"))
            return {"status": "PASS", "checks": checks, "actual_answer_type": f"http_{result.status_code}"}
        return {"status": "ERROR", "checks": [_check("transport_and_contract", "fail", result.error or "unknown error")],
                "actual_answer_type": None}

    resp = result.data
    actual = resp["answer_type"]
    checks.append(_check("answer_type", "pass" if actual in expected_types else "fail",
                         f"expected one of {expected_types}, got {actual}"))

    if actual == "not_found" or expected_types == ["not_found"]:
        exact = resp["answer"] == NOT_FOUND_MESSAGE
        if actual == "not_found":
            checks.append(_check("not_found_exact_message", "pass" if exact else "fail",
                                 "" if exact else f"answer was {resp['answer'][:120]!r}"))

    # Content
    contains = case.get("expected_answer_contains") or []
    if contains:
        missing = [s for s in contains if not contains_term(resp["answer"], s)]
        checks.append(_check("answer_contains", "fail" if missing else "pass",
                             f"missing {missing}" if missing else ""))
    forbidden = case.get("forbidden_strings") or []
    if forbidden:
        text = resp["answer"] + " " + resp.get("explanation", "")
        hits = [s for s in forbidden if contains_term(text, s)]
        checks.append(_check("no_forbidden_content", "fail" if hits else "pass", f"found {hits}" if hits else ""))

    # Citations
    cits = resp["citations"]
    if case.get("requires_citations") and actual not in ABSTAIN_TYPES:
        if not cits:
            checks.append(_check("citations_present", "fail", "no citations returned"))
        else:
            incomplete = [c.get("doc_id") for c in cits
                          if not (c.get("section") or c.get("page") is not None) or not c.get("version") or not c.get("effective_from")]
            checks.append(_check("citations_present", "pass"))
            checks.append(_check("citation_metadata_complete", "fail" if incomplete else "pass",
                                 f"missing section/page, version or effective_from on {incomplete}" if incomplete else ""))
    expected_ids = case.get("expected_citation_doc_ids") or []
    if expected_ids:
        if case.get("fixture_status") != "ready":
            checks.append(_check("citation_doc_ids", "skipped", "fixture pending: expected doc_ids not final"))
        else:
            got = {c.get("doc_id") for c in cits}
            missing = [d for d in expected_ids if d not in got]
            checks.append(_check("citation_doc_ids", "fail" if missing else "pass", f"missing {missing}" if missing else ""))

    # Tools and rules
    tool_names = [t["tool"] for t in resp["tools_invoked"]]
    expected_tools = case.get("expected_tools") or []
    if expected_tools:
        missing = [t for t in expected_tools if t not in tool_names]
        checks.append(_check("tools_invoked", "fail" if missing else "pass",
                             f"missing {missing}; got {tool_names}" if missing else ""))
    for tool, subset in (case.get("expected_tool_outputs") or {}).items():
        if case.get("fixture_status") != "ready":
            checks.append(_check(f"tool_output:{tool}", "skipped", "fixture pending: expected values not final"))
            continue
        outs = [t["output"] for t in resp["tools_invoked"] if t["tool"] == tool]
        ok = any(all(o.get(k) == v for k, v in subset.items()) for o in outs)
        checks.append(_check(f"tool_output:{tool}", "pass" if ok else "fail", "" if ok else f"expected {subset}, got {outs}"))
    if case.get("requires_applied_rules") and actual == "calculated":
        checks.append(_check("applied_rules_present", "pass" if resp["applied_rules"] else "fail",
                             "" if resp["applied_rules"] else "no applied_rules returned"))
    if case.get("requires_conflicts"):
        checks.append(_check("conflicts_listed", "pass" if resp["conflicts_detected"] else "fail",
                             "" if resp["conflicts_detected"] else "conflicts_detected is empty"))

    # Identity: tools may only ever run for the header student.
    used_ids = _tool_student_ids(resp)
    header = (case.get("student_id") or "").upper()
    leaked = sorted(i for i in used_ids if i in {s.upper() for s in case.get("forbidden_tool_student_ids") or []}
                    or (header and i != header) or not header)
    if case.get("forbidden_tool_student_ids") or case["category"] == "cross_student" or used_ids:
        checks.append(_check("no_cross_student_tools", "fail" if leaked else "pass",
                             f"tools ran for {leaked} (header {header or 'absent'})" if leaked else ""))

    status = "FAIL" if any(c["status"] == "fail" for c in checks) else "PASS"
    return {"status": status, "checks": checks, "actual_answer_type": actual}


# --- Metrics -----------------------------------------------------------------------------

def _rate(items: list[bool]) -> Optional[float]:
    return round(sum(items) / len(items), 4) if items else None


def _percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 1)


def _check_rate(rows: list[dict], names: tuple[str, ...]) -> Optional[float]:
    vals = [c["status"] == "pass" for r in rows for c in r["checks"]
            if c["check"] in names or any(c["check"].startswith(n + ":") for n in names) if c["status"] != "skipped"]
    return _rate(vals)


def metric(value, reason_if_na: str) -> dict:
    return {"value": value, "note": "" if value is not None else reason_if_na}


def summarise(rows: list[dict], mode: str = "live") -> dict:
    statuses = [r["status"] for r in rows]
    answered = [r for r in rows if r["status"] != "ERROR"]
    # In-process mock calls take microseconds; reporting that as latency would be misleading.
    latencies = [] if mode != "live" else [r["latency_ms"] for r in rows if r["http_status"] is not None]
    leak_checks = [c for r in rows for c in r["checks"] if c["check"] == "no_cross_student_tools"]
    type_ok = [any(c["check"] == "answer_type" and c["status"] == "pass" for c in r["checks"]) for r in rows]

    abstain_cases = [r for r in rows if set(r["expected_answer_type"]) <= ABSTAIN_TYPES]
    answer_cases = [r for r in rows if not set(r["expected_answer_type"]) & ABSTAIN_TYPES]

    hits = []
    for r in rows:
        retrieved = r.get("audit_sources_retrieved")
        if retrieved is not None and r["expected_citation_doc_ids"] and r["fixture_status"] == "ready":
            ids = {s.get("doc_id") for s in retrieved if isinstance(s, dict)}
            hits.append(any(d in ids for d in r["expected_citation_doc_ids"]))
    llm_calls = [r["audit_llm_calls"] for r in rows if isinstance(r.get("audit_llm_calls"), int)]
    tokens = [r["audit_tokens"] for r in rows if isinstance(r.get("audit_tokens"), int)]

    return {
        "mode": mode,
        "cases": len(rows),
        "pass": statuses.count("PASS"), "fail": statuses.count("FAIL"), "error": statuses.count("ERROR"),
        "skipped_checks": sum(c["status"] == "skipped" for r in rows for c in r["checks"]),
        "needs_human_review": sum(1 for r in rows if r.get("human_review") and r["status"] != "ERROR"),
        "metrics": {
            # Errored cases (API down, malformed response) count as wrong here, not as missing.
            "answer_type_accuracy": metric(_rate(type_ok), "no cases"),
            "answer_correctness_auto": metric(_check_rate(answered, ("answer_contains", "no_forbidden_content")),
                                              "no case has machine-checkable expected content yet"),
            "citation_correctness": metric(_check_rate(answered, ("citations_present", "citation_metadata_complete", "citation_doc_ids")),
                                           "no case required citations or all were abstentions"),
            "abstention_accuracy": metric(_rate([r["actual_answer_type"] in ABSTAIN_TYPES or str(r["actual_answer_type"]).startswith("http_")
                                                 for r in abstain_cases if r["status"] != "ERROR"]),
                                          "no abstention cases answered"),
            "false_abstention_rate": metric(_rate([r["actual_answer_type"] in ABSTAIN_TYPES for r in answer_cases if r["status"] != "ERROR"]),
                                            "no answerable cases answered"),
            "not_found_exact_message": metric(_check_rate(answered, ("not_found_exact_message",)), "no not_found responses"),
            "tool_correctness": metric(_check_rate(answered, ("tools_invoked", "tool_output")), "no tool cases answered"),
            "cross_student_leaks": metric(sum(c["status"] == "fail" for c in leak_checks) if leak_checks else None,
                                          "no answered case produced a cross-student check"),
            "retrieval_hit_rate": metric(_rate(hits), "needs --audit with sources_retrieved and fixture-ready expected doc_ids"),
            "latency_p50_ms": metric(_percentile(latencies, 50), "no HTTP responses (always N/A in mock mode)"),
            "latency_p95_ms": metric(_percentile(latencies, 95), "no HTTP responses (always N/A in mock mode)"),
            "llm_calls_total": metric(sum(llm_calls) if llm_calls and any(llm_calls) else None,
                                      "needs --audit and non-zero llm_calls in audit records (all zero = not instrumented)"),
            "tokens_total": metric(sum(tokens) if tokens and any(tokens) else None, "needs --audit and non-zero tokens in audit records"),
        },
    }


# --- Run ---------------------------------------------------------------------------------

def run_case(client: ApiClient, case: dict, with_audit: bool) -> dict:
    as_of = date.fromisoformat(case["as_of_date"]) if case.get("as_of_date") else None
    result = client.ask(case["question"], case.get("student_id"), as_of)
    scored = score_case(case, result)
    row = {
        "id": case["id"], "category": case["category"], "student_id": case.get("student_id"),
        "as_of_date": case.get("as_of_date"), "fixture_status": case.get("fixture_status"),
        "expected_answer_type": case["expected_answer_type"],
        "expected_citation_doc_ids": case.get("expected_citation_doc_ids") or [],
        "human_review": case.get("human_review"),
        "http_status": result.status_code, "latency_ms": round(result.latency_ms, 1),
        "error": result.error, "response": result.data if result.ok else None,
        "contract_warnings": result.warnings, **scored,
    }
    if with_audit and result.ok:
        audit = client.audit(result.data["trace_id"])
        row["audit_found"] = audit.ok
        if audit.ok:
            row["audit_sources_retrieved"] = audit.data.get("sources_retrieved")
            row["audit_llm_calls"] = audit.data.get("llm_calls")
            row["audit_tokens"] = audit.data.get("tokens")
            row["audit_latency_ms"] = audit.data.get("latency_ms")
        else:
            row["audit_error"] = audit.error
    return row


def _git_commit() -> Optional[str]:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


MOCK_BANNER = ("*** MOCK-ONLY RUN: responses are canned or injected (see eval/mock_api.py), not the real "
               "system. This checks the dataset and scoring code, NOT the assistant. ***")


def print_summary(summary: dict, rows: list[dict]) -> None:
    if summary["mode"] != "live":
        print("\n" + MOCK_BANNER)
    print(f"\n{'ID':<8} {'STATUS':<6} {'EXPECTED':<34} {'ACTUAL':<22} {'MS':>7}  REASON")
    for r in rows:
        reasons = "; ".join(f"{c['check']}: {c['reason']}" for c in r["checks"] if c["status"] == "fail")
        print(f"{r['id']:<8} {r['status']:<6} {'/'.join(r['expected_answer_type']):<34} "
              f"{str(r['actual_answer_type']):<22} {r['latency_ms']:>7.0f}  {reasons[:110]}")
    print(f"\nCases {summary['cases']}: {summary['pass']} pass, {summary['fail']} fail, {summary['error']} error; "
          f"{summary['skipped_checks']} checks skipped (fixtures pending); {summary['needs_human_review']} cases need human review")
    if summary["error"]:
        print(f"  WARNING: {summary['error']} case(s) ERRORED (no valid response). Only answer_type_accuracy "
              "counts them as wrong; the other rates exclude them, so do not read those as system quality.")
    if summary["mode"] != "live":
        print("  " + MOCK_BANNER)
    for name, m in summary["metrics"].items():
        val = "N/A" if m["value"] is None else m["value"]
        print(f"  {name:<26} {val}" + (f"  ({m['note']})" if m["value"] is None else ""))


def main(argv: Optional[list[str]] = None, session=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--base-url", default=None, help="defaults to API_BASE_URL or http://localhost:8000")
    ap.add_argument("--output", type=Path, default=None, help="results JSON (default eval/results/eval_<timestamp>.json)")
    ap.add_argument("--ids", default="", help="comma-separated case ids to run")
    ap.add_argument("--category", default="", help="only run this category")
    ap.add_argument("--audit", action="store_true", help="also fetch /audit/{trace_id} for retrieval hit rate, llm calls, tokens")
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument("--mock", action="store_true",
                    help="MOCK-ONLY: replay canned responses (no API needed); output is labelled as mock")
    args = ap.parse_args(argv)

    try:
        cases = load_dataset(args.dataset)
    except (DatasetError, OSError) as exc:
        print(f"Dataset invalid:\n{exc}", file=sys.stderr)
        return 2
    if args.validate_only:
        print(f"Dataset OK: {len(cases)} cases in {args.dataset}")
        return 0
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",") if i.strip()}
        cases = [c for c in cases if c["id"] in wanted]
    if args.category:
        cases = [c for c in cases if c["category"] == args.category]
    if not cases:
        print("No cases selected.", file=sys.stderr)
        return 2

    if args.mock and session is None:
        from eval.mock_api import MockSession
        session = MockSession(cases)
    mode = "live" if session is None else "mock"
    client = ApiClient(args.base_url or get_api_base_url(), session=session)
    started = datetime.now(timezone.utc)
    rows = [run_case(client, c, args.audit) for c in cases]
    summary = summarise(rows, mode)

    out = args.output or DEFAULT_OUTPUT_DIR / f"eval_{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "run": {
            "started_at": started.isoformat(), "base_url": client.base_url, "git_commit": _git_commit(),
            "dataset": str(args.dataset), "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
            "audit_enabled": args.audit, "mode": mode,
            "mode_note": ("MOCK-ONLY: canned or injected responses, not the real system" if mode != "live"
                          else "live HTTP calls to base_url"),
        },
        "summary": summary, "results": rows,
    }
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print_summary(summary, rows)
    print(f"\nDetailed results: {out}")
    return 1 if summary["fail"] or summary["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
