"""Realism noise -- data-quality issues, never anomalies.

The distinction this module exists to preserve: "unusual" and "anomalous" are
not the same thing.  A record with a missing passport number, a double-spaced
name or MOHAMED where the last feed said MOHAMMED is *messy*, and a detector
that flags mess as fraud will drown a reviewer.  So every effect here is
recorded in `dq_flags` and none of it is ever written to `labels_anomaly`.

What is deliberately left alone: any field an anomaly predicate reads.  `dob`
is the C06 blocking key, `hire_date` and `termination_date` decide C04,
`iqama_expiry` is C07 and `acting_role_since` is A12 -- a transposed digit in
any of those would be an unlabelled anomaly rather than a typo.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np

from .names import TRANSLITERATIONS, normalise

# Nullable columns that may go missing. None of them is read by a rule.
OPTIONAL_FIELDS: tuple[str, ...] = (
    "passport_no",
    "passport_expiry",
    "degree_field",
    "institution",
    "graduation_year",
    "residence_city",
)

# Dates safe to typo: neither is compared against a period by any predicate.
TYPO_DATE_FIELDS: tuple[str, ...] = ("passport_expiry", "probation_end")

FLAG_MISSING = "missing_field"
FLAG_CASING = "name_casing"
FLAG_TRANSLITERATION = "transliteration_variant"
FLAG_TYPO = "date_typo"
FLAG_LATE_POSTING = "late_posting"


def _casing_variant(name: str, choice: int) -> str:
    if choice == 0:
        return name.upper()
    if choice == 1:
        return name.lower()
    if choice == 2:
        return name.replace(" ", "  ", 1)
    return f" {name} "


def _transposed(day: date) -> date | None:
    """Swap the two digits of the day of month, when the result is still valid."""
    swapped = int(f"{day.day:02d}"[::-1])
    if swapped == day.day or not 1 <= swapped <= 28:
        return None
    return day.replace(day=swapped)


def apply(
    rows: list[dict[str, Any]],
    rng_missing: np.random.Generator,
    rng_name: np.random.Generator,
    rng_typo: np.random.Generator,
    settings: dict[str, float],
) -> None:
    """Mutate a chunk of employee records in place, recording every change.

    Rates come from `policy/population.yaml`; manual-entry records are noisier,
    which is both true of real HR systems and the reason `source_system` is
    worth carrying into the lake at all.
    """
    count = len(rows)
    missing_draw = rng_missing.random((count, len(OPTIONAL_FIELDS)))
    casing_draw = rng_name.random(count)
    casing_pick = rng_name.integers(0, 4, count)
    translit_draw = rng_name.random(count)
    typo_draw = rng_typo.random((count, len(TYPO_DATE_FIELDS)))

    base_rate = float(settings["missing_optional_rate"])
    manual_rate = float(settings["missing_optional_rate_manual"])
    casing_rate = float(settings["name_casing_rate"])
    translit_rate = float(settings["transliteration_rate"])
    typo_rate = float(settings["date_typo_rate"])

    for index, row in enumerate(rows):
        flags: list[str] = list(row.get("dq_flags") or [])
        rate = manual_rate if row["source_system"] == "manual" else base_rate
        for position, field in enumerate(OPTIONAL_FIELDS):
            if row.get(field) is not None and missing_draw[index, position] < rate:
                row[field] = None
                if FLAG_MISSING not in flags:
                    flags.append(FLAG_MISSING)

        if translit_draw[index] < translit_rate:
            parts = str(row["name_en"]).split(" ")
            for position, part in enumerate(parts):
                variants = TRANSLITERATIONS.get(part)
                if variants:
                    parts[position] = variants[int(translit_draw[index] * 1000) % len(variants)]
                    row["name_en"] = " ".join(parts)
                    flags.append(FLAG_TRANSLITERATION)
                    break

        if casing_draw[index] < casing_rate:
            row["name_en"] = _casing_variant(str(row["name_en"]), int(casing_pick[index]))
            flags.append(FLAG_CASING)

        # Derived from the noisy value on purpose: the normalised column only
        # earns its place if it is what resolves the noise.
        row["name_en_normalised"] = normalise(str(row["name_en"]))

        for position, field in enumerate(TYPO_DATE_FIELDS):
            value = row.get(field)
            if isinstance(value, date) and typo_draw[index, position] < typo_rate:
                swapped = _transposed(value)
                if swapped is not None:
                    row[field] = swapped
                    flags.append(FLAG_TYPO)

        row["dq_flags"] = flags


def late_posting_mask(
    rng: np.random.Generator, count: int, settings: dict[str, float]
) -> np.ndarray:
    """Payroll rows whose run id belongs to the following period's run."""
    return rng.random(count) < float(settings["late_posting_rate"])
