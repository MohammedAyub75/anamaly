"""Scale tiers, lake paths and period arithmetic.

`ScaleConfig` is the one place that knows how big a run is; everything else asks
it.  Chunk size is a constant rather than a tier property on purpose: chunk
boundaries must fall in the same places at 10k and at 1m, or the same seed would
produce different rows at different scales.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# Rows per chunk and per Parquet row-group. Fixed across every scale tier.
CHUNK_ROWS = 100_000

SCALES = ("10k", "100k", "1m")

DEFAULT_PERIODS = 24
DEFAULT_REFERENCE_DATE = date(2026, 8, 31)
DEFAULT_OUT = "data/raw"

# Every table written by the generator, in dependency order. This list is also
# the determinism contract: each table draws from its own spawned stream, keyed
# by position here, so adding a table never shifts another table's numbers.
TABLE_NAMES: tuple[str, ...] = (
    "dim_region",
    "dim_site",
    "dim_calendar",
    "dim_grade",
    "dim_allowance",
    "dim_job",
    "dim_org_unit",
    "employee_master",
    "fact_assignment_history",
    "fact_bank_account",
    "fact_payroll_monthly",
    "fact_payroll_allowance",
    "fact_attendance_monthly",
    "fact_system_activity_monthly",
)

# Tables partitioned by `period`; everything else is single-partition.
PERIOD_PARTITIONED = frozenset(
    {
        "fact_payroll_monthly",
        "fact_payroll_allowance",
        "fact_attendance_monthly",
        "fact_system_activity_monthly",
    }
)


def period_of(day: date) -> int:
    """`YYYYMM` as INT32, the project's period key."""
    return day.year * 100 + day.month


def period_add(period: int, months: int) -> int:
    year, month = divmod(period, 100)
    total = year * 12 + (month - 1) + months
    return (total // 12) * 100 + (total % 12) + 1


def period_diff(later: int, earlier: int) -> int:
    """Whole months from `earlier` to `later`; negative when `later` is before."""
    ly, lm = divmod(later, 100)
    ey, em = divmod(earlier, 100)
    return (ly - ey) * 12 + (lm - em)


def period_first_day(period: int) -> date:
    year, month = divmod(period, 100)
    return date(year, month, 1)


def period_last_day(period: int) -> date:
    year, month = divmod(period, 100)
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


@dataclass(frozen=True)
class ScaleConfig:
    """Everything a run needs to know about its own size and window."""

    scale: str
    employees: int
    org_units: int
    periods: int
    reference_date: date
    out_root: Path
    seed: int
    noise: bool = True

    @classmethod
    def build(
        cls,
        scale: str,
        seed: int,
        population: dict,
        out: str | Path = DEFAULT_OUT,
        periods: int = DEFAULT_PERIODS,
        reference_date: date = DEFAULT_REFERENCE_DATE,
        employees: int | None = None,
        noise: bool = True,
    ) -> ScaleConfig:
        if scale not in SCALES:
            raise ValueError(f"unknown scale {scale!r}; expected one of {SCALES}")
        tier = population["scales"][scale]
        count = int(employees if employees is not None else tier["employees"])
        # A smaller-than-tier run (the determinism slice, the test suite) still
        # needs enough org units to hang employees from, but never more units
        # than employees or a section could not have members.
        units = int(tier["org_units"])
        if employees is not None:
            units = max(64, min(units, count // 4))
        return cls(
            scale=scale,
            employees=count,
            org_units=units,
            periods=periods,
            reference_date=reference_date,
            out_root=Path(out),
            seed=seed,
            noise=noise,
        )

    # ------------------------------------------------------------------ paths

    @property
    def lake(self) -> Path:
        return self.out_root / f"scale={self.scale}"

    def table_dir(self, table: str) -> Path:
        return self.lake / table

    def part_path(self, table: str, chunk: int, period: int | None = None) -> Path:
        base = self.table_dir(table)
        if period is not None:
            base = base / f"period={period}"
        return base / f"part-{chunk:04d}.parquet"

    @property
    def manifest_path(self) -> Path:
        return self.lake / "manifest.json"

    # ---------------------------------------------------------------- periods

    @property
    def period_to(self) -> int:
        return period_of(self.reference_date)

    @property
    def period_from(self) -> int:
        return period_add(self.period_to, -(self.periods - 1))

    @property
    def period_list(self) -> list[int]:
        return [period_add(self.period_from, i) for i in range(self.periods)]

    @property
    def window_start(self) -> date:
        return period_first_day(self.period_from)

    # ----------------------------------------------------------------- chunks

    @property
    def chunk_count(self) -> int:
        return max(1, -(-self.employees // CHUNK_ROWS))

    def chunks(self) -> Iterator[tuple[int, int, int]]:
        """`(chunk_index, start_row, row_count)` over the employee population."""
        for index in range(self.chunk_count):
            start = index * CHUNK_ROWS
            yield index, start, min(CHUNK_ROWS, self.employees - start)
