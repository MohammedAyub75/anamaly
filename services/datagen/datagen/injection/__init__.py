"""Pass 2 -- anomaly injection and the ground truth that records it.

Pass 1 produces a population where every paid allowance satisfies its clause.
Pass 2 breaks specific clauses on purpose and writes down precisely what it
broke, in `labels_anomaly`; it also plants the legitimate look-alikes that keep
the evaluation honest, in `labels_confounder`.

The two passes are split because that is what makes the ground truth exact. An
injector that had to generate a career as well as break it could not say which
of the two produced a given row.  Here, every anomaly is a delta from a lake the
phase-1 gate has already certified as clean, so anything the gate now finds that
is *not* in `labels_anomaly` is a bug in this package, and the phase-2 gate says
so per code.

`labels_anomaly` is never an input to a detector. It is read only by the eval
harness (`docs/ANOMALY_CATALOG.md`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from policycore import runtime as policy_runtime

from ..config import ScaleConfig
from ..policy import DatagenPolicy
from ..schemas import ALLOWANCE_CODES, SCHEMAS
from ..writer import build_table, write_arrow
from . import confounders as confounder_module
from . import family_a, family_b, family_c, family_d
from .apply import apply_edits
from .context import Context, params_json

# Injection order, and it matters: each injector skips employees an earlier one
# has already claimed, so the scarcest cases have to go first or they find
# nobody left. The confounders lead, because several of them need a population
# feature that is genuinely rare -- a married couple both on the payroll, a
# leaver with a settlement month. D07 follows, as the only code needing a whole
# intact section of nine colleagues. Then the families in order: deterministic
# entitlement breaches, statistical ones, identity, behaviour.
#
# Changing this order changes the dataset.
STAGES = (
    confounder_module.PLANTERS,
    (family_d.d07,),
    family_a.INJECTORS,
    family_b.INJECTORS,
    family_c.INJECTORS,
    tuple(i for i in family_d.INJECTORS if i is not family_d.d07),
)

# Tables that exist before pass 2 runs; the label tables are its output.
DATA_TABLES = tuple(t for t in SCHEMAS if not t.startswith("labels_"))

# Where the working copy lives when `policy/runtime.yaml` says nothing.
DEFAULT_WORKSPACE = "data/_work"


@dataclass
class InjectionResult:
    """What pass 2 did, in the shape `manifest.json` declares it."""

    by_code: dict[str, int]
    confounders: dict[str, int]
    employees_with_anomaly: int
    row_counts: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0

    def manifest_payload(self, target_rate: float) -> dict[str, Any]:
        return {
            "target_anomaly_rate": target_rate,
            "employees_with_anomaly": self.employees_with_anomaly,
            "by_code": dict(sorted(self.by_code.items())),
            "confounders": dict(sorted(self.confounders.items())),
        }


def workspace(cfg: ScaleConfig, runtime: dict | None = None) -> Path:
    """Where pass 2 keeps its working copy of the lake. Removed when it ends."""
    settings = policy_runtime.section(runtime or {}, "datagen", "injection")
    root = Path(settings.get("workspace") or DEFAULT_WORKSPACE)
    return root / f"injection-{cfg.scale}.duckdb"


def connect(
    cfg: ScaleConfig, runtime: dict | None = None
) -> duckdb.DuckDBPyConnection:
    """A read copy of the clean lake -- the label tables do not exist yet.

    Materialised into tables with an index on `employee_id` rather than left as
    views over Parquet: every injector filters these by employee a few hundred
    times over, and against a view each of those is a fresh scan of the whole
    file. At 10k that is the difference between two minutes and ten seconds.

    **The copy is a database file, not an in-memory one** (phase 7). At 1m the
    lake is 140 million allowance rows and 24 million payroll rows; held in
    memory those tables are tens of gigabytes, and the machine that generates
    the dataset is the same 16 GB machine that has to score it. On disk, with
    the buffer budget from `policy/runtime.yaml`, the engine pages in what an
    injector asks for and pass 2 costs what its working set costs rather than
    what the whole lake weighs. The file is deleted when pass 2 finishes.
    """
    settings = policy_runtime.section(runtime or {}, "datagen", "injection")
    path = workspace(cfg, runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():  # a previous run that died before its cleanup
        path.unlink()
    con = duckdb.connect(str(path))
    limit = settings.get("memory_limit_gb")
    if limit:
        con.execute(f"SET memory_limit = '{float(limit):.1f}GB'")
    for table in DATA_TABLES:
        glob = str(cfg.table_dir(table) / "**" / "*.parquet").replace("\\", "/")
        con.execute(
            f"CREATE TABLE {table} AS SELECT * FROM read_parquet('{glob}', "
            "hive_partitioning=false)"
        )
        if table.startswith("fact_") or table == "employee_master":
            con.execute(f"CREATE INDEX ix_{table} ON {table}(employee_id)")
    return con


def inject(cfg: ScaleConfig, policy: DatagenPolicy, streams) -> InjectionResult:
    """Run every injector, plant every confounder, and rewrite the lake."""
    started = time.perf_counter()
    runtime = policy_runtime.load(policy.pack.root)
    con = connect(cfg, runtime)
    try:
        ctx = Context(cfg, policy, con, streams)
        for stage in STAGES:
            for injector in stage:
                injector(ctx)
                # Between injectors, not inside one: an injector holds its own
                # candidates until it has finished with them, and what it did
                # not edit is worth nothing to the next one (phase 7).
                ctx.release()
        _refresh_allowance_columns(ctx)
    finally:
        con.close()
        _discard_workspace(cfg, runtime)

    counts = apply_edits(cfg, ctx.edits)
    counts["labels_anomaly"] = _write_labels(cfg, ctx)
    counts["labels_confounder"] = _write_confounders(cfg, ctx)

    by_code: dict[str, int] = {}
    for label in ctx.labels:
        by_code[label.anomaly_code] = by_code.get(label.anomaly_code, 0) + 1
    planted: dict[str, int] = {}
    for confounder in ctx.confounders:
        planted[confounder.confounder_type] = planted.get(confounder.confounder_type, 0) + 1
    return InjectionResult(
        by_code=by_code,
        confounders=planted,
        employees_with_anomaly=len({label.employee_id for label in ctx.labels}),
        row_counts=counts,
        seconds=time.perf_counter() - started,
    )


def _discard_workspace(cfg: ScaleConfig, runtime: dict | None = None) -> None:
    """Delete the working copy. It is a cache of the lake, never an output."""
    path = workspace(cfg, runtime)
    for candidate in (path, path.with_suffix(path.suffix + ".wal")):
        if candidate.exists():
            candidate.unlink()


def _refresh_allowance_columns(ctx: Context) -> None:
    """Bring `employee_master`'s derived allowance columns back into step.

    `has_<CODE>`, `allowance_total_monthly` and `allowance_ratio` describe what
    the employee is paid in the current month, so an injection that changes the
    monthly allowance set has to move them too -- B03 is detected on
    `allowance_ratio`, and a stale one would hide the finding it is there to
    show.
    """
    touched = ctx.edits.touched_employees()
    for employee in sorted(touched):
        if employee not in ctx._master:  # pragma: no cover - defensive
            continue
        rows = ctx.payroll(employee)
        working = [p for p in sorted(rows) if rows[p]["base_pay"] > 0]
        if not working:
            continue
        paid = {row.code: row.cents for row in ctx.allowances(employee, working[-1])
                if not ctx.pack.allowances[row.code].one_off}
        total = sum(paid.values())
        base = ctx.master(employee)["base_salary"]
        ctx.set_master(
            employee,
            allowance_total_monthly=total,
            allowance_ratio=round(total / base, 6) if base else 0.0,
            **{f"has_{code}": code in paid for code in ALLOWANCE_CODES},
        )


def _write_labels(cfg: ScaleConfig, ctx: Context) -> int:
    rows = sorted(ctx.labels, key=lambda x: (x.employee_id, x.anomaly_code, x.period_from))
    arrow = build_table("labels_anomaly", {
        "employee_id": [r.employee_id for r in rows],
        "anomaly_code": [r.anomaly_code for r in rows],
        "family": [r.family for r in rows],
        "period_from": [r.period_from for r in rows],
        "period_to": [r.period_to for r in rows],
        "injected_severity": [r.injected_severity for r in rows],
        "injection_params_json": [params_json(r.params) for r in rows],
        "human_description": [r.human_description for r in rows],
        "work_site_id": [r.work_site_id for r in rows],
        "region_code": [r.region_code for r in rows],
        "expected_monthly_impact": [r.expected_monthly_impact for r in rows],
    })
    write_arrow(arrow, cfg.part_path("labels_anomaly", 0))
    return arrow.num_rows


def _write_confounders(cfg: ScaleConfig, ctx: Context) -> int:
    rows = sorted(ctx.confounders,
                  key=lambda x: (x.employee_id, x.confounder_type, x.period_from))
    arrow = build_table("labels_confounder", {
        "employee_id": [r.employee_id for r in rows],
        "confounder_type": [r.confounder_type for r in rows],
        "confounds_code": [r.confounds_code for r in rows],
        "period_from": [r.period_from for r in rows],
        "period_to": [r.period_to for r in rows],
        "injection_params_json": [params_json(r.params) for r in rows],
        "human_description": [r.human_description for r in rows],
        "work_site_id": [r.work_site_id for r in rows],
        "region_code": [r.region_code for r in rows],
        "expected_monthly_impact": [r.expected_monthly_impact for r in rows],
    })
    write_arrow(arrow, cfg.part_path("labels_confounder", 0))
    return arrow.num_rows


__all__ = ["InjectionResult", "connect", "inject", "workspace"]
