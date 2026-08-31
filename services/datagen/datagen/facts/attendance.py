"""`fact_attendance_monthly` -- days worked, days on leave, overtime.

Generated before payroll, not after, because payroll deducts unauthorised
absence and pays the overtime recorded here.  Two invariants are load-bearing:

* `days_worked + days_leave + absence_days` never exceeds `calendar_days` --
  that sum is precisely what D04 tests, and a clean row that breached it would
  be an unlabelled anomaly;
* a month with `long_leave_min_days` or more of leave draws no overtime at all,
  because leave and overtime in the same period is D03.
"""

from __future__ import annotations

from typing import Any

import numpy as np

LEAVE_TYPES = ("annual", "sick", "hajj", "unpaid", "emergency")

ROTATION_DAYS = {"rotation_28_28": 15, "rotation_14_14": 15}


def build_period(
    period: int,
    employee_ids: list[str],
    work_pattern: np.ndarray,
    calendar_days: int,
    working_days: int,
    draws: dict[str, np.ndarray],
    settings: dict[str, Any],
    overtime_settings: dict[str, Any],
) -> dict[str, Any]:
    count = len(employee_ids)
    long_leave = int(settings["long_leave_min_days"])
    max_absence = int(settings["max_absence_days"])
    absence_rate = float(settings["absence_rate"])
    sick_share = float(settings["sick_share"])
    annual_days = int(settings["annual_leave_days_per_year"])

    # Leave arrives in blocks, not evenly: an employee takes a fortnight, not
    # 2.5 days every month. The monthly probability is set so the yearly total
    # lands near policy.
    leave_block_chance = annual_days / (12 * 9.0)
    takes_leave = draws["leave"] < leave_block_chance
    leave_days = np.where(
        takes_leave, 1 + (draws["leave_days"] * 20).astype(np.int64), 0
    ).astype(np.int64)

    absent = draws["absence"] < absence_rate
    absence_days = np.where(
        absent, 1 + (draws["absence_days"] * max_absence).astype(np.int64), 0
    ).astype(np.int64)

    rotation = np.array(
        [ROTATION_DAYS.get(str(p), 0) for p in work_pattern], dtype=np.int64
    )
    is_rotation = rotation > 0
    base_worked = np.where(is_rotation, rotation, working_days)
    days_worked = np.clip(base_worked - leave_days - absence_days, 0, calendar_days)
    # The physical ceiling, enforced rather than hoped for.
    overflow = np.clip(days_worked + leave_days + absence_days - calendar_days, 0, None)
    days_worked = np.clip(days_worked - overflow, 0, calendar_days)

    eligible = np.isin(work_pattern.astype(str), overtime_settings["eligible_work_patterns"])
    cap = float(overtime_settings["clean_population_max_hours"])
    overtime = np.where(
        eligible & (leave_days < long_leave) & (days_worked > 0),
        np.round(draws["overtime"] * cap, 1),
        0.0,
    )
    # Office staff on a regular pattern rarely claim anything at all.
    overtime = np.where(draws["overtime_gate"] < 0.35, overtime, 0.0)

    breakdown: list[dict[str, int] | None] = []
    for index in range(count):
        total = int(leave_days[index])
        if total == 0:
            breakdown.append({})
            continue
        kind = "sick" if draws["leave_type"][index] < sick_share else "annual"
        if draws["leave_type"][index] > 0.97:
            kind = "hajj"
        breakdown.append({kind: total})

    cycle = [
        f"RC{period}{int(r):02d}" if r else None
        for r in rotation
    ]

    return {
        "employee_id": employee_ids,
        "period": [period] * count,
        "days_worked": days_worked.tolist(),
        "days_leave": leave_days.tolist(),
        "leave_type_breakdown": breakdown,
        "overtime_hours": overtime.tolist(),
        "absence_days": absence_days.tolist(),
        "rotation_cycle_id": cycle,
    }
