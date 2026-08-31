"""Layer 1: for each code, the injector and the detector must agree.

CLAUDE.md asks for exactly this test per anomaly code. It is parametrised over
the rule pack rather than written out seventeen times, so adding a rule file
adds its test automatically -- and a rule with no injected instances fails here
rather than showing up as a quiet 0% row in the report weeks later.
"""

from __future__ import annotations

import json

import pytest
from detector.layers.l1_rules import RuleSet, render, run_rules

pytestmark = pytest.mark.usefixtures("features")


def _codes(ruleset: RuleSet) -> list[str]:
    return sorted(ruleset.codes)


@pytest.fixture(scope="module")
def truth(labels_con):
    """Ground truth as `{code: {employee_id: (from, to)}}`. Tests only."""
    rows = labels_con.execute(
        "SELECT anomaly_code, employee_id, min(period_from), max(period_to) "
        "FROM labels_anomaly GROUP BY 1, 2"
    ).fetchall()
    out: dict[str, dict[str, tuple[int, int]]] = {}
    for code, employee, start, end in rows:
        out.setdefault(code, {})[employee] = (int(start), int(end))
    return out


@pytest.fixture(scope="module")
def found(l1):
    out: dict[str, dict[str, tuple[int, int]]] = {}
    for hit in l1.hits:
        out.setdefault(hit["anomaly_code"], {})[hit["employee_id"]] = (
            hit["period_from"], hit["period_to"]
        )
    return out


def test_every_rule_fired_on_something(ruleset: RuleSet, l1) -> None:
    silent = [code for code in ruleset.codes if not l1.by_code.get(code)]
    assert not silent, f"rules that found nothing at all: {silent}"


@pytest.mark.parametrize("code", _codes(RuleSet.load("policy")))
def test_injector_and_detector_agree(code, truth, found) -> None:
    injected = truth.get(code, {})
    detected = found.get(code, {})
    assert injected, f"{code} has a rule but no injected instances to find"

    missed = sorted(set(injected) - set(detected))
    assert not missed, (
        f"{code} recall gap: the injector broke {missed} and the rule does not "
        "find them. Reconcile docs/ANOMALY_CATALOG.md first, then the code."
    )
    spurious = sorted(set(detected) - set(injected))
    assert not spurious, (
        f"{code} precision gap: the rule flags {spurious}, which ground truth "
        "does not account for. Layer 1 emits 100%-precision hits."
    )


@pytest.mark.parametrize("code", _codes(RuleSet.load("policy")))
def test_detected_window_overlaps_the_injected_one(code, truth, found) -> None:
    """Finding the right person in the wrong months is only half a detection."""
    for employee, (hit_from, hit_to) in found.get(code, {}).items():
        label_from, label_to = truth[code][employee]
        assert hit_from <= label_to and hit_to >= label_from, (
            f"{code}/{employee}: found {hit_from}..{hit_to}, "
            f"injected {label_from}..{label_to}"
        )


def test_hits_collapse_into_one_window_per_case(l1) -> None:
    """Consecutive flagged months are one case a reviewer works, not fourteen."""
    seen: set[tuple[str, str]] = set()
    for hit in l1.hits:
        key = (hit["employee_id"], hit["anomaly_code"])
        assert key not in seen, f"{key} emitted more than one window"
        seen.add(key)
        assert hit["period_from"] <= hit["period_to"]
        assert hit["months_flagged"] >= 1


def test_every_hit_carries_a_reason_and_an_impact(l1) -> None:
    """docs/EVIDENCE_CONTRACT.md: an unexplained alert is a bug."""
    for hit in l1.hits:
        assert hit["description"].strip(), hit["anomaly_code"]
        assert "{" not in hit["description"], hit["description"]
        assert "None" not in hit["description"], hit["description"]
        assert hit["recommended_actions"], hit["anomaly_code"]
        assert hit["financial_impact_monthly"] is not None
        assert hit["financial_impact_confidence"] in ("exact", "estimated", "unknown")
        evidence = json.loads(hit["evidence_json"])
        assert evidence.get("employee_id") == hit["employee_id"]


def test_financial_impact_is_monthly_times_the_window(l1) -> None:
    for hit in l1.hits:
        if hit["financial_impact_monthly"]:
            expected = hit["financial_impact_monthly"] * hit["months_flagged"]
            assert abs(hit["financial_impact_cumulative"] - expected) < 0.51


def test_row_level_severity_follows_the_post(l1) -> None:
    """A11 is CRITICAL in a safety-critical post and MEDIUM elsewhere."""
    a11 = [h for h in l1.hits if h["anomaly_code"] == "A11"]
    assert a11
    for hit in a11:
        safety_critical = json.loads(hit["evidence_json"])["job_safety_critical"]
        assert hit["severity"] == ("CRITICAL" if safety_critical else "MEDIUM")


def test_rendering_is_null_safe() -> None:
    text = render("paid {amount} at {site}", {"amount": None, "site": "Dhahran"})
    assert text == "paid not recorded at Dhahran"


def test_a_rule_is_reproducible(con, ruleset: RuleSet, l1) -> None:
    again = run_rules(con, ruleset)
    assert again.by_code == l1.by_code
    assert [h["description"] for h in again.hits] == [
        h["description"] for h in l1.hits
    ]


def test_what_if_can_clear_a_finding(con, ruleset: RuleSet, l1) -> None:
    """The `score --what-if` path: change the fact, and the finding should go."""
    a05 = next(h for h in l1.hits if h["anomaly_code"] == "A05")
    columns = [d[0] for d in (con.execute(
        "SELECT * FROM features_period LIMIT 0").description or [])]
    projection = ", ".join(
        "'allowance' AS housing_type" if c == "housing_type" else c for c in columns
    )
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE one AS SELECT {projection} "
        "FROM features_period WHERE employee_id = ?", [a05["employee_id"]]
    )
    result = run_rules(con, ruleset, table="one")
    assert "A05" not in {h["anomaly_code"] for h in result.hits}
