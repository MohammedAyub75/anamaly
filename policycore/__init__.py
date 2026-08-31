"""Shared policy-evaluation core.

`policy/allowance_rules.yaml` is read by three consumers: datagen pass 1 (which
pays only entitled allowances), datagen pass 2 (which breaks specific clauses on
purpose) and the layer-1 rule engine (which detects those breaks).  Two
implementations of the same clause is how injector/detector drift starts, so the
clause parser, the entitlement resolver and the policy loader live here once and
every consumer imports them.

`services/datagen/datagen/entitlement.py` is the datagen-side adapter; the
phase-3 rule engine will be the second consumer.
"""

from __future__ import annotations

__all__ = ["clauses", "entitlement", "packs"]
__version__ = "1.0.0"
