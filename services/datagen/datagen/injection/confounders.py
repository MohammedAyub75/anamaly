"""Planted legitimate oddities -- `labels_confounder`, never `labels_anomaly`.

Without these the evaluation is meaningless, because anything unusual would be
a true positive.  Each one is built to sit just *below* the rule that owns its
family and well *above* the statistical norm: a senior specialist paid near the
top of the band but not over it, a mid-year jump that has its promotion record,
a shared account with a declared spouse behind it.  A detector that fires on
these is losing precision, and the eval harness is what says so.

`legit_final_settlement` plants nothing at all: pass 1 already pays exactly one
settlement month to every leaver with the service to earn it. What this adds is
the label saying which rows those are, so the harness can tell a legitimate
final payment from C04's overpayment.
"""

from __future__ import annotations

from ..config import period_diff, period_of
from .common import fill
from .context import Context, sar
from .family_b import _stack_allowances, rescale_salary, resync_base


def legit_high_earner(ctx: Context) -> None:
    """A senior specialist near the top of the band, with the record to match."""
    name = "legit_high_earner"
    spec = ctx.spec["confounders"][name]
    low, high = (float(x) for x in spec["band_position"])
    rng = ctx.rng(name)
    window = (ctx.cfg.period_from, ctx.cfg.period_to)
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "AND grade BETWEEN 9 AND 17 AND service_years >= 10 ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        record = ctx.master(employee)
        band = ctx.pack.grade_bands[(int(record["grade"]), record["nationality_class"])]
        position = low + float(rng.random()) * (high - low)
        target = int(int(band.salary_max * 100) * position)
        if target <= record["base_salary"]:
            return False
        was = record["base_salary"]
        rescale_salary(ctx, employee, target)
        ctx.confound(
            employee, name, window, target - was,
            f"Long-serving specialist paid {sar(target)} a month, near the top of "
            f"the grade {record['grade']} band but inside it, with a full "
            "assignment history behind the salary.",
            base_salary=target / 100, band_max=float(band.salary_max),
            band_position=round(position, 3),
            service_years=round(float(record["service_years"]), 1),
        )
        return True

    fill(ctx, name, pool, ctx.confounder_target(name), attempt)


def legit_salary_jump(ctx: Context) -> None:
    """A mid-year jump of the same size as B04's -- but with the promotion row."""
    name = "legit_salary_jump"
    low, high = (float(x) for x in ctx.spec["confounders"][name]["jump_pct"])
    rng = ctx.rng(name)
    pool = ctx.candidates(
        "SELECT DISTINCT employee_id FROM fact_assignment_history "
        "WHERE change_reason IN ('promotion', 'regrade') ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        record = ctx.master(employee)
        history = [dict(row) for row in ctx.history(employee)]
        moves = [i for i, row in enumerate(history)
                 if row["change_reason"] in ("promotion", "regrade")
                 and period_of(row["effective_from"]) > ctx.cfg.period_from]
        if not moves:
            return False
        index = moves[-1]
        band = ctx.pack.grade_bands[(int(history[index]["grade"]),
                                     record["nationality_class"])]
        jump = low + float(rng.random()) * (high - low)
        raised = int(history[index]["base_salary"] * (1 + jump / 100))
        if raised > int(band.salary_max * 100):
            return False
        for row in history[index:]:
            row["base_salary"] = raised
        ctx.set_history(employee, history)
        if history[-1]["effective_to"] is None:
            ctx.set_master(employee, base_salary=raised)
        resync_base(ctx, employee)
        period = period_of(history[index]["effective_from"])
        ctx.confound(
            employee, name, (period, ctx.cfg.period_to),
            raised - history[index - 1]["base_salary"] if index else 0,
            f"Salary rose {jump:.0f}% to {sar(raised)} a month on promotion to "
            f"grade {history[index]['grade']}, with the promotion recorded and "
            f"approved by {history[index]['approved_by']}.",
            jump_pct=round(jump, 2), base_salary=raised / 100,
            change_reason=history[index]["change_reason"],
            effective_from=history[index]["effective_from"].isoformat(),
        )
        return True

    fill(ctx, name, pool, ctx.confounder_target(name), attempt)


def spousal_shared_iban(ctx: Context) -> None:
    """A shared account with a declared spouse behind it -- C01's honest twin."""
    name = "spousal_shared_iban"
    window = (ctx.cfg.period_from, ctx.cfg.period_to)
    pairs = ctx.con.execute(
        "SELECT a.employee_id, b.employee_id FROM employee_master a "
        "JOIN employee_master b ON b.employee_id = a.spouse_employee_id "
        "AND a.employee_id < b.employee_id "
        "WHERE b.spouse_employee_id = a.employee_id AND a.dob <> b.dob "
        "ORDER BY a.employee_id"
    ).fetchall()
    target = ctx.confounder_target(name)
    made = 0
    for left, right in pairs:
        if made >= target:
            break
        if left in ctx.taken or right in ctx.taken:
            continue
        ctx.ensure([left, right])
        first, second = ctx.master(left), ctx.master(right)
        rows = [dict(r) for r in ctx.bank(right)]
        if not rows:
            continue
        for row in rows:
            row["iban"] = first["iban"]
            row["bank_code"] = first["bank_code"]
            # Metadata only, read by the eval harness and never by a detector.
            row["is_known_benign_share"] = True
        ctx.set_bank(right, rows)
        household = (second["name_en_normalised"] or "").split()
        family = (first["name_en_normalised"] or "").split()
        if household and family:
            household[-1] = family[-1]
        shared = " ".join(household)
        ctx.set_master(right, iban=first["iban"], bank_code=first["bank_code"],
                       name_en_normalised=shared, name_en=shared.title())
        left_rows = [dict(r) for r in ctx.bank(left)]
        for row in left_rows:
            row["is_known_benign_share"] = True
        ctx.set_bank(left, left_rows)
        for employee, partner in ((left, right), (right, left)):
            ctx.confound(
                employee, name, window, 0,
                "Married couple, both employed here, paid into the same family "
                "account -- each has the other declared as their spouse.",
                spouse_employee_id=partner,
                iban_masked="****" + first["iban"][-4:],
            )
        made += 2


def low_activity_role(ctx: Context) -> None:
    """Genuinely quiet work -- field staff, long-term sick -- but not a ghost."""
    name = "low_activity_role"
    low, high = (float(x) for x in ctx.spec["confounders"][name]["activity_score"])
    rng = ctx.rng(name)
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status IN ('active','on_leave') "
        "AND work_pattern IN ('rotation_28_28','rotation_14_14','shift') "
        "ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        periods = ctx.paid_periods(employee, (ctx.cfg.period_from, ctx.cfg.period_to))
        if len(periods) < 12:
            return False
        leave_months = 0
        for period in periods:
            if ctx.activity(employee, period) is None:
                continue
            score = low + float(rng.random()) * (high - low)
            ctx.set_activity(employee, period,
                             badge_swipes=int(2 + rng.integers(0, 4)),
                             email_count=0, erp_logins=int(rng.integers(0, 2)),
                             vpn_sessions=0, activity_score=round(score, 4))
            attendance = ctx.attendance(employee, period)
            if attendance is not None and attendance["days_leave"] > 0:
                leave_months += 1
        ctx.confound(
            employee, name, (periods[0], periods[-1]), 0,
            "Field-based role with almost no office system use, but real badge "
            f"entries and leave taken in {leave_months} of {len(periods)} months.",
            activity_score_range=[low, high], months_with_leave=leave_months,
            work_pattern=ctx.master(employee)["work_pattern"],
        )
        return True

    fill(ctx, name, pool, ctx.confounder_target(name), attempt)


def legit_final_settlement(ctx: Context) -> None:
    """The one payment after termination that is supposed to happen."""
    name = "legit_final_settlement"
    rows = ctx.con.execute(
        "SELECT a.employee_id, a.period, a.amount FROM fact_payroll_allowance a "
        "JOIN employee_master e USING (employee_id) "
        "WHERE a.allowance_code = 'SEVERANCE' AND e.termination_date IS NOT NULL "
        "ORDER BY a.employee_id"
    ).fetchall()
    target = ctx.confounder_target(name)
    chosen = [r for r in rows if r[0] not in ctx.taken][:target]
    if chosen:
        ctx.ensure([r[0] for r in chosen])
    for employee, period, amount in chosen:
        record = ctx.master(employee)
        settlement = int(amount.scaleb(2))
        ctx.confound(
            employee, name, (period, period), settlement,
            f"End-of-service settlement of {sar(settlement)} paid in the month "
            f"after the employee left on {record['termination_date'].isoformat()}, "
            "as the labour law requires.",
            settlement_period=period, amount=settlement / 100,
            termination_date=record["termination_date"].isoformat(),
        )


def legit_rotation_stack(ctx: Context) -> None:
    """An offshore rotation worker who genuinely holds six allowances."""
    name = "legit_rotation_stack"
    floor, ceiling = (float(x) for x in ctx.spec["confounders"][name]["target_ratio"])
    window = (ctx.cfg.period_from, ctx.cfg.period_to)
    pool = ctx.candidates(
        "SELECT e.employee_id FROM employee_master e JOIN dim_site s "
        "ON s.site_id = e.work_site_id WHERE e.status = 'active' "
        "AND NOT e.spouse_employed_internally AND e.grade BETWEEN 5 AND 13 "
        "AND s.hardship_tier >= 2 AND e.allowance_ratio < 0.6 ORDER BY e.employee_id"
    )

    def attempt(employee: str) -> bool:
        ratio = _stack_allowances(ctx, employee, floor, ceiling)
        if not ratio:
            return False
        periods = ctx.paid_periods(employee, window)
        row = ctx.payroll_row(employee, periods[-1])
        site = ctx.pack.sites_by_id[ctx.master(employee)["work_site_id"]]
        ctx.confound(
            employee, name, window, 0,
            f"Worker at {site.name_en} drawing {sar(row['allowance_total'])} of "
            f"allowances a month -- {ratio * 100:.0f}% of base -- every one of "
            "them earned by the posting.",
            allowance_ratio=round(ratio, 4), site_id=site.site_id,
            hardship_tier=site.hardship_tier,
            allowance_total=row["allowance_total"] / 100,
        )
        return True

    fill(ctx, name, pool, ctx.confounder_target(name), attempt)


def legit_retro_correction(ctx: Context) -> None:
    """Two backdated corrections that a recorded pay change accounts for."""
    name = "legit_retro_correction"
    entries = int(ctx.spec["confounders"][name]["entries"])
    pool = ctx.candidates(
        "SELECT e.employee_id FROM employee_master e LEFT JOIN "
        "(SELECT employee_id, count(*) AS n FROM fact_payroll_monthly "
        "WHERE retro_adjustment > 0 GROUP BY 1) r USING (employee_id) "
        "WHERE e.status = 'active' AND coalesce(r.n, 0) = 0 ORDER BY e.employee_id"
    )

    def attempt(employee: str) -> bool:
        periods = ctx.paid_periods(employee, (ctx.cfg.period_from, ctx.cfg.period_to))
        moves = [period_of(row["effective_from"]) for row in ctx.history(employee)
                 if row["change_reason"] in ("promotion", "increment", "regrade")
                 and period_of(row["effective_from"]) in periods]
        if not moves:
            return False
        anchor = moves[-1]
        chosen = [p for p in periods if 0 < period_diff(p, anchor) <= entries][:entries]
        if len(chosen) < entries:
            return False
        total = 0
        for period in chosen:
            row = ctx.payroll_row(employee, period)
            amount = int(row["base_pay"] * 0.04)
            amount -= amount % 100
            total += amount
            ctx.set_payroll(employee, period, retro_adjustment=amount)
        ctx.confound(
            employee, name, (chosen[0], chosen[-1]), total // entries,
            f"{entries} backdated corrections worth {sar(total)}, both following "
            "a recorded pay change that was applied late.",
            entries=entries, periods=chosen, anchor_period=anchor,
            total_adjustment=total / 100,
        )
        return True

    fill(ctx, name, pool, ctx.confounder_target(name), attempt)


PLANTERS = (
    legit_high_earner,
    legit_salary_jump,
    spousal_shared_iban,
    low_activity_role,
    legit_final_settlement,
    legit_rotation_stack,
    legit_retro_correction,
)
