"""Make the services importable from a bare checkout.

The generator lives in `services/datagen`, the detector in
`services/detector`, and the shared policy core at the repo root.  None of them
is pip-installed during a phase build -- `python tasks.py` puts them all on the
path the same way -- so pytest does it here rather than in each test file.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

for path in (ROOT, ROOT / "services" / "datagen", ROOT / "services" / "detector"):
    entry = str(path)
    if entry not in sys.path:
        sys.path.insert(0, entry)
