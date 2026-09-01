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

# In execution order. Later stages are declared here so `--stages` has a stable
# surface and asking for one that is not built yet fails loudly.
STAGES: tuple[str, ...] = ("features", "l1", "l2", "l3", "fusion")

BUILT: tuple[str, ...] = ("features", "l1", "l2")

STAGE_PHASE = {"l3": 5, "fusion": 6}

HITS_FILE = "l1_hits.parquet"
L2_HITS_FILE = "l2_hits.parquet"
STATE_FILE = "_stages.json"


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
    hits_path: Path | None = None
    l2_hits_path: Path | None = None

    @property
    def findings(self) -> list[dict]:
        """Every layer's hits in one list -- what phase 6 fuses and scores."""
        return (self.l1.hits if self.l1 else []) + (self.l2.hits if self.l2 else [])

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


def read_peer_hits(cfg: DetectorConfig) -> L2Result:
    """Load a cached layer-2 pass back. The cohort report is not cached: it is
    a description of the run rather than an input to it, and rebuilding it
    would mean re-running the stage the cache exists to skip."""
    frame = pl.read_parquet(cfg.run_dir / L2_HITS_FILE)
    result = L2Result(seconds=0.0, hits=frame.to_dicts())
    _recount(result)
    result.codes = tuple(sorted(result.by_code))
    return result


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

    result.seconds = round(time.perf_counter() - started, 3)
    return result
