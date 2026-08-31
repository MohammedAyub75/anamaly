"""`fact_payroll_monthly` and `fact_payroll_allowance` -- how the money is built.

Base pay is read out of `fact_assignment_history`, never invented here.  That
direction of dependency is the whole point of B04: if payroll could move a
salary on its own, a clean employee would carry an unlabelled "money moved with
no paperwork" finding.

Month-to-month totals are deliberately *stable*.  D06 looks for a change-point
against an employee's own baseline, so random jitter in net pay would drown the
signal before pass 2 ever injects one.  The only variation is structured:
overtime, the annual bonus month, an occasional absence, and the odd retro
correction -- capped below the count D02 tests for.

All arithmetic is in integer minor units. `gross` and `net` are stored, not
derived on read, and the gate reconciles them to the cent on every row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .. import entitlement as ent
from ..config import ScaleConfig, period_add, period_diff, period_of
from .assignment import Career, Interval, period_state

SEVERANCE_CODE = "SEVERANCE"


@dataclass
class PayrollPlan:
    """Per-employee facts that are constant across the 24 periods."""

    loan_cents: np.ndarray
    bonus_pct: np.ndarray
    retro: dict[int, dict[int, int]] = field(default_factory=dict)
    late_posting: np.ndarray | None = None


def plan_chunk(
    cfg: ScaleConfig,
    policy,
    streams,
    chunk: int,
    records: list[dict[str, Any]],
) -> PayrollPlan:
    """Draw the once-per-employee payroll facts for a chunk."""
    table = streams.table("fact_payroll_monthly")
    count = len(records)
    payroll = policy.payroll
    loan_spec = payroll["loan"]
    retro_spec = payroll["retro_adjustment"]
    bonus_by_rating = {int(k): float(v) for k, v in payroll["bonus"]["pct_of_base_by_rating"].items()}

    loan_draw = table.field("loan", chunk).random((count, 2))
    retro_draw = table.field("retro", chunk).random((count, 4))

    low, high = (float(x) for x in loan_spec["pct_of_base_range"])
    has_loan = loan_draw[:, 0] < float(loan_spec["employee_share"])
    loan_cents = np.where(
        has_loan,
        (np.array([r["base_salary"] for r in records]) * (low + loan_draw[:, 1] * (high - low))),
        0,
    ).astype(np.int64)
    loan_cents -= loan_cents % 100

    bonus_pct = np.array(
        [bonus_by_rating.get(int(r["performance_rating_y1"] or 3), 0.0) for r in records]
    )

    periods = cfg.period_list
    max_retro = int(retro_spec["max_per_employee_clean"])
    retro_low, retro_high = (float(x) for x in retro_spec["pct_of_base_range"])
    retro: dict[int, dict[int, int]] = {}
    for offset in range(count):
        if retro_draw[offset, 0] >= float(retro_spec["employee_share"]):
            continue
        # One or two corrections, never the three that D02 looks for.
        how_many = 1 + int(retro_draw[offset, 1] < 0.25) * (max_retro - 1)
        placed: dict[int, int] = {}
        for slot in range(how_many):
            period = periods[int(retro_draw[offset, 2 + slot] * len(periods)) % len(periods)]
            fraction = retro_low + retro_draw[offset, 1] * (retro_high - retro_low)
            cents = int(records[offset]["base_salary"] * fraction)
            placed[period] = cents - cents % 100
        retro[offset] = placed
    return PayrollPlan(loan_cents=loan_cents, bonus_pct=bonus_pct, retro=retro)


def settlement_period(cfg: ScaleConfig, policy, record: dict[str, Any]) -> int | None:
    """The single period a terminated employee's SEVERANCE lands in, if any."""
    separation = policy.payroll["separation"]
    terminated = record["termination_date"]
    if terminated is None:
        return None
    if float(record["service_years"]) < float(separation["min_service_years"]):
        return None
    return period_add(period_of(terminated), int(separation["settlement_months"]))


def active(
    cfg: ScaleConfig,
    policy,
    records: list[dict[str, Any]],
    careers: list[Career],
    period: int,
) -> tuple[list[int], list[bool]]:
    """Chunk offsets paid in `period`, and whether the row is the settlement.

    A terminated employee is paid to the end of their termination month plus
    exactly one settlement month. Anything beyond that window is C04, so it does
    not exist in pass 1.
    """
    offsets: list[int] = []
    settlement: list[bool] = []
    for offset, record in enumerate(records):
        hired = period_of(record["hire_date"])
        if period < hired:
            continue
        terminated = record["termination_date"]
        if terminated is not None:
            end = period_of(terminated)
            if period > end:
                if period == settlement_period(cfg, policy, record):
                    offsets.append(offset)
                    settlement.append(True)
                continue
        offsets.append(offset)
        settlement.append(False)
    return offsets, settlement


def as_at_row(
    cfg: ScaleConfig,
    record: dict[str, Any],
    career: Career,
    interval: Interval,
    period: int,
    site,
    safety_critical: bool,
) -> dict[str, Any]:
    """The employee as they were in `period`, for evaluating entitlement then."""
    row = dict(record)
    row["grade"] = interval.grade
    row["base_salary"] = interval.base_cents
    row["service_years"] = max(
        0.0, float(record["service_years"]) - period_diff(cfg.period_to, period) / 12.0
    )
    row["months_since_site_change"] = (
        period_diff(period, period_of(career.site_change))
        if career.site_change is not None and period_of(career.site_change) <= period
        else 999
    )
    terminated = record["termination_date"]
    row["status"] = (
        "terminated" if terminated is not None and period > period_of(terminated)
        else ("active" if record["status"] == "terminated" else record["status"])
    )
    acting = record["acting_role_since"]
    row["acting_role_flag"] = bool(
        record["acting_role_flag"] and acting is not None and period_of(acting) <= period
    )
    # The post held in that interval, not the one held today: safety-critical
    # status gates ON_CALL, SAFETY and CERT_PREMIUM.
    return ent.feature_row(row, site, interval.safety_critical)


def build_period(
    cfg: ScaleConfig,
    policy,
    resolver: ent.EntitlementResolver,
    period: int,
    records: list[dict[str, Any]],
    careers: list[Career],
    offsets: list[int],
    settlement: list[bool],
    sites,
    safety: np.ndarray,
    cost_center_by_unit: dict[str, str],
    attendance_by_offset: dict[int, tuple[float, int]],
    plan: PayrollPlan,
    late: np.ndarray,
    calendar_days: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payroll = policy.payroll
    gosi = payroll["gosi"]
    ceiling_cents = int(float(gosi["contributory_ceiling"]) * 100)
    rates = policy.gosi_rates
    overtime_spec = payroll["overtime"]
    multiplier = float(overtime_spec["multiplier"])
    standard_hours = float(overtime_spec["standard_monthly_hours"])
    bonus_month = int(payroll["bonus"]["payment_month"])

    monthly: dict[str, list[Any]] = {name: [] for name in (
        "employee_id", "period", "base_pay", "overtime_hours", "overtime_pay",
        "bonus", "retro_adjustment", "gosi_employee", "gosi_employer",
        "loan_deduction", "absence_deduction", "allowance_total", "gross", "net",
        "cost_center", "payroll_run_id", "paid_flag",
    )}
    allowance: dict[str, list[Any]] = {name: [] for name in (
        "employee_id", "period", "allowance_code", "amount", "amount_basis",
        "eligibility_snapshot_json",
    )}

    run_id = f"PR{period}-01"
    next_run_id = f"PR{period_add(period, 1)}-01"

    for offset, is_settlement in zip(offsets, settlement, strict=True):
        record = records[offset]
        career = careers[offset]
        interval = period_state(career, period)
        if interval is None:
            continue
        employee = record["employee_id"]
        site = sites[interval.site_index]

        if is_settlement:
            # The one legitimate payment after termination: no base pay, no
            # overtime, and a single SEVERANCE line. Anything else here is C04.
            severance = policy.pack.allowances[SEVERANCE_CODE]
            snapshot_row = as_at_row(
                cfg, record, career, interval, period, site, bool(safety[offset])
            )
            snapshot_row["status"] = "terminated"
            amount = int(severance.resolve_amount(snapshot_row) * 100)
            allowance["employee_id"].append(employee)
            allowance["period"].append(period)
            allowance["allowance_code"].append(SEVERANCE_CODE)
            allowance["amount"].append(amount)
            allowance["amount_basis"].append(severance.amount_basis)
            allowance["eligibility_snapshot_json"].append(
                ent.snapshot_json(severance, snapshot_row)
            )
            _append_monthly(
                monthly, employee, period, base=0, overtime_hours=0.0, overtime=0,
                bonus=0, retro=0, gosi_employee=0, gosi_employer=0, loan=0,
                absence=0, allowance_total=amount,
                cost_center=cost_center_by_unit[interval.org_unit_id],
                run_id=run_id, paid=not record["payroll_hold_flag"],
            )
            continue

        row = as_at_row(cfg, record, career, interval, period, site, bool(safety[offset]))
        payments = resolver.payments(row)
        allowance_total = 0
        for payment in payments:
            allowance["employee_id"].append(employee)
            allowance["period"].append(period)
            allowance["allowance_code"].append(payment.code)
            allowance["amount"].append(payment.cents)
            allowance["amount_basis"].append(payment.amount_basis)
            allowance["eligibility_snapshot_json"].append(payment.snapshot_json)
            allowance_total += payment.cents

        base = interval.base_cents
        hours, absence_days = attendance_by_offset.get(offset, (0.0, 0))
        hourly = base / standard_hours
        overtime_pay = round(hourly * multiplier * hours)
        overtime_pay -= overtime_pay % 100

        bonus = 0
        if record["bonus_eligible"] and period % 100 == bonus_month:
            bonus = int(base * float(plan.bonus_pct[offset]))
            bonus -= bonus % 100

        retro = plan.retro.get(offset, {}).get(period, 0)

        housing = next((p.cents for p in payments if p.code == "HOUSING"), 0)
        contributory = min(base + housing, ceiling_cents)
        employee_pct, employer_pct = rates[record["gosi_class"]]
        gosi_employee = round(contributory * employee_pct / 100)
        gosi_employer = round(contributory * employer_pct / 100)

        absence = round(base / calendar_days) * int(absence_days)
        loan = int(plan.loan_cents[offset])

        _append_monthly(
            monthly, employee, period, base=base, overtime_hours=hours,
            overtime=overtime_pay, bonus=bonus, retro=retro,
            gosi_employee=gosi_employee, gosi_employer=gosi_employer, loan=loan,
            absence=absence, allowance_total=allowance_total,
            # Charged to the unit the employee belonged to THEN. Posting a
            # transferred employee to the unit they hold today would put the
            # charge on a cost centre they had already left, which is C08.
            cost_center=cost_center_by_unit[interval.org_unit_id],
            run_id=next_run_id if late[offset] else run_id,
            paid=not record["payroll_hold_flag"],
        )

    return monthly, allowance


def _append_monthly(
    monthly: dict[str, list[Any]],
    employee: str,
    period: int,
    *,
    base: int,
    overtime_hours: float,
    overtime: int,
    bonus: int,
    retro: int,
    gosi_employee: int,
    gosi_employer: int,
    loan: int,
    absence: int,
    allowance_total: int,
    cost_center: str,
    run_id: str,
    paid: bool,
) -> None:
    gross = base + allowance_total + overtime + bonus + retro
    net = gross - gosi_employee - loan - absence
    monthly["employee_id"].append(employee)
    monthly["period"].append(period)
    monthly["base_pay"].append(base)
    monthly["overtime_hours"].append(float(overtime_hours))
    monthly["overtime_pay"].append(overtime)
    monthly["bonus"].append(bonus)
    monthly["retro_adjustment"].append(retro)
    monthly["gosi_employee"].append(gosi_employee)
    monthly["gosi_employer"].append(gosi_employer)
    monthly["loan_deduction"].append(loan)
    monthly["absence_deduction"].append(absence)
    monthly["allowance_total"].append(allowance_total)
    monthly["gross"].append(gross)
    monthly["net"].append(net)
    monthly["cost_center"].append(cost_center)
    monthly["payroll_run_id"].append(run_id)
    monthly["paid_flag"].append(bool(paid))
