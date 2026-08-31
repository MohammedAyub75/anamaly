"""`dim_region` -- the 13 administrative regions, with the map's denominators.

`headcount_weight_total` is the reason this table carries derived columns at
all: every map metric defaults to alerts per 1,000 employees, and without a
per-region denominator on the dimension the UI would have to recompute it on
every request.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..policy import DatagenPolicy


def build(policy: DatagenPolicy) -> dict[str, Any]:
    pack = policy.pack
    site_count = Counter(s.region_code for s in pack.sites)
    weight_total: Counter[str] = Counter()
    for site in pack.sites:
        weight_total[site.region_code] += site.headcount_weight

    regions = sorted(pack.regions, key=lambda r: r["code"])
    return {
        "region_code": [r["code"] for r in regions],
        "region_name_en": [r["name_en"] for r in regions],
        "region_name_ar": [r["name_ar"] for r in regions],
        "centroid_lat": [float(r["centroid_lat"]) for r in regions],
        "centroid_lon": [float(r["centroid_lon"]) for r in regions],
        "site_count": [int(site_count[r["code"]]) for r in regions],
        "headcount_weight_total": [float(weight_total[r["code"]]) for r in regions],
    }
