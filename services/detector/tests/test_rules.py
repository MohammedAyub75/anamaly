"""The rule pack: it loads, it is complete, and a malformed rule is fatal.

A silently skipped rule is a silent 0% recall, so every one of these asserts
that the loader *raises* rather than that it copes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from detector.layers.l1_rules import Rule, RuleError, RuleSet, period_label

REFERENCE = "A01_remote_site_allowance_at_ineligible_site.yaml"

# Every code docs/ANOMALY_CATALOG.md marks as a layer-1 detector.
L1_CODES = (
    "A01", "A02", "A03", "A04", "A05", "A06", "A07", "A08", "A09", "A10",
    "A11", "A12", "C04", "C07", "C08", "D03", "D04",
)


def test_pack_loads_every_layer_one_code(ruleset: RuleSet) -> None:
    assert set(ruleset.codes) == set(L1_CODES)
    assert all(r.enabled for r in ruleset.rules)


def test_every_family_a_code_has_a_rule(ruleset: RuleSet) -> None:
    """Family A is the phase-3 gate. A missing file is a permanent 0% row."""
    family_a = {f"A{n:02d}" for n in range(1, 13)}
    assert family_a <= set(ruleset.codes)


def test_rules_carry_reviewer_facing_evidence(ruleset: RuleSet) -> None:
    for rule in ruleset.rules:
        assert rule.evidence_fields, rule.id
        assert "employee_id" in rule.evidence_fields, rule.id
        assert 1 <= len(rule.recommended_actions) <= 5, rule.id
        assert rule.regulatory_reference.strip(), rule.id


def test_no_ml_jargon_reaches_the_reviewer(ruleset: RuleSet) -> None:
    """docs/EVIDENCE_CONTRACT.md: the bundle's text is read by HR, not by us."""
    banned = (
        "z-score", "z score", "isolation forest", "reconstruction error",
        "residual", "percentile", "anomaly score", "robust z", "shap",
    )
    for rule in ruleset.rules:
        text = " ".join(
            [rule.description_template, rule.name_en, *rule.recommended_actions]
        ).lower()
        for word in banned:
            assert word not in text, f"{rule.id} says {word!r} to a reviewer"


def test_exclusions_are_null_safe(ruleset: RuleSet) -> None:
    """A null on one side of an exclusion must not silently eat a true positive."""
    for rule in ruleset.rules:
        for exclusion in rule.exclusions:
            assert f"NOT coalesce(({exclusion}), FALSE)" in rule.where


@pytest.mark.parametrize(
    "mutation, fragment",
    [
        ({"id": "Z99"}, "not an anomaly code"),
        ({"family": "B"}, "disagrees with id"),
        ({"severity": "URGENT"}, "severity"),
        ({"evidence_fields": []}, "evidence_fields"),
        ({"recommended_actions": []}, "recommended_actions"),
        ({"description_template": "{no_such_field}"}, "neither an evidence field"),
        ({"financial_impact": {"cumulative_expr": "1"}}, "monthly_expr"),
        ({"financial_impact": {"monthly_expr": "1", "cumulative_expr": "1",
                               "confidence": "vibes"}}, "confidence"),
    ],
)
def test_malformed_rule_is_fatal(tmp_path: Path, mutation, fragment) -> None:
    source = Path(__file__).resolve().parents[3] / "policy" / "rules" / REFERENCE
    doc = yaml.safe_load(source.read_text(encoding="utf-8"))
    doc.update(mutation)
    target = tmp_path / REFERENCE
    target.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(RuleError) as exc:
        Rule.parse(target)
    assert fragment in str(exc.value)


def test_filename_must_match_the_code(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[3] / "policy" / "rules" / REFERENCE
    target = tmp_path / "A99_wrong_name.yaml"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(RuleError, match="filename must start with"):
        Rule.parse(target)


def test_two_rules_cannot_claim_one_code(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[3] / "policy" / "rules" / REFERENCE
    rules = tmp_path / "rules"
    rules.mkdir()
    text = source.read_text(encoding="utf-8")
    (rules / REFERENCE).write_text(text, encoding="utf-8")
    (rules / "A01_duplicate.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(RuleError, match="two rules claim A01"):
        RuleSet.load(tmp_path)


def test_empty_rule_directory_is_fatal(tmp_path: Path) -> None:
    (tmp_path / "rules").mkdir()
    with pytest.raises(RuleError, match="no rule files"):
        RuleSet.load(tmp_path)


def test_evidence_fields_must_exist_in_the_feature_store(ruleset: RuleSet) -> None:
    with pytest.raises(RuleError, match="feature store does not have"):
        ruleset.check_columns(["employee_id"])


def test_predicate_typo_fails_to_bind(con, ruleset: RuleSet, tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[3] / "policy" / "rules" / REFERENCE
    doc = yaml.safe_load(source.read_text(encoding="utf-8"))
    doc["sql_predicate"] = "allowance_REMOTE_SITE_amout > 0"
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / REFERENCE).write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(RuleError, match="will not bind"):
        RuleSet.load(tmp_path).check_executable(con)


def test_period_label_reads_as_a_month() -> None:
    assert period_label(202403) == "March 2024"
    assert period_label(None) == "an unrecorded month"
