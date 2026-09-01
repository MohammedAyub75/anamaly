"""`policy/runtime.yaml` -- the engineering pack, shared by both services.

It sits beside the nine policy packs and is deliberately *not* one of them:
`policycore.packs.POLICY_FILES` decides what goes into `policy_digest`, and the
rule for membership is whether the file changes what the system *says*. A memory
limit, a thread count and a chunk size change only what it *costs*, so editing
this file must not invalidate a lake -- otherwise tuning a batch would mean
regenerating twenty-four million payroll rows.

Loaded here rather than in either service because both spend the same machine:
the generator's pass 2 and the detector's feature build are the two places a
1m run can run out of memory, and one file naming both budgets is easier to
reason about than two.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

RUNTIME_FILE = "runtime.yaml"


def load(root: str | Path = "policy") -> dict[str, Any]:
    """The runtime pack, or an empty one.

    Missing is not an error: every reader supplies its own default, because a
    checkout without the file must still run -- just not necessarily inside a
    1m memory budget.
    """
    path = Path(root) / RUNTIME_FILE
    if not path.exists():
        return {}
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def section(runtime: dict[str, Any], *names: str) -> dict[str, Any]:
    """One nested section of the pack, e.g. `section(rt, "datagen", "injection")`."""
    current: Any = runtime
    for name in names:
        current = (current or {}).get(name) or {}
    return dict(current)
