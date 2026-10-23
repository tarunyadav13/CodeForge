"""Member 3: Source Register (Annex B) and corpus checks. No network, no backend."""
import csv
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import runner  # noqa: E402

REGISTER = ROOT / "data" / "source_register.csv"
# Field order of the frozen SourceRecord (handoff section 8).
FIELDS = ["doc_id", "title", "issuer", "authority_level", "doc_type", "version", "effective_from",
          "effective_to", "supersedes", "scope_programmes", "scope_batches", "provenance",
          "retrieved_on", "synthetic"]
DOC_TYPES = {"regulation", "circular", "notice", "faq", "handbook", "unofficial"}
# Real documents whose effective_from is deliberately blank (see data/README.md). Any other blank fails.
UNRESOLVED_EFFECTIVE_FROM = {"UGC-FIRSTDEG-2003", "MSRUAS-ACADREG-2022", "IITM-INTMA-ORD"}


def rows():
    with REGISTER.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_header_matches_frozen_source_record():
    with REGISTER.open(encoding="utf-8", newline="") as f:
        assert next(csv.reader(f)) == FIELDS


def test_register_has_three_real_and_synthetic_documents():
    rs = rows()
    assert len({r["doc_id"] for r in rs}) == len(rs)
    assert sum(r["synthetic"] == "N" for r in rs) >= 3
    assert 1 <= sum(r["synthetic"] == "Y" for r in rs)
    assert {r["synthetic"] for r in rs} <= {"Y", "N"}


@pytest.mark.parametrize("r", rows(), ids=lambda r: r["doc_id"])
def test_row_fields(r):
    for k in ("doc_id", "title", "issuer", "version", "provenance", "scope_programmes", "scope_batches"):
        assert r[k].strip(), k
    assert 1 <= int(r["authority_level"]) <= 5
    assert r["doc_type"] in DOC_TYPES
    date.fromisoformat(r["retrieved_on"])
    if r["effective_to"]:
        date.fromisoformat(r["effective_to"])
    if r["effective_from"]:
        date.fromisoformat(r["effective_from"])
    else:
        assert r["synthetic"] == "N" and r["doc_id"] in UNRESOLVED_EFFECTIVE_FROM, "undocumented blank effective_from"


@pytest.mark.parametrize("r", rows(), ids=lambda r: r["doc_id"])
def test_real_and_synthetic_provenance(r):
    if r["synthetic"] == "N":
        assert r["provenance"].startswith("https://")
        pdf = ROOT / r["provenance"].split("file ")[1].split(";")[0].split(")")[0]
        assert pdf.read_bytes()[:5] == b"%PDF-", pdf
    else:
        assert "SYNTHETIC" in r["provenance"]
        text = (ROOT / "data" / "documents" / "synthetic" / f"{r['doc_id']}.md").read_text(encoding="utf-8")
        assert "SYNTHETIC TEST FIXTURE" in text and "NOT real university policy" in text
        assert f"doc_id: {r['doc_id']}" in text
        assert f"Effective from: {r['effective_from']}" in text


def test_supersession_targets_exist_and_conflict_scenario_is_intact():
    by = {r["doc_id"]: r for r in rows()}
    assert by["SYN-CIRC-ATT-80"]["supersedes"] == "SYN-REG-ATT-75"
    assert by["SYN-REG-ATT-75"]["effective_from"] < by["SYN-CIRC-ATT-80"]["effective_from"]
    assert by["SYN-FAQ-ATT-65"]["authority_level"] == "5"
    a, b = by["SYN-CIRC-FEE-A"], by["SYN-CIRC-FEE-B"]
    assert (a["authority_level"], a["effective_from"], a["supersedes"]) == (b["authority_level"], b["effective_from"], b["supersedes"])


def test_no_reserved_judge_values_in_corpus():
    blob = REGISTER.read_text(encoding="utf-8").upper()
    assert "JDG" not in blob


def test_eval_citation_ids_exist_in_register():
    ids = {r["doc_id"] for r in rows()}
    for case in runner.load_dataset(ROOT / "eval" / "eval_set.jsonl"):
        for d in case.get("expected_citation_doc_ids", []):
            assert d in ids, f"{case['id']} cites {d}, not in data/source_register.csv"


def test_ready_cases_with_dates_follow_the_scenario():
    cases = {c["id"]: c for c in runner.load_dataset(ROOT / "eval" / "eval_set.jsonl")}
    reg = {r["doc_id"]: r["effective_from"] for r in rows()}
    assert cases["EV-009"]["as_of_date"] >= reg["SYN-CIRC-ATT-80"]
    assert cases["EV-010"]["as_of_date"] < reg["SYN-CIRC-ATT-80"]
    assert reg["SYN-REG-ATT-75"] <= cases["EV-010"]["as_of_date"]
    assert cases["EV-011"]["as_of_date"] < reg["SYN-CIRC-ATT-80"]  # 80% is upcoming
    json.dumps(cases)  # serialisable
