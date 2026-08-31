"""Datagen's side of the shared entitlement core.

The clause evaluation itself lives in `policycore` so the phase-3 rule engine
gets identical answers.  What is added here is generator-specific:

* `feature_row()` -- assembling the flat denormalised row the clauses read, from
  an employee, the site they are posted to and the job they hold.
* `EntitlementResolver` -- a memoised resolver.  Pass 1 evaluates entitlement
  once per employee-period, which is 24 evaluations per employee; almost all of
  them see identical inputs, so the *set* of payable codes is cached on the
  fields the clauses actually read and only the amounts are recomputed.
* `fit_allowance_load()` -- the repair ladder that keeps the clean population
  under the allowance-load ceiling.  It works by changing the POPULATION (an
  employee is company-housed rather than housed by allowance, their family is
  not resident, the post is not safety-critical) and never by withholding an
  allowance somebody is entitled to.  Withholding would put a policy breach in
  the clean set pointing the other way, and pass 1 would no longer be clean.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from policycore import entitlement as core
from policycore.packs import PolicyPack, Site

# Every field any clause can read. Built explicitly so a policy edit that
# references an unknown field fails on the first row rather than silently
# evaluating to False.
EMPLOYEE_FIELDS: tuple[str, ...] = (
    "grade", "base_salary", "housing_type", "transport_mode", "work_pattern",
    "dependents_count", "dependents_in_kingdom", "marital_status",
    "spouse_employed_internally", "nationality_class", "service_years",
    "acting_role_flag", "months_since_site_change", "status",
    "languages_count", "has_valid_required_certifications",
)

SITE_FIELDS: tuple[str, ...] = (
    "remote_allowance_eligible", "site_class", "hardship_tier",
    "offshore_eligible", "camp_available", "family_housing_available",
    "rotation_supported",
)

# Fields whose exact value never changes which codes are payable, only how much.
# Excluded from the cache key so a salary increment does not evict the entry.
_AMOUNT_ONLY = frozenset({"base_salary"})

# `service_years <= 5` and `months_since_site_change <= 6` are the only
# comparisons those two fields take part in, so they are bucketed rather than
# used raw: without this every month of service would be its own cache entry.
_SERVICE_YEARS_CAP = 6.0
_SITE_CHANGE_CAP = 7


def feature_row(
    employee: Mapping[str, Any], site: Site, job_safety_critical: bool
) -> dict[str, Any]:
    """The flat row the eligibility clauses are written against.

    Unit boundary: the generator carries money as int64 minor units, but the
    policy pack is written in SAR (`rate_pct: 25.0` means a quarter of the
    salary, not of the halalas). The conversion happens here, once, so no
    caller has to remember which side of the line it is on.
    """
    row: dict[str, Any] = {name: employee.get(name) for name in EMPLOYEE_FIELDS}
    row["base_salary"] = to_sar(employee.get("base_salary"))
    for name in SITE_FIELDS:
        row[f"site.{name}"] = getattr(site, name)
    row["job.safety_critical"] = bool(job_safety_critical)
    return row


def to_sar(cents: Any) -> Decimal:
    return (Decimal(int(cents or 0)) / 100).quantize(Decimal("0.01"))


def set_base_salary(employee: dict[str, Any], row: dict[str, Any], cents: int) -> None:
    """Keep the minor-unit record and the SAR feature row in step."""
    employee["base_salary"] = int(cents)
    row["base_salary"] = to_sar(cents)


def _cache_key(row: Mapping[str, Any]) -> tuple:
    service = min(float(row["service_years"] or 0.0), _SERVICE_YEARS_CAP)
    since = min(int(row["months_since_site_change"] or 0), _SITE_CHANGE_CAP)
    return (
        row["grade"], row["housing_type"], row["transport_mode"],
        row["work_pattern"], row["dependents_count"], row["dependents_in_kingdom"],
        row["marital_status"], row["spouse_employed_internally"],
        row["nationality_class"], round(service, 2), row["acting_role_flag"],
        since, row["status"], row["languages_count"],
        row["has_valid_required_certifications"],
        row["site.site_class"], row["site.hardship_tier"],
        row["site.remote_allowance_eligible"], row["site.offshore_eligible"],
        row["site.camp_available"], row["site.rotation_supported"],
        row["job.safety_critical"],
    )


@dataclass
class Payment:
    """One allowance actually paid, ready to become a `fact_payroll_allowance` row."""

    code: str
    cents: int
    amount_basis: str
    snapshot_json: str


class EntitlementResolver:
    """Memoised entitlement resolution over the clean population."""

    def __init__(self, pack: PolicyPack) -> None:
        self.pack = pack
        self._codes: dict[tuple, tuple[str, ...]] = {}

    def codes(self, row: Mapping[str, Any]) -> tuple[str, ...]:
        """The payable codes for this row, ignoring amounts."""
        key = _cache_key(row)
        cached = self._codes.get(key)
        if cached is None:
            # Amounts still have to be resolved here, because a site_table entry
            # of 0 means "not payable at this tier" and a per-dependent
            # allowance with no resident dependents pays nothing.
            cached = tuple(e.code for e in core.resolve(self.pack, row))
            self._codes[key] = cached
        return cached

    def payments(self, row: Mapping[str, Any]) -> list[Payment]:
        """Everything this row is entitled to, as payable rows in minor units."""
        out: list[Payment] = []
        for code in self.codes(row):
            allowance = self.pack.allowances[code]
            amount = allowance.resolve_amount(row)
            if amount <= 0:
                continue
            out.append(
                Payment(
                    code=code,
                    cents=int(amount * 100),
                    amount_basis=allowance.amount_basis,
                    snapshot_json=snapshot_json(allowance, row),
                )
            )
        return out

    def total_cents(self, row: Mapping[str, Any]) -> int:
        return sum(p.cents for p in self.payments(row))


def snapshot_json(allowance, row: Mapping[str, Any]) -> str:
    """The field values eligibility was judged on, frozen at payment time.

    This is what lets an alert tell a reviewer *why it was payable then* rather
    than only what was paid, so it carries the clause's own fields plus the
    grade, salary and site attributes every amount basis depends on.
    """
    fields = (*allowance.eligibility.fields, "grade", "base_salary",
              "site.hardship_tier", "site.site_class")
    return json.dumps(
        {name: _jsonable(row.get(name)) for name in dict.fromkeys(fields)},
        sort_keys=True,
        ensure_ascii=False,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    return value


# --------------------------------------------------------------------------
# The allowance-load repair ladder
# --------------------------------------------------------------------------

# Applied in order until the ratio is under the ceiling. Each step is a change
# to who this employee IS, not to what they are paid: an offshore worker housed
# in the company camp, a junior expatriate whose family is not resident, a post
# that is not safety-critical. The last step moves them up their own salary
# band, which is the only lever that never removes an entitlement at all.
REPAIR_STEPS: tuple[str, ...] = (
    "house_by_company",
    "own_transport",
    "family_not_resident",
    "single_language",
    "not_acting",
    "not_safety_critical",
    "raise_within_band",
)


def _apply_step(step: str, employee: dict[str, Any], row: dict[str, Any]) -> bool:
    """Apply one repair. Returns True when it actually changed something."""
    if step == "house_by_company" and row["housing_type"] == "allowance":
        if row["site.family_housing_available"]:
            target = "company_family_housing"
        elif row["site.camp_available"]:
            target = "company_camp_bachelor"
        else:
            # Nowhere to house them, so they house themselves without support.
            target = "own"
        employee["housing_type"] = row["housing_type"] = target
        return True
    if step == "own_transport" and row["transport_mode"] == "allowance":
        employee["transport_mode"] = row["transport_mode"] = "own"
        employee["company_bus_route_id"] = None
        return True
    if step == "family_not_resident" and (row["dependents_in_kingdom"] or 0) > 0:
        employee["dependents_in_kingdom"] = row["dependents_in_kingdom"] = 0
        return True
    if step == "single_language" and (row["languages_count"] or 0) > 1:
        employee["languages_count"] = row["languages_count"] = 1
        employee["languages"] = ["Arabic"]
        return True
    if step == "not_acting" and row["acting_role_flag"]:
        employee["acting_role_flag"] = row["acting_role_flag"] = False
        employee["acting_role_since"] = None
        return True
    if step == "not_safety_critical" and row["job.safety_critical"]:
        row["job.safety_critical"] = False
        employee["_needs_non_safety_job"] = True
        return True
    return False


def fit_allowance_load(
    resolver: EntitlementResolver,
    employee: dict[str, Any],
    row: dict[str, Any],
    ceiling: float,
    band_max_cents: int,
    steps: tuple[str, ...] = REPAIR_STEPS,
    base_cents: int | None = None,
) -> int:
    """Bring `allowance_total / base_salary` under `ceiling`. Returns the total.

    The clean population must sit clear of the B03 injection range or B03 would
    be measuring nothing.  The ladder is deterministic and ordered, so the same
    seed produces the same repaired population.
    """
    # `row` is not always the employee as they are today: the caller may be
    # repairing against the worst period of a career, in which case the base
    # pay to measure against is that period, not the current salary.
    total = resolver.total_cents(row)
    base = int(base_cents if base_cents is not None else employee["base_salary"])
    if base > 0 and total <= ceiling * base:
        return total

    for step in steps:
        if step == "raise_within_band":
            # Last resort: move the employee up their own band. The percentage
            # allowances scale with base pay, so this is a fixed point rather
            # than a division -- two or three passes always settle it.
            for _ in range(4):
                needed = int(total / ceiling) + 1
                if needed <= base:
                    break
                base = min(band_max_cents, needed + (-needed % 1000))
                set_base_salary(employee, row, base)
                total = resolver.total_cents(row)
                if base >= band_max_cents:
                    break
        elif _apply_step(step, employee, row):
            total = resolver.total_cents(row)
        if base > 0 and total <= ceiling * base:
            break
    return total
