"""The single definition of who may legitimately receive which allowance.

Datagen pass 1 pays exactly what `resolve()` returns, which is what makes the
clean population genuinely clean; the phase-3 rule engine will call the same
function on the same feature row to decide whether a *paid* allowance was
payable. One implementation, so an entitlement can never mean one thing to the
generator and another to the detector.

An allowance must pass all three gates before it is payable:

    1. its own eligibility clause in `policy/allowance_rules.yaml`
    2. the grade gate in `policy/grade_bands.yaml` -> `grade_entitlements`
    3. the `mutual_exclusions` set

The feature row is flat: employee fields under their own names, site fields
under `site.<field>`, job fields under `job.<field>`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .packs import Allowance, PolicyPack

# Fields quoted into `eligibility_snapshot_json` for every payment, on top of
# whatever the clause itself referenced. These are what let an alert say *why
# it was payable then* rather than only what the amount was.
_ALWAYS_SNAPSHOT = ("grade", "base_salary", "site.hardship_tier", "site.site_class")


@dataclass(frozen=True)
class Entitlement:
    """One payable allowance, with the evidence for why it was payable."""

    code: str
    amount: Decimal
    amount_basis: str
    snapshot: dict[str, Any]


def _snapshot_for(allowance: Allowance, row: Mapping[str, Any]) -> dict[str, Any]:
    fields = list(allowance.eligibility.fields)
    for extra in _ALWAYS_SNAPSHOT:
        if extra not in fields:
            fields.append(extra)
    if allowance.per_dependent and "dependents_in_kingdom" not in fields:
        fields.append("dependents_in_kingdom")
    out: dict[str, Any] = {}
    for name in fields:
        value = row.get(name)
        out[name] = str(value) if isinstance(value, Decimal) else value
    return out


def eligible_codes(
    pack: PolicyPack, row: Mapping[str, Any], include_one_off: bool = False
) -> list[str]:
    """Codes whose own clause and grade gate both pass, before exclusions.

    `one_off` allowances are excluded by default. SEVERANCE is the only one:
    its clause (`status == 'terminated'`) stays true for every month after the
    employee leaves, so treating it as a monthly entitlement would pay a full
    salary every period -- which is C04, the very thing it must not look like.
    Payroll asks for it explicitly, once, in the settlement month.
    """
    gate = set(pack.grade_entitlements.get(int(row["grade"]), ()))
    return [
        code
        for code, allowance in pack.allowances.items()
        if code in gate
        and (include_one_off or not allowance.one_off)
        and allowance.eligibility.evaluate(row)
    ]


def apply_exclusions(
    pack: PolicyPack, row: Mapping[str, Any], amounts: dict[str, Decimal]
) -> dict[str, Decimal]:
    """Drop codes barred by `mutual_exclusions`.

    `both_present` exclusions keep the highest-value code and drop the rest --
    the more specific entitlement wins (FIELD_MESSING over MEAL), and ties fall
    back to the order the codes are written in the policy so the outcome is
    stable. Conditional exclusions drop every listed code when the condition
    holds.
    """
    kept = dict(amounts)
    for exclusion in pack.exclusions:
        present = [c for c in exclusion.codes if c in kept]
        if exclusion.is_both_present:
            if len(present) > 1:
                winner = max(present, key=lambda c: (kept[c], -exclusion.codes.index(c)))
                for code in present:
                    if code != winner:
                        del kept[code]
        elif present and exclusion.clause is not None and exclusion.clause.evaluate(row):
            for code in present:
                del kept[code]
    return kept


def resolve(
    pack: PolicyPack, row: Mapping[str, Any], include_one_off: bool = False
) -> list[Entitlement]:
    """Every allowance this row is entitled to, with amounts, sorted by code."""
    amounts: dict[str, Decimal] = {}
    for code in eligible_codes(pack, row, include_one_off):
        amount = pack.allowances[code].resolve_amount(row)
        # A site_table entry of 0 means "not payable at this tier"; a
        # per-dependent allowance with no resident dependents is likewise zero.
        if amount > 0:
            amounts[code] = amount
    amounts = apply_exclusions(pack, row, amounts)
    return [
        Entitlement(
            code=code,
            amount=amounts[code],
            amount_basis=pack.allowances[code].amount_basis,
            snapshot=_snapshot_for(pack.allowances[code], row),
        )
        for code in sorted(amounts)
    ]


def total(entitlements: list[Entitlement]) -> Decimal:
    return sum((e.amount for e in entitlements), Decimal("0.00"))
