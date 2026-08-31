"""`dim_calendar` -- 24 periods with Hijri mapping, holidays and Ramadan windows.

Attendance and overtime are only plausible against a real Saudi working
calendar: the weekend is Friday and Saturday, Ramadan carries reduced statutory
hours, and the two Eids move through the Gregorian year.  `calendar_days` is
also the ceiling D04 tests against, so this table has to be right or an
attendance anomaly becomes undetectable.

The Hijri conversion is the tabular ("Kuwaiti") algorithm rather than an
observation-based one: it is arithmetic, needs no external data, and is
therefore deterministic -- which matters more here than being accurate to the
day of a moon sighting.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Any

from ..config import ScaleConfig, period_first_day

# Saudi weekend.
WEEKEND = (4, 5)  # Friday, Saturday (Monday == 0)

HIJRI_RAMADAN = 9
HIJRI_SHAWWAL = 10
HIJRI_DHUL_HIJJAH = 12

# Fixed-date national holidays.
NATIONAL_DAY = (9, 23)
FOUNDING_DAY = (2, 22)


def _gregorian_to_jdn(day: date) -> int:
    a = (14 - day.month) // 12
    year = day.year + 4800 - a
    month = day.month + 12 * a - 3
    return (
        day.day
        + (153 * month + 2) // 5
        + 365 * year
        + year // 4
        - year // 100
        + year // 400
        - 32045
    )


def to_hijri(day: date) -> tuple[int, int, int]:
    """Gregorian date to (hijri_year, hijri_month, hijri_day)."""
    value = _gregorian_to_jdn(day) - 1948440 + 10632
    cycles = (value - 1) // 10631
    value = value - 10631 * cycles + 354
    year_in_cycle = ((10985 - value) // 5316) * ((50 * value) // 17719) + (
        value // 5670
    ) * ((43 * value) // 15238)
    value = (
        value
        - ((30 - year_in_cycle) // 15) * ((17719 * year_in_cycle) // 50)
        - (year_in_cycle // 16) * ((15238 * year_in_cycle) // 43)
        + 29
    )
    month = (24 * value) // 709
    day_of_month = value - (709 * month) // 24
    return 30 * cycles + year_in_cycle - 30, month, day_of_month


def is_public_holiday(day: date) -> bool:
    """Saudi public holidays: the two Eids, National Day and Founding Day."""
    if (day.month, day.day) in (NATIONAL_DAY, FOUNDING_DAY):
        return True
    _, hijri_month, hijri_day = to_hijri(day)
    if hijri_month == HIJRI_SHAWWAL and 1 <= hijri_day <= 4:
        return True
    return hijri_month == HIJRI_DHUL_HIJJAH and 9 <= hijri_day <= 13


def build(cfg: ScaleConfig) -> dict[str, Any]:
    periods = cfg.period_list
    rows: list[dict[str, Any]] = []
    for period in periods:
        first = period_first_day(period)
        days_in_month = monthrange(first.year, first.month)[1]
        holidays = 0
        working = 0
        ramadan_days = 0
        ramadan_flag = False
        for offset in range(days_in_month):
            day = first + timedelta(days=offset)
            holiday = is_public_holiday(day)
            holidays += holiday
            if day.weekday() not in WEEKEND and not holiday:
                working += 1
            if to_hijri(day)[1] == HIJRI_RAMADAN:
                ramadan_days += 1
                ramadan_flag = True
        hijri_year, hijri_month, _ = to_hijri(first)
        rows.append(
            {
                "period": period,
                "year": first.year,
                "month": first.month,
                "hijri_year": hijri_year,
                "hijri_month": hijri_month,
                "calendar_days": days_in_month,
                "working_days": working,
                "public_holiday_days": holidays,
                "is_ramadan": ramadan_flag,
                "ramadan_overlap_days": ramadan_days,
            }
        )
    return {key: [row[key] for row in rows] for key in rows[0]}


def calendar_days_by_period(cfg: ScaleConfig) -> dict[int, int]:
    """`{period: days_in_month}` -- the D04 ceiling, needed by the fact builders."""
    out: dict[int, int] = {}
    for period in cfg.period_list:
        first = period_first_day(period)
        out[period] = monthrange(first.year, first.month)[1]
    return out


def working_days_by_period(cfg: ScaleConfig) -> dict[int, int]:
    table = build(cfg)
    return dict(zip(table["period"], table["working_days"], strict=True))
