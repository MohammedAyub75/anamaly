"""The edit set pass 2 accumulates, and the ground-truth rows it emits.

Pass 2 never regenerates the lake.  It loads the rows it intends to break,
mutates them in Python and hands `apply.py` a set of *complete replacement
rows*, so the rewrite is a substitution rather than a second generator.  That
is what keeps the injected dataset a strict, auditable delta from the clean one
the phase-1 gate signed off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Key = tuple[str, int]  # (employee_id, period)


@dataclass(slots=True)
class AllowanceRow:
    """One `fact_payroll_allowance` row, without its employee/period key.

    Slotted because there are a great many of them: pass 2 holds every
    allowance row of every employee it looks at, which at 1m is millions of
    objects, and a slotted instance is a third of the size of one carrying a
    `__dict__`.
    """

    code: str
    cents: int
    basis: str
    snapshot: str


@dataclass
class Edits:
    """Everything pass 2 changed, keyed the way `apply.py` rewrites it."""

    master: dict[str, dict[str, Any]] = field(default_factory=dict)
    payroll: dict[Key, dict[str, Any]] = field(default_factory=dict)
    payroll_new: dict[Key, dict[str, Any]] = field(default_factory=dict)
    allowances: dict[Key, list[AllowanceRow]] = field(default_factory=dict)
    attendance: dict[Key, dict[str, Any]] = field(default_factory=dict)
    activity: dict[Key, dict[str, Any]] = field(default_factory=dict)
    history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    bank: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def touched_employees(self) -> set[str]:
        out = set(self.master) | set(self.history) | set(self.bank)
        for mapping in (self.payroll, self.payroll_new, self.allowances,
                        self.attendance, self.activity):
            out |= {key[0] for key in mapping}
        return out


@dataclass
class Label:
    """One `labels_anomaly` row: what was broken, where, and what it costs."""

    employee_id: str
    anomaly_code: str
    period_from: int
    period_to: int
    injected_severity: str
    params: dict[str, Any]
    human_description: str
    work_site_id: str
    region_code: str
    expected_monthly_impact: int  # minor units, like everything else internally

    @property
    def family(self) -> str:
        return self.anomaly_code[0]


@dataclass
class Confounder:
    """One `labels_confounder` row: a legitimate oddity, deliberately unlabelled."""

    employee_id: str
    confounder_type: str
    confounds_code: str
    period_from: int
    period_to: int
    params: dict[str, Any]
    human_description: str
    work_site_id: str
    region_code: str
    expected_monthly_impact: int
