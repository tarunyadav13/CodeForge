"""graph.resolver: Annex A for rule_registry candidates (the hook behind tools.set_policy_resolver)."""
from datetime import date

import pytest

from graph.resolver import resolve_rule_conflict

AS_OF = date(2026, 10, 6)


def rule(rule_id, doc, level, src_from, value="80", section="1", supersedes="", op=">="):
    return {"rule_id": rule_id, "operator": op, "value": value, "source_doc_id": doc, "source_section": section,
            "source_authority_level": level, "source_effective_from": src_from, "source_supersedes": supersedes}


REG = rule("ATT-1", "REG", 1, "2024-07-01", "75", "4.1")
CIRC = rule("ATT-2", "CIRC", 2, "2026-08-01", "80", "1", supersedes="REG#4.1")


def decide(*cands, as_of=AS_OF):
    return resolve_rule_conflict(list(cands), as_of, "B.Tech CSE", 2024)


def test_explicit_supersession_by_a_circular_picks_the_circular():
    d = decide(REG, CIRC)
    assert d["winning"]["rule_id"] == "ATT-2" and d["conflicts_detected"][0]["step"].startswith("2")
    assert d["conflicts_detected"][0]["resolved"] is True and d["conflicts_detected"][0]["doc_ids"] == ["CIRC", "REG"]


def test_supersession_is_clause_scoped():
    other_clause = rule("ATT-3", "REG", 1, "2024-07-01", "65", "9.9")
    d = decide(other_clause, CIRC)  # the circular only replaces REG#4.1, so REG 9.9 survives and outranks it on authority
    assert d["winning"]["rule_id"] == "ATT-3"


def test_without_supersession_higher_authority_wins_regardless_of_date():
    d = decide(REG, rule("ATT-2", "CIRC", 2, "2026-08-01", "80", "1"))
    assert d["winning"]["rule_id"] == "ATT-1" and d["conflicts_detected"][0]["step"].startswith("3")


def test_same_authority_later_source_wins():
    a, b = rule("A", "D1", 2, "2025-01-01", "70"), rule("B", "D2", 2, "2026-01-01", "78")
    d = decide(a, b)
    assert d["winning"]["rule_id"] == "B" and any(c["step"].startswith("4") for c in d["conflicts_detected"])


def test_same_authority_same_date_different_values_is_unresolved_never_chosen():
    d = decide(rule("A", "D1", 2, "2026-01-01", "70"), rule("B", "D2", 2, "2026-01-01", "78"))
    assert d["winning"] is None and d["conflicts_detected"][-1]["status"] == "unresolved" and "disagree" in d["reason"]


def test_same_authority_same_date_same_value_is_not_a_conflict():
    d = decide(rule("B", "D2", 2, "2026-01-01", "80"), rule("A", "D1", 2, "2026-01-01", "80"))
    assert d["winning"]["rule_id"] == "A"  # deterministic tie-break on rule_id


def test_level_5_never_wins_and_cannot_supersede():
    informal = rule("F", "FAQ", 5, "2026-09-01", "65", supersedes="REG#4.1")
    assert decide(informal)["winning"] is None
    assert decide(REG, informal)["winning"]["rule_id"] == "ATT-1"


def test_only_level_1_or_2_sources_can_supersede():
    notice = rule("N", "NOTICE", 3, "2026-09-01", "70", supersedes="REG#4.1")
    assert decide(REG, notice)["winning"]["rule_id"] == "ATT-1"  # REG stays (and also wins on authority)


def test_a_rule_whose_source_is_not_yet_effective_is_not_current():
    assert decide(REG, CIRC, as_of=date(2026, 7, 1))["winning"]["rule_id"] == "ATT-1"
    assert decide(CIRC, as_of=date(2026, 7, 1))["winning"] is None


@pytest.mark.parametrize("bad", [{"source_authority_level": None}, {"source_effective_from": None}, {"source_authority_level": "x"}])
def test_candidates_without_authority_or_date_are_never_chosen(bad):
    assert decide({**REG, **bad})["winning"] is None


def test_importing_the_workflow_registers_this_resolver_with_the_tools():
    """Checked in a fresh interpreter: other tests legitimately reset the process-wide resolver to None."""
    import subprocess
    import sys
    from pathlib import Path

    code = ("import graph.workflow; from tools import get_policy_resolver; from graph.resolver import resolve_rule_conflict;"
            " print(get_policy_resolver() is resolve_rule_conflict)")
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                         capture_output=True, text=True, timeout=120, env={**__import__("os").environ, "MOCK_LLM": "true"})
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip().endswith("True")


def test_register_policy_resolver_can_be_called_again_after_a_reset():
    import graph.workflow as wf
    from tools import get_policy_resolver, set_policy_resolver

    set_policy_resolver(None)
    wf.register_policy_resolver()
    assert get_policy_resolver() is resolve_rule_conflict
