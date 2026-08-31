"""Datagen's view of the policy packs.

The loading, `class_defaults` resolution, band materialisation and clause
parsing all live in `policycore` because the phase-3 rule engine needs exactly
the same answers.  What belongs here is only what the *generator* needs on top:
the sampling weights, the ordinal education scale and the derived lookup tables
that turn a policy pack into something a vectorised generator can index into.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

import numpy as np

from policycore.packs import EDUCATION_ORDER, NATIONALITY_CLASSES, PolicyPack

POLICY_ROOT = "policy"


class DatagenPolicy:
    """A `PolicyPack` plus the index tables the generator draws against."""

    def __init__(self, pack: PolicyPack) -> None:
        self.pack = pack

    @classmethod
    def load(cls, root: str | Path = POLICY_ROOT) -> DatagenPolicy:
        return cls(PolicyPack.load(root))

    # ------------------------------------------------------------------ sites

    @cached_property
    def site_ids(self) -> list[str]:
        return [s.site_id for s in self.pack.sites]

    @cached_property
    def site_weights(self) -> np.ndarray:
        """Headcount weights -- deliberately Eastern-Province-heavy."""
        return np.array([s.headcount_weight for s in self.pack.sites], dtype=np.float64)

    @cached_property
    def site_index(self) -> dict[str, int]:
        return {site_id: i for i, site_id in enumerate(self.site_ids)}

    def site_at(self, index: int):
        return self.pack.sites[index]

    # ----------------------------------------------------------------- grades

    @cached_property
    def grade_weights(self) -> np.ndarray:
        weights = self.pack.raw["grade_bands.yaml"]["grade_distribution_weights"]
        return np.array([float(weights[g]) for g in range(1, 21)], dtype=np.float64)

    def band(self, grade: int, nationality_class: str):
        return self.pack.grade_bands[(int(grade), nationality_class)]

    @cached_property
    def band_bounds(self) -> dict[str, np.ndarray]:
        """(min, max) in minor units, indexed [grade-1, class_index]. Vectorised lookup."""
        lows = np.zeros((20, 3), dtype=np.int64)
        highs = np.zeros((20, 3), dtype=np.int64)
        for grade in range(1, 21):
            for ci, klass in enumerate(NATIONALITY_CLASSES):
                band = self.pack.grade_bands[(grade, klass)]
                lows[grade - 1, ci] = int(band.salary_min * 100)
                highs[grade - 1, ci] = int(band.salary_max * 100)
        return {"min": lows, "max": highs}

    # -------------------------------------------------------------- education

    @staticmethod
    def education_rank(level: str) -> int:
        return EDUCATION_ORDER.index(level)

    @cached_property
    def education_by_grade_band(self) -> dict[int, tuple[list[str], np.ndarray]]:
        """Education mix per grade, expanded from the `1-4:` band form."""
        out: dict[int, tuple[list[str], np.ndarray]] = {}
        spec = self.pack.population["education"]["by_grade_band"]
        for key, mix in spec.items():
            low, _, high = str(key).partition("-")
            levels = list(mix)
            weights = np.array([float(mix[level]) for level in levels], dtype=np.float64)
            for grade in range(int(low), int(high or low) + 1):
                out[grade] = (levels, weights)
        return out

    # ---------------------------------------------------------------- payroll

    @cached_property
    def gosi_rates(self) -> dict[str, tuple[float, float]]:
        classes = self.pack.payroll["gosi"]["classes"]
        return {
            name: (float(spec["employee_pct"]), float(spec["employer_pct"]))
            for name, spec in classes.items()
        }

    @property
    def population(self) -> dict:
        return self.pack.population

    @property
    def payroll(self) -> dict:
        return self.pack.payroll


def mix(spec: dict[str, float]) -> tuple[list[str], np.ndarray]:
    """Split a `{value: weight}` mapping into parallel lists for weighted sampling."""
    values = list(spec)
    weights = np.array([float(spec[v]) for v in values], dtype=np.float64)
    return values, weights
