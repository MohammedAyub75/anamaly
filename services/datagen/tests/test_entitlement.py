"""One case per allowance code: entitled pays, ineligible does not, amount is right.

`policycore.entitlement` is the single definition of who may receive what --
datagen pass 1 pays exactly what it returns, pass 2 breaks its clauses on
purpose, and the phase-3 rule engine reads the same clauses back to detect the
breaks.  These cases are what stop the three consumers drifting apart.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from policycore import entitlement as core
from policycore.packs import Site

# (code, grade, employee overrides, site overrides, expected SAR)
CASES: list[tuple[str, int, dict, dict, str]] = [
    ("HOUSING", 10, {"housing_type": "allowance"}, {}, "5000.00"),
    ("TRANSPORT", 10, {"transport_mode": "allowance"}, {}, "1500.00"),
    ("REMOTE_SITE", 10, {},
     {"remote_allowance_eligible": True, "site_class": "plant", "hardship_tier": 3},
     "3200.00"),
    ("HARDSHIP", 10, {}, {"hardship_tier": 1}, "800.00"),
    ("OFFSHORE", 10, {},
     {"site_class": "offshore", "offshore_eligible": True}, "4200.00"),
    ("ROTATION", 10, {"work_pattern": "rotation_28_28"},
     {"rotation_supported": True}, "1800.00"),
    ("SCHOOL_ASSIST", 10,
     {"marital_status": "married", "dependents_count": 2, "dependents_in_kingdom": 2},
     {}, "5000.00"),
    ("FAMILY", 10,
     {"marital_status": "married", "dependents_count": 1, "dependents_in_kingdom": 1},
     {}, "1200.00"),
    ("SHIFT", 10, {"work_pattern": "shift"}, {}, "3000.00"),
    ("ON_CALL", 10, {"job.safety_critical": True}, {}, "900.00"),
    ("CAR", 16, {}, {}, "4000.00"),
    ("FUEL", 10, {"transport_mode": "own"}, {}, "600.00"),
    ("MOBILE", 10, {}, {}, "250.00"),
    ("SAFETY", 10, {"job.safety_critical": True}, {"site_class": "plant"}, "400.00"),
    ("ACTING_ROLE", 10, {"acting_role_flag": True}, {}, "2400.00"),
    ("EXPAT_PREMIUM", 10, {"nationality_class": "expat"}, {}, "1600.00"),
    ("SAUDI_DEV_SCHEME", 10, {"nationality_class": "saudi", "service_years": 3.0}, {},
     "1500.00"),
    ("RELOCATION", 10, {"months_since_site_change": 3}, {}, "3500.00"),
    ("MEAL", 10, {}, {"camp_available": False}, "500.00"),
    ("FIELD_MESSING", 8, {}, {"site_class": "drilling_camp"}, "750.00"),
    ("UNIFORM", 8, {}, {"site_class": "plant"}, "200.00"),
    ("TRAVEL_TIME", 10, {"work_pattern": "rotation_14_14"},
     {"rotation_supported": True}, "650.00"),
    ("CERT_PREMIUM", 10,
     {"job.safety_critical": True, "has_valid_required_certifications": True}, {},
     "1100.00"),
    ("LANGUAGE", 10, {"languages_count": 2}, {}, "450.00"),
    ("SECURITY_CLEARANCE", 10, {}, {"site_class": "refinery"}, "800.00"),
]

# The field to spoil to make each case ineligible.
SPOILERS: dict[str, dict] = {
    "HOUSING": {"housing_type": "company_camp_bachelor"},
    "TRANSPORT": {"transport_mode": "own"},
    "REMOTE_SITE": {"site.remote_allowance_eligible": False},
    "HARDSHIP": {"site.hardship_tier": 0},
    "OFFSHORE": {"site.site_class": "plant"},
    "ROTATION": {"work_pattern": "regular"},
    "SCHOOL_ASSIST": {"dependents_in_kingdom": 0},
    "FAMILY": {"spouse_employed_internally": True},
    "SHIFT": {"work_pattern": "regular"},
    "ON_CALL": {"job.safety_critical": False},
    "CAR": {"grade": 13},
    "FUEL": {"transport_mode": "company_bus"},
    "SAFETY": {"job.safety_critical": False},
    "ACTING_ROLE": {"acting_role_flag": False},
    "EXPAT_PREMIUM": {"nationality_class": "saudi"},
    "SAUDI_DEV_SCHEME": {"service_years": 9.0},
    "RELOCATION": {"months_since_site_change": 999},
    "MEAL": {"site.camp_available": True},
    "FIELD_MESSING": {"site.site_class": "plant"},
    "UNIFORM": {"site.site_class": "hq"},
    "TRAVEL_TIME": {"work_pattern": "regular"},
    "CERT_PREMIUM": {"has_valid_required_certifications": False},
    "LANGUAGE": {"languages_count": 1},
    "SECURITY_CLEARANCE": {"site.site_class": "depot"},
}


def make_site(**overrides) -> Site:
    base = {
        "site_id": "TS-TST-001", "name_en": "Test Site", "name_ar": "موقع اختبار",
        "city": "Test", "region_code": "SA-04", "latitude": 26.0, "longitude": 50.0,
        "site_class": "plant", "hardship_tier": 0,
        "remote_allowance_eligible": False, "offshore_eligible": False,
        "camp_available": True, "family_housing_available": False,
        "rotation_supported": False, "headcount_weight": 1.0,
    }
    base.update(overrides)
    return Site(**base)


def make_row(grade: int = 10, **overrides) -> dict:
    """A row entitled to almost nothing, so each case turns on one clause."""
    site_overrides = {k[5:]: v for k, v in overrides.items() if k.startswith("site.")}
    row = {
        "grade": grade,
        "base_salary": Decimal("20000.00"),
        "housing_type": "own",
        "transport_mode": "company_bus",
        "work_pattern": "regular",
        "dependents_count": 0,
        "dependents_in_kingdom": 0,
        "marital_status": "single",
        "spouse_employed_internally": False,
        "nationality_class": "gcc",
        "service_years": 9.0,
        "acting_role_flag": False,
        "months_since_site_change": 999,
        "status": "active",
        "languages_count": 1,
        "has_valid_required_certifications": False,
        "job.safety_critical": False,
    }
    site = make_site(**site_overrides)
    for name in (
        "remote_allowance_eligible", "site_class", "hardship_tier",
        "offshore_eligible", "camp_available", "family_housing_available",
        "rotation_supported",
    ):
        row[f"site.{name}"] = getattr(site, name)
    row.update({k: v for k, v in overrides.items() if not k.startswith("site.")})
    return row


def paid(pack, row, include_one_off: bool = False) -> dict[str, Decimal]:
    return {
        e.code: e.amount for e in core.resolve(pack, row, include_one_off=include_one_off)
    }


@pytest.mark.parametrize("code,grade,employee,site,expected", CASES, ids=[c[0] for c in CASES])
def test_entitled_employee_is_paid_the_policy_amount(
    policy, code, grade, employee, site, expected
):
    row = make_row(grade, **employee, **{f"site.{k}": v for k, v in site.items()})
    amounts = paid(policy.pack, row)
    assert code in amounts, f"{code} should be payable: {sorted(amounts)}"
    assert amounts[code] == Decimal(expected)


@pytest.mark.parametrize("code,grade,employee,site,expected", CASES, ids=[c[0] for c in CASES])
def test_ineligible_employee_is_not_paid(policy, code, grade, employee, site, expected):
    if code not in SPOILERS:
        pytest.skip(f"{code} is universal; there is no clause to break")
    overrides = {**employee, **{f"site.{k}": v for k, v in site.items()}, **SPOILERS[code]}
    spoiled_grade = overrides.pop("grade", grade)
    row = make_row(spoiled_grade, **overrides)
    assert code not in paid(policy.pack, row)


def test_every_allowance_code_is_covered(policy):
    """A new code without a case here would be silently untested."""
    covered = {c[0] for c in CASES} | {"SEVERANCE"}
    assert covered == set(policy.pack.allowances)


def test_severance_is_one_off_and_excluded_from_monthly_pay(policy):
    """Paying it monthly would look exactly like C04, which is the point."""
    row = make_row(10, status="terminated", service_years=6.0)
    assert "SEVERANCE" not in paid(policy.pack, row)
    once = paid(policy.pack, row, include_one_off=True)
    assert once["SEVERANCE"] == Decimal("20000.00")


def test_grade_gate_blocks_an_otherwise_eligible_allowance(policy):
    """CAR is payable from grade 14 up; the clause and the gate must both hold."""
    assert "CAR" in paid(policy.pack, make_row(16))
    assert "CAR" not in paid(policy.pack, make_row(13))


def test_cap_is_applied_to_a_percentage_allowance(policy):
    """TRANSPORT is 10% of base capped at 1,500 SAR."""
    low = paid(policy.pack, make_row(10, base_salary=Decimal("9000.00"),
                                     transport_mode="allowance"))
    high = paid(policy.pack, make_row(10, base_salary=Decimal("40000.00"),
                                      transport_mode="allowance"))
    assert low["TRANSPORT"] == Decimal("900.00")
    assert high["TRANSPORT"] == Decimal("1500.00")


def test_per_dependent_allowance_is_capped_at_max_dependents(policy):
    """SCHOOL_ASSIST pays per resident dependent, up to three."""
    def amount(children: int) -> Decimal:
        row = make_row(10, marital_status="married", dependents_count=children,
                       dependents_in_kingdom=children)
        return paid(policy.pack, row).get("SCHOOL_ASSIST", Decimal(0))

    assert amount(1) == Decimal("2500.00")
    assert amount(3) == Decimal("7500.00")
    assert amount(6) == Decimal("7500.00")
    assert amount(0) == Decimal(0)


def test_site_table_zero_means_not_payable(policy):
    """A tier with a 0 in the site table is not a free allowance."""
    row = make_row(10, **{"site.remote_allowance_eligible": True,
                          "site.hardship_tier": 1})
    assert "REMOTE_SITE" not in paid(policy.pack, row)


def test_mutual_exclusion_company_bus_blocks_both_travel_allowances(policy):
    """A06: TRANSPORT and FUEL are both barred on a company bus route."""
    row = make_row(10, transport_mode="company_bus")
    amounts = paid(policy.pack, row)
    assert "TRANSPORT" not in amounts and "FUEL" not in amounts


def test_mutual_exclusion_keeps_only_one_messing_allowance(policy):
    """MEAL and FIELD_MESSING are both-present exclusive; the larger one wins."""
    row = make_row(8, **{"site.site_class": "drilling_camp", "site.camp_available": False})
    amounts = paid(policy.pack, row)
    assert "FIELD_MESSING" in amounts
    assert "MEAL" not in amounts


def test_remote_site_allowance_is_barred_at_head_office(policy):
    """A01, the flagship case: HQ is never a remote posting."""
    row = make_row(10, **{"site.remote_allowance_eligible": True,
                          "site.site_class": "hq", "site.hardship_tier": 3})
    assert "REMOTE_SITE" not in paid(policy.pack, row)


def test_family_allowance_is_never_paid_to_both_spouses(policy):
    row = make_row(10, marital_status="married", dependents_count=1,
                   dependents_in_kingdom=1, spouse_employed_internally=True)
    assert "FAMILY" not in paid(policy.pack, row)


def test_a_null_field_never_satisfies_a_clause(policy):
    """Missingness is a data-quality issue, not grounds for an entitlement."""
    row = make_row(10, housing_type=None)
    assert "HOUSING" not in paid(policy.pack, row)
