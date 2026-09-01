"""The evaluation harness -- the only thing in the system that reads ground truth.

Because the anomalies were injected, evaluation here is exact rather than
estimated: `labels_anomaly` says precisely which employees carry which code, so
recall is a count and not a sample.

Two halves, and the second is as important as the first. Recall says what the
detector finds; the planted confounders say what it wrongly finds. A detector
that flags everything scores 100% recall and is useless to a reviewer, so the
confounder table is reported beside the recall table and never after it.

Nothing else in the codebase may import from this module's connection: the
`labels_*` views exist only on `lake.connect_labels()` (see `detector/lake.py`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import duckdb

from ..config import DetectorConfig
from ..lake import connect_labels
from ..layers.l1_rules import L1Result, RuleSet
from ..layers.l2_peer import CohortAssignment, L2Result
from ..layers.l2_salary import SalaryExpectation
from ..layers.l3_graph import GraphSummary, L3Result
from ..layers.l3_ml import MLScores
from ..layers.l4_fusion import BAND_ORDER, L4Result

# Precision is reported at the depths a reviewer actually works to. @100 matters
# most: those are the alerts somebody opens on Monday morning.
PRECISION_AT = (100, 1000, 5000)


# Which layer each code is expected to be caught by, from the Detector column of
# `docs/ANOMALY_CATALOG.md`, with the phase that delivers it. Presentation only:
# the catalogue is authoritative, and this map exists so a code with no detector
# yet reads as "not built" rather than as a failure.
PLANNED: dict[str, str] = {
    **{f"A{n:02d}": "L1 rule" for n in range(1, 13)},
    "B01": "L2 peer",
    "B02": "L2 peer",
    "B03": "L2 peer",
    "B04": "L2 + L1 join",
    "B05": "L2 peer",
    "B06": "L2 peer",
    "B07": "L2 peer",
    "C01": "L3 graph",
    "C02": "L3 graph",
    "C03": "L3 ML + rules",
    "C04": "L1 rule",
    "C05": "L3 graph",
    "C06": "L3 graph",
    "C07": "L1 rule",
    "C08": "L1 rule",
    "D01": "L2 peer",
    "D02": "L2 peer",
    "D03": "L1 rule",
    "D04": "L1 rule",
    "D05": "L2 + L4",
    "D06": "L2 CUSUM",
    "D07": "L2 aggregate",
}



@dataclass
class CodeScore:
    """One row of the per-anomaly-code table -- the core development feedback loop."""

    code: str
    family: str
    detector: str
    built: bool
    injected: int
    detected: int
    hits: int
    true_positives: int
    window_agreement: int

    @property
    def recall(self) -> float | None:
        return self.detected / self.injected if self.injected else None

    @property
    def precision(self) -> float | None:
        return self.true_positives / self.hits if self.hits else None

    @property
    def window_rate(self) -> float | None:
        return self.window_agreement / self.detected if self.detected else None


@dataclass
class ConfounderScore:
    """One planted legitimate look-alike, and whether the detector fell for it."""

    confounder_type: str
    confounds_code: str
    planted: int
    flagged_any: int
    flagged_by_its_code: int
    flagged_critical: int
    flagged_high: int
    codes_fired: list[str] = field(default_factory=list)


@dataclass
class MLSeparation:
    """Whether the unsupervised layer ranks the injected cases above the rest.

    Layer 3's two models produce no anomaly code, so they cannot appear in the
    recall table -- but "it scored everybody the same" is exactly the failure
    that table would have caught for a rule, and it has to be visible somewhere.
    Recall at a depth is the honest measure: if a reviewer worked the top decile
    of the model's ranking alone, how much of the injected set would they meet?
    """

    scored: int
    labelled: int
    labelled_median: float
    population_median: float
    top_decile_recall: float
    top_percent_recall: float
    device: str
    used_cuda: bool
    features: int
    contamination: float
    epochs: int
    forest_seconds: float
    autoencoder_seconds: float

    @property
    def lift(self) -> float:
        """How many times better than working a random tenth of the workforce."""
        return self.top_decile_recall / 0.10 if self.top_decile_recall else 0.0


@dataclass
class AlertSummary:
    """The queue layer 4 produced, measured the way a reviewer would meet it.

    The recall table above is about findings; this is about *alerts*, and the
    two differ on purpose. Findings are what the detectors said; alerts are what
    somebody is asked to work, banded to a weekly budget. A detector can have
    perfect recall and still fail here -- by burying the five things that matter
    under three hundred that do not.
    """

    alerts: int
    findings: int
    employees: int
    dropped_low_impact: int
    suppressed: int
    corroborated: int
    validated: int
    by_severity: dict[str, int]
    budget: dict[str, float]
    thresholds: dict[str, float]
    within_budget: dict[str, bool]
    precision_by_band: dict[str, float | None]
    impact_by_band: dict[str, float]
    confounders_by_band: dict[str, int]
    critical_confounders: list[str]
    codes_covered: int

    @property
    def per_1000(self) -> float:
        """Alerts per 1,000 employees -- the only comparable rate across tiers."""
        return self.alerts / self.employees * 1000 if self.employees else 0.0

    @property
    def collapse(self) -> float:
        """Findings per alert. B06 raises two flagged bonus months for one case."""
        return self.findings / self.alerts if self.alerts else 0.0

    @property
    def budget_ok(self) -> bool:
        return all(self.within_budget.values())


@dataclass
class EvalReport:
    """Everything `docs/EVAL_REPORT.md` renders, computed once."""

    scale: str
    run_id: str
    employees: int
    codes: list[CodeScore]
    confounders: list[ConfounderScore]
    precision_at: dict[int, float | None]
    severity_counts: dict[str, int]
    severity_budget: dict[str, float]
    impact_total: float
    runtime: dict[str, float]
    # One entry per scale tier that has been run, from
    # `data/runs/runtime_profile.json`. The spec asks the report for a runtime
    # profile "per stage at each scale tier", and a tier is only ever measured
    # by having been run -- so the profile accumulates across sessions rather
    # than being rebuilt by a run that only knows about its own scale.
    profiles: dict[str, dict]
    policy_digest: dict[str, str]
    rule_digest: str
    unlabelled_hits: int
    seconds: float
    warnings: list[str] = field(default_factory=list)
    # Layer 2's description of itself: which rung of the cohort ladder every
    # employee ended up on, and what the expected-salary model looked like.
    # Reported beside recall because a cohort that fell back to `grade` alone
    # explains a weak peer signal better than any threshold does.
    cohorts: CohortAssignment | None = None
    salary: SalaryExpectation | None = None
    # Layer 3's description of itself: how many candidate components the graph
    # search actually had to walk, and whether the two unsupervised models rank
    # the injected set above the rest of the workforce.
    graph: GraphSummary | None = None
    ml: MLSeparation | None = None
    # Layer 4's description of itself: the queue, its bands, and whether the
    # bands still separate a true finding from a planted look-alike once the
    # budget has decided how many of each there may be.
    alerts: AlertSummary | None = None

    # ------------------------------------------------------------ summaries

    def family(self, letter: str) -> list[CodeScore]:
        return [c for c in self.codes if c.family == letter]

    def family_recall(self, letter: str) -> float | None:
        rows = [c for c in self.family(letter) if c.injected and c.built]
        injected = sum(c.injected for c in rows)
        return sum(c.detected for c in rows) / injected if injected else None

    def family_precision(self, letter: str) -> float | None:
        rows = [c for c in self.family(letter) if c.hits and c.built]
        hits = sum(c.hits for c in rows)
        return sum(c.true_positives for c in rows) / hits if hits else None

    @property
    def implemented(self) -> list[CodeScore]:
        return [c for c in self.codes if c.built]

    @property
    def zero_recall(self) -> list[CodeScore]:
        """Codes with a detector that finds nothing. Always a bug, never a threshold."""
        return [c for c in self.implemented if c.injected and not c.detected]

    @property
    def pending(self) -> list[CodeScore]:
        """Codes whose detector a later phase owns. Not failures -- not built yet."""
        return [c for c in self.codes if not c.built]


def _register_hits(
    con: duckdb.DuckDBPyConnection, findings: list[dict[str, Any]]
) -> None:
    """Put every layer's hits in front of DuckDB so scoring is one set-based pass."""
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE hits (
            employee_id VARCHAR, anomaly_code VARCHAR, family VARCHAR,
            severity VARCHAR, period_from INTEGER, period_to INTEGER,
            financial_impact_monthly DOUBLE, financial_impact_cumulative DOUBLE
        )
        """
    )
    if findings:
        con.executemany(
            "INSERT INTO hits VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    h["employee_id"], h["anomaly_code"], h["family"], h["severity"],
                    h["period_from"], h["period_to"],
                    h["financial_impact_monthly"], h["financial_impact_cumulative"],
                )
                for h in findings
            ],
        )


def _code_scores(
    con: duckdb.DuckDBPyConnection,
    detectors: dict[str, str],
    planned: dict[str, str],
) -> list[CodeScore]:
    """Per-code recall and precision, over every code the catalogue defines.

    Three small passes rather than one clever join: recall is counted over
    employees (an employee either carries the code or does not) and precision
    over windows (a rule firing three times on one innocent employee is three
    things a reviewer opens, and the number should say so).
    """
    injected = {
        code: (family, int(n))
        for code, family, n in con.execute(
            "SELECT anomaly_code, any_value(family), count(DISTINCT employee_id) "
            "FROM labels_anomaly GROUP BY 1"
        ).fetchall()
    }
    raised = {
        code: int(n)
        for code, n in con.execute(
            "SELECT anomaly_code, count(*) FROM hits GROUP BY 1"
        ).fetchall()
    }
    agreed = {
        code: (int(tp), int(found), int(overlap))
        for code, tp, found, overlap in con.execute(
            """
            SELECT h.anomaly_code,
                   count(*) FILTER (WHERE l.employee_id IS NOT NULL),
                   count(DISTINCT l.employee_id),
                   count(DISTINCT CASE WHEN h.period_from <= l.label_to
                                        AND h.period_to >= l.label_from
                                       THEN l.employee_id END)
            FROM hits h
            LEFT JOIN (SELECT anomaly_code, employee_id,
                              min(period_from) AS label_from,
                              max(period_to)   AS label_to
                       FROM labels_anomaly GROUP BY 1, 2) l
              ON l.anomaly_code = h.anomaly_code AND l.employee_id = h.employee_id
            GROUP BY 1
            """
        ).fetchall()
    }

    out: list[CodeScore] = []
    for code in sorted(set(injected) | set(raised)):
        family, count = injected.get(code, (code[0], 0))
        tp, detected, overlap = agreed.get(code, (0, 0, 0))
        out.append(
            CodeScore(
                code=code,
                family=family,
                detector=detectors.get(code, planned.get(code, "pending")),
                built=code in detectors,
                injected=count,
                detected=detected,
                hits=raised.get(code, 0),
                true_positives=tp,
                window_agreement=overlap,
            )
        )
    return out


def _confounder_scores(con: duckdb.DuckDBPyConnection) -> list[ConfounderScore]:
    """The false-positive half: legitimate oddities the detector must leave alone."""
    rows = con.execute(
        """
        SELECT c.confounder_type,
               any_value(c.confounds_code)                              AS confounds_code,
               count(DISTINCT c.employee_id)                            AS planted,
               count(DISTINCT h.employee_id)                            AS flagged_any,
               count(DISTINCT CASE WHEN h.anomaly_code = c.confounds_code
                                   THEN h.employee_id END)              AS flagged_by_code,
               count(DISTINCT CASE WHEN h.severity = 'CRITICAL'
                                   THEN h.employee_id END)              AS flagged_critical,
               count(DISTINCT CASE WHEN h.severity = 'HIGH'
                                   THEN h.employee_id END)              AS flagged_high,
               list(DISTINCT h.anomaly_code) FILTER (WHERE h.anomaly_code IS NOT NULL)
                                                                        AS codes_fired
        FROM labels_confounder c
        LEFT JOIN hits h USING (employee_id)
        GROUP BY c.confounder_type
        ORDER BY c.confounder_type
        """
    ).fetchall()
    return [
        ConfounderScore(
            confounder_type=r[0],
            confounds_code=r[1],
            planted=int(r[2]),
            flagged_any=int(r[3]),
            flagged_by_its_code=int(r[4]),
            flagged_critical=int(r[5]),
            flagged_high=int(r[6]),
            codes_fired=sorted(r[7] or []),
        )
        for r in rows
    ]


def _precision_at(con: duckdb.DuckDBPyConnection) -> dict[int, float | None]:
    """What a reviewer experiences: precision down a ranked worklist.

    Ranked by cumulative financial impact then by recency, which is the order
    `policy/fusion.yaml` says a reviewer works a band in.
    """
    ranked = con.execute(
        """
        SELECT (l.employee_id IS NOT NULL)::INT AS correct
        FROM hits h
        LEFT JOIN (SELECT DISTINCT anomaly_code, employee_id FROM labels_anomaly) l
          ON l.anomaly_code = h.anomaly_code AND l.employee_id = h.employee_id
        ORDER BY h.financial_impact_cumulative DESC, h.period_from ASC
        """
    ).fetchall()
    flags = [int(r[0]) for r in ranked]
    out: dict[int, float | None] = {}
    for k in PRECISION_AT:
        window = flags[:k]
        out[k] = sum(window) / len(window) if window else None
    return out


def _ml_separation(
    con: duckdb.DuckDBPyConnection, scores: MLScores
) -> MLSeparation | None:
    """Score the unsupervised layer against ground truth, without it ever seeing it.

    The models were fitted on `lake.connect()`, which has no view over the
    labels; this runs afterwards on the harness's own connection, over the
    scores they produced.
    """
    if scores is None or scores.table is None or not scores.rows:
        return None
    con.register("ml_scores_eval", scores.table)
    try:
        row = con.execute(
            """
            WITH scored AS (
                SELECT m.employee_id, m.ml_score,
                       (l.employee_id IS NOT NULL) AS labelled,
                       percent_rank() OVER (ORDER BY m.ml_score) * 100 AS rank_pct
                FROM ml_scores_eval m
                LEFT JOIN (SELECT DISTINCT employee_id FROM labels_anomaly) l
                  USING (employee_id)
            )
            SELECT count(*),
                   count(*) FILTER (WHERE labelled),
                   median(ml_score) FILTER (WHERE labelled),
                   median(ml_score),
                   count(*) FILTER (WHERE labelled AND rank_pct >= 90),
                   count(*) FILTER (WHERE labelled AND rank_pct >= 99)
            FROM scored
            """
        ).fetchone()
    finally:
        con.unregister("ml_scores_eval")
    scored, labelled = int(row[0]), int(row[1])
    return MLSeparation(
        scored=scored,
        labelled=labelled,
        labelled_median=float(row[2] or 0.0),
        population_median=float(row[3] or 0.0),
        top_decile_recall=(int(row[4]) / labelled) if labelled else 0.0,
        top_percent_recall=(int(row[5]) / labelled) if labelled else 0.0,
        device=scores.device,
        used_cuda=scores.used_cuda,
        features=scores.features,
        contamination=scores.contamination,
        epochs=scores.epochs,
        forest_seconds=scores.forest_seconds,
        autoencoder_seconds=scores.autoencoder_seconds,
    )


def _alert_summary(
    con: duckdb.DuckDBPyConnection, cfg: DetectorConfig, result: L4Result
) -> AlertSummary:
    """Score the fused queue: precision per band, and who reached CRITICAL.

    Precision is measured per band because that is the number a reviewer feels.
    A CRITICAL band that is 60% right is a worse product than a MEDIUM band that
    is 60% right, and one overall figure hides which of the two you have.
    """
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE alerts (
            alert_id VARCHAR, employee_id VARCHAR, anomaly_code VARCHAR,
            severity VARCHAR, score INTEGER, impact DOUBLE, suppressed BOOLEAN
        )
        """
    )
    con.executemany(
        "INSERT INTO alerts VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (a.alert_id, a.employee_id, a.anomaly_code, a.severity, a.score,
             a.financial_impact_cumulative, a.suppressed)
            for a in result.alerts
        ],
    )
    rows = con.execute(
        """
        SELECT a.severity,
               count(*),
               count(*) FILTER (WHERE l.employee_id IS NOT NULL),
               coalesce(sum(a.impact), 0)
        FROM alerts a
        LEFT JOIN (SELECT DISTINCT anomaly_code, employee_id FROM labels_anomaly) l
          ON l.anomaly_code = a.anomaly_code AND l.employee_id = a.employee_id
        GROUP BY 1
        """
    ).fetchall()
    precision = {r[0]: (int(r[2]) / int(r[1]) if r[1] else None) for r in rows}
    impact = {r[0]: float(r[3]) for r in rows}

    confounders = con.execute(
        """
        SELECT a.severity, count(DISTINCT c.employee_id),
               list(DISTINCT c.confounder_type)
        FROM labels_confounder c
        JOIN alerts a USING (employee_id)
        GROUP BY 1
        """
    ).fetchall()
    by_band = {r[0]: int(r[1]) for r in confounders}
    critical = sorted(
        {name for r in confounders if r[0] == "CRITICAL" for name in (r[2] or [])}
    )

    tuning = result.tuning
    budget = dict(tuning.budget) if tuning else {}
    within = (
        {band: tuning.within_tolerance(band) for band in ("CRITICAL", "HIGH")}
        if tuning else {}
    )
    return AlertSummary(
        alerts=result.total,
        findings=result.findings_in,
        employees=cfg.employees,
        dropped_low_impact=result.dropped_low_impact,
        suppressed=result.suppressed,
        corroborated=result.corroborated,
        validated=result.validated,
        by_severity={
            band: result.by_severity.get(band, 0) for band in BAND_ORDER
        },
        budget=budget,
        thresholds=dict(result.thresholds),
        within_budget=within,
        precision_by_band={band: precision.get(band) for band in BAND_ORDER},
        impact_by_band={band: impact.get(band, 0.0) for band in BAND_ORDER},
        confounders_by_band={band: by_band.get(band, 0) for band in BAND_ORDER},
        critical_confounders=critical,
        codes_covered=len(result.by_code),
    )


def evaluate(
    cfg: DetectorConfig,
    ruleset: RuleSet,
    l1: L1Result,
    l2: L2Result | None = None,
    l3: L3Result | None = None,
    l4: L4Result | None = None,
    *,
    planned: dict[str, str] | None = None,  # defaults to PLANNED
    runtime: dict[str, float] | None = None,
    profiles: dict[str, dict] | None = None,
    policy_digest: dict[str, str] | None = None,
    rule_digest: str = "",
) -> EvalReport:
    """Score one detection run against the injected ground truth."""
    started = time.perf_counter()
    if not cfg.has_ground_truth:
        raise RuntimeError(
            "this lake carries no ground truth (generated with --no-inject); "
            "there is nothing to evaluate against"
        )
    detectors = {code: "L1 rule" for code in ruleset.codes}
    detectors.update(l2.detectors if l2 else {})
    detectors.update(l3.detectors if l3 else {})
    findings = (
        list(l1.hits)
        + (list(l2.hits) if l2 else [])
        + (list(l3.hits) if l3 else [])
    )

    con = connect_labels(cfg)
    try:
        _register_hits(con, findings)
        codes = _code_scores(con, detectors, planned or PLANNED)
        confounders = _confounder_scores(con)
        precision = _precision_at(con)
        separation = _ml_separation(con, l3.ml) if l3 and l3.ml else None
        queue = _alert_summary(con, cfg, l4) if l4 else None
        severity = dict(
            con.execute(
                "SELECT severity, count(*) FROM hits GROUP BY 1 ORDER BY 1"
            ).fetchall()
        )
        impact = con.execute(
            "SELECT coalesce(sum(financial_impact_cumulative), 0) FROM hits"
        ).fetchone()
        unlabelled = con.execute(
            """
            SELECT count(*) FROM hits h
            LEFT JOIN (SELECT DISTINCT anomaly_code, employee_id FROM labels_anomaly) l
              ON l.anomaly_code = h.anomaly_code AND l.employee_id = h.employee_id
            WHERE l.employee_id IS NULL
            """
        ).fetchone()
    finally:
        con.close()

    budget = cfg.manifest.get("injection", {})
    fusion_budget = {
        "CRITICAL": cfg.scaled(500),
        "HIGH": cfg.scaled(5000),
    }

    report = EvalReport(
        scale=cfg.scale,
        run_id=cfg.run_id,
        employees=cfg.employees,
        codes=codes,
        confounders=confounders,
        precision_at=precision,
        severity_counts={k: int(v) for k, v in severity.items()},
        severity_budget=fusion_budget,
        impact_total=float(impact[0]) if impact else 0.0,
        runtime=dict(runtime or {}),
        profiles=dict(profiles or {}),
        policy_digest=dict(policy_digest or {}),
        rule_digest=rule_digest,
        unlabelled_hits=int(unlabelled[0]) if unlabelled else 0,
        seconds=round(time.perf_counter() - started, 3),
        cohorts=l2.cohorts if l2 else None,
        salary=l2.salary if l2 else None,
        graph=l3.graph if l3 else None,
        ml=separation,
        alerts=queue,
    )
    if not budget.get("by_code"):
        report.warnings.append("lake manifest carries no injection counts")
    for row in report.zero_recall:
        report.warnings.append(
            f"{row.code} has a detector and finds nothing -- reconcile the "
            "injector and the rule in docs/ANOMALY_CATALOG.md before tuning"
        )
    return report


def hit_rows(result) -> list[dict[str, Any]]:
    """The hits as plain dicts, for the Parquet writer in `run.py`."""
    return list(result.hits)
