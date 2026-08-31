"""Run configuration: where the lake is, how big it is, and what window it covers.

The detector deliberately does not import `datagen`.  Everything it needs to
know about a lake is written into `data/raw/scale=<n>/manifest.json`, which
`docs/DATA_DICTIONARY.md` section 3 defines as the contract between the two
services -- so the generator can be rewritten without the detector noticing,
and a lake copied onto another machine carries its own description with it.

The period arithmetic below is duplicated from the generator on purpose for the
same reason: four functions over a `YYYYMM` integer are cheaper to restate than
a service dependency, and `period` is a fixed part of the data contract rather
than something either service is free to change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

SCALES = ("10k", "100k", "1m")

DEFAULT_LAKE = "data/raw"
DEFAULT_FEATURES = "data/features"
DEFAULT_RUNS = "data/runs"

# Rows per Parquet row-group, matching the generator's chunking so a feature
# scan reads whole row-groups rather than straddling them.
ROW_GROUP_ROWS = 100_000


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


class LakeError(RuntimeError):
    """Raised when the lake is missing, unreadable or not what was asked for."""


@dataclass(frozen=True)
class DetectorConfig:
    """One detection run: which lake, which window, where the outputs go."""

    scale: str
    lake_root: Path
    features_root: Path
    runs_root: Path
    run_id: str
    manifest: dict

    @classmethod
    def build(
        cls,
        scale: str,
        run_id: str | None = None,
        lake: str | Path = DEFAULT_LAKE,
        features: str | Path = DEFAULT_FEATURES,
        runs: str | Path = DEFAULT_RUNS,
    ) -> DetectorConfig:
        if scale not in SCALES:
            raise ValueError(f"unknown scale {scale!r}; expected one of {SCALES}")
        lake_root = Path(lake)
        manifest_path = lake_root / f"scale={scale}" / "manifest.json"
        if not manifest_path.exists():
            raise LakeError(
                f"no lake at {manifest_path.parent}. Generate it first:\n"
                f"    python tasks.py datagen --scale {scale} --seed 42"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return cls(
            scale=scale,
            lake_root=lake_root,
            features_root=Path(features),
            runs_root=Path(runs),
            # A run id defaults to the last period in the lake, which is what a
            # monthly production run would be named after.
            run_id=run_id or str(manifest["period_to"]),
            manifest=manifest,
        )

    # ------------------------------------------------------------------ paths

    @property
    def lake(self) -> Path:
        return self.lake_root / f"scale={self.scale}"

    @property
    def features(self) -> Path:
        return self.features_root / f"scale={self.scale}"

    @property
    def run_dir(self) -> Path:
        return self.runs_root / f"run_id={self.run_id}"

    @property
    def features_manifest(self) -> Path:
        return self.features / "manifest.json"

    def raw_glob(self, table: str) -> str:
        return str(self.lake / table / "**" / "*.parquet").replace("\\", "/")

    def feature_glob(self, table: str) -> str:
        return str(self.features / table / "**" / "*.parquet").replace("\\", "/")

    def feature_dir(self, table: str) -> Path:
        return self.features / table

    # ---------------------------------------------------------------- window

    @property
    def employees(self) -> int:
        return int(self.manifest["employee_count"])

    @property
    def period_from(self) -> int:
        return int(self.manifest["period_from"])

    @property
    def period_to(self) -> int:
        return int(self.manifest["period_to"])

    @property
    def periods(self) -> int:
        return int(self.manifest["period_count"])

    @property
    def period_list(self) -> list[int]:
        return [period_add(self.period_from, i) for i in range(self.periods)]

    @property
    def policy_digest(self) -> dict[str, str]:
        return dict(self.manifest.get("policy_digest") or {})

    @property
    def has_ground_truth(self) -> bool:
        """False on a `--no-inject` lake: there is nothing for the harness to score."""
        return bool((self.manifest.get("injection") or {}).get("by_code"))

    def scaled(self, at_1m: float) -> float:
        """A 1m-scale budget scaled linearly to this tier (`policy/fusion.yaml`)."""
        return at_1m * self.employees / 1_000_000
