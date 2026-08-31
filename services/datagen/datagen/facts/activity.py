"""`fact_system_activity_monthly` -- the proxy signals that make ghosts visible.

C03 is the reason this table exists: an employee paid every month with no badge
swipes, no ERP logins and no leave variance is a ghost, and without an activity
feed there is nothing to notice.  So the clean population has to be genuinely
*active*: `activity_score` is floored at `policy/population.yaml` ->
`activity.min_activity_score`, well clear of the "approximately zero for six
consecutive periods" predicate.

Remote and hybrid workers are the interesting case. They legitimately swipe a
badge almost never, so a detector keyed on badge swipes alone would flag them
every month; their ERP and VPN activity is what keeps the composite score up,
and that is exactly the confounder the score is designed to survive.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def build_period(
    period: int,
    employee_ids: list[str],
    work_pattern: np.ndarray,
    grade: np.ndarray,
    days_worked: np.ndarray,
    draws: dict[str, np.ndarray],
    settings: dict[str, Any],
) -> dict[str, Any]:
    patterns = work_pattern.astype(str)
    badge_rate = np.array(
        [float(settings["badge_swipes_per_worked_day"].get(p, 1.5)) for p in patterns]
    )
    vpn_rate = np.array(
        [float(settings["vpn_sessions_per_worked_day"].get(p, 0.3)) for p in patterns]
    )
    email_bands = sorted(
        (int(k), float(v)) for k, v in settings["emails_per_worked_day"].items()
    )
    email_rate = np.array([_band(int(g), email_bands) for g in grade])

    worked = np.asarray(days_worked, dtype=np.float64)
    jitter = 0.75 + 0.5 * draws["jitter"]

    badge = np.round(worked * badge_rate * jitter).astype(np.int64)
    email = np.round(worked * email_rate * jitter).astype(np.int64)
    erp = np.round(
        worked * float(settings["erp_logins_per_worked_day"]) * jitter
    ).astype(np.int64)
    vpn = np.round(worked * vpn_rate * jitter).astype(np.int64)

    # A normalised composite, floored so no clean employee ever looks dormant.
    reference = np.maximum(worked, 1.0)
    score = (
        0.35 * np.minimum(badge / (reference * 2.0), 1.0)
        + 0.30 * np.minimum(email / (reference * 8.0), 1.0)
        + 0.20 * np.minimum(erp / (reference * 1.5), 1.0)
        + 0.15 * np.minimum(vpn / (reference * 1.0), 1.0)
    )
    score = np.clip(score, float(settings["min_activity_score"]), 1.0)

    return {
        "employee_id": employee_ids,
        "period": [period] * len(employee_ids),
        "badge_swipes": badge.tolist(),
        "email_count": email.tolist(),
        "erp_logins": erp.tolist(),
        "vpn_sessions": vpn.tolist(),
        "activity_score": np.round(score, 4).tolist(),
    }


def _band(grade: int, bands: list[tuple[int, float]]) -> float:
    value = bands[0][1]
    for floor, rate in bands:
        if grade >= floor:
            value = rate
    return value
