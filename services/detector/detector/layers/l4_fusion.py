"""Layer 4: fusion, severity banding, the evidence bundle and financial impact.

Three layers have each said something about an employee in their own units --
a broken clause, a robust distance from a peer group, a reconstruction the
models could not manage.  This layer turns those into one ranked queue a
reviewer works from, and into the object that queue is read through.

Four decisions carry the design:

* **An alert is one (employee, anomaly code)**, not one finding.  `alert_id` is
  defined as stable for that pair plus an evidence fingerprint, and suppression
  matches on the same three, so the grain is fixed by the contract rather than
  chosen here.  It is also what collapses B06's two flagged bonus months into
  the one case a reviewer actually works.
* **Every layer is ranked in its own population**, because their raw outputs are
  not comparable: layer 1 emits a fact, layer 2 a distance, layer 3 a
  percentile.  Ranking each layer's findings among its own and blending the
  ranks is the only combination that does not silently privilege whichever
  layer happens to produce the largest numbers.
* **A rule hit is floored, never averaged away.**  `rule_hit_floor` guarantees a
  broken policy clause lands at least in the HIGH band whatever the models
  think, because the reviewer can be shown the clause.
* **The bands are tuned to the budget, not to the data.**  A severity band is a
  statement about reviewer capacity -- five CRITICAL cases a week at this scale
  -- and a detector that calls forty things CRITICAL has not found forty
  emergencies, it has made the word meaningless.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import duckdb

from ..evidence.builder import (
    AlertIdRegistry,
    EvidenceError,
    build_bundle,
    fingerprint,
    validate,
)
from .l1_rules import period_label

LAYER = "fusion"

# The four contributors of `policy/fusion.yaml`, in the order the evidence
# bundle lists them. Kept as a constant rather than read from the pack so that
# a bundle written today and one written after a weight is retuned still carry
# the same four keys -- `layer_scores` is contract, the weights are config.
LAYERS: tuple[str, ...] = ("rules", "peer_stats", "ml_unsupervised", "graph")

# Weakest wins. A fused impact is only as trustworthy as its least trustworthy
# part, so an exact figure summed with an estimated one is estimated.
CONFIDENCE_ORDER = ("exact", "estimated", "unknown")

# Worst first: the order a reviewer's queue is sorted in, and the order the
# report counts bands in. Never the alphabet, which agrees with it today only
# by accident.
BAND_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "WATCHLIST")

# A deterministic correlation id for a batch run. The id has to span UI -> API
# -> detector, so it cannot be random here and then quoted differently by a
# re-run scoring the same data; uuid5 over the run and the policy it was scored
# under gives one value both sides can recompute.
CORRELATION_NAMESPACE = uuid.UUID("6f3a1d3c-6a1b-5f2e-9c74-0a1e2b3c4d5e")


class L4Error(RuntimeError):
    """Fusion could not produce a queue. Never a threshold, always a bug."""


@dataclass
class BandTuning:
    """Where the severity thresholds landed, and how close to the budget."""

    thresholds: dict[str, float]
    configured: dict[str, float]
    counts: dict[str, int]
    budget: dict[str, float]
    tolerance: float
    remainder: str
    bands: list[str] = field(default_factory=list)

    def within_tolerance(self, band: str) -> bool:
        """Did the queue come out the size the budget asked for?

        Under budget is only a failure when the band floors were not what
        stopped it: a run with nothing severe in it should produce no CRITICAL
        alerts, and reporting that as a miss would push the next person to
        lower a floor until the number looked right.
        """
        target = self.budget.get(band)
        if not target:
            return True
        got = self.counts.get(band, 0)
        if got < target and self.thresholds.get(band, 0.0) <= self.configured.get(
            band, 0.0
        ):
            return True
        return abs(got - target) <= target * self.tolerance

    @property
    def ok(self) -> bool:
        return all(self.within_tolerance(b) for b in ("CRITICAL", "HIGH"))


@dataclass
class ScoredAlert:
    """One row of `alerts.parquet` and the bundle that explains it."""

    alert_id: str
    employee_id: str
    anomaly_code: str
    family: str
    layer: str
    severity: str
    score: int
    layer_scores: dict[str, float]
    contributing_layers: list[str]
    period_from: int
    period_to: int
    months_flagged: int
    financial_impact_monthly: float
    financial_impact_cumulative: float
    financial_impact_confidence: str
    evidence_fingerprint: str
    suppressed: bool
    suppression_reason: str | None
    findings: int
    rank_in_band: int = 0
    evidence_json: str = ""


@dataclass
class L4Result:
    """What the fusion pass produced and what it had to leave out."""

    seconds: float = 0.0
    alerts: list[ScoredAlert] = field(default_factory=list)
    tuning: BandTuning | None = None
    by_severity: dict[str, int] = field(default_factory=dict)
    by_code: dict[str, int] = field(default_factory=dict)
    by_layer: dict[str, int] = field(default_factory=dict)
    findings_in: int = 0
    dropped_low_impact: int = 0
    suppressed: int = 0
    corroborated: int = 0
    validated: int = 0
    scored_population: int = 0
    thresholds: dict[str, float] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.alerts)

    @property
    def live(self) -> list[ScoredAlert]:
        """Everything a reviewer would see: suppression hides, it never deletes."""
        return [a for a in self.alerts if not a.suppressed]


# --------------------------------------------------------------------- scoring


def percentile_ranks(values: list[float]) -> list[float]:
    """`cume_dist` as 0-100: the share of the population at or below each value.

    Deliberately never 0. A layer score of zero means "this layer said nothing
    about this employee" -- the evidence contract requires `contributing_layers`
    to agree with the non-zero entries of `layer_scores` -- so the weakest
    finding a layer produced must still score above nothing at all.
    """
    if not values:
        return []
    ordered = sorted(values)
    n = len(ordered)
    return [
        (bisect_left(ordered, v) + ordered.count(v)) / n * 100.0 for v in values
    ]


def split_layers(
    policy, l1_hits: list[dict], l2_hits: list[dict], l3_hits: list[dict]
) -> dict[str, list[dict]]:
    """Every finding under the `layer_weights` contributor that produced it.

    Layers 1 and 2 map wholesale; layer 3 splits, because a shared bank account
    and a payroll record the models could not account for are two different
    kinds of evidence that happen to have been built in the same phase.
    """
    out: dict[str, list[dict]] = {name: [] for name in LAYERS}
    out["rules"] = list(l1_hits)
    out["peer_stats"] = list(l2_hits)
    code_layer = policy.code_layer
    for hit in l3_hits:
        layer = code_layer.get(hit["anomaly_code"])
        if layer not in out:
            raise L4Error(
                f"{hit['anomaly_code']}: graph_ml.yaml declares layer {layer!r}, "
                f"which is not one of {list(LAYERS)}"
            )
        out[layer].append(hit)
    return out


def tune_bands(
    scores: list[float],
    *,
    configured: dict[str, float],
    budget: dict[str, float],
    tolerance: float,
    remainder: str,
    consumes: list[bool] | None = None,
) -> BandTuning:
    """Fill the week's queue: the most serious cases first, until the slots run out.

    `scores` arrives in queue order -- worst first -- and comes back as one band
    per alert. Two things decide a band and both are in the pack. The **budget**
    is capacity: a reviewer has room for five CRITICAL cases at this scale, and
    a detector that calls forty things CRITICAL has not found forty emergencies,
    it has made the word meaningless. The **configured band floors** are the
    bound on that: a queue with only two genuinely severe records leaves the
    other three slots empty rather than promoting a WATCHLIST record to fill
    them.

    Tuning by capacity rather than by threshold alone is not a refinement, it is
    the only thing that works at this scale. The score is an integer 0-100 over
    a few hundred alerts, so its top is a run of ties -- moving a threshold by
    one point moves the CRITICAL count from three to eight, and no threshold
    lands on five. `provenance.severity_thresholds` therefore records the score
    at each boundary the run actually produced, and every alert satisfies
    `score >= threshold[severity]`.

    A suppressed alert is banded like any other but consumes no slot: nobody is
    going to work it this week.
    """
    consumes = consumes if consumes is not None else [True] * len(scores)
    order = ("CRITICAL", "HIGH", "MEDIUM")
    room = {band: budget.get(band) for band in order}
    used = dict.fromkeys(order, 0)
    counts: dict[str, int] = {band: 0 for band in (*order, remainder)}
    bands: list[str] = []
    lowest: dict[str, float] = {}

    for score, consuming in zip(scores, consumes):
        chosen = remainder
        for band in order:
            if score < configured.get(band, 0.0):
                continue
            limit = room[band]
            if limit is not None and used[band] >= limit:
                continue
            chosen = band
            if consuming:
                used[band] += 1
            break
        bands.append(chosen)
        counts[chosen] = counts.get(chosen, 0) + 1
        lowest[chosen] = min(lowest.get(chosen, score), score)

    thresholds = {
        band: float(lowest.get(band, configured.get(band, 0.0))) for band in order
    }
    return BandTuning(
        thresholds=thresholds,
        configured=dict(configured),
        counts=counts,
        budget=dict(budget),
        tolerance=tolerance,
        remainder=remainder,
        bands=bands,
    )


def _corroborated(base: float, bonus: float) -> float:
    """The corroboration bonus, spent on the distance still left to certainty.

    `corroboration_bonus` is written in points and reads as an addition, and at
    the bottom of the scale that is exactly what this is. Near the top it is
    damped, because a plain `min(100, base + bonus)` would flatten every
    corroborated alert above 94 onto the same 100 -- and a queue where the top
    of the ranking is a nine-way tie cannot be banded to a budget at all. Two
    layers agreeing closes part of the gap to certainty; it never manufactures
    certainty out of a merely high score.
    """
    return base + bonus * (100.0 - base) / 100.0


def band_of(score: float, thresholds: dict[str, float], remainder: str) -> str:
    """The strongest band a score is *eligible* for under the tuned thresholds.

    The band an alert actually got may be lower, because the budget is capacity
    and the slots above it can be full. What this function decides is the check
    the evidence contract asks for and the one that must never fail: an alert
    always satisfies `score >= threshold[severity]`, so its severity is never
    stronger than what this returns.
    """
    for band in ("CRITICAL", "HIGH", "MEDIUM"):
        if score >= thresholds[band]:
            return band
    return remainder


# ----------------------------------------------------------------- the pass


def _plain(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _weakest(confidences: list[str]) -> str:
    ranked = [c for c in CONFIDENCE_ORDER if c in confidences]
    return ranked[-1] if ranked else "unknown"


def _impact(code: str, findings: list[dict]) -> dict[str, Any]:
    """One financial figure for a fused alert, in the two senses that differ.

    `cumulative` is everything the findings have already cost, summed. `monthly`
    is what is *still* going out each month, so only the windows that reach the
    latest month count towards it -- two separate one-month overpayments last
    year are SAR 0 a month of live exposure, and a reviewer ranking by monthly
    burn must not see them as ongoing.
    """
    period_from = min(int(f["period_from"]) for f in findings)
    period_to = max(int(f["period_to"]) for f in findings)
    monthly = sum(
        float(f["financial_impact_monthly"] or 0.0)
        for f in findings
        if int(f["period_to"]) == period_to
    )
    cumulative = sum(float(f["financial_impact_cumulative"] or 0.0) for f in findings)
    months = sum(int(f["months_flagged"] or 0) for f in findings)
    return {
        "monthly": round(monthly, 2),
        "cumulative": round(cumulative, 2),
        "currency": "SAR",
        "basis": (
            f"{len(findings)} flagged window(s) for {code} covering {months} "
            f"month(s) between {period_label(period_from)} and "
            f"{period_label(period_to)}"
        ),
        "confidence": _weakest(
            [str(f.get("financial_impact_confidence") or "unknown") for f in findings]
        ),
        "periods_affected": {"from": period_from, "to": period_to},
        "_months": months,
    }


def _suppression(
    employee_id: str,
    code: str,
    print_: str,
    cumulative: float,
    dismissals: dict[tuple[str, str, str], dict],
    config: dict,
) -> dict[str, Any]:
    """A previously dismissed finding, hidden rather than deleted.

    Three things have to match -- employee, code and the evidence fingerprint --
    so a dismissal covers the finding somebody actually looked at and nothing
    else. It lapses after `expires_after_runs`, and a materially larger amount
    resurfaces it: the reviewer accepted SAR 3,000 a month, not SAR 9,000.
    """
    clear = {"suppressed": False, "reason": None, "prior_disposition_id": None}
    if not config.get("enabled", True):
        return clear
    prior = dismissals.get((employee_id, code, print_))
    if not prior:
        return clear
    runs = int(prior.get("runs_since") or 0)
    expires = int(config.get("expires_after_runs") or 0)
    if expires and runs >= expires:
        return clear
    before = prior.get("cumulative_impact")
    grow = float(config.get("resurface_if_impact_increases_pct") or 0) / 100.0
    if before and cumulative > float(before) * (1.0 + grow):
        return clear
    return {
        "suppressed": True,
        "reason": "A reviewer dismissed this same finding and nothing about it "
                  "has changed since",
        "prior_disposition_id": str(prior.get("disposition_id") or ""),
    }


def _display_rows(
    con: duckdb.DuckDBPyConnection, employees: list[str]
) -> dict[str, dict]:
    """Everything the bundle denormalises about an employee, in one query."""
    con.execute("CREATE OR REPLACE TEMP TABLE alert_employees (employee_id VARCHAR)")
    con.executemany(
        "INSERT INTO alert_employees VALUES (?)", [(e,) for e in employees]
    )
    rows = con.execute(
        """
        SELECT e.employee_id, e.name_en, e.name_ar, e.badge_no, e.grade,
               e.job_title_en, e.org_unit_name_en, e.site_name_en,
               e.work_site_id AS site_id, e.region_code, e.employment_type,
               e.status
        FROM features_employee e
        JOIN alert_employees USING (employee_id)
        """
    )
    names = [d[0] for d in rows.description or []]
    return {
        row[0]: {name: _plain(value) for name, value in zip(names, row)}
        for row in rows.fetchall()
    }


def _timelines(
    con: duckdb.DuckDBPyConnection, periods: list[int]
) -> dict[str, dict[int, dict]]:
    """The 24-month pay series per employee, with the month an event happened.

    The event is the assignment's own change reason the month it changes, said
    in words: a reviewer lining a spike up against `promotion` understands the
    alert without opening another screen, which is the whole point of the
    bundle being self-contained.
    """
    rows = con.execute(
        """
        SELECT p.employee_id, p.period, p.base_pay, p.allowance_total, p.net,
               p.asat_change_reason,
               lag(p.asat_change_reason) OVER (
                   PARTITION BY p.employee_id ORDER BY p.period
               ) AS previous_reason
        FROM features_period p
        JOIN alert_employees USING (employee_id)
        WHERE p.period BETWEEN ? AND ?
        ORDER BY p.employee_id, p.period
        """,
        [min(periods), max(periods)],
    ).fetchall()
    out: dict[str, dict[int, dict]] = {}
    for employee_id, period, base, allowance, net, reason, previous in rows:
        event = None
        if reason and reason != previous:
            event = str(reason).replace("_", " ")
        out.setdefault(employee_id, {})[int(period)] = {
            "period": int(period),
            "base_pay": float(_plain(base) or 0.0),
            "allowance_total": float(_plain(allowance) or 0.0),
            "net": float(_plain(net) or 0.0),
            "flagged": False,
            "event": event,
        }
    return out


def _timeline_for(
    series: dict[int, dict], periods: list[int], window: tuple[int, int]
) -> list[dict]:
    """Exactly the run window, ascending, no gaps -- padded where a record has
    no row, because a reviewer reading a flat line must be able to tell a month
    of no pay from a month that is missing from the chart."""
    first, last = window
    out = []
    for period in periods:
        row = series.get(period)
        row = dict(row) if row else {
            "period": period, "base_pay": None, "allowance_total": None,
            "net": None, "event": None,
        }
        row["flagged"] = first <= period <= last
        out.append(row)
    return out


def run_fusion(
    con: duckdb.DuckDBPyConnection,
    cfg,
    policy,
    *,
    l1_hits: list[dict],
    l2_hits: list[dict],
    l3_hits: list[dict],
    ml_scores: dict[str, float] | None = None,
    dismissals: list[dict] | None = None,
    registry_path=None,
    correlation_id: str | None = None,
    log=None,
) -> L4Result:
    """Fuse every layer's findings into one ranked, explained, validated queue."""
    started = time.perf_counter()
    result = L4Result()
    ml_scores = dict(ml_scores or {})
    hits = split_layers(policy, l1_hits, l2_hits, l3_hits)
    result.findings_in = sum(len(v) for v in hits.values())

    # ------------------------------------------------ 0. the money floor
    floor = policy.min_cumulative_impact
    for layer, findings in hits.items():
        keep = []
        for hit in findings:
            cumulative = float(hit["financial_impact_cumulative"] or 0.0)
            monthly = float(hit["financial_impact_monthly"] or 0.0)
            if (cumulative > 0 or monthly > 0) and cumulative < floor:
                result.dropped_low_impact += 1
                continue
            keep.append(hit)
        hits[layer] = keep

    # --------------------------------- 1. each layer ranked in its own set
    per_finding: dict[int, float] = {}
    per_employee: dict[str, dict[str, float]] = {name: {} for name in LAYERS}
    for layer, findings in hits.items():
        ranks = percentile_ranks(
            [float(h["financial_impact_cumulative"] or 0.0) for h in findings]
        )
        for hit, rank in zip(findings, ranks):
            per_finding[id(hit)] = rank
            employee = str(hit["employee_id"])
            current = per_employee[layer].get(employee, 0.0)
            per_employee[layer][employee] = max(current, rank)

    ml_floor = policy.ml_contribution_floor
    result.scored_population = len(ml_scores)

    # ------------------------------------------ 2. one alert per employee+code
    groups: dict[tuple[str, str], list[dict]] = {}
    group_layer: dict[tuple[str, str], str] = {}
    for layer, findings in hits.items():
        for hit in findings:
            key = (str(hit["employee_id"]), str(hit["anomaly_code"]))
            groups.setdefault(key, []).append(hit)
            group_layer[key] = layer

    weights = policy.layer_weights
    bonus = policy.corroboration_bonus
    rule_floor = policy.rule_hit_floor

    staged: list[dict] = []
    for (employee_id, code), findings in sorted(groups.items()):
        layer = group_layer[(employee_id, code)]
        scores = {
            name: per_employee[name].get(employee_id, 0.0) for name in LAYERS
        }
        scores[layer] = max(per_finding[id(h)] for h in findings)
        population = float(ml_scores.get(employee_id, 0.0))
        if population >= ml_floor:
            scores["ml_unsupervised"] = max(scores["ml_unsupervised"], population)

        contributing = [name for name in LAYERS if scores[name] > 0]
        if not contributing:  # pragma: no cover - a finding always contributes
            raise L4Error(f"{employee_id}/{code}: no contributing layer")
        total_weight = sum(weights[name] for name in contributing)
        base = sum(weights[n] * scores[n] for n in contributing) / total_weight
        if "rules" in contributing:
            base = max(base, rule_floor)
        score = round(_corroborated(base, bonus.get(len(contributing), 0.0)))
        if len(contributing) > 1:
            result.corroborated += 1

        impact = _impact(code, findings)
        print_ = fingerprint(employee_id, code, findings)
        staged.append(
            {
                "employee_id": employee_id, "code": code, "layer": layer,
                "findings": findings, "scores": scores,
                "contributing": contributing, "score": score,
                "impact": impact, "fingerprint": print_,
            }
        )

    # ------------------------------------------------------ 3. suppression
    prior = {
        (str(d["employee_id"]), str(d["anomaly_code"]),
         str(d["evidence_fingerprint"])): d
        for d in (dismissals or [])
    }
    suppression_config = policy.suppression
    for item in staged:
        item["suppression"] = _suppression(
            item["employee_id"], item["code"], item["fingerprint"],
            item["impact"]["cumulative"], prior, suppression_config,
        )
    result.suppressed = sum(1 for i in staged if i["suppression"]["suppressed"])

    # ------------------------------------- 4. bands, tuned to the alert budget
    budget = {
        "CRITICAL": cfg.scaled(float(policy.alert_budget["critical"])),
        "HIGH": cfg.scaled(float(policy.alert_budget["high"])),
    }
    # Worst first, and money breaks a tie: the queue is filled from the top, so
    # the order the bands are handed out in is the order a reviewer would have
    # worked them in anyway.
    queue = sorted(
        staged,
        key=lambda i: (
            -i["score"],
            -i["impact"]["cumulative"],
            i["impact"]["periods_affected"]["from"],
            i["employee_id"],
            i["code"],
        ),
    )
    tuning = tune_bands(
        [i["score"] for i in queue],
        configured=policy.severity_bands,
        budget=budget,
        tolerance=policy.budget_tolerance,
        remainder=policy.remainder_disposition,
        consumes=[not i["suppression"]["suppressed"] for i in queue],
    )
    result.tuning = tuning
    result.thresholds = dict(tuning.thresholds)
    for item, band in zip(queue, tuning.bands):
        item["severity"] = band

    # -------------------------------------------------- 5. identity and order
    registry = AlertIdRegistry(
        registry_path
        if registry_path is not None
        else cfg.runs_root / "alert_ids.json"
    )
    keys = [
        AlertIdRegistry.key(i["employee_id"], i["code"], i["fingerprint"])
        for i in staged
    ]
    assigned = registry.assign(keys)
    registry.save()
    for item, key in zip(staged, keys):
        item["alert_id"] = assigned[key]

    # `ranking.within_band_order`: reviewers work the expensive findings first.
    ordered = sorted(
        staged,
        key=lambda i: (
            BAND_ORDER.index(i["severity"]) if i["severity"] in BAND_ORDER else 9,
            -i["impact"]["cumulative"],
            -i["score"],
            i["impact"]["periods_affected"]["from"],
            i["alert_id"],
        ),
    )
    rank: dict[str, int] = {}
    for item in ordered:
        rank[item["severity"]] = rank.get(item["severity"], 0) + 1
        item["rank_in_band"] = rank[item["severity"]]

    # ------------------------------------------------- 6. the evidence bundle
    employees = sorted({i["employee_id"] for i in staged})
    display = _display_rows(con, employees) if employees else {}
    series = _timelines(con, cfg.period_list) if employees else {}

    by_code_alerts: dict[str, list[dict]] = {}
    for item in staged:
        by_code_alerts.setdefault(item["code"], []).append(item)

    scored_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = json.dumps(policy.digest, sort_keys=True)
    correlation = correlation_id or str(
        uuid.uuid5(CORRELATION_NAMESPACE, f"{cfg.run_id}|{digest}")
    )
    packs = "sha256:" + hashlib.sha256(digest.encode("utf-8")).hexdigest()

    for item in ordered:
        code = item["code"]
        window = (
            item["impact"]["periods_affected"]["from"],
            item["impact"]["periods_affected"]["to"],
        )
        similar = [
            other["alert_id"]
            for other in sorted(
                by_code_alerts[code], key=lambda o: -o["score"]
            )
            if other["alert_id"] != item["alert_id"]
        ][:5]
        corroboration = _corroborating_reasons(policy, item, hits, per_employee)
        impact = {k: v for k, v in item["impact"].items() if not k.startswith("_")}
        bundle = build_bundle(
            alert_id=item["alert_id"],
            run_id=cfg.run_id,
            employee_id=item["employee_id"],
            anomaly_code=code,
            layer=item["layer"],
            severity=item["severity"],
            score=item["score"],
            layer_scores=item["scores"],
            contributing_layers=item["contributing"],
            findings=item["findings"],
            corroboration=corroboration,
            display=display.get(item["employee_id"], {}),
            timeline=_timeline_for(
                series.get(item["employee_id"], {}), cfg.period_list, window
            ),
            impact=impact,
            similar_cases=similar,
            suppression=item["suppression"],
            provenance={
                "detector_version": _version(),
                "policy_digest": packs,
                "scored_at": scored_at,
                "data_scale": cfg.scale,
                "correlation_id": correlation,
                "evidence_fingerprint": item["fingerprint"],
                "severity_thresholds": {
                    band: float(tuning.thresholds[band])
                    for band in ("CRITICAL", "HIGH", "MEDIUM")
                },
            },
        )
        validate(bundle)
        result.validated += 1
        result.alerts.append(
            ScoredAlert(
                alert_id=item["alert_id"],
                employee_id=item["employee_id"],
                anomaly_code=code,
                family=code[0],
                layer=item["layer"],
                severity=item["severity"],
                score=item["score"],
                layer_scores={k: round(v, 1) for k, v in item["scores"].items()},
                contributing_layers=list(item["contributing"]),
                period_from=window[0],
                period_to=window[1],
                months_flagged=int(item["impact"]["_months"]),
                financial_impact_monthly=float(impact["monthly"]),
                financial_impact_cumulative=float(impact["cumulative"]),
                financial_impact_confidence=str(impact["confidence"]),
                evidence_fingerprint=item["fingerprint"],
                suppressed=bool(item["suppression"]["suppressed"]),
                suppression_reason=item["suppression"]["reason"],
                findings=len(item["findings"]),
                rank_in_band=item["rank_in_band"],
                evidence_json=json.dumps(bundle, ensure_ascii=False, default=str),
            )
        )

    for alert in result.alerts:
        result.by_severity[alert.severity] = (
            result.by_severity.get(alert.severity, 0) + 1
        )
        result.by_code[alert.anomaly_code] = (
            result.by_code.get(alert.anomaly_code, 0) + 1
        )
        result.by_layer[alert.layer] = result.by_layer.get(alert.layer, 0) + 1

    result.seconds = round(time.perf_counter() - started, 3)
    if log:
        log(
            f"  fusion    {result.total:,} alerts from {result.findings_in:,} "
            f"findings; "
            + ", ".join(
                f"{band} {result.by_severity.get(band, 0)}"
                for band in ("CRITICAL", "HIGH", "MEDIUM", tuning.remainder)
            )
            + f"  {result.seconds:.2f}s"
        )
    return result


def _corroborating_reasons(
    policy, item: dict, hits: dict[str, list[dict]],
    per_employee: dict[str, dict[str, float]],
) -> list[dict]:
    """What the other contributing layers add, in their own words.

    A layer that produced a finding of its own is quoted from it. The two
    unsupervised models never produce a finding, so their sentence comes from
    `fusion.yaml` -- and says something about the record rather than about the
    method, because the reader is an HR reviewer (CLAUDE.md).
    """
    from ..evidence.builder import REASON_TYPE

    out: list[dict] = []
    for layer in item["contributing"]:
        if layer == item["layer"]:
            continue
        others = [
            h for h in hits.get(layer, [])
            if str(h["employee_id"]) == item["employee_id"]
            and str(h["anomaly_code"]) != item["code"]
        ]
        if others:
            best = max(
                others, key=lambda h: float(h["financial_impact_cumulative"] or 0.0)
            )
            out.append(
                {
                    "type": REASON_TYPE[layer],
                    "rule_id": best["anomaly_code"],
                    "text": best["description"],
                    "regulatory_reference": best.get("regulatory_reference"),
                    "since": str(best["period_from"]),
                    "evidence_fields": {},
                }
            )
        elif layer == "ml_unsupervised":
            out.append(
                {
                    "type": REASON_TYPE[layer],
                    "rule_id": None,
                    "text": policy.corroboration_text("ml_unsupervised"),
                    "regulatory_reference": None,
                    "since": None,
                    "evidence_fields": {},
                }
            )
    return out


def _version() -> str:
    from .. import __version__

    return __version__


__all__ = [
    "BAND_ORDER",
    "LAYERS",
    "BandTuning",
    "EvidenceError",
    "L4Error",
    "L4Result",
    "ScoredAlert",
    "band_of",
    "percentile_ranks",
    "run_fusion",
    "split_layers",
    "tune_bands",
]
