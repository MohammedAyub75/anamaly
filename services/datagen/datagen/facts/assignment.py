"""`fact_assignment_history` -- the paperwork behind every pay change.

This table is the reason B04 exists: a salary that moves with no row here is
money moved with no authorisation.  So the generator works the other way round
from how it might seem natural -- the career is synthesised first, and payroll
reads base pay *out of* this history rather than the history being derived from
payroll.  If a clean employee's pay changed without a row, pass 1 would have
injected an unlabelled B04.

Three spacings are load-bearing, and all three come from `policy/`:

* promotions at least `promotion_min_months_in_grade` apart, so no rolling
  24-month window ever holds more than `max_grade_jump_per_24m` grades (D01);
* increments at least `increment_min_months` apart -- 13, not 12, because a
  rolling twelve-month window would otherwise catch two of them (B07);
* a site change only from `site_change_min_grade` up, because RELOCATION is a
  flat 3,500 SAR and on a junior salary that one allowance would dominate the
  pay packet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np

from ..config import ScaleConfig, period_of

CHANGE_REASONS = (
    "hire", "promotion", "transfer", "regrade", "increment", "acting",
    "return_from_acting", "termination",
)


@dataclass
class Interval:
    """One row of `fact_assignment_history`, before ids are attached."""

    start: date
    end: date | None
    grade: int
    job_code: str
    org_unit_id: str
    site_index: int
    base_cents: int
    change_reason: str
    safety_critical: bool = False


@dataclass
class Career:
    """An employee's whole assignment history plus the state it leaves behind."""

    intervals: list[Interval] = field(default_factory=list)
    last_increment: date | None = None
    last_promotion: date | None = None
    site_change: date | None = None
    steps: int = 1

    @property
    def current(self) -> Interval:
        return self.intervals[-1]

    def as_at(self, day: date) -> Interval | None:
        """The interval covering `day`; None before the employee was hired."""
        for interval in reversed(self.intervals):
            if interval.start <= day:
                return interval
        return None


def _band_salary_cents(policy, grade: int, klass: str, position: float) -> int:
    band = policy.band(grade, klass)
    low = int(band.salary_min * 100)
    high = int(band.salary_max * 100)
    value = low + round(position * (high - low))
    return value - value % 1000  # salaries sit on a 10 SAR grid


def build_career(
    *,
    policy,
    cfg: ScaleConfig,
    hire: date,
    grade: int,
    nationality_class: str,
    job_for_grade,
    org_unit_id: str,
    site_index: int,
    min_grade: int,
    alt_site_index: int,
    band_position: float,
    draws: dict[str, float],
    terminated_on: date | None,
) -> Career:
    """Synthesise one employee's career, ending in their current state.

    `draws` carries the pre-drawn random numbers for this employee so that the
    whole function is pure: the same inputs always produce the same career, no
    matter which chunk or scale tier it is generated in.
    """
    career_policy = policy.population["career"]
    min_in_grade = int(career_policy["promotion_min_months_in_grade"])
    increment_gap = int(career_policy["increment_min_months"])
    site_change_min_grade = int(career_policy["site_change_min_grade"])

    end = terminated_on or cfg.reference_date
    service_months = max(0, (end.year - hire.year) * 12 + end.month - hire.month)

    # --- promotions ------------------------------------------------------
    # A career cannot start below the floor the posting itself implies: nobody
    # works an offshore platform at grade 2, and a start grade under the floor
    # would put the site's flat allowances against a junior band and breach the
    # allowance-load ceiling in the employee's own early periods.
    capacity = max(0, (service_months - 12) // min_in_grade)
    wanted = int(draws["promotions"] * (grade - 1) + 0.5)
    promotions = int(min(capacity, grade - min_grade, wanted))
    promotions = max(0, promotions)
    spacing = ((service_months - 12) // promotions) if promotions else 0
    promotion_months = [12 + (i + 1) * spacing for i in range(promotions)]

    # --- increments ------------------------------------------------------
    increment_months = [
        month
        for month in range(increment_gap, service_months + 1, increment_gap + 2)
        if all(abs(month - p) > 2 for p in promotion_months)
    ][: policy.band(grade, nationality_class).step_count - 1]

    # --- transfers -------------------------------------------------------
    transfers = int(draws["transfers"] * (service_months / 12) *
                    float(career_policy["transfer_annual_hazard"]) + 0.5)
    transfer_months = []
    if transfers and service_months > 24:
        step = max(18, service_months // (transfers + 1))
        transfer_months = [
            month
            for month in range(step, service_months, step)
            if all(abs(month - p) > 2 for p in promotion_months)
        ][:transfers]

    events: list[tuple[int, str]] = [(0, "hire")]
    events += [(m, "promotion") for m in promotion_months]
    events += [(m, "increment") for m in increment_months]
    events += [(m, "transfer") for m in transfer_months]
    # Sorted by (month, reason) so the order is total and never depends on the
    # order the lists happened to be concatenated in.
    events.sort(key=lambda item: (item[0], CHANGE_REASONS.index(item[1])))
    # One event per month: two intervals starting on the same day would leave
    # the earlier one ending before it began, and the gate checks contiguity.
    seen: set[int] = set()
    events = [e for e in events if not (e[0] in seen or seen.add(e[0]))]

    start_grade = max(min_grade, grade - promotions)
    total_steps = len(events) - 1
    career = Career()
    current_grade = start_grade
    current_site = site_index
    current_org = org_unit_id
    steps_in_grade = 1

    for index, (month, reason) in enumerate(events):
        if reason == "promotion":
            current_grade = min(20, current_grade + 1)
            steps_in_grade = 1
        elif reason == "increment":
            steps_in_grade += 1
        elif reason == "transfer":
            current_org = org_unit_id
            if current_grade >= site_change_min_grade:
                current_site = alt_site_index
        # Earlier intervals sit slightly lower in the band than the current
        # one, so pay rises monotonically and every rise has a row here.
        remaining = total_steps - index
        position = min(0.98, max(0.02, band_position - 0.015 * remaining))
        # The job follows the grade: a promotion is a new post, and a job code
        # whose band no longer contains the grade is exactly what A08 detects.
        job_code, safety_critical = job_for_grade(current_grade)
        career.intervals.append(
            Interval(
                start=_add_months(hire, month),
                end=None,
                grade=current_grade,
                job_code=job_code,
                org_unit_id=current_org,
                site_index=current_site,
                base_cents=_band_salary_cents(
                    policy, current_grade, nationality_class, position
                ),
                change_reason=reason,
                safety_critical=safety_critical,
            )
        )
        if reason == "promotion":
            career.last_promotion = career.intervals[-1].start
        elif reason == "increment":
            career.last_increment = career.intervals[-1].start
        elif reason == "transfer" and current_site != site_index:
            career.site_change = career.intervals[-1].start

    if terminated_on is not None:
        last = career.intervals[-1]
        if terminated_on <= last.start:
            terminated_on = last.start + timedelta(days=1)
        career.intervals.append(
            Interval(
                start=terminated_on,
                end=None,
                grade=last.grade,
                job_code=last.job_code,
                org_unit_id=last.org_unit_id,
                site_index=last.site_index,
                base_cents=last.base_cents,
                change_reason="termination",
                safety_critical=last.safety_critical,
            )
        )

    # Contiguous and non-overlapping: each interval ends the day before the
    # next one starts, and only the current interval is open.
    for earlier, later in zip(career.intervals, career.intervals[1:], strict=False):
        earlier.end = later.start - timedelta(days=1)
    career.steps = min(policy.band(grade, nationality_class).step_count, steps_in_grade)
    return career


def _add_months(day: date, months: int) -> date:
    total = day.year * 12 + (day.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    # Clamp the day so month-end hires do not roll into the following month.
    last = [31, 29 if _leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(day.day, last))


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def rows(
    cfg: ScaleConfig,
    employee_ids: np.ndarray,
    careers: list[Career],
    site_ids: list[str],
    approvers: np.ndarray,
    managers: np.ndarray,
) -> dict[str, Any]:
    """Flatten a chunk's careers into `fact_assignment_history` columns."""
    out_ids: list[str] = []
    starts: list[date] = []
    ends: list[date | None] = []
    grades: list[int] = []
    jobs: list[str] = []
    orgs: list[str] = []
    sites: list[str] = []
    manager_col: list[str | None] = []
    salaries: list[int] = []
    reasons: list[str] = []
    approved: list[str] = []

    for position, career in enumerate(careers):
        employee = employee_ids[position]
        for interval in career.intervals:
            out_ids.append(employee)
            starts.append(interval.start)
            ends.append(interval.end)
            grades.append(interval.grade)
            jobs.append(interval.job_code)
            orgs.append(interval.org_unit_id)
            sites.append(site_ids[interval.site_index])
            manager_col.append(managers[position])
            salaries.append(interval.base_cents)
            reasons.append(interval.change_reason)
            approved.append(approvers[position])

    return {
        "employee_id": out_ids,
        "effective_from": starts,
        "effective_to": ends,
        "grade": grades,
        "job_code": jobs,
        "org_unit_id": orgs,
        "work_site_id": sites,
        "manager_id": manager_col,
        "base_salary": salaries,
        "change_reason": reasons,
        "approved_by": approved,
    }


def period_state(career: Career, period: int) -> Interval | None:
    """The assignment in force at the end of `period`."""
    year, month = divmod(period, 100)
    last_day = _add_months(date(year, month, 1), 1) - timedelta(days=1)
    return career.as_at(last_day)


def first_period(career: Career) -> int:
    return period_of(career.intervals[0].start)
