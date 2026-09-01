"""Layer 4: the queue a reviewer works, and the object they read it through.

Layers 1-3 are tested against ground truth -- did the detector find what the
injector planted.  There is no ground truth for a *queue*: nothing in the lake
says which five findings deserve to be CRITICAL this week.  So what is asserted
here is the arithmetic and the invariants instead.

Three of them carry the phase.  **A rule hit is never averaged away**, because
the floor is the whole reason a policy violation outranks a model's opinion.
**Capacity only ever breaks a tie**, because a budget that cut the queue
anywhere else would be hiding findings rather than ordering them. And **every
bundle validates**, because an invalid bundle is not discovered by the detector
that wrote it -- it is discovered by the UI, in front of a reviewer, months
later.
"""

from __future__ import annotations

import json

import pytest
from detector.evidence.builder import (
    AlertIdRegistry,
    EvidenceError,
    fingerprint,
    load_schema,
    validate,
)
from detector.layers.l4_fusion import (
    BAND_ORDER,
    LAYERS,
    band_of,
    percentile_ranks,
    run_fusion,
    split_layers,
    tune_bands,
)

pytestmark = pytest.mark.usefixtures("features")

# The same list the phase gate enforces. Every string a reviewer reads comes
# out of this layer, so it is held to the same standard as the UI (CLAUDE.md).
JARGON = (
    "z-score", "robust z", "sigma", "isolation forest", "autoencoder",
    "reconstruction", "residual", "shap", "percentile", "outlier", "cusum",
    "anomaly score", "embedding", "neural",
)

BANDS = {
    "configured": {"CRITICAL": 88.0, "HIGH": 72.0, "MEDIUM": 55.0},
}


# --------------------------------------------------------------- normalisation


def test_percentile_ranks_never_zero():
    """Zero means 'this layer said nothing'. The weakest finding still said
    something, so the scale has to start above zero."""
    ranks = percentile_ranks([1.0, 2.0, 3.0, 4.0])
    assert min(ranks) > 0
    assert max(ranks) == 100.0


def test_percentile_ranks_are_monotone():
    values = [5.0, 100.0, 0.0, 50.0]
    ranks = percentile_ranks(values)
    ordered = [r for _, r in sorted(zip(values, ranks))]
    assert ordered == sorted(ordered)


def test_percentile_ranks_tie():
    """Equal exposure is equal rank. Ties are what capacity later breaks."""
    assert percentile_ranks([7.0, 7.0, 1.0]) == [100.0, 100.0, pytest.approx(33.33, abs=0.01)]


def test_percentile_ranks_empty():
    assert percentile_ranks([]) == []


# ---------------------------------------------------------------- band tuning


def _tune(scores, critical=5, high=50, **kwargs):
    return tune_bands(
        sorted(scores, reverse=True),
        configured=BANDS["configured"],
        budget={"CRITICAL": critical, "HIGH": high},
        tolerance=0.2,
        remainder="WATCHLIST",
        **kwargs,
    )


def test_budget_caps_the_top_band():
    """Ten alerts tied at 100 and five slots: five get them, five do not."""
    tuning = _tune([100] * 10, critical=5, high=50)
    assert tuning.counts["CRITICAL"] == 5
    assert tuning.counts["HIGH"] == 5


def test_band_floors_bound_the_budget():
    """An empty slot is not filled by promoting something that does not qualify."""
    tuning = _tune([60] * 10, critical=5, high=50)
    assert tuning.counts["CRITICAL"] == 0
    assert tuning.counts["MEDIUM"] == 10
    # Under budget for a reason the floor explains is not a budget miss.
    assert tuning.within_tolerance("CRITICAL")


def test_remainder_goes_to_watchlist():
    tuning = _tune([10, 20, 30], critical=5, high=50)
    assert tuning.counts["WATCHLIST"] == 3
    assert set(tuning.bands) == {"WATCHLIST"}


def test_thresholds_record_the_boundary_actually_cut():
    tuning = _tune([100, 99, 98, 97, 96, 95, 94], critical=3, high=2)
    assert tuning.thresholds["CRITICAL"] == 98
    assert tuning.thresholds["HIGH"] == 96
    assert tuning.bands[:5] == ["CRITICAL"] * 3 + ["HIGH"] * 2


def test_a_suppressed_alert_consumes_no_slot():
    """Nobody is going to work a suppressed alert, so it takes nobody's place."""
    scores = [100] * 6
    tuning = tune_bands(
        scores,
        configured=BANDS["configured"],
        budget={"CRITICAL": 5, "HIGH": 50},
        tolerance=0.2,
        remainder="WATCHLIST",
        consumes=[False] + [True] * 5,
    )
    assert tuning.counts["CRITICAL"] == 6


def test_tune_bands_on_an_empty_queue():
    tuning = _tune([])
    assert tuning.counts["CRITICAL"] == 0
    assert tuning.thresholds == BANDS["configured"]


def test_band_of_is_an_upper_bound():
    thresholds = {"CRITICAL": 99.0, "HIGH": 88.0, "MEDIUM": 55.0}
    assert band_of(100, thresholds, "WATCHLIST") == "CRITICAL"
    assert band_of(90, thresholds, "WATCHLIST") == "HIGH"
    assert band_of(10, thresholds, "WATCHLIST") == "WATCHLIST"


# ------------------------------------------------------------------- the layers


def test_every_code_maps_to_a_layer(policy, ruleset, l4):
    """A code with no layer would score under no weight at all."""
    mapped = set(policy.code_layer) | set(ruleset.codes)
    assert len(mapped) == 34
    assert set(policy.code_layer.values()) <= set(LAYERS)


def test_split_layers_keeps_every_finding(policy, l1, l2, l3):
    split = split_layers(policy, l1.hits, l2.hits, l3.hits)
    assert sum(len(v) for v in split.values()) == len(l1.hits) + len(l2.hits) + len(l3.hits)
    assert {h["anomaly_code"] for h in split["ml_unsupervised"]} == {"C03"}


# --------------------------------------------------------------------- the grain


def test_one_alert_per_employee_and_code(l4):
    pairs = {(a.employee_id, a.anomaly_code) for a in l4.alerts}
    assert len(pairs) == l4.total


def test_repeated_windows_collapse_into_one_case(l4):
    """B06 flags both bonus months. A reviewer works the employee, once."""
    fused = [a for a in l4.alerts if a.findings > 1]
    assert fused
    assert l4.findings_in > l4.total


def test_every_code_reaches_the_queue(l4):
    assert len(l4.by_code) == 34


# ------------------------------------------------------------------- the score


def test_a_rule_hit_is_never_averaged_away(l4, policy):
    """The floor is the point: a broken clause outranks a quiet model."""
    floored = [a for a in l4.alerts if "rules" in a.contributing_layers]
    assert floored
    assert min(a.score for a in floored) >= policy.rule_hit_floor


def test_scores_stay_in_range(l4):
    assert all(0 <= a.score <= 100 for a in l4.alerts)
    assert all(isinstance(a.score, int) for a in l4.alerts)


def test_contributing_layers_match_the_scores(l4):
    """The evidence contract's own consistency rule, asserted on every alert."""
    for alert in l4.alerts:
        named = sorted(k for k, v in alert.layer_scores.items() if v > 0)
        assert sorted(alert.contributing_layers) == named


def test_corroboration_cannot_manufacture_certainty(l4):
    """Agreement closes part of the gap to 100; it never lands you on it."""
    corroborated = [a for a in l4.alerts if len(a.contributing_layers) > 1]
    assert corroborated
    saturated = [a for a in corroborated if a.score == 100]
    for alert in saturated:
        assert min(alert.layer_scores[n] for n in alert.contributing_layers) == 100


def test_every_layer_reaches_the_queue(l4):
    assert set(l4.by_layer) == set(LAYERS)


# ------------------------------------------------------------ financial impact


def test_monthly_impact_is_live_exposure_only(l4):
    """Two one-month overpayments last year are SAR 0 a month going out now."""
    for alert in l4.alerts:
        bundle = json.loads(alert.evidence_json)
        assert bundle["financial_impact"]["periods_affected"]["to"] == alert.period_to
        assert bundle["financial_impact"]["cumulative"] >= 0


def test_impact_confidence_takes_the_weakest_part(l4):
    assert {a.financial_impact_confidence for a in l4.alerts} <= {
        "exact", "estimated", "unknown"
    }


def test_the_money_floor_spares_a_non_financial_finding(con, cfg, policy, l1, l2, l3,
                                                        ml_scores):
    """A qualification gap has no money on it and is not therefore noise."""
    free = [h for h in l1.hits
            if not h["financial_impact_cumulative"] and not h["financial_impact_monthly"]]
    assert free
    codes = {h["anomaly_code"] for h in free}
    result = run_fusion(con, cfg, policy, l1_hits=l1.hits, l2_hits=l2.hits,
                        l3_hits=l3.hits, ml_scores=ml_scores)
    assert codes <= set(result.by_code)


# -------------------------------------------------------------- the fingerprint


def test_fingerprint_is_stable(l1):
    hit = l1.hits[0]
    first = fingerprint(hit["employee_id"], hit["anomaly_code"], [hit])
    second = fingerprint(hit["employee_id"], hit["anomaly_code"], [dict(hit)])
    assert first == second


def test_fingerprint_moves_when_the_amount_does(l1):
    """A dismissed finding that comes back larger is a new finding."""
    hit = dict(l1.hits[0])
    before = fingerprint(hit["employee_id"], hit["anomaly_code"], [hit])
    hit["financial_impact_cumulative"] = hit["financial_impact_cumulative"] + 1000
    assert fingerprint(hit["employee_id"], hit["anomaly_code"], [hit]) != before


def test_alert_ids_are_stable_and_unique(tmp_path):
    registry = AlertIdRegistry(tmp_path / "alert_ids.json")
    first = registry.assign(["b", "a", "c"])
    registry.save()
    reloaded = AlertIdRegistry(tmp_path / "alert_ids.json")
    second = reloaded.assign(["a", "b", "c", "d"])
    assert second["a"] == first["a"] and second["b"] == first["b"]
    assert len(set(second.values())) == 4
    assert first["a"] == "ALT-000001"  # sorted order, so two machines agree


# ------------------------------------------------------------------ the bundle


def test_the_schema_is_loadable():
    schema = load_schema()
    assert schema["properties"]["schema_version"]["const"] == 1


def test_every_bundle_validates(l4):
    assert l4.validated == l4.total
    for alert in l4.alerts:
        validate(json.loads(alert.evidence_json))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("severity", "URGENT"),
        ("score", 101),
        ("reasons", []),
        ("recommended_actions", []),
        ("anomaly_codes", []),
        ("schema_version", 2),
    ],
)
def test_the_validator_rejects_a_broken_bundle(l4, field, value):
    """The schema is a gate, not documentation."""
    bundle = json.loads(l4.alerts[0].evidence_json)
    bundle[field] = value
    with pytest.raises(EvidenceError):
        validate(bundle)


def test_the_validator_rejects_a_whole_identifier(l4):
    """`link_value_masked` is the last four digits. Never the account number."""
    linked = [a for a in l4.alerts if json.loads(a.evidence_json).get("graph_context")]
    assert linked
    bundle = json.loads(linked[0].evidence_json)
    bundle["graph_context"]["link_value_masked"] = "SA0380000000608010167519"
    with pytest.raises(EvidenceError):
        validate(bundle)


def test_every_alert_says_why(l4):
    for alert in l4.alerts:
        bundle = json.loads(alert.evidence_json)
        assert bundle["reasons"]
        assert all(reason["text"].strip() for reason in bundle["reasons"])


def test_the_timeline_is_the_whole_window(cfg, l4):
    for alert in l4.alerts[:50]:
        bundle = json.loads(alert.evidence_json)
        assert [row["period"] for row in bundle["timeline"]] == cfg.period_list
        assert any(row["flagged"] for row in bundle["timeline"])


def test_the_bundle_is_self_contained(l4):
    """Six months later, with nothing else to query."""
    bundle = json.loads(l4.alerts[0].evidence_json)
    display = bundle["employee_display"]
    assert display["name_en"] and display["job_title_en"]
    assert bundle["provenance"]["policy_digest"].startswith("sha256:")
    assert bundle["provenance"]["severity_thresholds"]["CRITICAL"] > 0


def test_severity_agrees_with_the_recorded_thresholds(l4):
    for alert in l4.alerts:
        bundle = json.loads(alert.evidence_json)
        floor = bundle["provenance"]["severity_thresholds"].get(bundle["severity"])
        if floor is not None:
            assert bundle["score"] >= floor


def test_capacity_only_ever_breaks_a_tie(l4):
    for alert in l4.alerts:
        bundle = json.loads(alert.evidence_json)
        thresholds = bundle["provenance"]["severity_thresholds"]
        eligible = band_of(bundle["score"], thresholds, "WATCHLIST")
        if BAND_ORDER.index(eligible) < BAND_ORDER.index(bundle["severity"]):
            assert bundle["score"] == thresholds[eligible]


def test_no_jargon_reaches_the_reviewer(l4):
    for alert in l4.alerts:
        bundle = json.loads(alert.evidence_json)
        texts = [r["text"] for r in bundle["reasons"]] + bundle["recommended_actions"]
        for text in texts:
            found = [word for word in JARGON if word in text.lower()]
            assert not found, f"{alert.alert_id}: {found} in {text!r}"


def test_the_models_speak_in_business_terms(l4, policy):
    """The two unsupervised models have no code to name, so their corroborating
    sentence describes the record rather than the method."""
    sentence = policy.corroboration_text("ml_unsupervised")
    assert not [w for w in JARGON if w in sentence.lower()]
    corroborated = [
        b for b in (json.loads(a.evidence_json) for a in l4.alerts)
        if any(r["type"] == "ml" and r["rule_id"] is None for r in b["reasons"])
    ]
    assert corroborated


# -------------------------------------------------------------- suppression


def _dismissal(alert, **overrides):
    return {
        "employee_id": alert.employee_id,
        "anomaly_code": alert.anomaly_code,
        "evidence_fingerprint": alert.evidence_fingerprint,
        "disposition_id": "DISP-0001",
        "runs_since": 1,
        "cumulative_impact": alert.financial_impact_cumulative,
        **overrides,
    }


def _refuse(con, cfg, policy, l1, l2, l3, ml_scores, dismissals):
    return run_fusion(
        con, cfg, policy, l1_hits=l1.hits, l2_hits=l2.hits, l3_hits=l3.hits,
        ml_scores=ml_scores, dismissals=dismissals,
    )


def test_a_dismissal_hides_rather_than_deletes(con, cfg, policy, l1, l2, l3,
                                               ml_scores, l4):
    worked = l4.alerts[0]
    result = _refuse(con, cfg, policy, l1, l2, l3, ml_scores, [_dismissal(worked)])
    assert result.total == l4.total
    assert result.suppressed == 1
    hidden = [a for a in result.alerts if a.alert_id == worked.alert_id]
    assert hidden and hidden[0].suppressed and hidden[0].suppression_reason


def test_a_dismissal_expires(con, cfg, policy, l1, l2, l3, ml_scores, l4):
    expires = int(policy.suppression["expires_after_runs"])
    result = _refuse(con, cfg, policy, l1, l2, l3, ml_scores,
                     [_dismissal(l4.alerts[0], runs_since=expires)])
    assert result.suppressed == 0


def test_a_larger_amount_resurfaces(con, cfg, policy, l1, l2, l3, ml_scores, l4):
    worked = l4.alerts[0]
    result = _refuse(
        con, cfg, policy, l1, l2, l3, ml_scores,
        [_dismissal(worked, cumulative_impact=worked.financial_impact_cumulative / 2)],
    )
    assert result.suppressed == 0


def test_a_dismissal_of_something_else_changes_nothing(con, cfg, policy, l1, l2, l3,
                                                       ml_scores, l4):
    worked = l4.alerts[0]
    result = _refuse(con, cfg, policy, l1, l2, l3, ml_scores,
                     [_dismissal(worked, evidence_fingerprint="sha256:different")])
    assert result.suppressed == 0


# ------------------------------------------------------------------ the budget


def test_the_queue_fits_the_budget(cfg, policy, l4):
    for band, at_1m in (("CRITICAL", "critical"), ("HIGH", "high")):
        target = cfg.scaled(float(policy.alert_budget[band.lower()]))
        got = l4.by_severity.get(band, 0)
        assert abs(got - target) <= target * policy.budget_tolerance


def test_fusion_is_deterministic(con, cfg, policy, l1, l2, l3, ml_scores, l4):
    """Same data, same queue: an alert id is what a case is filed under."""
    again = run_fusion(con, cfg, policy, l1_hits=l1.hits, l2_hits=l2.hits,
                       l3_hits=l3.hits, ml_scores=ml_scores)
    assert [(a.alert_id, a.score, a.severity) for a in again.alerts] == [
        (a.alert_id, a.score, a.severity) for a in l4.alerts
    ]
