"""Family C -- identity and payroll fraud.

Rare, high severity, and mostly about two records that should not be related
being related: one bank account behind two people, one identity behind two
records, a leaver who never left the payroll.

These injectors reuse existing employees rather than manufacturing new ones.  A
manufactured record would need a career, a payroll series and an activity
history invented for it in pass 2, which is a second generator; taking two
people who already have all of that and making them share an account is both
less code and a truer picture, because both pay streams are real.
"""

from __future__ import annotations

from ..config import period_add, period_first_day, period_of
from .common import fill
from .context import Context, sar

# Which bank-account rows a ring rewrites: all of them, so the sharing is
# visible over time and not only on today's row.
_RING_REASON = "employee_request"


def _surname(record: dict) -> str:
    parts = (record["name_en_normalised"] or "").split()
    return parts[-1] if parts else ""


def _rings(ctx: Context, code: str, pool: list[str], size_range: tuple[int, int],
           join) -> None:
    """Group unrelated employees into rings and let `join` fuse each one."""
    low, high = size_range
    rng = ctx.rng(code)
    target = ctx.target(code)
    ordered = sorted(pool)
    order = ctx.rng(f"pick:{code}").permutation(len(ordered))
    queue = [ordered[i] for i in order]
    made = 0
    index = 0
    while made < target and index < len(queue):
        batch = [e for e in queue[index : index + 60] if e not in ctx.taken]
        index += 60
        if len(batch) < high:
            continue
        ctx.ensure(batch)
        ring: list[str] = []
        want = int(rng.integers(low, high + 1))
        for employee in batch:
            if employee in ctx.taken:
                continue
            record = ctx.master(employee)
            if any(record["org_unit_id"] == ctx.master(m)["org_unit_id"]
                   or record["dob"] == ctx.master(m)["dob"]
                   or _surname(record) == _surname(ctx.master(m)) for m in ring):
                continue
            ring.append(employee)
            if len(ring) == want:
                if join(ring):
                    made += len(ring)
                ring = []
                want = int(rng.integers(low, high + 1))
                if made >= target:
                    break


def c01(ctx: Context) -> None:
    """One bank account collecting the pay of several unrelated employees."""
    code = "C01"
    low, high = (int(x) for x in ctx.code_spec(code)["ring_size"])
    window = (ctx.cfg.period_from, ctx.cfg.period_to)

    def join(ring: list[str]) -> bool:
        leader = ctx.master(ring[0])
        iban, bank = leader["iban"], leader["bank_code"]
        total = 0
        for employee in ring:
            rows = [dict(r) for r in ctx.bank(employee)]
            if not rows:
                return False
            for row in rows:
                row["iban"] = iban
                row["bank_code"] = bank
                row["is_known_benign_share"] = False
            rows[-1]["change_reason"] = _RING_REASON if employee != ring[0] else \
                rows[-1]["change_reason"]
            ctx.set_bank(employee, rows)
            ctx.set_master(employee, iban=iban, bank_code=bank)
            paid = ctx.paid_periods(employee, window)
            total += ctx.payroll_row(employee, paid[-1])["net"] if paid else 0
        for employee in ring:
            ctx.label(
                employee, code, window, total // len(ring),
                f"{len(ring)} employees in different departments are paid into "
                f"the same bank account, together drawing {sar(total)} a month.",
                iban_masked="****" + iban[-4:], ring=list(ring),
                ring_size=len(ring), monthly_disbursement=total / 100,
            )
        return True

    _rings(ctx, code,
           ctx.candidates(
               "SELECT employee_id FROM employee_master WHERE status = 'active' "
               "AND spouse_employee_id IS NULL ORDER BY employee_id"),
           (low, high), join)


def c02(ctx: Context) -> None:
    """One national ID or iqama number behind two employee records."""
    code = "C02"
    low, high = (int(x) for x in ctx.code_spec(code)["ring_size"])
    window = (ctx.cfg.period_from, ctx.cfg.period_to)

    def join(ring: list[str]) -> bool:
        leader = ctx.master(ring[0])
        klass = leader["nationality_class"]
        if any(ctx.master(e)["nationality_class"] != klass for e in ring):
            return False
        column = "national_id" if klass == "saudi" else "iqama_no"
        value = leader[column]
        if not value:
            return False
        for employee in ring[1:]:
            ctx.set_master(employee, **{column: value})
        for employee in ring:
            ctx.label(
                employee, code, window, 0,
                f"The same {'national ID' if klass == 'saudi' else 'iqama number'} "
                f"appears on {len(ring)} separate employee records, each drawing "
                "its own salary.",
                identifier_masked="****" + value[-4:], field=column,
                ring=list(ring), ring_size=len(ring),
            )
        return True

    _rings(ctx, code,
           ctx.candidates(
               "SELECT employee_id FROM employee_master WHERE status = 'active' "
               "ORDER BY employee_id"),
           (low, high), join)


def c03(ctx: Context) -> None:
    """A ghost employee: paid every month, present nowhere."""
    code = "C03"
    window = (ctx.cfg.period_from, ctx.cfg.period_to)
    # Nothing has happened to this employee since the window opened: no
    # transfer, no promotion, no increment. A ghost has no assignment history
    # because there is nobody there to have one.
    pool = ctx.candidates(
        "SELECT e.employee_id FROM employee_master e WHERE e.status = 'active' "
        "AND NOT e.payroll_hold_flag AND NOT EXISTS (SELECT 1 FROM "
        "fact_assignment_history h WHERE h.employee_id = e.employee_id "
        f"AND h.effective_from >= DATE '{ctx.cfg.window_start}') "
        "ORDER BY e.employee_id"
    )

    def attempt(employee: str) -> bool:
        periods = ctx.paid_periods(employee, window)
        if len(periods) < 12:
            return False
        for period in periods:
            if ctx.activity(employee, period) is None:
                continue
            ctx.set_activity(employee, period, badge_swipes=0, email_count=0,
                             erp_logins=0, vpn_sessions=0, activity_score=0.0)
            if ctx.attendance(employee, period) is not None:
                ctx.set_attendance(employee, period, days_leave=0, absence_days=0,
                                   leave_type_breakdown=[])
        total = sum(ctx.payroll_row(employee, p)["net"] for p in periods)
        ctx.label(
            employee, code, (periods[0], periods[-1]),
            total // len(periods),
            f"Paid for {len(periods)} consecutive months with no badge entry, no "
            f"system login and no leave ever taken -- {sar(total)} in total.",
            months_paid=len(periods), cumulative_paid=total / 100,
            badge_swipes=0, erp_logins=0,
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def c04(ctx: Context) -> None:
    """A leaver still on the payroll after the final settlement."""
    code = "C04"
    low, high = (int(x) for x in ctx.code_spec(code)["months_past"])
    rng = ctx.rng(code)
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE termination_date IS NOT NULL "
        "AND (year(termination_date) * 100 + month(termination_date)) <= "
        f"{period_add(ctx.cfg.period_to, -3)} ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        record = ctx.master(employee)
        rows = ctx.payroll(employee)
        working = [p for p in sorted(rows) if rows[p]["base_pay"] > 0]
        if not working:
            return False
        last = working[-1]
        months = int(rng.integers(low, high + 1))
        # From the month after the last one already paid, whatever that was: a
        # leaver with under two years' service gets no settlement month, and
        # starting from a fixed offset would leave a hole in the payroll series.
        first = period_add(max(rows), 1)
        periods = [period_add(first, i) for i in range(months)]
        periods = [p for p in periods if p <= ctx.cfg.period_to and p not in rows]
        if not periods:
            return False
        template = rows[last]
        source = ctx.allowances(employee, last)
        for period in periods:
            fresh = dict(template)
            fresh.update(period=period, overtime_hours=0.0, overtime_pay=0,
                         bonus=0, retro_adjustment=0, absence_days=0,
                         absence_deduction=0, payroll_run_id=f"PR{period}-01",
                         paid_flag=True)
            ctx.add_payroll_row(fresh)
            ctx.set_allowances(employee, period, [r for r in source])
        total = template["net"] * len(periods)
        ctx.label(
            employee, code, (periods[0], periods[-1]), template["net"],
            f"Salary paid for {len(periods)} months after the employee left on "
            f"{record['termination_date'].isoformat()} and their final "
            f"settlement was made -- {sar(total)} overpaid.",
            termination_date=record["termination_date"].isoformat(),
            termination_reason=record["termination_reason"],
            months_paid=len(periods), cumulative_overpayment=total / 100,
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def c05(ctx: Context) -> None:
    """An assignment approved by the person it benefits."""
    code = "C05"
    pool = ctx.candidates(
        "SELECT DISTINCT employee_id FROM fact_assignment_history "
        "WHERE change_reason <> 'hire' AND approved_by IS NOT NULL "
        "ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        history = [dict(row) for row in ctx.history(employee)]
        recent = [i for i, row in enumerate(history)
                  if row["change_reason"] != "hire"
                  and period_of(row["effective_from"]) >= ctx.cfg.period_from]
        index = recent[-1] if recent else (len(history) - 1 if len(history) > 1 else None)
        if index is None or history[index]["change_reason"] == "hire":
            return False
        history[index]["approved_by"] = employee
        ctx.set_history(employee, history)
        period = max(ctx.cfg.period_from, period_of(history[index]["effective_from"]))
        ctx.label(
            employee, code, (period, ctx.cfg.period_to),
            history[index]["base_salary"],
            f"The employee approved their own {history[index]['change_reason']} "
            f"on {history[index]['effective_from'].isoformat()}, moving their "
            f"salary to {sar(history[index]['base_salary'])} a month.",
            change_reason=history[index]["change_reason"],
            effective_from=history[index]["effective_from"].isoformat(),
            approved_by=employee,
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt, batch=60)


def _fuzz(name: str, rng) -> str:
    """A near-miss spelling: two adjacent letters swapped inside a word."""
    letters = list(name)
    positions = [i for i in range(1, len(letters) - 1)
                 if letters[i].isalpha() and letters[i + 1].isalpha()
                 and letters[i] != letters[i + 1]]
    if not positions:
        return name + "H"
    at = positions[int(rng.integers(0, len(positions)))]
    letters[at], letters[at + 1] = letters[at + 1], letters[at]
    return "".join(letters)


def c06(ctx: Context) -> None:
    """The same person on the payroll twice, under a near-miss spelling."""
    code = "C06"
    window = (ctx.cfg.period_from, ctx.cfg.period_to)
    rng = ctx.rng(code)

    def join(ring: list[str]) -> bool:
        leader, twin = ctx.master(ring[0]), ctx.master(ring[1])
        if leader["nationality_class"] != twin["nationality_class"]:
            return False
        fuzzed = _fuzz(leader["name_en_normalised"], rng)
        if fuzzed == leader["name_en_normalised"]:
            return False
        rows = [dict(r) for r in ctx.bank(ring[1])]
        if not rows:
            return False
        for row in rows:
            row["iban"] = leader["iban"]
            row["bank_code"] = leader["bank_code"]
            row["is_known_benign_share"] = False
        ctx.set_bank(ring[1], rows)
        ctx.set_master(ring[1], dob=leader["dob"], name_en=fuzzed.title(),
                       name_en_normalised=fuzzed, iban=leader["iban"],
                       bank_code=leader["bank_code"])
        for employee in ring:
            other = ring[1] if employee == ring[0] else ring[0]
            paid = ctx.paid_periods(employee, window)
            impact = ctx.payroll_row(employee, paid[-1])["net"] if paid else 0
            ctx.label(
                employee, code, window, impact,
                f"Two employee records share a date of birth and a bank account "
                f"and differ by two letters in the name -- {leader['name_en']} "
                f"and {fuzzed.title()} -- and both are being paid.",
                paired_with=other, name_en=leader["name_en"],
                near_duplicate_name=fuzzed.title(),
                dob=leader["dob"].isoformat(),
            )
        return True

    _rings(ctx, code,
           ctx.candidates(
               "SELECT employee_id FROM employee_master WHERE status = 'active' "
               "AND spouse_employee_id IS NULL ORDER BY employee_id"),
           (2, 2), join)


def c07(ctx: Context) -> None:
    """Payroll running on an expired residence permit."""
    code = "C07"
    low, high = (int(x) for x in ctx.code_spec(code)["expired_months"])
    rng = ctx.rng(code)
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "AND iqama_no IS NOT NULL AND iqama_expiry IS NOT NULL "
        "AND NOT payroll_hold_flag ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        months = int(rng.integers(low, high + 1))
        # The permit lapses on the first of the month, so that month is already
        # a month paid on an expired permit -- the window has to start there or
        # the first flagged period would carry no label.
        expired_from = period_add(ctx.cfg.period_to, -months)
        expiry = period_first_day(expired_from)
        periods = ctx.paid_periods(employee, (expired_from, ctx.cfg.period_to))
        if not periods:
            return False
        ctx.set_master(employee, iqama_expiry=expiry)
        row = ctx.payroll_row(employee, periods[-1])
        ctx.label(
            employee, code, (periods[0], periods[-1]), row["net"],
            f"Salary paid for {len(periods)} months after the employee's iqama "
            f"expired on {expiry.isoformat()} -- a regulatory exposure, not only "
            "a pay one.",
            iqama_expiry=expiry.isoformat(), months_paid_after_expiry=len(periods),
            nationality_class=ctx.master(employee)["nationality_class"],
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


def c08(ctx: Context) -> None:
    """Salary charged to a cost centre the employee has no assignment to."""
    code = "C08"
    rng = ctx.rng(code)
    centres = sorted(set(ctx.cost_center_by_unit.values()))
    pool = ctx.candidates(
        "SELECT employee_id FROM employee_master WHERE status = 'active' "
        "ORDER BY employee_id"
    )

    def attempt(employee: str) -> bool:
        window = ctx.window(code, rng)
        periods = ctx.paid_periods(employee, window)
        if not periods:
            return False
        own = ctx.payroll_row(employee, periods[0])["cost_center"]
        options = [c for c in centres if c != own]
        charged = options[int(rng.integers(0, len(options)))]
        total = 0
        for period in periods:
            row = ctx.payroll_row(employee, period)
            total += row["gross"]
            ctx.set_payroll(employee, period, cost_center=charged)
        unit = ctx.master(employee)["org_unit_id"]
        ctx.label(
            employee, code, (periods[0], periods[-1]), total // len(periods),
            f"Salary charged to cost centre {charged} for {len(periods)} months, "
            f"while the employee belongs to unit {unit} (cost centre {own}).",
            charged_cost_center=charged, own_cost_center=own, org_unit_id=unit,
            months=len(periods), total_charged=total / 100,
        )
        return True

    fill(ctx, code, pool, ctx.target(code), attempt)


INJECTORS = (c01, c02, c03, c04, c05, c06, c07, c08)
