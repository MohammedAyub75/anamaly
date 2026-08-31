"""Selection and payment helpers shared by the four injector families.

`fill` is the pattern every injector follows: take a candidate pool from SQL,
walk it in a seeded, stable random order, and *attempt* each candidate until the
target count succeeds.  Attempting rather than assigning is what lets the guards
in `Context.guard_step` veto a victim whose injection would collide with another
code, without the injector having to encode that reasoning in its selection SQL.

Candidates are loaded in batches: a pool is often thousands of employees and
loading a whole pool's payroll history to use twenty of them would cost more
than the rest of the phase.
"""

from __future__ import annotations

from collections.abc import Callable

from .context import Context


def fill(
    ctx: Context,
    code: str,
    pool: list[str],
    count: int,
    attempt: Callable[[str], bool],
    batch: int = 0,
) -> int:
    """Attempt candidates until `count` of them take. Returns how many did."""
    if count <= 0 or not pool:
        return 0
    ordered = sorted(pool)
    order = ctx.rng(f"pick:{code}").permutation(len(ordered))
    size = batch or max(24, 4 * count)
    done = 0
    for start in range(0, len(order), size):
        if done >= count:
            break
        window = [ordered[i] for i in order[start : start + size]]
        window = [e for e in window if e not in ctx.taken]
        if not window:
            continue
        ctx.ensure(window)
        for employee in window:
            if done >= count:
                break
            if employee in ctx.taken:
                continue
            if attempt(employee):
                done += 1
    return done


def pay(
    ctx: Context, employee: str, code: str, window: tuple[int, int], amount: int
) -> list[int]:
    """Pay a flat monthly amount for every paid period in the window."""
    periods = ctx.paid_periods(employee, window)
    for period in periods:
        ctx.add_allowance(employee, period, code, amount)
    return periods


def pay_resolved(
    ctx: Context, employee: str, code: str, window: tuple[int, int]
) -> tuple[list[int], int]:
    """Pay an allowance at exactly the amount the policy table gives, per period.

    Percentage allowances follow the salary held in that period, which is what
    keeps A07 -- "amount outside the policy table" -- silent on a violation that
    is about eligibility rather than about arithmetic.
    """
    periods = ctx.paid_periods(employee, window)
    last = 0
    for period in periods:
        last = ctx.policy_amount(code, employee, period)
        ctx.add_allowance(employee, period, code, last)
    return periods, last


def already_paid(ctx: Context, employee: str, code: str, window: tuple[int, int]) -> bool:
    """True when the employee already draws this allowance inside the window."""
    return any(
        any(row.code == code for row in ctx.allowances(employee, period))
        for period in ctx.paid_periods(employee, window)
    )


def site_rate(ctx: Context, code: str, site) -> int:
    """The site-table rate to pay, in minor units.

    The site's own tier when that is payable, otherwise the tier-2 rate: a
    tier-0 posting drawing a site allowance is a mis-classification, and tier 2
    is what the record would have to claim for the payment to look ordinary.
    Paying the site's own rate where one exists is what keeps A07 quiet.
    """
    table = ctx.pack.allowances[code].site_table
    own = table.get(int(site.hardship_tier), 0)
    value = own if own > 0 else table.get(2, 0)
    return int(value * 100)
