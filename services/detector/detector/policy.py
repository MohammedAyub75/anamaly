"""The detector's view of the policy packs.

Thin, on purpose.  Loading, `class_defaults` resolution, band materialisation
and clause parsing all live in `policycore` because the generator needs exactly
the same answers -- two implementations of one clause is how injector/detector
drift starts.  What belongs here is only what the *detector* needs on top: the
SQL forms of the policy tables (so DuckDB can recompute an entitlement itself
rather than trusting the number in the lake), the cohort ladder, and the digest
check that refuses to score a run against stale ground truth.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

from policycore.packs import EDUCATION_ORDER, PolicyPack

POLICY_ROOT = "policy"

ROTATION_PATTERNS = ("rotation_28_28", "rotation_14_14")

# The amount tolerance A07 is judged on. One riyal: payroll rounds to the cent,
# so anything above a riyal is a decision somebody made, not a rounding artefact.
AMOUNT_TOLERANCE_SAR = 1.00


class DigestMismatch(RuntimeError):
    """The policy pack changed but the lake did not. Every figure would be wrong."""


class DetectorPolicy:
    """A `PolicyPack` plus the SQL fragments the feature build needs."""

    def __init__(self, pack: PolicyPack) -> None:
        self.pack = pack

    @classmethod
    def load(cls, root: str | Path = POLICY_ROOT) -> DetectorPolicy:
        return cls(PolicyPack.load(root))

    # ----------------------------------------------------------------- basics

    @property
    def digest(self) -> dict[str, str]:
        return self.pack.digest

    @property
    def fusion(self) -> dict:
        return self.pack.fusion

    @cached_property
    def allowance_codes(self) -> tuple[str, ...]:
        """Every allowance code, in a fixed order so the wide pivot is stable."""
        return tuple(sorted(self.pack.allowances))

    @cached_property
    def cohort_ladder(self) -> tuple[tuple[str, ...], ...]:
        """The fallback ladder from `policy/fusion.yaml`, most specific first."""
        return tuple(
            tuple(level) for level in self.fusion["peer_cohort"]["fallback_order"]
        )

    @property
    def cohort_min_size(self) -> int:
        return int(self.fusion["peer_cohort"]["min_size"])

    def verify_digest(self, manifest: dict) -> list[str]:
        """Policy files whose hash no longer matches the one the lake was built under.

        A mismatch means the detector is being evaluated against ground truth
        generated under a different policy: not a warning, a wrong answer.
        """
        recorded = dict(manifest.get("policy_digest") or {})
        return sorted(
            name for name, value in self.digest.items()
            if recorded.get(name) not in (None, value)
        )

    def require_digest(self, manifest: dict) -> None:
        drifted = self.verify_digest(manifest)
        if drifted:
            raise DigestMismatch(
                "policy changed since the lake was generated: "
                + ", ".join(drifted)
                + "\nregenerate before scoring:  python tasks.py datagen --scale "
                + str(manifest.get("scale", "10k"))
                + " --seed "
                + str(manifest.get("seed", 42))
            )

    # -------------------------------------------------------------- SQL forms

    def expected_amount_sql(self, code: str) -> str:
        """SQL recomputing one allowance's policy amount from the feature row.

        DuckDB evaluates this over the lake, so A07 is an independent second
        opinion on every amount paid rather than a restatement of whatever the
        payroll row happens to say.  The column names are those of the as-at
        feature row assembled in `features/sql/`.
        """
        allowance = self.pack.allowances[code]
        if allowance.amount_basis == "fixed":
            if allowance.per_dependent:
                cap = allowance.max_dependents or 99
                return f"{allowance.amount} * least(dependents_in_kingdom, {cap})"
            return f"{allowance.amount}"
        if allowance.amount_basis == "pct_of_base":
            value = f"round(asat_base_salary * {allowance.rate_pct} / 100, 2)"
            if allowance.cap is not None:
                value = f"least({value}, {allowance.cap})"
            return value
        if allowance.amount_basis == "grade_table":
            whens = " ".join(
                f"WHEN {g} THEN {v}" for g, v in sorted(allowance.grade_table.items())
            )
            return f"CASE asat_grade {whens} ELSE 0 END"
        whens = " ".join(
            f"WHEN {t} THEN {v}" for t, v in sorted(allowance.site_table.items())
        )
        return f"CASE site_hardship_tier {whens} ELSE 0 END"

    def expected_amount_case(self) -> str:
        """One CASE over `allowance_code`, for the long-format allowance features."""
        branches = " ".join(
            f"WHEN '{code}' THEN {self.expected_amount_sql(code)}"
            for code in self.allowance_codes
        )
        return f"CASE allowance_code {branches} ELSE NULL END"

    def education_rank_sql(self, column: str) -> str:
        """The ordinal education scale as SQL, so A11 is a comparison not a join."""
        whens = " ".join(
            f"WHEN '{level}' THEN {rank}" for rank, level in enumerate(EDUCATION_ORDER)
        )
        return f"CASE {column} {whens} ELSE -1 END"

    def gosi_class_sql(self, column: str = "nationality_class") -> str:
        """The GOSI class `nationality_class` implies -- the A09 cross-check."""
        whens = " ".join(
            f"WHEN '{k}' THEN '{v}'"
            for k, v in sorted(self.pack.gosi_class_by_nationality.items())
        )
        return f"CASE {column} {whens} ELSE 'unknown' END"

    def duration_limit(self, code: str) -> int | None:
        """`max_consecutive_months` for a time-limited allowance (A12)."""
        return self.pack.allowances[code].max_consecutive_months

    @cached_property
    def duration_limited_codes(self) -> tuple[str, ...]:
        return tuple(
            code for code in self.allowance_codes
            if self.pack.allowances[code].max_consecutive_months is not None
        )

    @property
    def hard_ceiling_ratio(self) -> float:
        return float(self.pack.allowance_load["hard_ceiling_ratio"])

    @property
    def band_policy(self) -> dict:
        return self.pack.band_policy

    @property
    def overtime(self) -> dict:
        return self.pack.payroll["overtime"]

    @property
    def separation(self) -> dict:
        return self.pack.payroll["separation"]

    # -------------------------------------------------------- layer 2 (peer)

    @property
    def peer_stats(self) -> dict:
        """`policy/peer_stats.yaml`: the layer-2 dials, severities and wording."""
        return self.pack.peer_stats

    @property
    def robust(self) -> dict:
        return self.peer_stats["robust"]

    @property
    def expected_salary(self) -> dict:
        return self.peer_stats["expected_salary"]

    @property
    def cusum(self) -> dict:
        return self.peer_stats["cusum"]

    @cached_property
    def peer_codes(self) -> dict[str, dict]:
        return dict(self.peer_stats["codes"])

    def peer_threshold(self, code: str, name: str) -> float:
        """One code's dial. Missing is a bug in the pack, not a default to guess."""
        thresholds = self.peer_codes[code].get("thresholds") or {}
        if name not in thresholds:
            raise KeyError(f"peer_stats.yaml: codes.{code}.thresholds.{name} is missing")
        return float(thresholds[name])

    # The peer layer's cross-pack numbers. Each of these already has a home in
    # another pack and is read from there: two copies of one threshold is how an
    # injector and a detector drift apart.

    @property
    def peer_cohort(self) -> dict:
        return self.fusion["peer_cohort"]

    @property
    def robust_z_threshold(self) -> float:
        return float(self.peer_cohort["robust_z_threshold"])

    @property
    def percentile_flag_high(self) -> float:
        return float(self.peer_cohort["percentile_flag_high"])

    @property
    def percentile_flag_low(self) -> float:
        return float(self.peer_cohort["percentile_flag_low"])

    @property
    def overpayment_tolerance_pct(self) -> float:
        return float(self.band_policy["overpayment_tolerance_pct"])

    @property
    def underpayment_tolerance_pct(self) -> float:
        return float(self.band_policy["underpayment_tolerance_pct"])

    @property
    def max_increments_per_12m(self) -> int:
        return int(self.band_policy["max_increments_per_12m"])

    @property
    def max_grade_jump_per_24m(self) -> int:
        return int(self.band_policy["max_grade_jump_per_24m"])

    @property
    def legal_overtime_hours(self) -> float:
        return float(self.overtime["legal_monthly_max_hours"])

    @property
    def bonus_pct_by_rating(self) -> dict[int, float]:
        return {
            int(k): float(v)
            for k, v in self.pack.payroll["bonus"]["pct_of_base_by_rating"].items()
        }

    @property
    def max_retro_entries_clean(self) -> int:
        return int(self.pack.payroll["retro_adjustment"]["max_per_employee_clean"])

    def allowance_label(self, code: str) -> str:
        """The display name a reviewer reads. Never the raw code (CLAUDE.md)."""
        return self.pack.allowances[code].name_en

    def bonus_entitlement_sql(self, rating: str, base: str) -> str:
        """The bonus the performance rating entitles the employee to, as SQL."""
        whens = " ".join(
            f"WHEN {r} THEN {base} * {pct}"
            for r, pct in sorted(self.bonus_pct_by_rating.items())
        )
        return f"CASE {rating} {whens} ELSE NULL END"

    # ------------------------------------------------------- layer 3 (graph)

    @property
    def graph_ml(self) -> dict:
        """`policy/graph_ml.yaml`: the layer-3 matrix, models and graph dials."""
        return self.pack.graph_ml

    @property
    def matrix(self) -> dict:
        return self.graph_ml["matrix"]

    @property
    def isolation_forest(self) -> dict:
        return self.graph_ml["isolation_forest"]

    @property
    def autoencoder(self) -> dict:
        return self.graph_ml["autoencoder"]

    @property
    def graph(self) -> dict:
        return self.graph_ml["graph"]

    @cached_property
    def graph_codes(self) -> dict[str, dict]:
        return dict(self.graph_ml["codes"])

    def graph_threshold(self, code: str, name: str) -> float:
        """One code's dial. Missing is a bug in the pack, not a default to guess."""
        thresholds = self.graph_codes[code].get("thresholds") or {}
        if name not in thresholds:
            raise KeyError(f"graph_ml.yaml: codes.{code}.thresholds.{name} is missing")
        return float(thresholds[name])

    def allowance_label_case(self, column: str = "allowance_code") -> str:
        """Allowance code -> display name, as SQL. No raw code reaches a reviewer."""
        whens = " ".join(
            f"WHEN '{code}' THEN '{self.allowance_label(code)}'"
            for code in self.allowance_codes
        )
        return f"CASE {column} {whens} ELSE {column} END"
