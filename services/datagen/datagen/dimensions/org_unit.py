"""`dim_org_unit` -- a five-level hierarchy, acyclic by construction.

Acyclicity is not asserted after the fact, it is made impossible: a unit's
parent is only ever drawn from the level above, so every chain terminates at a
level-1 business line.  That matters because C05 detects manager-hierarchy
cycles, and a cycle that the *generator* created would be an unlabelled anomaly.

Every level-5 section carries a `primary_site_id`. The first sections are handed
out one per site so no site is left without a unit to staff it, and the rest are
weighted by `headcount_weight` -- which is what actually produces the
Eastern-Province-heavy distribution the map has to normalise away.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..config import ScaleConfig
from ..policy import DatagenPolicy
from ..rng import StreamRegistry, weighted_index

# Share of all units at each level. Level 1 is always the business-line list.
LEVEL_SHARES = (0.019, 0.075, 0.300, 0.600)

LEVEL_NAMES_EN = {1: "Business Line", 2: "Admin Area", 3: "Division",
                  4: "Department", 5: "Section"}
LEVEL_NAMES_AR = {1: "خط أعمال", 2: "منطقة إدارية", 3: "إدارة",
                  4: "قسم", 5: "شعبة"}

BUSINESS_LINE_AR = {
    "Upstream Operations": "عمليات المنبع",
    "Downstream Manufacturing": "التصنيع والمصب",
    "Gas Operations": "عمليات الغاز",
    "Engineering and Project Management": "الهندسة وإدارة المشاريع",
    "Technical Services": "الخدمات الفنية",
    "Corporate Affairs": "الشؤون المؤسسية",
    "Finance and Commercial": "المالية والتجارية",
    "Human Resources and Support": "الموارد البشرية والدعم",
}


def _level_counts(target: int, level_one: int) -> list[int]:
    """Unit count per level, never fewer than the level above it has parents."""
    counts = [level_one]
    for share in LEVEL_SHARES:
        counts.append(max(counts[-1], round(target * share)))
    return counts


def build(cfg: ScaleConfig, policy: DatagenPolicy, streams: StreamRegistry) -> dict[str, Any]:
    table = streams.table("dim_org_unit")
    lines = list(policy.population["business_lines"])
    counts = _level_counts(cfg.org_units, len(lines))

    ids: list[str] = []
    names_en: list[str] = []
    names_ar: list[str] = []
    levels: list[int] = []
    parents: list[str | None] = []
    business: list[str] = []
    cost_centers: list[str] = []
    sites: list[str] = []

    site_ids = policy.site_ids
    site_rng = table.field("primary_site")
    # One section per site first, so every site can be staffed; the remainder
    # follows the headcount weights.
    section_total = counts[4]
    seeded = min(len(site_ids), section_total)
    weighted = weighted_index(site_rng, policy.site_weights, max(0, section_total - seeded))
    section_sites = [site_ids[i] for i in range(seeded)] + [site_ids[i] for i in weighted]
    # Levels 1-4 are administrative; their site is only a label, but it is
    # still a foreign key, so it is drawn from the same weights.
    upper_sites = [
        site_ids[i]
        for i in weighted_index(table.field("upper_site"), policy.site_weights,
                                sum(counts[:4]))
    ]

    level_start: list[int] = []
    serial = 0
    for level in range(1, 6):
        level_start.append(len(ids))
        count = counts[level - 1]
        parent_start = level_start[level - 2] if level > 1 else 0
        parent_count = counts[level - 2] if level > 1 else 0
        for index in range(count):
            serial += 1
            ids.append(f"OU{serial:06d}")
            levels.append(level)
            cost_centers.append(f"CC{serial:06d}")
            if level == 1:
                line = lines[index]
                parents.append(None)
                names_en.append(line)
                names_ar.append(BUSINESS_LINE_AR[line])
                business.append(line)
                # A business line sits wherever its largest site is; the value
                # is only a label for levels 1-4, since employees hang off
                # sections.
                sites.append(upper_sites[len(sites)])
            else:
                # Round-robin against the level above: deterministic, balanced,
                # and it guarantees every parent has at least one child when
                # the counts allow it.
                parent_offset = parent_start + index % parent_count
                parents.append(ids[parent_offset])
                line = business[parent_offset]
                business.append(line)
                names_en.append(f"{line} {LEVEL_NAMES_EN[level]} {index + 1:03d}")
                names_ar.append(
                    f"{BUSINESS_LINE_AR[line]} - {LEVEL_NAMES_AR[level]} {index + 1:03d}"
                )
                sites.append(
                    section_sites[index] if level == 5 else upper_sites[len(sites)]
                )

    return {
        "org_unit_id": ids,
        "org_unit_name_en": names_en,
        "org_unit_name_ar": names_ar,
        "level": levels,
        "parent_org_unit_id": parents,
        "business_line": business,
        "cost_center": cost_centers,
        # Backfilled by the pipeline once employees exist -- this column is the
        # one forward reference in the dimension layer.
        "head_employee_id": [None] * len(ids),
        "primary_site_id": sites,
    }


def sections(table: dict[str, Any]) -> np.ndarray:
    """Row positions of the level-5 sections, which is where employees hang."""
    return np.flatnonzero(np.asarray(table["level"]) == 5)
