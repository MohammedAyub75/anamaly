"""Family A -- entitlement and policy violations.

These are facts rather than probabilities: a clause in
`policy/allowance_rules.yaml` is broken and the exact clause can be quoted to
the reviewer.  Every injector here therefore breaks *one* clause and leaves the
rest of the record alone, and pays at the amount the policy table gives so that
A07 -- "amount outside the policy table" -- stays silent on a finding that is
about eligibility rather than arithmetic.
"""

from __future__ import annotations

from datetime import date

from policycore.packs import EDUCATION_ORDER

from ..config import period_first_day, period_of
from .common import already_paid, fill, pay, pay_resolved, site_rate
from .context import Context, sar


def _add_months(day: date, months: int) -> date:
    total = day.year * 12 + (day.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    last = [31, 29 if year % 4 == 0 and (year % 100 or not year % 400) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(day.day, last))


def _flat_allowance(
    ctx: Context, code: str, allowance: str, pool_sql: str, description: str,
    rate=None,
) -> None:
    """The shared shape of A01/A02/A03/A10: pay one allowance to someone barred.

    `rate` returns the monthly amount for a victim; the default asks the policy
    table what that employee's own record would attract.
    """
    rng = ctx.rng(code)
    pool = ctx.candidates(pool_sql)

    def attempt(employee: str) -> bool:
        window = ctx.window(code, rng)
        if already_paid(ctx, employee, allowance, window):
            return False
        amount = rate(employee, window) if rate else ctx.policy_amount(
            allowance, employee, ctx.cfg.period_to
        )
        if amount <= 0 or not ctx.guard_step(employee, window, amount):
            return False
        periods = pay(ctx, employee, allowance, window, amount)
        if not periods:
            return False
        site = ctx.pack.sites_by_id[ctx.master(employee)["work_site_id"]]
        ctx.label(
            employee, code, (periods[0], periods[-1]), amount,
            description.format(amount=sar(amount), site=site.name_en,
                               site_class=site.site_class, tier=site.hardship_tier),
            allowance_code=allowance, monthly_amount=amount / 100,
            months_paid=len(periods), site_id=site.site_id,
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def a01(ctx: Context) -> None:
    """Remote-site allowance at a site that does not qualify for one."""
    _flat_allowance(
        ctx, "A01", "REMOTE_SITE",
        "SELECT e.employee_id FROM employee_master e JOIN dim_site s "
        "ON s.site_id = e.work_site_id WHERE e.status = 'active' "
        "AND NOT e.has_REMOTE_SITE AND (NOT s.remote_allowance_eligible "
        "OR s.site_class IN ('hq','office','medical','training')) "
        "ORDER BY e.employee_id",
        "Remote-site allowance of {amount} a month paid at {site}, which is not "
        "a remote posting.",
        rate=lambda e, w: site_rate(
            ctx, "REMOTE_SITE", ctx.pack.sites_by_id[ctx.master(e)["work_site_id"]]
        ),
    )


def a02(ctx: Context) -> None:
    """Hardship allowance at a site classified as no-hardship."""
    _flat_allowance(
        ctx, "A02", "HARDSHIP",
        "SELECT e.employee_id FROM employee_master e JOIN dim_site s "
        "ON s.site_id = e.work_site_id WHERE e.status = 'active' "
        "AND NOT e.has_HARDSHIP AND s.hardship_tier = 0 ORDER BY e.employee_id",
        "Hardship allowance of {amount} a month paid at {site}, a site carrying "
        "no hardship classification.",
        rate=lambda e, w: site_rate(
            ctx, "HARDSHIP", ctx.pack.sites_by_id[ctx.master(e)["work_site_id"]]
        ),
    )


def a03(ctx: Context) -> None:
    """Offshore allowance for an onshore assignment."""
    _flat_allowance(
        ctx, "A03", "OFFSHORE",
        "SELECT e.employee_id FROM employee_master e JOIN dim_site s "
        "ON s.site_id = e.work_site_id WHERE e.status = 'active' "
        "AND NOT e.has_OFFSHORE AND s.site_class <> 'offshore' "
        "ORDER BY e.employee_id",
        "Offshore allowance of {amount} a month paid to an employee posted at "
        "{site}, which is onshore.",
    )


def a04(ctx: Context) -> None:
    """School assistance with no qualifying dependents in the Kingdom."""
    code = "A04"
    rng = ctx.rng(code)
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "AND NOT has_SCHOOL_ASSIST AND (dependents_count = 0 "
        "OR dependents_in_kingdom = 0) ORDER BY employee_id"
    )
    allowance = ctx.pack.allowances["SCHOOL_ASSIST"]

    def attempt(employee: str) -> bool:
        window = ctx.window(code, rng)
        if already_paid(ctx, employee, "SCHOOL_ASSIST", window):
            return False
        amount = int(allowance.amount * 100)  # one child's worth
        if not ctx.guard_step(employee, window, amount):
            return False
        periods = pay(ctx, employee, "SCHOOL_ASSIST", window, amount)
        if not periods:
            return False
        record = ctx.master(employee)
        ctx.label(
            employee, code, (periods[0], periods[-1]), amount,
            f"School assistance of {sar(amount)} a month paid to an employee "
            f"with {record['dependents_in_kingdom']} dependents resident in the "
            "Kingdom.",
            allowance_code="SCHOOL_ASSIST", monthly_amount=amount / 100,
            months_paid=len(periods),
            dependents_count=record["dependents_count"],
            dependents_in_kingdom=record["dependents_in_kingdom"],
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def a05(ctx: Context) -> None:
    """Housing allowance drawn while living in company accommodation."""
    code = "A05"
    rng = ctx.rng(code)
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "AND NOT has_HOUSING AND housing_type IN "
        "('company_camp_bachelor','company_family_housing') ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        window = ctx.window(code, rng)
        if already_paid(ctx, employee, "HOUSING", window):
            return False
        amount = ctx.policy_amount("HOUSING", employee, ctx.cfg.period_to)
        if amount <= 0 or not ctx.guard_step(employee, window, amount):
            return False
        periods, last = pay_resolved(ctx, employee, "HOUSING", window)
        if not periods:
            return False
        record = ctx.master(employee)
        ctx.label(
            employee, code, (periods[0], periods[-1]), last,
            f"Housing allowance of {sar(last)} a month paid while the employee "
            f"is housed by the company ({record['housing_type'].replace('_', ' ')}).",
            allowance_code="HOUSING", monthly_amount=last / 100,
            months_paid=len(periods), housing_type=record["housing_type"],
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def a06(ctx: Context) -> None:
    """Transport allowance drawn while riding a company bus route."""
    code = "A06"
    rng = ctx.rng(code)
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "AND NOT has_TRANSPORT AND transport_mode = 'company_bus' "
        "ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        window = ctx.window(code, rng)
        if already_paid(ctx, employee, "TRANSPORT", window):
            return False
        amount = ctx.policy_amount("TRANSPORT", employee, ctx.cfg.period_to)
        if amount <= 0 or not ctx.guard_step(employee, window, amount):
            return False
        periods, last = pay_resolved(ctx, employee, "TRANSPORT", window)
        if not periods:
            return False
        record = ctx.master(employee)
        ctx.label(
            employee, code, (periods[0], periods[-1]), last,
            f"Transport allowance of {sar(last)} a month paid to an employee on "
            f"company bus route {record['company_bus_route_id']}.",
            allowance_code="TRANSPORT", monthly_amount=last / 100,
            months_paid=len(periods), bus_route=record["company_bus_route_id"],
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def a07(ctx: Context) -> None:
    """An allowance the employee is entitled to, paid above the policy amount."""
    code = "A07"
    spec = ctx.code_spec(code)
    low, high = (float(x) for x in spec["amount_factor"])
    rng = ctx.rng(code)
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "AND allowance_total_monthly > 0 ORDER BY employee_id"
    )
    factors = {}

    def attempt(employee: str) -> bool:
        window = ctx.window(code, rng)
        periods = ctx.paid_periods(employee, window)
        if not periods:
            return False
        factor = factors.setdefault(employee, low + float(rng.random()) * (high - low))
        # The largest allowance whose inflation still fits under the guards --
        # a big enough overpayment to matter, small enough not to become D06.
        rows = sorted(ctx.allowances(employee, periods[0]),
                      key=lambda r: (-r.cents, r.code))
        for row in rows:
            if ctx.pack.allowances[row.code].one_off:
                continue
            delta = int(row.cents * (factor - 1))
            if delta <= 0 or not ctx.guard_step(employee, window, delta):
                continue
            paid = 0
            for period in periods:
                current = next((r for r in ctx.allowances(employee, period)
                                if r.code == row.code), None)
                if current is None:
                    continue
                paid = int(current.cents * factor)
                ctx.add_allowance(employee, period, row.code, paid,
                                  snapshot=current.snapshot)
            expected = ctx.policy_amount(row.code, employee, periods[-1])
            ctx.label(
                employee, code, (periods[0], periods[-1]), paid - expected,
                f"{ctx.pack.allowances[row.code].name_en} paid at {sar(paid)} a "
                f"month where the policy amount is {sar(expected)}.",
                allowance_code=row.code, factor=round(factor, 3),
                paid_amount=paid / 100, expected_amount=expected / 100,
                months_paid=len(periods),
            )
            return True
        return False

    fill(ctx, code, pool, ctx.target(code), attempt)


def a08(ctx: Context) -> None:
    """A grade outside the permitted band for the job code held."""
    code = "A08"
    by_family: dict[str, list] = {}
    for job in ctx.jobs.values():
        if not job.safety_critical:
            by_family.setdefault(job.job_family, []).append(job)
    for jobs in by_family.values():
        jobs.sort(key=lambda j: j.job_code)

    rng = ctx.rng(code)
    pool = ctx.candidates(
        "SELECT e.employee_id FROM employee_master e JOIN dim_job j "
        "USING (job_code) WHERE e.status = 'active' AND NOT j.safety_critical "
        "ORDER BY e.employee_id"
    )
    education = list(EDUCATION_ORDER)

    def attempt(employee: str) -> bool:
        record = ctx.master(employee)
        held = ctx.jobs[record["job_code"]]
        rank = education.index(record["education_level"])
        options = [
            job for job in by_family.get(held.job_family, [])
            if (record["grade"] < job.min_grade or record["grade"] > job.max_grade)
            and education.index(job.min_education) <= rank
            and len(job.required_certifications) <= record["certifications_count"]
        ]
        if not options:
            return False
        chosen = options[int(rng.integers(0, len(options)))]
        history = [dict(row) for row in ctx.history(employee)]
        if not history:
            return False
        history[-1]["job_code"] = chosen.job_code
        ctx.set_history(employee, history)
        ctx.set_master(employee, job_code=chosen.job_code,
                       job_family=chosen.job_family)
        opened = max(ctx.cfg.period_from, period_of(history[-1]["effective_from"]))
        ctx.label(
            employee, code, (opened, ctx.cfg.period_to), 0,
            f"Grade {record['grade']} held against job code "
            f"{chosen.job_code} ({chosen.job_family}), where the permitted "
            f"range is grade {chosen.min_grade} to {chosen.max_grade}.",
            job_code=chosen.job_code, grade=record["grade"],
            min_grade=chosen.min_grade, max_grade=chosen.max_grade,
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def a09(ctx: Context) -> None:
    """A nationality-restricted benefit paid to an ineligible class."""
    code = "A09"
    rng = ctx.rng(code)
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "AND grade >= 9 AND nationality_class <> 'expat' "
        "AND NOT has_EXPAT_PREMIUM ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        window = ctx.window(code, rng)
        if already_paid(ctx, employee, "EXPAT_PREMIUM", window):
            return False
        amount = ctx.policy_amount("EXPAT_PREMIUM", employee, ctx.cfg.period_to)
        if amount <= 0 or not ctx.guard_step(employee, window, amount):
            return False
        periods, last = pay_resolved(ctx, employee, "EXPAT_PREMIUM", window)
        if not periods:
            return False
        record = ctx.master(employee)
        ctx.label(
            employee, code, (periods[0], periods[-1]), last,
            f"Expatriate premium of {sar(last)} a month paid to a "
            f"{record['nationality_class']} national, who is not eligible for it.",
            allowance_code="EXPAT_PREMIUM", monthly_amount=last / 100,
            months_paid=len(periods), nationality_class=record["nationality_class"],
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def a10(ctx: Context) -> None:
    """Rotation allowance without a rotation work pattern."""
    _flat_allowance(
        ctx, "A10", "ROTATION",
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "AND NOT has_ROTATION AND work_pattern NOT IN "
        "('rotation_28_28','rotation_14_14') ORDER BY employee_id",
        "Rotation allowance of {amount} a month paid to an employee who does "
        "not work a rotation pattern.",
    )


def a11(ctx: Context) -> None:
    """A required certification expired while the safety-critical post is held."""
    code = "A11"
    low, high = (int(x) for x in ctx.code_spec(code)["expired_months"])
    rng = ctx.rng(code)
    pool = ctx.candidates(
        "SELECT e.employee_id FROM employee_master e JOIN dim_job j "
        "USING (job_code) WHERE e.status = 'active' AND j.safety_critical "
        "AND e.certifications_count > 0 ORDER BY e.employee_id"
    )

    def attempt(employee: str) -> bool:
        record = ctx.master(employee)
        certifications = [dict(c) for c in (record["certifications"] or [])]
        if not certifications:
            return False
        months = int(rng.integers(low, high + 1))
        expiry = _add_months(ctx.cfg.reference_date, -months)
        certifications[0]["expiry"] = expiry
        ctx.set_master(employee, certifications=certifications,
                       has_valid_required_certifications=False)
        # The certification premium is no longer earned, so it stops for every
        # period rather than mid-series: an expiry is not a change-point.
        for period in ctx.paid_periods(employee, (ctx.cfg.period_from, ctx.cfg.period_to)):
            rows = [r for r in ctx.allowances(employee, period) if r.code != "CERT_PREMIUM"]
            if len(rows) != len(ctx.allowances(employee, period)):
                ctx.set_allowances(employee, period, rows)
        job = ctx.jobs[record["job_code"]]
        ctx.label(
            employee, code, (ctx.cfg.period_from, ctx.cfg.period_to), 0,
            f"Certification {certifications[0]['code']} expired on "
            f"{expiry.isoformat()} while the employee remains in a "
            f"safety-critical post ({job.job_family}).",
            certification=certifications[0]["code"], expiry=expiry.isoformat(),
            months_expired=months, safety_critical=True,
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def a12(ctx: Context) -> None:
    """An acting-role allowance running past the permitted duration."""
    code = "A12"
    low, high = (int(x) for x in ctx.code_spec(code)["overrun_months"])
    maximum = int(ctx.pack.allowances["ACTING_ROLE"].max_consecutive_months)
    rng = ctx.rng(code)
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "AND acting_role_flag AND has_ACTING_ROLE ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        window = (ctx.cfg.period_from, ctx.cfg.period_to)
        periods = ctx.paid_periods(employee, window)
        if not periods:
            return False
        months = int(rng.integers(low, high + 1))
        since = _add_months(period_first_day(periods[0]), -months)
        amount = ctx.policy_amount("ACTING_ROLE", employee, periods[0])
        # Backdating means the allowance is payable across the whole window, so
        # it is paid across the whole window: a gap would be a change-point.
        row = ctx.payroll_row(employee, periods[0])
        if amount <= 0 or row["allowance_total"] + amount > float(
            ctx.guards["max_allowance_ratio"]
        ) * row["base_pay"]:
            return False
        ctx.set_master(employee, acting_role_since=since)
        for period in periods:
            ctx.add_allowance(employee, period, "ACTING_ROLE",
                              ctx.policy_amount("ACTING_ROLE", employee, period))
        elapsed = months + len(periods) - 1
        ctx.label(
            employee, code, (periods[0], periods[-1]), amount,
            f"Acting-role allowance of {sar(amount)} a month still running "
            f"{elapsed} months after the acting assignment began, against a "
            f"policy maximum of {maximum} months.",
            allowance_code="ACTING_ROLE", acting_role_since=since.isoformat(),
            months_elapsed=elapsed, policy_max_months=maximum,
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


INJECTORS = (a01, a02, a03, a04, a05, a06, a07, a08, a09, a10, a11, a12)
