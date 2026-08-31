"""Generation order and orchestration.

Dimensions first -- they are small and everything joins to them -- then the
population, then the per-period facts, chunk by chunk.  `dim_org_unit`'s
`head_employee_id` is the single forward reference, backfilled once employees
exist.

The chunk loop is what keeps memory bounded.  At 1m scale `fact_payroll_monthly`
is ~24M rows; only one chunk-period of it is ever live, and each chunk-period is
written to its own Parquet part under `period=<n>/`, so nothing is appended and
nothing is held.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import entitlement as ent
from .config import ScaleConfig
from .dimensions import allowance as allowance_dim
from .dimensions import calendar as calendar_dim
from .dimensions import grade as grade_dim
from .dimensions import job as job_dim
from .dimensions import org_unit as org_dim
from .dimensions import region as region_dim
from .dimensions import site as site_dim
from .facts import activity, attendance, banking, payroll
from .facts import assignment as assignment_fact
from .facts import employee as employee_fact
from .noise import late_posting_mask
from .policy import DatagenPolicy
from .rng import StreamRegistry
from .writer import LakeWriter, clear_lake


@dataclass
class RunResult:
    """What a generation produced, for the CLI and the gate to report on."""

    manifest: dict[str, Any]
    seconds: float
    injection_seconds: float = 0.0


def generate(cfg: ScaleConfig, policy: DatagenPolicy | None = None) -> RunResult:
    started = time.perf_counter()
    policy = policy or DatagenPolicy.load()
    streams = StreamRegistry(cfg.seed)
    writer = LakeWriter(cfg)
    clear_lake(cfg)

    # --- dimensions ------------------------------------------------------
    writer.write("dim_region", region_dim.build(policy))
    writer.write("dim_site", site_dim.build(policy))
    writer.write("dim_calendar", calendar_dim.build(cfg))
    writer.write("dim_grade", grade_dim.build(policy))
    writer.write("dim_allowance", allowance_dim.build(policy))
    jobs = job_dim.build()
    writer.write("dim_job", jobs)

    org_table = org_dim.build(cfg, policy, streams)
    org = employee_fact.OrgIndex.build(org_table, policy)

    # --- population ------------------------------------------------------
    population = employee_fact.build_population(cfg, policy, streams, org)
    org_table["head_employee_id"] = [
        population.head_of_unit.get(position) for position in range(len(org_table["org_unit_id"]))
    ]
    writer.write("dim_org_unit", org_table)

    resolver = ent.EntitlementResolver(policy.pack)
    calendar_days = calendar_dim.calendar_days_by_period(cfg)
    working_days = calendar_dim.working_days_by_period(cfg)
    safety_by_job = {code: flag for code, flag in
                     zip(jobs["job_code"], jobs["safety_critical"], strict=True)}

    for chunk, start, count in cfg.chunks():
        result = employee_fact.build_chunk(
            cfg, policy, streams, org, population, jobs, resolver, chunk, start, count
        )
        writer.write("employee_master", result.columns, chunk=chunk)
        _write_chunk_facts(
            cfg, policy, streams, writer, org, population, resolver, safety_by_job,
            calendar_days, working_days, chunk, start, count, result,
        )

    # --- pass 2 ----------------------------------------------------------
    # Injection runs over the lake pass 1 just wrote rather than inside the
    # chunk loop: every anomaly is then a delta from a population the phase-1
    # gate has certified clean, which is what makes the ground truth exact.
    counts = dict(writer.row_counts)
    payload: dict[str, Any] | None = None
    elapsed = 0.0
    if cfg.inject:
        from .injection import inject

        result = inject(cfg, policy, streams)
        counts.update(result.row_counts)
        payload = result.manifest_payload(
            float(policy.pack.injection["target_anomaly_rate"])
        )
        elapsed = result.seconds
    else:
        for table in ("labels_anomaly", "labels_confounder"):
            counts[table] = writer.write(table, _empty(table))

    manifest = writer.write_manifest(
        policy.pack.digest, injection=payload, row_counts=counts
    )
    return RunResult(manifest=manifest, seconds=time.perf_counter() - started,
                     injection_seconds=elapsed)


def _empty(table: str) -> dict[str, list]:
    """An empty label table, so a `--no-inject` lake still has every table."""
    from .schemas import SCHEMAS

    return {field.name: [] for field in SCHEMAS[table]}


def _write_chunk_facts(
    cfg: ScaleConfig,
    policy: DatagenPolicy,
    streams: StreamRegistry,
    writer: LakeWriter,
    org: employee_fact.OrgIndex,
    population: employee_fact.Population,
    resolver: ent.EntitlementResolver,
    safety_by_job: dict[str, bool],
    calendar_days: dict[int, int],
    working_days: dict[int, int],
    chunk: int,
    start: int,
    count: int,
    result: employee_fact.ChunkResult,
) -> None:
    records = result.records
    careers = result.careers
    sites = policy.pack.sites
    pop = policy.population

    ids = [r["employee_id"] for r in records]
    patterns = np.array([r["work_pattern"] for r in records], dtype=object)
    grades = np.array([r["grade"] for r in records], dtype=np.int64)
    safety = np.array([safety_by_job[r["job_code"]] for r in records], dtype=bool)
    cost_center_by_unit = dict(zip(org.ids, org.cost_centers, strict=True))

    writer.write(
        "fact_assignment_history",
        assignment_fact.rows(cfg, np.array(ids, dtype=object), careers, policy.site_ids),
        chunk=chunk,
    )

    bank_stream = streams.table("fact_bank_account")
    writer.write(
        "fact_bank_account",
        banking.build(
            cfg, records, start,
            bank_stream.field("change", chunk).random(count),
            bank_stream.field("reason", chunk).random(count),
            float(pop["banking"]["iban_change_share"]),
        ),
        chunk=chunk,
    )

    plan = payroll.plan_chunk(cfg, policy, streams, chunk, records)
    attendance_stream = streams.table("fact_attendance_monthly")
    activity_stream = streams.table("fact_system_activity_monthly")
    payroll_stream = streams.table("fact_payroll_monthly")

    for period in cfg.period_list:
        offsets, settlement = payroll.active(cfg, policy, records, careers, period)
        if not offsets:
            continue
        working = [o for o, s in zip(offsets, settlement, strict=True) if not s]

        # Drawn across the whole chunk and indexed by offset, so who happens to
        # be active in a period never shifts anybody else's numbers.
        draws = {
            name: attendance_stream.field(f"{name}:{period}", chunk).random(count)
            for name in ("leave", "leave_days", "absence", "absence_days",
                         "overtime", "overtime_gate", "leave_type")
        }
        attendance_rows = attendance.build_period(
            period=period,
            employee_ids=[ids[o] for o in working],
            work_pattern=patterns[working],
            calendar_days=calendar_days[period],
            working_days=working_days[period],
            draws={k: v[working] for k, v in draws.items()},
            settings=pop["attendance"],
            overtime_settings=policy.payroll["overtime"],
        )
        writer.write("fact_attendance_monthly", attendance_rows, chunk=chunk, period=period)

        by_offset = {
            offset: (hours, absent)
            for offset, hours, absent in zip(
                working, attendance_rows["overtime_hours"],
                attendance_rows["absence_days"], strict=True,
            )
        }
        late = late_posting_mask(
            payroll_stream.field(f"late:{period}", chunk), count, pop["noise"]
        ) if cfg.noise else np.zeros(count, dtype=bool)

        monthly, allowance_rows = payroll.build_period(
            cfg, policy, resolver, period, records, careers, offsets, settlement,
            sites, safety, cost_center_by_unit, by_offset, plan, late,
            calendar_days[period],
        )
        writer.write("fact_payroll_monthly", monthly, chunk=chunk, period=period)
        writer.write("fact_payroll_allowance", allowance_rows, chunk=chunk, period=period)

        writer.write(
            "fact_system_activity_monthly",
            activity.build_period(
                period=period,
                employee_ids=[ids[o] for o in working],
                work_pattern=patterns[working],
                grade=grades[working],
                days_worked=np.asarray(attendance_rows["days_worked"]),
                draws={"jitter": activity_stream.field(f"jitter:{period}", chunk).random(count)[working]},
                settings=pop["activity"],
            ),
            chunk=chunk,
            period=period,
        )
