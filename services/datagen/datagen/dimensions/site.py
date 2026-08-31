"""`dim_site` -- 180 sites with every boolean resolved.

The `class_defaults` inheritance in `policy/sites.yaml` is resolved once, in
`policycore`, and materialised here. Reviewers never see the inheritance: the
Policy Explorer renders resolved values, and the detector compares against
resolved values, so the lake stores resolved values.
"""

from __future__ import annotations

from typing import Any

from ..policy import DatagenPolicy


def build(policy: DatagenPolicy) -> dict[str, Any]:
    sites = policy.pack.sites
    return {
        "site_id": [s.site_id for s in sites],
        "site_name_en": [s.name_en for s in sites],
        "site_name_ar": [s.name_ar for s in sites],
        "city": [s.city for s in sites],
        "region_code": [s.region_code for s in sites],
        "latitude": [s.latitude for s in sites],
        "longitude": [s.longitude for s in sites],
        "site_class": [s.site_class for s in sites],
        "hardship_tier": [s.hardship_tier for s in sites],
        "remote_allowance_eligible": [s.remote_allowance_eligible for s in sites],
        "offshore_eligible": [s.offshore_eligible for s in sites],
        "camp_available": [s.camp_available for s in sites],
        "family_housing_available": [s.family_housing_available for s in sites],
        "rotation_supported": [s.rotation_supported for s in sites],
        "headcount_weight": [s.headcount_weight for s in sites],
    }
