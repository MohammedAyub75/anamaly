"""`dim_allowance` -- the 26 allowance codes as a queryable table.

`regulatory_reference` is carried into the lake rather than looked up later
because the evidence bundle quotes it verbatim to the reviewer: an alert that
says "HR-COMP-011 Remote Assignment s.1" is actionable, one that says "policy
violation" is not.
"""

from __future__ import annotations

from typing import Any

from ..policy import DatagenPolicy
from ..schemas import ALLOWANCE_CODES


def build(policy: DatagenPolicy) -> dict[str, Any]:
    allowances = policy.pack.allowances
    missing = sorted(set(ALLOWANCE_CODES) ^ set(allowances))
    if missing:
        raise ValueError(
            "policy/allowance_rules.yaml and datagen.schemas.ALLOWANCE_CODES "
            f"disagree on: {missing}"
        )
    rows = [allowances[code] for code in ALLOWANCE_CODES]
    return {
        "allowance_code": [a.code for a in rows],
        "name_en": [a.name_en for a in rows],
        "name_ar": [a.name_ar for a in rows],
        "amount_basis": [a.amount_basis for a in rows],
        # `amount` and `cap` are populated per basis; a fixed allowance has no
        # cap and a percentage allowance has no flat amount.
        "amount": _money_column(rows, "amount"),
        "rate_pct": [a.rate_pct for a in rows],
        "cap": _money_column(rows, "cap"),
        "eligibility_rule_id": [a.eligibility_rule_id for a in rows],
        "violation_codes": [list(a.violation_codes) for a in rows],
        "regulatory_reference": [a.regulatory_reference for a in rows],
        "one_off": [a.one_off for a in rows],
    }


def _money_column(rows, attribute: str):
    """Nullable money: pyarrow takes Decimals directly for a 26-row table."""
    return [getattr(a, attribute) for a in rows]
