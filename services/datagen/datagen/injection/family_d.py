"""Family D -- behavioural and temporal drift.

Where family A asks "was this payable?", family D asks "why did this change?".
Three of these codes work by adding allowances the employee has no claim to,
and they are deliberately drawn from `unowned_allowance_codes` in
`policy/injection.yaml` -- codes no family-A rule polices.  Money that appears
with nothing in the record to account for it is precisely the finding; if it
were paid as REMOTE_SITE it would be A01 instead, and the ground truth would
name the wrong code.

The three differ in what surrounds the step: D05 puts it just after a manager
change, D06 puts it where nothing at all happened, and D07 spreads it across a
whole section in stages small enough that no individual step is a finding.
"""

from __future__ import annotations

from datetime import timedelta

from ..config import (
    period_add,
    period_diff,
    period_first_day,
    period_of,
)
from .common import fill
from .context import Context, sar


def unowned_stack(
    ctx: Context, employee: str, period: int, target: int,
    maximum: int | None = None, best_effort: bool = False,
) -> list[tuple[str, int]]:
    """Allowance codes totalling at least `target`, none of them already paid.

    `maximum` is a ceiling the stack must not cross, and it is what keeps D05's
    step from also being D06's change-point: the codes come in fixed sizes, so
    without it the last one added can overshoot the target by a fifth.

    With `best_effort`, returns whatever the employee has room for instead of
    nothing -- D07 needs a section to move together, and a single member who
    already draws half the list must not veto the whole section.
    """
    held = {row.code for row in ctx.allowances(employee, period)}
    chosen: list[tuple[str, int]] = []
    running = 0
    for code in ctx.spec["unowned_allowance_codes"]:
        if code in held or running >= target:
            continue
        amount = ctx.policy_amount(code, employee, period)
        if amount <= 0 or (maximum is not None and running + amount > maximum):
            continue
        chosen.append((code, amount))
        running += amount
    if running >= target or best_effort:
        return chosen
    return []


def d01(ctx: Context) -> None:
    """Three grades in twenty-four months -- a promotion velocity outlier."""
    code = "D01"
    low, high = (int(x) for x in ctx.code_spec(code)["grade_drop"])
    allowed = int(ctx.pack.band_policy["max_grade_jump_per_24m"])
    rng = ctx.rng(code)
    start = ctx.cfg.window_start
    pool = ctx.candidates(
        "SELECT employee_id FROM fact_assignment_history GROUP BY 1 "
        "HAVING count(*) >= 3 ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        history = [dict(row) for row in ctx.history(employee)]
        earlier = [i for i, row in enumerate(history)
                   if row["effective_to"] is not None and row["effective_to"] < start]
        if not earlier or earlier[-1] + 1 >= len(history):
            return False
        index = earlier[-1]
        later = history[index + 1]
        if period_diff(period_of(later["effective_from"]),
                       period_of(history[index]["effective_from"])) > 24:
            return False
        drop = int(rng.integers(low, high + 1))
        floor = ctx.jobs[history[index]["job_code"]].min_grade
        if history[index]["grade"] - drop < floor:
            return False
        # Everything before the window drops together, so the record reads as a
        # career that climbed fast rather than as a demotion followed by a jump.
        for position in range(index + 1):
            row = history[position]
            row["grade"] = max(ctx.jobs[row["job_code"]].min_grade, row["grade"] - drop)
        ctx.set_history(employee, history)
        window = (max(ctx.cfg.period_from, period_of(later["effective_from"])),
                  ctx.cfg.period_to)
        ctx.label(
            employee, code, window, 0,
            f"Grade rose from {history[index]['grade']} to {later['grade']} in "
            f"under two years -- {later['grade'] - history[index]['grade']} "
            f"grades, where {allowed} is the expected maximum.",
            from_grade=history[index]["grade"], to_grade=later["grade"],
            grades_gained=later["grade"] - history[index]["grade"],
            max_grade_jump_per_24m=allowed,
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def d02(ctx: Context) -> None:
    """A stream of retroactive adjustments to one employee."""
    code = "D02"
    spec = ctx.code_spec(code)
    fewest, most = (int(x) for x in spec["entries"])
    low, high = (float(x) for x in spec["pct_of_base"])
    rng = ctx.rng(code)
    pool = ctx.candidates(
        "SELECT e.employee_id FROM employee_master e LEFT JOIN "
        "(SELECT employee_id, count(*) AS n FROM fact_payroll_monthly "
        "WHERE retro_adjustment > 0 GROUP BY 1) r USING (employee_id) "
        "WHERE e.status = 'active' AND coalesce(r.n, 0) = 0 ORDER BY e.employee_id"
    )

    def attempt(employee: str) -> bool:
        periods = ctx.paid_periods(employee, (ctx.cfg.period_from, ctx.cfg.period_to))
        count = int(rng.integers(fewest, most + 1))
        if len(periods) < count * 3:
            return False
        spacing = len(periods) // count
        chosen = [periods[i * spacing] for i in range(count)]
        total = 0
        for period in chosen:
            row = ctx.payroll_row(employee, period)
            share = low + float(rng.random()) * (high - low)
            amount = int(row["base_pay"] * share)
            amount -= amount % 100
            total += amount
            ctx.set_payroll(employee, period, retro_adjustment=amount)
        ctx.label(
            employee, code, (chosen[0], chosen[-1]), total // count,
            f"{count} backdated pay corrections in {len(periods)} months, worth "
            f"{sar(total)} in total, none of them tied to a change in the "
            "assignment record.",
            entries=count, periods=chosen, total_adjustment=total / 100,
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def d03(ctx: Context) -> None:
    """Leave and overtime claimed for the same month."""
    code = "D03"
    spec = ctx.code_spec(code)
    leave_low, leave_high = (int(x) for x in spec["leave_days"])
    hours_low, hours_high = (int(x) for x in spec["overtime_hours"])
    overtime = ctx.pack.payroll["overtime"]
    multiplier = float(overtime["multiplier"])
    standard = float(overtime["standard_monthly_hours"])
    calendar = dict(ctx.con.execute(
        "SELECT period, calendar_days FROM dim_calendar").fetchall())
    rng = ctx.rng(code)
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "AND work_pattern IN ('shift','rotation_28_28','rotation_14_14','regular') "
        "ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        periods = ctx.paid_periods(employee, (ctx.cfg.period_from, ctx.cfg.period_to))
        usable = [p for p in periods if ctx.attendance(employee, p) is not None]
        if not usable:
            return False
        period = usable[int(rng.integers(0, len(usable)))]
        attendance = ctx.attendance(employee, period)
        days = int(rng.integers(leave_low, leave_high + 1))
        hours = float(int(rng.integers(hours_low, hours_high + 1)))
        worked = int(calendar[period]) - days - int(attendance["absence_days"])
        if worked < 1:
            return False
        ctx.set_attendance(employee, period, days_leave=days, days_worked=worked,
                           overtime_hours=hours,
                           leave_type_breakdown=[("annual", days)])
        row = ctx.payroll_row(employee, period)
        pay = round(row["base_pay"] / standard * multiplier * hours)
        pay -= pay % 100
        ctx.set_payroll(employee, period, overtime_hours=hours, overtime_pay=pay)
        ctx.label(
            employee, code, (period, period), pay,
            f"{days} days of leave and {hours:.0f} hours of overtime claimed for "
            f"the same month, worth {sar(pay)}.",
            days_leave=days, overtime_hours=hours, overtime_pay=pay / 100,
            period=period,
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def d04(ctx: Context) -> None:
    """More days accounted for in a month than the month actually has."""
    code = "D04"
    low, high = (int(x) for x in ctx.code_spec(code)["excess_days"])
    calendar = dict(ctx.con.execute(
        "SELECT period, calendar_days FROM dim_calendar").fetchall())
    rng = ctx.rng(code)
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        periods = [p for p in ctx.paid_periods(
            employee, (ctx.cfg.period_from, ctx.cfg.period_to))
            if ctx.attendance(employee, p) is not None]
        if not periods:
            return False
        period = periods[int(rng.integers(0, len(periods)))]
        attendance = ctx.attendance(employee, period)
        days = int(calendar[period])
        excess = int(rng.integers(low, high + 1))
        # Leave and absence stay as they were; the working days are what the
        # timesheet inflated, which is what the source system would show.
        worked = days - int(attendance["days_leave"]) - int(attendance["absence_days"]) + excess
        if worked <= int(attendance["days_worked"]):
            return False
        ctx.set_attendance(employee, period, days_worked=worked)
        ctx.label(
            employee, code, (period, period), 0,
            f"{worked} days worked, {attendance['days_leave']} on leave and "
            f"{attendance['absence_days']} absent recorded for a month with only "
            f"{days} days in it.",
            days_worked=worked, days_leave=int(attendance["days_leave"]),
            absence_days=int(attendance["absence_days"]),
            calendar_days=days, excess_days=excess, period=period,
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def d05(ctx: Context) -> None:
    """Allowances appearing right after a new manager takes over.

    The manager change is injected too. Pass 1's in-window manager changes all
    come with a promotion -- transfers cluster early in a career -- and D05
    deliberately ignores those, because crossing a grade band adds allowances by
    policy. So this plants the case the catalogue describes: the same grade, the
    same post, a different manager, and allowances that follow.
    """
    code = "D05"
    low, high = (float(x) for x in ctx.code_spec(code)["step_ratio_of_base"])
    rng = ctx.rng(code)
    # The manager's own manager: senior enough by construction, and it cannot
    # close a cycle, because they are already above the employee in the chain.
    pool = ctx.candidates(
        "SELECT e.employee_id FROM employee_master e "
        "JOIN employee_master m ON m.employee_id = e.manager_id "
        "JOIN employee_master g ON g.employee_id = m.manager_id "
        "WHERE e.status = 'active' AND e.allowance_ratio < 0.55 "
        "AND g.employee_id <> e.employee_id ORDER BY e.employee_id"
    )
    successor = dict(ctx.con.execute(
        "SELECT e.employee_id, m.manager_id FROM employee_master e "
        "JOIN employee_master m ON m.employee_id = e.manager_id "
        "WHERE m.manager_id IS NOT NULL"
    ).fetchall())

    def attempt(employee: str) -> bool:
        periods = ctx.paid_periods(employee, (ctx.cfg.period_from, ctx.cfg.period_to))
        history = [dict(row) for row in ctx.history(employee)]
        if len(periods) < 8 or not history or history[-1]["effective_to"] is not None:
            return False
        middle = [p for p in periods[3:-3]
                  if period_first_day(p) > history[-1]["effective_from"]]
        if not middle:
            return False
        change = middle[int(rng.integers(0, len(middle)))]
        start = period_add(change, 1 + int(rng.integers(0, 2)))
        after = [p for p in periods if p >= start]
        if len(after) < 2:
            return False

        row = ctx.payroll_row(employee, after[0])
        standing = ctx.standing(employee, period_add(after[0], -1))
        target = int(row["base_pay"] * (low + float(rng.random()) * (high - low)))
        # Big enough to be D05's step, capped small enough that the same step is
        # not also D06's change-point.
        ceiling = int(standing * 0.23)
        target = min(target, ceiling)
        if target < int(row["base_pay"] * low):
            return False
        stack = unowned_stack(ctx, employee, after[0], target, maximum=ceiling)
        added = sum(amount for _, amount in stack)
        if len(stack) < 2 or not ctx.ratio_ok(employee, after, [a for a, _ in stack]):
            return False

        was = history[-1]["manager_id"]
        replacement = successor.get(employee)
        if replacement is None or replacement == employee:
            return False
        opened = dict(history[-1])
        opened.update(effective_from=period_first_day(change), manager_id=replacement,
                      change_reason="transfer", approved_by=replacement)
        history[-1] = dict(history[-1])
        history[-1]["effective_to"] = period_first_day(change) - timedelta(days=1)
        ctx.set_history(employee, [*history, opened])
        ctx.set_master(employee, manager_id=replacement)

        for period in after:
            for allowance, _ in stack:
                ctx.add_allowance(employee, period, allowance,
                                  ctx.policy_amount(allowance, employee, period))
        ctx.label(
            employee, code, (after[0], after[-1]), added,
            f"{len(stack)} new allowances worth {sar(added)} a month "
            f"({added / row['base_pay'] * 100:.0f}% of base pay) started within "
            "two months of a change of manager, with no promotion to explain them.",
            manager_change_period=change, previous_manager=was,
            new_manager=replacement, allowances=[a for a, _ in stack],
            monthly_amount=added / 100, grade_change=False,
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def d06(ctx: Context) -> None:
    """A step in take-home pay with nothing in the record to explain it."""
    code = "D06"
    low, high = (float(x) for x in ctx.code_spec(code)["step_ratio_of_standing"])
    rng = ctx.rng(code)
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "AND allowance_ratio < 0.35 AND grade <= 12 ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        periods = ctx.paid_periods(employee, (ctx.cfg.period_from, ctx.cfg.period_to))
        if len(periods) < 10:
            return False
        changes = ({period_of(row["effective_from"]) for row in ctx.history(employee)}
                   | ctx.manager_change_periods(employee))
        quiet = [p for p in periods[4:-3]
                 if not any(abs(period_diff(p, c)) <= 2 for c in changes)]
        if not quiet:
            return False
        start = quiet[int(rng.integers(0, len(quiet)))]
        standing = ctx.standing(employee, period_add(start, -1))
        target = int(standing * (low + float(rng.random()) * (high - low)))
        stack = unowned_stack(ctx, employee, start, target)
        added = sum(amount for _, amount in stack)
        after = [p for p in periods if p >= start]
        if not stack or not ctx.ratio_ok(employee, after, [a for a, _ in stack]):
            return False
        for period in after:
            for allowance, _ in stack:
                ctx.add_allowance(employee, period, allowance,
                                  ctx.policy_amount(allowance, employee, period))
        ctx.label(
            employee, code, (after[0], after[-1]), added,
            f"Take-home pay rose {added / standing * 100:.0f}% in "
            f"{after[0]} and stayed there, with no promotion, transfer, "
            "increment or change of manager anywhere near it.",
            step_period=after[0], monthly_step=added / 100,
            standing_before=standing / 100,
            allowances=[a for a, _ in stack],
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def d07(ctx: Context) -> None:
    """A whole section's allowance load drifting up together.

    The drift is planned member by member and then checked at the level the
    detector works at: the section's mean allowance load over its last six
    months against the same mean over its first twelve, across the employees
    present in both. Planning it any other way would leave the outcome to luck,
    because how much room a member has depends on what they already draw.
    """
    code = "D07"
    spec = ctx.code_spec(code)
    months = int(spec["drift_months"])
    low, high = (float(x) for x in spec["drift_ratio"])
    minimum = int(spec["min_members"])
    rng = ctx.rng(code)
    baseline_window = set(ctx.periods[:12])
    recent_window = set(ctx.periods[-6:])
    units = [
        row[0] for row in ctx.con.execute(
            "SELECT org_unit_id FROM employee_master GROUP BY 1 "
            f"HAVING count(*) BETWEEN {minimum} AND {minimum * 4} "
            "AND max(allowance_ratio) < 0.85 ORDER BY 1"
        ).fetchall()
    ]
    target_units = max(1, round(float(spec["rate"]) * ctx.cfg.employees))
    order = ctx.rng(f"pick:{code}").permutation(len(units))
    done = 0

    for position in order:
        if done >= target_units:
            break
        unit = units[position]
        roster = [
            row[0] for row in ctx.con.execute(
                "SELECT employee_id FROM employee_master WHERE org_unit_id = "
                f"'{unit}' ORDER BY employee_id"
            ).fetchall()
        ]
        ctx.ensure(roster)
        # Everyone the detector would put in the comparison: paid in the
        # baseline window and paid in the recent one. Anybody else is invisible
        # to D07, so lifting them would be a finding with no home.
        members: dict[str, tuple[list[int], list[int]]] = {}
        for employee in roster:
            paid = set(ctx.paid_periods(employee, (ctx.cfg.period_from, ctx.cfg.period_to)))
            base = sorted(paid & baseline_window)
            tail = sorted(paid & recent_window)
            if base and tail:
                members[employee] = (base, tail)
        if len(members) < minimum or any(m in ctx.taken for m in members):
            continue

        drift = low + float(rng.random()) * (high - low)
        plan: dict[str, tuple[list[int], list[tuple[str, int]], dict[int, int]]] = {}
        before: list[float] = []
        after: list[float] = []
        for employee, (base, tail) in members.items():
            row = ctx.payroll_row(employee, tail[-1])
            wanted = int(row["base_pay"] * (row["allowance_total"] / row["base_pay"])
                         * (drift - 1) * 1.5) if row["base_pay"] else 0
            stack = unowned_stack(ctx, employee, tail[-1], wanted, best_effort=True)
            while stack and not ctx.ratio_ok(employee, tail, [c for c, _ in stack]):
                stack.pop()
            staged = _stages(tail, stack)
            plan[employee] = (tail, stack, staged)
            for period in base:
                paid_row = ctx.payroll_row(employee, period)
                before.append(paid_row["allowance_total"] / paid_row["base_pay"])
            for period in tail:
                paid_row = ctx.payroll_row(employee, period)
                after.append((paid_row["allowance_total"] + staged[period])
                             / paid_row["base_pay"])
        baseline_mean = sum(before) / len(before)
        recent_mean = sum(after) / len(after)
        # A comfortable margin over the detector's own threshold: the section
        # has to be unmistakable, not borderline.
        if not baseline_mean or recent_mean < baseline_mean * 1.45:
            continue

        for employee, (tail, stack, staged) in plan.items():
            for allowance, _ in stack:
                begins = _begins(tail, stack, allowance)
                for period in [p for p in tail if p >= begins]:
                    ctx.add_allowance(employee, period, allowance,
                                      ctx.policy_amount(allowance, employee, period))
            added = staged[tail[-1]]
            ctx.label(
                employee, code, (tail[0], tail[-1]), added,
                f"Every member of unit {unit} picked up new allowances over the "
                f"last {months} months, lifting the section's allowance bill by "
                f"about {(recent_mean / baseline_mean - 1) * 100:.0f}% -- "
                f"{sar(added)} a month for this employee.",
                org_unit_id=unit, members=len(plan),
                drift_ratio=round(recent_mean / baseline_mean, 3),
                allowances=[a for a, _ in stack], monthly_amount=added / 100,
            )
        done += 1


def _begins(tail: list[int], stack: list[tuple[str, int]], allowance: str) -> int:
    """When one allowance in the drift starts: thirds of the way through."""
    stages = [tail[0], tail[len(tail) // 3], tail[2 * len(tail) // 3]]
    index = [c for c, _ in stack].index(allowance)
    return stages[min(index * len(stages) // max(1, len(stack)), len(stages) - 1)]


def _stages(tail: list[int], stack: list[tuple[str, int]]) -> dict[int, int]:
    """How much extra allowance is in force in each month of the drift.

    Introduced in thirds, so no single month is a personal step: the section
    moving together is the finding, not any one employee.
    """
    out: dict[int, int] = {}
    for period in tail:
        out[period] = sum(amount for allowance, amount in stack
                          if _begins(tail, stack, allowance) <= period)
    return out


INJECTORS = (d01, d02, d03, d04, d05, d06, d07)
