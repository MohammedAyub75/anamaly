"""Anomaly injection -- **phase 2 owns this package**.

Pass 2 breaks specific eligibility clauses on purpose and records precisely what
it broke in `labels_anomaly`, plus the planted legitimate oddities in
`labels_confounder`.  Nothing here yet, deliberately: phase 1's only job is a
population where every paid allowance satisfies its clause, and an injector
present but half-wired would be the easiest possible way to leak an unlabelled
violation into the clean set.

See `docs/ANOMALY_CATALOG.md` for the 34 codes and the `add-anomaly-rule` skill
for the pattern each injector follows.
"""

from __future__ import annotations

__all__: list[str] = []
