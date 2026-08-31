"""Deterministic synthetic Saudi/energy-sector HR and payroll generator.

Pass 1 (phase 1) builds a clean, policy-compliant population: every paid
allowance satisfies its eligibility clause in `policy/allowance_rules.yaml`.
Pass 2 (phase 2) breaks specific clauses on purpose and records exactly what it
broke.  The split is the reason ground truth is exact -- if pass 1 leaks a
violation, that violation is an unlabelled anomaly and every downstream recall
figure is wrong.
"""

from __future__ import annotations

GENERATOR_VERSION = "1.0.0"

__all__ = ["GENERATOR_VERSION"]
