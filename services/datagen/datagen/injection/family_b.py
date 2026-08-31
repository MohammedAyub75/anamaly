"""Family B -- compensation outliers against the peer group.

Statistical rather than deterministic, so these injectors move real money and
have to leave the record internally consistent while they do it: a salary that
changes has to change in `employee_master`, in every assignment interval and in
every payroll row, and every percentage allowance has to follow it, or the
finding would be an arithmetic break rather than a compensation outlier.

`resync_base` is that rule in one place.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import pairwise

from policycore import entitlement as core

from .. import entitlement as ent
from ..config import period_diff, period_first_day, period_of
from .common import fill
from .context import Context, sar
from .model import AllowanceRow


def resync_base(ctx: Context, employee: str, periods: list[int] | None = None) -> None:
    """Make payroll follow the assignment history again, allowances included.

    Base pay is read out of `fact_assignment_history` in pass 1 and must go on
    being read out of it after pass 2 moves a salary. Percentage allowances are
    recomputed from the same as-at salary, which is exactly what A07 recomputes
    independently in SQL -- so a salary injection never doubles as an
    amount-outside-the-policy-table finding.
    """
    # Every period with a payroll row, not only the working ones: a leaver's
    # settlement month carries no base pay but its SEVERANCE line is a
    # percentage of the salary, so it has to move with the salary too.
    for period in (periods if periods is not None
                   else sorted(ctx.payroll(employee))):
        interval = ctx.interval_at(employee, period)
        payroll = ctx.payroll_row(employee, period)
        if interval is None or payroll is None:
            continue
        rows = []
        for row in ctx.allowances(employee, period):
            if ctx.pack.allowances[row.code].amount_basis == "pct_of_base":
                rows.append(AllowanceRow(row.code,
                                         ctx.policy_amount(row.code, employee, period),
                                         row.basis, row.snapshot))
            else:
                rows.append(row)
        ctx.set_allowances(employee, period, rows)
        if payroll["base_pay"] > 0:
            ctx.set_payroll(employee, period, base_pay=interval["base_salary"])


def rescale_salary(ctx: Context, employee: str, target_cents: int) -> None:
    """Move an employee's salary to `target_cents`, career and payroll included."""
    record = ctx.master(employee)
    current = record["base_salary"]
    if current <= 0:
        return
    factor = target_cents / current
    history = []
    for row in ctx.history(employee):
        row = dict(row)
        row["base_salary"] = int(round(row["base_salary"] * factor / 100) * 100)
        history.append(row)
    ctx.set_history(employee, history)
    ctx.set_master(employee, base_salary=history[-1]["base_salary"])
    resync_base(ctx, employee)


def _band_medians(ctx: Context) -> dict[tuple[int, str], int]:
    rows = ctx.con.execute(
        "SELECT grade, nationality_class, median(base_salary) FROM employee_master "
        "GROUP BY 1, 2"
    ).fetchall()
    return {(int(g), k): int(m.scaleb(2)) for g, k, m in rows}


def b01(ctx: Context) -> None:
    """A base salary above the top of the approved band."""
    code = "B01"
    low, high = (float(x) for x in ctx.code_spec(code)["band_factor"])
    rng = ctx.rng(code)
    medians = _band_medians(ctx)
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "AND grade BETWEEN 5 AND 18 ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        record = ctx.master(employee)
        band = ctx.pack.grade_bands[(int(record["grade"]), record["nationality_class"])]
        factor = low + float(rng.random()) * (high - low)
        target = int(int(band.salary_max * 100) * factor)
        if target <= record["base_salary"]:
            return False
        was = record["base_salary"]
        rescale_salary(ctx, employee, target)
        median = medians.get((int(record["grade"]), record["nationality_class"]), was)
        ctx.label(
            employee, code, (ctx.cfg.period_from, ctx.cfg.period_to), target - was,
            f"Base salary of {sar(target)} a month against a band maximum of "
            f"{sar(int(band.salary_max * 100))} for grade {record['grade']}; the "
            f"typical salary at this grade is {sar(median)}.",
            base_salary=target / 100, previous_salary=was / 100,
            band_max=float(band.salary_max), cohort_median=median / 100,
            factor=round(factor, 3),
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def b02(ctx: Context) -> None:
    """A base salary below the bottom of the approved band -- an underpayment."""
    code = "B02"
    low, high = (float(x) for x in ctx.code_spec(code)["shortfall_pct"])
    rng = ctx.rng(code)
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "AND grade BETWEEN 2 AND 18 ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        record = ctx.master(employee)
        band = ctx.pack.grade_bands[(int(record["grade"]), record["nationality_class"])]
        shortfall = low + float(rng.random()) * (high - low)
        target = int(int(band.salary_min * 100) * (1 - shortfall / 100))
        if target >= record["base_salary"] or target <= 0:
            return False
        # Cutting the salary raises allowances as a share of it, and a share
        # over the ceiling is B03. Checked against the allowance load as it
        # stands, which is the conservative reading: the percentage allowances
        # will fall with the salary, the flat ones will not.
        factor = target / record["base_salary"]
        ceiling = float(ctx.guards["max_allowance_ratio"])
        for period in ctx.paid_periods(employee, (ctx.cfg.period_from, ctx.cfg.period_to)):
            paid = ctx.payroll_row(employee, period)
            if paid["allowance_total"] > ceiling * paid["base_pay"] * factor:
                return False
        was = record["base_salary"]
        rescale_salary(ctx, employee, target)
        ctx.label(
            employee, code, (ctx.cfg.period_from, ctx.cfg.period_to), target - was,
            f"Base salary of {sar(target)} a month is {shortfall:.0f}% below the "
            f"grade {record['grade']} minimum of "
            f"{sar(int(band.salary_min * 100))} -- the employee is underpaid.",
            base_salary=target / 100, band_min=float(band.salary_min),
            shortfall_pct=round(shortfall, 2),
            monthly_shortfall=(int(band.salary_min * 100) - target) / 100,
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


# Attribute changes that make an employee entitled to more, without breaking a
# single clause: they are housed by allowance rather than in the camp, their
# family is resident, they drive themselves. Every one of them is a legitimate
# state to be in -- which is the point of B03. The total is the symptom, and
# each allowance in it has to be reviewed on its own.
_STACK = {
    "housing_type": "allowance",
    "transport_mode": "allowance",
    "company_bus_route_id": None,
    "marital_status": "married",
    "dependents_count": 3,
    "dependents_in_kingdom": 3,
    "languages_count": 2,
    "languages": ["Arabic", "English"],
}


def _stacked_total(ctx: Context, employee: str, period: int) -> tuple[int, list]:
    """What the stacked record would be entitled to in `period`."""
    record = dict(ctx.master(employee))
    record.update(_STACK)
    interval = ctx.interval_at(employee, period)
    if interval is not None:
        record["grade"] = interval["grade"]
        record["base_salary"] = interval["base_salary"]
    base = ctx.feature_row(employee, period)
    row = ent.feature_row(record, ctx.pack.sites_by_id[
        interval["work_site_id"] if interval else record["work_site_id"]
    ], bool(base["job.safety_critical"]))
    row["service_years"] = base["service_years"]
    row["months_since_site_change"] = base["months_since_site_change"]
    row["status"] = base["status"]
    row["acting_role_flag"] = base["acting_role_flag"]
    entitlements = core.resolve(ctx.pack, row)
    rows = [
        AllowanceRow(e.code, int(e.amount * 100), e.amount_basis,
                     ent.snapshot_json(ctx.pack.allowances[e.code], row))
        for e in entitlements
    ]
    return sum(r.cents for r in rows), rows


def _stack_allowances(ctx: Context, employee: str, floor: float, ceiling: float) -> float:
    """Re-entitle an employee by changing who they are. Returns the load reached.

    Applied across the whole window rather than from a date, because an
    allowance load that steps mid-series is a change-point (D06) and this is a
    finding about the level, not about the change.
    """
    periods = ctx.paid_periods(employee, (ctx.cfg.period_from, ctx.cfg.period_to))
    if not periods:
        return 0.0
    total, _ = _stacked_total(ctx, employee, periods[-1])
    base = ctx.payroll_row(employee, periods[-1])["base_pay"]
    ratio = total / base if base else 0.0
    if not floor <= ratio <= ceiling:
        return 0.0
    for period in periods:
        _, rows = _stacked_total(ctx, employee, period)
        ctx.set_allowances(employee, period, rows)
    ctx.set_master(employee, **_STACK)
    return ratio


def b03(ctx: Context) -> None:
    """An allowance load far above what the peer group carries."""
    code = "B03"
    floor, ceiling = (float(x) for x in ctx.code_spec(code)["target_ratio"])
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "AND NOT spouse_employed_internally AND grade BETWEEN 5 AND 13 "
        "AND allowance_ratio < 0.7 ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        ratio = _stack_allowances(ctx, employee, floor, ceiling)
        if not ratio:
            return False
        window = (ctx.cfg.period_from, ctx.cfg.period_to)
        row = ctx.payroll_row(employee, ctx.paid_periods(employee, window)[-1])
        ctx.label(
            employee, code, window, row["allowance_total"],
            f"Allowances of {sar(row['allowance_total'])} a month against a base "
            f"salary of {sar(row['base_pay'])} -- {ratio * 100:.0f}% of base, "
            "far above the norm for the peer group.",
            allowance_ratio=round(ratio, 4),
            allowance_total=row["allowance_total"] / 100,
            base_pay=row["base_pay"] / 100,
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def _step_period(ctx: Context, employee: str, rng) -> int | None:
    """A period with no assignment record within a month either side of it.

    That clearance is what makes the money unexplained: a step beside a
    promotion row is a promotion, and the reviewer can see why.
    """
    periods = ctx.paid_periods(employee, (ctx.cfg.period_from, ctx.cfg.period_to))
    if len(periods) < 8:
        return None
    changes = {period_of(row["effective_from"]) for row in ctx.history(employee)}
    middle = [p for p in periods[3:-2]
              if not any(abs(period_diff(p, c)) <= 1 for c in changes)]
    if not middle:
        return None
    return middle[int(rng.integers(0, len(middle)))]


def b04(ctx: Context) -> None:
    """A salary jump with no assignment record behind it."""
    code = "B04"
    low, high = (float(x) for x in ctx.code_spec(code)["jump_pct"])
    rng = ctx.rng(code)
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        start = _step_period(ctx, employee, rng)
        if start is None:
            return False
        jump = low + float(rng.random()) * (high - low)
        periods = ctx.paid_periods(employee, (start, ctx.cfg.period_to))
        before = ctx.payroll_row(employee, periods[0])["base_pay"]
        delta = 0
        for period in periods:
            row = ctx.payroll_row(employee, period)
            raised = int(row["base_pay"] * (1 + jump / 100))
            raised -= raised % 100
            delta = raised - row["base_pay"]
            ctx.set_payroll(employee, period, base_pay=raised)
        ctx.label(
            employee, code, (periods[0], periods[-1]), delta,
            f"Pay rose {jump:.0f}% -- from {sar(before)} to "
            f"{sar(before + delta)} a month -- with no promotion, regrade or "
            "increment recorded anywhere in the assignment history.",
            jump_pct=round(jump, 2), before=before / 100,
            after=(before + delta) / 100, step_period=periods[0],
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def b05(ctx: Context) -> None:
    """Overtime beyond the legal monthly maximum, or beyond base pay itself."""
    code = "B05"
    spec = ctx.code_spec(code)
    low, high = (int(x) for x in spec["overtime_hours"])
    fewest, most = (int(x) for x in spec["periods"])
    overtime = ctx.pack.payroll["overtime"]
    multiplier = float(overtime["multiplier"])
    standard = float(overtime["standard_monthly_hours"])
    legal = int(overtime["legal_monthly_max_hours"])
    rng = ctx.rng(code)
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "AND work_pattern IN ('shift','rotation_28_28','rotation_14_14','regular') "
        "ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        window = (ctx.cfg.period_from, ctx.cfg.period_to)
        # Leave in the same period would make this D03 as well, so the periods
        # chosen are ones with no meaningful leave in them.
        usable = [p for p in ctx.paid_periods(employee, window)
                  if (ctx.attendance(employee, p) or {}).get("days_leave", 99) < 10]
        if len(usable) < most:
            return False
        count = int(rng.integers(fewest, most + 1))
        start = int(rng.integers(0, len(usable) - count + 1))
        hours = float(int(rng.integers(low, high + 1)))
        pay = 0
        for period in usable[start : start + count]:
            row = ctx.payroll_row(employee, period)
            pay = round(row["base_pay"] / standard * multiplier * hours)
            pay -= pay % 100
            ctx.set_payroll(employee, period, overtime_hours=hours, overtime_pay=pay)
            ctx.set_attendance(employee, period, overtime_hours=hours)
        chosen = usable[start : start + count]
        base = ctx.payroll_row(employee, chosen[0])["base_pay"]
        ctx.label(
            employee, code, (chosen[0], chosen[-1]), pay,
            f"{hours:.0f} overtime hours claimed in a single month -- worth "
            f"{sar(pay)} against a base salary of {sar(base)} -- where the legal "
            f"maximum is {legal} hours.",
            overtime_hours=hours, overtime_pay=pay / 100, base_pay=base / 100,
            legal_monthly_max_hours=legal, months=count,
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def b06(ctx: Context) -> None:
    """A top-decile bonus paid on three years of bottom-end ratings."""
    code = "B06"
    factor = float(ctx.code_spec(code)["bonus_factor"])
    row = ctx.con.execute(
        "SELECT quantile_cont(bonus, 0.9) FROM fact_payroll_monthly WHERE bonus > 0"
    ).fetchone()
    threshold = int(row[0].scaleb(2)) if row and row[0] is not None else 0
    pool = ctx.candidates(
        "SELECT DISTINCT employee_id FROM fact_payroll_monthly WHERE bonus > 0 "
        "ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        paid = [p for p, r in sorted(ctx.payroll(employee).items()) if r["bonus"] > 0]
        if not paid:
            return False
        period = paid[-1]
        amount = int(threshold * factor)
        amount -= amount % 100
        was = ctx.payroll_row(employee, period)["bonus"]
        ctx.set_payroll(employee, period, bonus=amount)
        ctx.set_master(employee, performance_rating_y1=1, performance_rating_y2=2,
                       performance_rating_y3=1)
        # Every bonus this employee has drawn is now inconsistent with the
        # rating record, not only the one that was inflated, so the window is
        # the whole bonus history rather than the single month.
        ctx.label(
            employee, code, (paid[0], paid[-1]), amount - was,
            f"Bonus of {sar(amount)} paid to an employee rated in the bottom two "
            f"bands for three consecutive years; the top-decile bonus starts at "
            f"{sar(threshold)}.",
            bonus=amount / 100, previous_bonus=was / 100,
            top_decile=threshold / 100, ratings=[1, 2, 1],
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def b07(ctx: Context) -> None:
    """More salary increments inside twelve months than policy allows."""
    code = "B07"
    fewest, most = (int(x) for x in ctx.code_spec(code)["increments"])
    step_pct = float(ctx.pack.raw["grade_bands.yaml"]["steps"]["increment_pct"])
    allowed = int(ctx.pack.band_policy["max_increments_per_12m"])
    rng = ctx.rng(code)
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        history = [dict(row) for row in ctx.history(employee)]
        periods = ctx.paid_periods(employee, (ctx.cfg.period_from, ctx.cfg.period_to))
        if len(periods) < 14 or not history:
            return False
        # The interval must be long enough to hold the extra increments and it
        # must be the one the observation window actually sits in.
        index = len(history) - 1
        opened = history[index]["effective_from"]
        if period_of(opened) > periods[-12]:
            return False
        count = int(rng.integers(fewest, most + 1))
        start = max(period_of(opened), periods[2])
        anchors = [p for p in periods if p >= start][:11]
        if len(anchors) < count + 1:
            return False
        spacing = max(1, len(anchors) // (count + 1))
        marks = [anchors[(i + 1) * spacing] for i in range(count)]
        salary = history[index]["base_salary"]
        pieces = []
        template = history[index]
        for position, period in enumerate([None, *marks]):
            piece = dict(template)
            piece["base_salary"] = int(
                round(salary / (1 + step_pct / 100) ** (count - position) / 100) * 100
            )
            if period is not None:
                piece["effective_from"] = period_first_day(period)
                piece["change_reason"] = "increment"
                piece["approved_by"] = template["manager_id"] or template["approved_by"]
            pieces.append(piece)
        for earlier, later in pairwise(pieces):
            earlier["effective_to"] = later["effective_from"] - timedelta(days=1)
        pieces[-1]["effective_to"] = template["effective_to"]
        if any(p["approved_by"] is None for p in pieces[1:]):
            return False
        ctx.set_history(employee, history[:index] + pieces)
        resync_base(ctx, employee)
        ctx.set_master(employee, last_increment_date=pieces[-1]["effective_from"])
        raised = pieces[-1]["base_salary"] - pieces[0]["base_salary"]
        ctx.label(
            employee, code, (marks[0], marks[-1]), raised // max(1, count),
            f"{count + 1} salary increments inside twelve months, worth "
            f"{sar(raised)} a month in total, where policy allows "
            f"{allowed} a year.",
            increments=count + 1, dates=[str(p["effective_from"]) for p in pieces[1:]],
            total_increase=raised / 100, max_increments_per_12m=allowed,
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


INJECTORS = (b01, b02, b03, b04, b05, b06, b07)
