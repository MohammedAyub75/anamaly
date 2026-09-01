"""The batch orchestrator: features -> L1 -> (L2 -> L3 -> fusion, phases 4-6).

Every stage is independently re-runnable and cached on `stage + input digest +
policy_digest` (docs/specs/detector.md). Changing a fusion weight must re-run
layer 4 only; it must never rebuild the feature store, because a feature build
that runs when it did not need to is how a fifteen-minute target becomes a
forty-minute one.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from .config import DetectorConfig
from .features.build import build as build_features
from .features.build import cache_key as features_cache_key
from .features.build import feature_columns
from .lake import connect
from .layers.l1_rules import L1Result, RuleSet, run_rules
from .layers.l2_peer import L2Result, run_peer
from .layers.l3_graph import L3Result, run_l3
from .layers.l4_fusion import L4Result, ScoredAlert, run_fusion

# In execution order. Later stages are declared here so `--stages` has a stable
# surface and asking for one that is not built yet fails loudly.
STAGES: tuple[str, ...] = ("features", "l1", "l2", "l3", "fusion")

BUILT: tuple[str, ...] = ("features", "l1", "l2", "l3", "fusion")

STAGE_PHASE: dict[str, int] = {}

HITS_FILE = "l1_hits.parquet"
L2_HITS_FILE = "l2_hits.parquet"
L3_HITS_FILE = "l3_hits.parquet"
L3_SCORES_FILE = "l3_scores.parquet"
ALERTS_FILE = "alerts.parquet"
STATE_FILE = "_stages.json"

# Phase 13 writes this; phase 6 only has to read it, and to keep working when
# nobody has dismissed anything yet. A suppression rule that only exists once
# there is something to suppress is a rule that is first exercised in
# production.
DISMISSALS_FILE = "dismissals.parquet"


class StageNotBuilt(RuntimeError):
    """A stage that a later phase owns. Named rather than silently skipped."""


@dataclass
class RunResult:
    """One batch run: what each stage produced and how long it took."""

    run_id: str
    scale: str
    seconds: float = 0.0
    stage_seconds: dict[str, float] = field(default_factory=dict)
    stage_cached: dict[str, bool] = field(default_factory=dict)
    rows: dict[str, int] = field(default_factory=dict)
    l1: L1Result | None = None
    l2: L2Result | None = None
    l3: L3Result | None = None
    l4: L4Result | None = None
    hits_path: Path | None = None
    l2_hits_path: Path | None = None
    l3_hits_path: Path | None = None
    l3_scores_path: Path | None = None
    alerts_path: Path | None = None

    @property
    def findings(self) -> list[dict]:
        """Every layer's hits in one list -- what phase 6 fuses and scores."""
        return (
            (self.l1.hits if self.l1 else [])
            + (self.l2.hits if self.l2 else [])
            + (self.l3.hits if self.l3 else [])
        )

    @property
    def runtime(self) -> dict[str, float]:
        return dict(self.stage_seconds)


def rule_digest(ruleset: RuleSet) -> str:
    """A hash over every rule file, so editing a predicate invalidates the stage."""
    digest = hashlib.sha256()
    for rule in sorted(ruleset.rules, key=lambda r: r.id):
        digest.update(rule.path.read_bytes())
    return "sha256:" + digest.hexdigest()


def layer2_digest(policy) -> str:
    """A hash over the layer-2 detectors and their dials.

    The policy digest already covers `peer_stats.yaml`; this adds the two
    modules that turn it into SQL, for the same reason the feature cache keys
    on its own `.sql` files -- editing a detector must invalidate the stage, or
    the next run silently scores the previous one.
    """
    digest = hashlib.sha256()
    for name in ("l2_peer.py", "l2_salary.py"):
        digest.update((Path(__file__).parent / "layers" / name).read_bytes())
    digest.update(json.dumps(policy.peer_stats, sort_keys=True).encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def layer3_digest(policy) -> str:
    """A hash over the layer-3 models, the graph checks and their dials.

    Same contract as `layer2_digest`: editing a detector or a hyperparameter
    must invalidate the stage, because a cached score fitted under different
    settings is a wrong number rather than a stale one.
    """
    digest = hashlib.sha256()
    for name in ("l3_graph.py", "l3_ml.py"):
        digest.update((Path(__file__).parent / "layers" / name).read_bytes())
    digest.update(json.dumps(policy.graph_ml, sort_keys=True).encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def layer4_digest(policy) -> str:
    """A hash over the fusion engine, the bundle builder and the schema.

    `fusion.yaml` is already in the policy digest, but a weight is not the only
    thing that changes an alert: so does the shape of the object it is written
    into. The schema is hashed with the code because a bundle that validated
    yesterday and fails today is a different answer, not a stale one.
    """
    digest = hashlib.sha256()
    digest.update((Path(__file__).parent / "layers" / "l4_fusion.py").read_bytes())
    evidence = Path(__file__).parent / "evidence"
    digest.update((evidence / "builder.py").read_bytes())
    digest.update((evidence / "schemas" / "evidence_v1.json").read_bytes())
    digest.update(json.dumps(policy.fusion, sort_keys=True).encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def _state(cfg: DetectorConfig) -> dict:
    path = cfg.run_dir / STATE_FILE
    if not path.exists():
        return {}
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return {}


def _save_state(cfg: DetectorConfig, state: dict) -> None:
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    (cfg.run_dir / STATE_FILE).write_text(json.dumps(state, indent=2), encoding="utf-8")


def resolve_stages(requested: str | list[str] | None) -> list[str]:
    """`--stages features,l1` to a validated list, in execution order."""
    if requested is None:
        return list(BUILT)
    names = (
        [s.strip() for s in requested.split(",") if s.strip()]
        if isinstance(requested, str)
        else list(requested)
    )
    unknown = [n for n in names if n not in STAGES]
    if unknown:
        raise ValueError(f"unknown stage(s) {unknown}; expected {list(STAGES)}")
    pending = [n for n in names if n not in BUILT]
    if pending:
        raise StageNotBuilt(
            f"stage(s) {pending} are delivered in phase "
            + ", ".join(str(STAGE_PHASE[n]) for n in pending)
            + " -- see docs/specs/detector.md"
        )
    return [s for s in STAGES if s in names]


def write_hits(cfg: DetectorConfig, result, filename: str = HITS_FILE) -> Path:
    """One layer's findings as Parquet, the input phase 6 fuses and scores."""
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.run_dir / filename
    schema = {
        "employee_id": pl.Utf8,
        "anomaly_code": pl.Utf8,
        "family": pl.Utf8,
        "severity": pl.Utf8,
        "rule_name_en": pl.Utf8,
        "rule_name_ar": pl.Utf8,
        "allowance_code": pl.Utf8,
        "regulatory_reference": pl.Utf8,
        "period_from": pl.Int32,
        "period_to": pl.Int32,
        "months_flagged": pl.Int32,
        "financial_impact_monthly": pl.Float64,
        "financial_impact_cumulative": pl.Float64,
        "financial_impact_confidence": pl.Utf8,
        "description": pl.Utf8,
        "recommended_actions": pl.List(pl.Utf8),
        "evidence_json": pl.Utf8,
    }
    frame = pl.DataFrame(result.hits, schema=schema)
    frame.write_parquet(path)
    return path


def _recount(result) -> None:
    for row in result.hits:
        code = row["anomaly_code"]
        result.by_code[code] = result.by_code.get(code, 0) + 1
    for code in result.by_code:
        result.employees_by_code[code] = len(
            {h["employee_id"] for h in result.hits if h["anomaly_code"] == code}
        )


def read_hits(cfg: DetectorConfig) -> L1Result:
    """Load a cached layer-1 pass back into the shape the harness scores."""
    frame = pl.read_parquet(cfg.run_dir / HITS_FILE)
    result = L1Result(seconds=0.0, hits=frame.to_dicts())
    _recount(result)
    return result


def write_scores(cfg: DetectorConfig, result: L3Result) -> Path:
    """Layer 3's per-employee scores -- what phase 6 fuses as `ml_unsupervised`.

    Written beside the findings rather than folded into them: the two graph
    codes are findings about a handful of employees, while the models score
    every employee in the population and most of those scores never become an
    alert on their own.
    """
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.run_dir / L3_SCORES_FILE
    table = result.ml.table if result.ml else None
    if table is None:
        pl.DataFrame(schema={"employee_id": pl.Utf8}).write_parquet(path)
        return path
    pl.from_arrow(table).write_parquet(path)
    return path


def read_peer_hits(cfg: DetectorConfig) -> L2Result:
    """Load a cached layer-2 pass back. The cohort report is not cached: it is
    a description of the run rather than an input to it, and rebuilding it
    would mean re-running the stage the cache exists to skip."""
    frame = pl.read_parquet(cfg.run_dir / L2_HITS_FILE)
    result = L2Result(seconds=0.0, hits=frame.to_dicts())
    _recount(result)
    result.codes = tuple(sorted(result.by_code))
    return result


def read_graph_hits(cfg: DetectorConfig) -> L3Result:
    """Load a cached layer-3 pass back. As for layer 2, the description of the
    run -- the component counts and the model summary -- is not cached: it
    describes the pass rather than feeding it, and rebuilding it would mean
    re-fitting the models the cache exists to skip."""
    frame = pl.read_parquet(cfg.run_dir / L3_HITS_FILE)
    result = L3Result(seconds=0.0, hits=frame.to_dicts())
    _recount(result)
    result.codes = tuple(sorted(result.by_code))
    return result


ALERT_SCHEMA = {
    "alert_id": pl.Utf8,
    "employee_id": pl.Utf8,
    "anomaly_code": pl.Utf8,
    "family": pl.Utf8,
    "layer": pl.Utf8,
    "severity": pl.Utf8,
    "score": pl.Int32,
    "rank_in_band": pl.Int32,
    "layer_score_rules": pl.Float64,
    "layer_score_peer_stats": pl.Float64,
    "layer_score_ml_unsupervised": pl.Float64,
    "layer_score_graph": pl.Float64,
    "contributing_layers": pl.List(pl.Utf8),
    "period_from": pl.Int32,
    "period_to": pl.Int32,
    "months_flagged": pl.Int32,
    "financial_impact_monthly": pl.Float64,
    "financial_impact_cumulative": pl.Float64,
    "financial_impact_confidence": pl.Utf8,
    "evidence_fingerprint": pl.Utf8,
    "suppressed": pl.Boolean,
    "suppression_reason": pl.Utf8,
    "findings": pl.Int32,
    "evidence_json": pl.Utf8,
}


def alert_rows(result: L4Result) -> list[dict]:
    """The fused alerts as flat rows. `layer_scores` is flattened rather than
    nested because a queue is filtered and sorted on those four numbers, and a
    struct column is not what a SQL filter or a Postgres upsert wants."""
    rows = []
    for alert in result.alerts:
        row = {
            k: getattr(alert, k)
            for k in ALERT_SCHEMA
            if not k.startswith("layer_score_")
        }
        for name, value in alert.layer_scores.items():
            row[f"layer_score_{name}"] = float(value)
        rows.append(row)
    return rows


def write_alerts(cfg: DetectorConfig, result: L4Result) -> Path:
    """`alerts.parquet`: the ranked queue plus the bundle that explains each row.

    The bundle travels in the row rather than as one JSON file per alert. Phase
    8 upserts it into Postgres as JSONB in one pass, and 35,000 small files at
    1m scale would be a directory nobody can copy.
    """
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.run_dir / ALERTS_FILE
    pl.DataFrame(alert_rows(result), schema=ALERT_SCHEMA).write_parquet(path)
    return path


def read_alerts(cfg: DetectorConfig) -> L4Result:
    """Load a cached fusion pass back. The band tuning is not cached: it
    describes the decision the pass made rather than feeding it, and the
    thresholds it chose are already in every bundle's provenance."""
    frame = pl.read_parquet(cfg.run_dir / ALERTS_FILE)
    result = L4Result()
    for row in frame.to_dicts():
        scores = {
            name: float(row.pop(f"layer_score_{name}"))
            for name in ("rules", "peer_stats", "ml_unsupervised", "graph")
        }
        result.alerts.append(ScoredAlert(layer_scores=scores, **row))
    for alert in result.alerts:
        result.by_severity[alert.severity] = (
            result.by_severity.get(alert.severity, 0) + 1
        )
        result.by_code[alert.anomaly_code] = (
            result.by_code.get(alert.anomaly_code, 0) + 1
        )
        result.by_layer[alert.layer] = result.by_layer.get(alert.layer, 0) + 1
    result.suppressed = sum(1 for a in result.alerts if a.suppressed)
    result.validated = len(result.alerts)
    return result


def read_ml_scores(path: Path | None) -> dict[str, float]:
    """Layer 3's per-employee score, which fusion weights as `ml_unsupervised`."""
    if path is None or not Path(path).exists():
        return {}
    frame = pl.read_parquet(path)
    if "ml_score" not in frame.columns:
        return {}
    return {
        str(e): float(s)
        for e, s in zip(frame["employee_id"], frame["ml_score"])
    }


def read_dismissals(cfg: DetectorConfig) -> list[dict]:
    """Prior dismissals, if phase 13 has written any. Empty is the normal case."""
    path = cfg.runs_root / DISMISSALS_FILE
    if not path.exists():
        return []
    return pl.read_parquet(path).to_dicts()


def run(
    cfg: DetectorConfig,
    policy,
    ruleset: RuleSet,
    *,
    stages: str | list[str] | None = None,
    force: bool = False,
    threads: int | None = None,
    log=None,
) -> RunResult:
    """Execute the requested stages, reusing whatever is still valid on disk."""
    wanted = resolve_stages(stages)
    started = time.perf_counter()
    result = RunResult(run_id=cfg.run_id, scale=cfg.scale)
    state = _state(cfg)

    if "features" in wanted:
        built = build_features(cfg, policy, force=force, threads=threads, log=log)
        # The time reported is what the stage cost when it last really ran, not
        # what it cost to notice the cache was still good. A runtime profile
        # full of zeroes would hide the stage that is about to get slow.
        result.stage_seconds["features" + (" (cached)" if built.cached else "")] = (
            built.seconds
        )
        result.stage_cached["features"] = built.cached
        result.rows.update(built.row_counts)
        if log:
            log(
                f"features  {built.row_counts.get('features_period', 0):,} rows"
                + ("  (cached)" if built.cached else f"  {built.seconds:.2f}s")
            )

    if "l1" in wanted:
        key = "|".join(
            [features_cache_key(cfg, policy), rule_digest(ruleset), cfg.run_id]
        )
        if not force and state.get("l1") == key and (cfg.run_dir / HITS_FILE).exists():
            result.l1 = read_hits(cfg)
            result.hits_path = cfg.run_dir / HITS_FILE
            result.stage_seconds["l1 (cached)"] = float(state.get("l1_seconds", 0.0))
            result.stage_cached["l1"] = True
            if log:
                log(f"l1        {len(result.l1.hits):,} findings  (cached)")
        else:
            ruleset.check_columns(feature_columns(cfg))
            con = connect(cfg, features=True, threads=threads)
            try:
                ruleset.check_executable(con)
                l1 = run_rules(con, ruleset, log=log)
            finally:
                con.close()
            result.l1 = l1
            result.hits_path = write_hits(cfg, l1)
            result.stage_seconds["l1"] = l1.seconds
            result.stage_cached["l1"] = False
            state["l1"] = key
            state["l1_seconds"] = l1.seconds
            _save_state(cfg, state)
            if log:
                log(f"l1        {l1.total:,} findings  {l1.seconds:.2f}s")

    if "l2" in wanted:
        key = "|".join(
            [features_cache_key(cfg, policy), layer2_digest(policy), cfg.run_id]
        )
        cached = cfg.run_dir / L2_HITS_FILE
        if not force and state.get("l2") == key and cached.exists():
            result.l2 = read_peer_hits(cfg)
            result.l2_hits_path = cached
            result.stage_seconds["l2 (cached)"] = float(state.get("l2_seconds", 0.0))
            result.stage_cached["l2"] = True
            if log:
                log(f"l2        {len(result.l2.hits):,} findings  (cached)")
        else:
            con = connect(cfg, features=True, threads=threads)
            try:
                l2 = run_peer(con, policy, log=log)
            finally:
                con.close()
            result.l2 = l2
            result.l2_hits_path = write_hits(cfg, l2, L2_HITS_FILE)
            result.stage_seconds["l2"] = l2.seconds
            result.stage_cached["l2"] = False
            state["l2"] = key
            state["l2_seconds"] = l2.seconds
            _save_state(cfg, state)
            if log:
                log(f"l2        {l2.total:,} findings  {l2.seconds:.2f}s")

    if "l3" in wanted:
        key = "|".join(
            [features_cache_key(cfg, policy), layer3_digest(policy), cfg.run_id]
        )
        cached = cfg.run_dir / L3_HITS_FILE
        scores = cfg.run_dir / L3_SCORES_FILE
        if (
            not force
            and state.get("l3") == key
            and cached.exists()
            and scores.exists()
        ):
            result.l3 = read_graph_hits(cfg)
            result.l3_hits_path = cached
            result.l3_scores_path = scores
            result.stage_seconds["l3 (cached)"] = float(state.get("l3_seconds", 0.0))
            result.stage_cached["l3"] = True
            if log:
                log(f"l3        {len(result.l3.hits):,} findings  (cached)")
        else:
            con = connect(cfg, features=True, threads=threads)
            try:
                l3 = run_l3(con, policy, log=log)
            finally:
                con.close()
            result.l3 = l3
            result.l3_hits_path = write_hits(cfg, l3, L3_HITS_FILE)
            result.l3_scores_path = write_scores(cfg, l3)
            result.stage_seconds["l3"] = l3.seconds
            result.stage_cached["l3"] = False
            state["l3"] = key
            state["l3_seconds"] = l3.seconds
            _save_state(cfg, state)
            if log:
                log(f"l3        {l3.total:,} findings  {l3.seconds:.2f}s")

    if "fusion" in wanted:
        # Fusion depends on what every layer found, so its key carries theirs.
        # A re-run that only changed a weight re-runs this stage and nothing
        # else, which is the whole point of the stage cache.
        key = "|".join(
            [
                features_cache_key(cfg, policy),
                str(state.get("l1")), str(state.get("l2")), str(state.get("l3")),
                layer4_digest(policy), cfg.run_id,
            ]
        )
        cached = cfg.run_dir / ALERTS_FILE
        if not force and state.get("fusion") == key and cached.exists():
            result.l4 = read_alerts(cfg)
            result.alerts_path = cached
            result.stage_seconds["fusion (cached)"] = float(
                state.get("fusion_seconds", 0.0)
            )
            result.stage_cached["fusion"] = True
            if log:
                log(f"fusion    {len(result.l4.alerts):,} alerts  (cached)")
        else:
            if result.l1 is None and (cfg.run_dir / HITS_FILE).exists():
                result.l1 = read_hits(cfg)
            if result.l2 is None and (cfg.run_dir / L2_HITS_FILE).exists():
                result.l2 = read_peer_hits(cfg)
            if result.l3 is None and (cfg.run_dir / L3_HITS_FILE).exists():
                result.l3 = read_graph_hits(cfg)
            if result.l1 is None:
                raise StageNotBuilt(
                    "fusion has nothing to fuse: run the l1, l2 and l3 stages "
                    "first, or drop --stages to run the whole batch"
                )
            scores_path = result.l3_scores_path or (cfg.run_dir / L3_SCORES_FILE)
            con = connect(cfg, features=True, threads=threads)
            try:
                l4 = run_fusion(
                    con, cfg, policy,
                    l1_hits=result.l1.hits if result.l1 else [],
                    l2_hits=result.l2.hits if result.l2 else [],
                    l3_hits=result.l3.hits if result.l3 else [],
                    ml_scores=read_ml_scores(scores_path),
                    dismissals=read_dismissals(cfg),
                    log=log,
                )
            finally:
                con.close()
            result.l4 = l4
            result.alerts_path = write_alerts(cfg, l4)
            result.stage_seconds["fusion"] = l4.seconds
            result.stage_cached["fusion"] = False
            state["fusion"] = key
            state["fusion_seconds"] = l4.seconds
            _save_state(cfg, state)
            if log:
                log(f"fusion    {l4.total:,} alerts  {l4.seconds:.2f}s")

    result.seconds = round(time.perf_counter() - started, 3)
    return result
