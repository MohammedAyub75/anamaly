"""`dim_grade` -- the 20 x 3 salary band table, materialised.

`policy/grade_bands.yaml` states the base band once per grade and a multiplier
per nationality class. The multiplier form is DRY but useless to a reviewer and
awkward for a detector, so the 60 resolved rows are written to the lake and the
Policy Explorer renders those.
"""

from __future__ import annotations

from typing import Any

from policycore.packs import NATIONALITY_CLASSES

from ..policy import DatagenPolicy


def build(policy: DatagenPolicy) -> dict[str, Any]:
    bands = [
        policy.pack.grade_bands[(grade, klass)]
        for grade in range(1, 21)
        for klass in NATIONALITY_CLASSES
    ]
    return {
        "grade": [b.grade for b in bands],
        "nationality_class": [b.nationality_class for b in bands],
        "salary_min": [int(b.salary_min * 100) for b in bands],
        "salary_mid": [int(b.salary_mid * 100) for b in bands],
        "salary_max": [int(b.salary_max * 100) for b in bands],
        "step_count": [b.step_count for b in bands],
        "step_increment_pct": [b.step_increment_pct for b in bands],
        "entitled_allowance_codes": [list(b.entitled_allowance_codes) for b in bands],
        "gosi_class": [b.gosi_class for b in bands],
    }
