"""Loading, validation and digesting of the YAML policy packs.

Every consumer of `policy/` goes through `PolicyPack.load()`, for three reasons:
the `class_defaults` inheritance in `sites.yaml` and the multiplier form in
`grade_bands.yaml` must be resolved exactly once (two resolutions is two
policies), the clause strings must be parsed once into `Predicate` objects, and
the SHA-256 digest of every file has to travel into `manifest.json` so the eval
harness can refuse to score a run against stale ground truth.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml

from .clauses import ClauseSet

# The files that make up a policy pack. Order is fixed so the digest map is
# stable across runs and platforms.
POLICY_FILES = (
    "sites.yaml",
    "grade_bands.yaml",
    "allowance_rules.yaml",
    "payroll.yaml",
    "population.yaml",
    "fusion.yaml",
    "injection.yaml",
    "peer_stats.yaml",
)

NATIONALITY_CLASSES = ("saudi", "gcc", "expat")

# Ordinal ranking for the education comparison in A11.
EDUCATION_ORDER = ("secondary", "diploma", "bachelor", "master", "doctorate")

MONEY = Decimal("0.01")


def money(value: Any) -> Decimal:
    """Quantise to the stored DECIMAL(12,2). Half-up, because payroll rounds up."""
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


class PolicyError(ValueError):
    """Raised when a policy pack is internally inconsistent."""


def _parse_range_key(key: str) -> range:
    """`grade_entitlements` keys are written as `5-8`; expand to a grade range."""
    low, _, high = key.partition("-")
    return range(int(low), int(high or low) + 1)


@dataclass(frozen=True)
class Allowance:
    """One row of `dim_allowance`, with its eligibility already parsed."""

    code: str
    name_en: str
    name_ar: str
    amount_basis: str
    amount: Decimal | None
    rate_pct: float | None
    cap: Decimal | None
    grade_table: dict[int, Decimal]
    site_table: dict[int, Decimal]
    per_dependent: bool
    max_dependents: int | None
    max_consecutive_months: int | None
    one_off: bool
    eligibility: ClauseSet
    violation_codes: tuple[str, ...]
    regulatory_reference: str
    eligibility_rule_id: str | None

    def resolve_amount(self, row: Mapping[str, Any]) -> Decimal:
        """The monthly SAR this allowance pays for this row. Zero = not payable."""
        if self.amount_basis == "fixed":
            base = self.amount or Decimal(0)
            if self.per_dependent:
                count = int(row.get("dependents_in_kingdom") or 0)
                if self.max_dependents is not None:
                    count = min(count, self.max_dependents)
                base = base * count
            return money(base)
        if self.amount_basis == "pct_of_base":
            value = money(row["base_salary"]) * Decimal(str(self.rate_pct or 0)) / Decimal(100)
            if self.cap is not None:
                value = min(value, self.cap)
            return money(value)
        if self.amount_basis == "grade_table":
            return money(self.grade_table.get(int(row["grade"]), Decimal(0)))
        if self.amount_basis == "site_table":
            tier = int(row["site.hardship_tier"] if "site.hardship_tier" in row
                       else row["site"]["hardship_tier"])
            return money(self.site_table.get(tier, Decimal(0)))
        raise PolicyError(f"unknown amount_basis {self.amount_basis!r} on {self.code}")


@dataclass(frozen=True)
class Exclusion:
    """A `mutual_exclusions` entry: codes that must never be paid together."""

    codes: tuple[str, ...]
    forbidden_when: str
    code: str
    clause: ClauseSet | None  # None when forbidden_when is the literal both_present

    @property
    def is_both_present(self) -> bool:
        return self.forbidden_when.strip() == "both_present"


@dataclass(frozen=True)
class Site:
    """A resolved `dim_site` row: class defaults applied, overrides on top."""

    site_id: str
    name_en: str
    name_ar: str
    city: str
    region_code: str
    latitude: float
    longitude: float
    site_class: str
    hardship_tier: int
    remote_allowance_eligible: bool
    offshore_eligible: bool
    camp_available: bool
    family_housing_available: bool
    rotation_supported: bool
    headcount_weight: float


@dataclass(frozen=True)
class GradeBand:
    """One resolved `dim_grade` row (grade x nationality class)."""

    grade: int
    nationality_class: str
    salary_min: Decimal
    salary_mid: Decimal
    salary_max: Decimal
    step_count: int
    step_increment_pct: float
    entitled_allowance_codes: tuple[str, ...]
    gosi_class: str


@dataclass
class PolicyPack:
    """Every YAML pack under `policy/`, resolved and validated."""

    root: Path
    raw: dict[str, dict[str, Any]] = field(repr=False)
    digest: dict[str, str] = field(repr=False)

    # ---------------------------------------------------------------- loading

    @classmethod
    def load(cls, root: str | Path) -> PolicyPack:
        root = Path(root)
        raw: dict[str, dict[str, Any]] = {}
        digest: dict[str, str] = {}
        for name in POLICY_FILES:
            path = root / name
            if not path.exists():
                raise PolicyError(f"missing policy file: {path}")
            data = path.read_bytes()
            digest[name] = "sha256:" + hashlib.sha256(data).hexdigest()
            raw[name] = yaml.safe_load(data.decode("utf-8"))
        pack = cls(root=root, raw=raw, digest=digest)
        pack.validate()
        return pack

    # ------------------------------------------------------------- resolution

    @cached_property
    def regions(self) -> list[dict[str, Any]]:
        return list(self.raw["sites.yaml"]["regions"])

    @cached_property
    def sites(self) -> list[Site]:
        doc = self.raw["sites.yaml"]
        defaults = doc["class_defaults"]
        out: list[Site] = []
        for entry in doc["sites"]:
            base = dict(defaults[entry["class"]])
            base.update({k: v for k, v in entry.items() if k in base})
            out.append(
                Site(
                    site_id=entry["id"],
                    name_en=entry["name_en"],
                    name_ar=entry["name_ar"],
                    city=entry["city"],
                    region_code=entry["region"],
                    latitude=float(entry["lat"]),
                    longitude=float(entry["lon"]),
                    site_class=entry["class"],
                    hardship_tier=int(entry["hardship_tier"]),
                    remote_allowance_eligible=bool(base["remote_allowance_eligible"]),
                    offshore_eligible=bool(base["offshore_eligible"]),
                    camp_available=bool(base["camp_available"]),
                    family_housing_available=bool(base["family_housing_available"]),
                    rotation_supported=bool(base["rotation_supported"]),
                    headcount_weight=float(entry["headcount_weight"]),
                )
            )
        return out

    @cached_property
    def sites_by_id(self) -> dict[str, Site]:
        return {s.site_id: s for s in self.sites}

    @cached_property
    def grade_entitlements(self) -> dict[int, tuple[str, ...]]:
        """Flatten the `1-4: [...]` band form into one entry per grade."""
        out: dict[int, tuple[str, ...]] = {}
        for key, codes in self.raw["grade_bands.yaml"]["grade_entitlements"].items():
            for grade in _parse_range_key(str(key)):
                out[grade] = tuple(codes)
        return out

    @cached_property
    def grade_bands(self) -> dict[tuple[int, str], GradeBand]:
        """Materialise the 20 x 3 band table from the base band x multiplier form."""
        doc = self.raw["grade_bands.yaml"]
        steps = doc["steps"]
        classes = doc["nationality_classes"]
        out: dict[tuple[int, str], GradeBand] = {}
        for grade, band in sorted(doc["bands"].items()):
            for klass in NATIONALITY_CLASSES:
                spec = classes[klass]
                mult = (Decimal(str(spec["multiplier"]))
                        if grade >= int(spec["applies_from"]) else Decimal(1))
                out[(int(grade), klass)] = GradeBand(
                    grade=int(grade),
                    nationality_class=klass,
                    salary_min=money(Decimal(str(band["min"])) * mult),
                    salary_mid=money(Decimal(str(band["mid"])) * mult),
                    salary_max=money(Decimal(str(band["max"])) * mult),
                    step_count=int(steps["count"]),
                    step_increment_pct=float(steps["increment_pct"]),
                    entitled_allowance_codes=self.grade_entitlements[int(grade)],
                    gosi_class=str(spec["gosi_class"]),
                )
        return out

    @cached_property
    def gosi_class_by_nationality(self) -> dict[str, str]:
        classes = self.raw["grade_bands.yaml"]["nationality_classes"]
        return {k: str(v["gosi_class"]) for k, v in classes.items()}

    @cached_property
    def allowances(self) -> dict[str, Allowance]:
        out: dict[str, Allowance] = {}
        for code, spec in self.raw["allowance_rules.yaml"]["allowances"].items():
            out[code] = Allowance(
                code=code,
                name_en=spec["name_en"],
                name_ar=spec["name_ar"],
                amount_basis=spec["amount_basis"],
                amount=money(spec["amount"]) if "amount" in spec else None,
                rate_pct=float(spec["rate_pct"]) if "rate_pct" in spec else None,
                cap=money(spec["cap"]) if "cap" in spec else None,
                grade_table={int(k): money(v)
                             for k, v in (spec.get("grade_table") or {}).items()},
                site_table={int(str(k).removeprefix("tier_")): money(v)
                            for k, v in (spec.get("site_table") or {}).items()},
                per_dependent=bool(spec.get("per_dependent", False)),
                max_dependents=spec.get("max_dependents"),
                max_consecutive_months=spec.get("max_consecutive_months"),
                one_off=bool(spec.get("one_off", False)),
                eligibility=ClauseSet.parse_all((spec.get("eligibility") or {}).get("all")),
                violation_codes=tuple(spec.get("violation_codes") or ()),
                regulatory_reference=spec["regulatory_reference"],
                eligibility_rule_id=self._rule_id_for(spec.get("violation_codes") or ()),
            )
        return out

    def _rule_id_for(self, violation_codes: tuple[str, ...] | list[str]) -> str | None:
        """The `policy/rules/*.yaml` that polices this allowance, if one exists."""
        for code in violation_codes:
            if list((self.root / "rules").glob(f"{code}_*.yaml")):
                return code
        return None

    @cached_property
    def exclusions(self) -> tuple[Exclusion, ...]:
        out: list[Exclusion] = []
        for spec in self.raw["allowance_rules.yaml"].get("mutual_exclusions") or []:
            when = str(spec["forbidden_when"])
            clause = None if when.strip() == "both_present" else ClauseSet.parse_all([when])
            out.append(
                Exclusion(
                    codes=tuple(spec["codes"]),
                    forbidden_when=when,
                    code=str(spec["code"]),
                    clause=clause,
                )
            )
        return tuple(out)

    @cached_property
    def allowance_load(self) -> dict[str, float]:
        return dict(self.raw["allowance_rules.yaml"]["allowance_load"])

    @cached_property
    def band_policy(self) -> dict[str, float]:
        return dict(self.raw["grade_bands.yaml"]["band_policy"])

    @property
    def payroll(self) -> dict[str, Any]:
        return self.raw["payroll.yaml"]

    @property
    def population(self) -> dict[str, Any]:
        return self.raw["population.yaml"]

    @property
    def fusion(self) -> dict[str, Any]:
        return self.raw["fusion.yaml"]

    @property
    def injection(self) -> dict[str, Any]:
        """Pass-2 dials: rates, magnitudes, windows and the collision guards."""
        return self.raw["injection.yaml"]

    @property
    def peer_stats(self) -> dict[str, Any]:
        """Layer-2 dials: robust-statistic guards, the salary model, the codes."""
        return self.raw["peer_stats.yaml"]

    # ------------------------------------------------------------- validation

    def validate(self) -> None:
        """Fail loudly on an inconsistent pack rather than generating against it."""
        codes = set(self.allowances)
        gated = {c for cs in self.grade_entitlements.values() for c in cs}
        unknown = sorted(gated - codes)
        if unknown:
            raise PolicyError(f"grade_entitlements names unknown allowances: {unknown}")
        ungated = sorted(codes - gated)
        if ungated:
            raise PolicyError(
                "these allowances are payable at no grade at all, so they could "
                f"never be paid: {ungated}"
            )
        for exclusion in self.exclusions:
            unknown = sorted(set(exclusion.codes) - codes)
            if unknown:
                raise PolicyError(f"mutual_exclusions names unknown allowances: {unknown}")
        missing = sorted(set(self.grade_entitlements) ^ set(self.raw["grade_bands.yaml"]["bands"]))
        if missing:
            raise PolicyError(f"grade bands and grade entitlements disagree on: {missing}")
        for site in self.sites:
            if not 0 <= site.hardship_tier <= 3:
                raise PolicyError(f"{site.site_id}: hardship_tier out of range")
        for allowance in self.allowances.values():
            if allowance.amount_basis not in (
                "fixed", "pct_of_base", "grade_table", "site_table"
            ):
                raise PolicyError(
                    f"{allowance.code}: unknown amount_basis {allowance.amount_basis!r}"
                )
