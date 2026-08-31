"""Arrow schemas for every table, transcribed from `docs/DATA_DICTIONARY.md`.

The dictionary is the contract; this module is that contract in a form the
writer can enforce and the phase-1 gate can compare against.  Declaring the
schema up front rather than letting a DataFrame infer one is what makes
DECIMAL(12,2), DATE, the certification struct list and the leave MAP come out
of the writer exactly as documented, at every scale, on every platform.

Column order here is the column order in the dictionary, and the gate asserts
the written Parquet matches it -- names, order and types.
"""

from __future__ import annotations

import pyarrow as pa

MONEY = pa.decimal128(12, 2)
DATE = pa.date32()
TS = pa.timestamp("us", tz="UTC")

# Ordered so `has_<CODE>` columns land in a stable place regardless of YAML
# iteration order. Kept here (not derived from the pack) so a policy edit that
# renames a code fails the schema gate loudly instead of silently reshaping the
# widest table in the lake.
ALLOWANCE_CODES: tuple[str, ...] = (
    "ACTING_ROLE", "CAR", "CERT_PREMIUM", "EXPAT_PREMIUM", "FAMILY",
    "FIELD_MESSING", "FUEL", "HARDSHIP", "HOUSING", "LANGUAGE", "MEAL",
    "MOBILE", "OFFSHORE", "ON_CALL", "RELOCATION", "REMOTE_SITE", "ROTATION",
    "SAFETY", "SAUDI_DEV_SCHEME", "SCHOOL_ASSIST", "SECURITY_CLEARANCE",
    "SEVERANCE", "SHIFT", "TRANSPORT", "TRAVEL_TIME", "UNIFORM",
)

CERTIFICATION = pa.struct(
    [("code", pa.string()), ("issued", DATE), ("expiry", DATE)]
)

DIM_REGION = pa.schema(
    [
        ("region_code", pa.string()),
        ("region_name_en", pa.string()),
        ("region_name_ar", pa.string()),
        ("centroid_lat", pa.float64()),
        ("centroid_lon", pa.float64()),
        ("site_count", pa.int32()),
        ("headcount_weight_total", pa.float64()),
    ]
)

DIM_SITE = pa.schema(
    [
        ("site_id", pa.string()),
        ("site_name_en", pa.string()),
        ("site_name_ar", pa.string()),
        ("city", pa.string()),
        ("region_code", pa.string()),
        ("latitude", pa.float64()),
        ("longitude", pa.float64()),
        ("site_class", pa.string()),
        ("hardship_tier", pa.int8()),
        ("remote_allowance_eligible", pa.bool_()),
        ("offshore_eligible", pa.bool_()),
        ("camp_available", pa.bool_()),
        ("family_housing_available", pa.bool_()),
        ("rotation_supported", pa.bool_()),
        ("headcount_weight", pa.float64()),
    ]
)

DIM_ORG_UNIT = pa.schema(
    [
        ("org_unit_id", pa.string()),
        ("org_unit_name_en", pa.string()),
        ("org_unit_name_ar", pa.string()),
        ("level", pa.int8()),
        ("parent_org_unit_id", pa.string()),
        ("business_line", pa.string()),
        ("cost_center", pa.string()),
        ("head_employee_id", pa.string()),
        ("primary_site_id", pa.string()),
    ]
)

DIM_JOB = pa.schema(
    [
        ("job_code", pa.string()),
        ("job_title_en", pa.string()),
        ("job_title_ar", pa.string()),
        ("job_family", pa.string()),
        ("min_grade", pa.int8()),
        ("max_grade", pa.int8()),
        ("min_education", pa.string()),
        ("required_certifications", pa.list_(pa.string())),
        ("safety_critical", pa.bool_()),
    ]
)

DIM_GRADE = pa.schema(
    [
        ("grade", pa.int8()),
        ("nationality_class", pa.string()),
        ("salary_min", MONEY),
        ("salary_mid", MONEY),
        ("salary_max", MONEY),
        ("step_count", pa.int8()),
        ("step_increment_pct", pa.float64()),
        ("entitled_allowance_codes", pa.list_(pa.string())),
        ("gosi_class", pa.string()),
    ]
)

DIM_ALLOWANCE = pa.schema(
    [
        ("allowance_code", pa.string()),
        ("name_en", pa.string()),
        ("name_ar", pa.string()),
        ("amount_basis", pa.string()),
        ("amount", MONEY),
        ("rate_pct", pa.float64()),
        ("cap", MONEY),
        ("eligibility_rule_id", pa.string()),
        ("violation_codes", pa.list_(pa.string())),
        ("regulatory_reference", pa.string()),
        ("one_off", pa.bool_()),
    ]
)

DIM_CALENDAR = pa.schema(
    [
        ("period", pa.int32()),
        ("year", pa.int32()),
        ("month", pa.int8()),
        ("hijri_year", pa.int32()),
        ("hijri_month", pa.int8()),
        ("calendar_days", pa.int8()),
        ("working_days", pa.int8()),
        ("public_holiday_days", pa.int8()),
        ("is_ramadan", pa.bool_()),
        ("ramadan_overlap_days", pa.int8()),
    ]
)

EMPLOYEE_MASTER = pa.schema(
    [
        # identity
        ("employee_id", pa.string()),
        ("badge_no", pa.string()),
        ("name_en", pa.string()),
        ("name_ar", pa.string()),
        ("name_en_normalised", pa.string()),
        ("gender", pa.string()),
        ("dob", DATE),
        ("nationality", pa.string()),
        ("nationality_class", pa.string()),
        ("national_id", pa.string()),
        ("iqama_no", pa.string()),
        ("iqama_expiry", DATE),
        ("passport_no", pa.string()),
        ("passport_expiry", DATE),
        # personal
        ("marital_status", pa.string()),
        ("dependents_count", pa.int8()),
        ("dependents_in_kingdom", pa.int8()),
        ("spouse_employed_internally", pa.bool_()),
        ("spouse_employee_id", pa.string()),
        # qualification
        ("education_level", pa.string()),
        ("degree_field", pa.string()),
        ("institution", pa.string()),
        ("graduation_year", pa.int32()),
        ("certifications", pa.list_(CERTIFICATION)),
        ("certifications_count", pa.int8()),
        ("has_valid_required_certifications", pa.bool_()),
        ("languages", pa.list_(pa.string())),
        ("languages_count", pa.int8()),
        ("training_hours_ytd", pa.int32()),
        # employment
        ("hire_date", DATE),
        ("service_years", pa.float64()),
        ("service_band", pa.string()),
        ("employment_type", pa.string()),
        ("contract_type", pa.string()),
        ("status", pa.string()),
        ("termination_date", DATE),
        ("termination_reason", pa.string()),
        ("probation_end", DATE),
        ("rehire_flag", pa.bool_()),
        # position
        ("grade", pa.int8()),
        ("pay_grade_step", pa.int8()),
        ("job_code", pa.string()),
        ("job_family", pa.string()),
        ("org_unit_id", pa.string()),
        ("cost_center", pa.string()),
        ("manager_id", pa.string()),
        ("position_id", pa.string()),
        ("acting_role_flag", pa.bool_()),
        ("acting_role_since", DATE),
        # location
        ("work_site_id", pa.string()),
        ("region_code", pa.string()),
        ("residence_city", pa.string()),
        ("work_pattern", pa.string()),
        ("rotation_cycle_days", pa.int8()),
        ("housing_type", pa.string()),
        ("transport_mode", pa.string()),
        ("company_bus_route_id", pa.string()),
        ("remote_work_approved_flag", pa.bool_()),
        ("months_since_site_change", pa.int16()),
        # compensation
        ("base_salary", MONEY),
        ("currency", pa.string()),
        ("last_increment_date", DATE),
        ("last_promotion_date", DATE),
        ("months_in_grade", pa.int16()),
        ("performance_rating_y1", pa.int8()),
        ("performance_rating_y2", pa.int8()),
        ("performance_rating_y3", pa.int8()),
        ("bonus_eligible", pa.bool_()),
        ("gosi_class", pa.string()),
        # banking
        ("bank_code", pa.string()),
        ("iban", pa.string()),
        ("iban_effective_from", DATE),
        ("payment_method", pa.string()),
        ("payroll_hold_flag", pa.bool_()),
    ]
    # derived allowance flags
    + [(f"has_{code}", pa.bool_()) for code in ALLOWANCE_CODES]
    + [
        ("allowance_total_monthly", MONEY),
        ("allowance_ratio", pa.float64()),
        # data quality
        ("source_system", pa.string()),
        ("record_created_at", TS),
        ("record_updated_at", TS),
        ("dq_flags", pa.list_(pa.string())),
    ]
)

FACT_PAYROLL_MONTHLY = pa.schema(
    [
        ("employee_id", pa.string()),
        ("period", pa.int32()),
        ("base_pay", MONEY),
        ("overtime_hours", pa.float64()),
        ("overtime_pay", MONEY),
        ("bonus", MONEY),
        ("retro_adjustment", MONEY),
        ("gosi_employee", MONEY),
        ("gosi_employer", MONEY),
        ("loan_deduction", MONEY),
        ("absence_deduction", MONEY),
        ("allowance_total", MONEY),
        ("gross", MONEY),
        ("net", MONEY),
        ("cost_center", pa.string()),
        ("payroll_run_id", pa.string()),
        ("paid_flag", pa.bool_()),
    ]
)

FACT_PAYROLL_ALLOWANCE = pa.schema(
    [
        ("employee_id", pa.string()),
        ("period", pa.int32()),
        ("allowance_code", pa.string()),
        ("amount", MONEY),
        ("amount_basis", pa.string()),
        ("eligibility_snapshot_json", pa.string()),
    ]
)

FACT_ASSIGNMENT_HISTORY = pa.schema(
    [
        ("employee_id", pa.string()),
        ("effective_from", DATE),
        ("effective_to", DATE),
        ("grade", pa.int8()),
        ("job_code", pa.string()),
        ("org_unit_id", pa.string()),
        ("work_site_id", pa.string()),
        ("manager_id", pa.string()),
        ("base_salary", MONEY),
        ("change_reason", pa.string()),
        ("approved_by", pa.string()),
    ]
)

FACT_ATTENDANCE_MONTHLY = pa.schema(
    [
        ("employee_id", pa.string()),
        ("period", pa.int32()),
        ("days_worked", pa.int8()),
        ("days_leave", pa.int8()),
        ("leave_type_breakdown", pa.map_(pa.string(), pa.int8())),
        ("overtime_hours", pa.float64()),
        ("absence_days", pa.int8()),
        ("rotation_cycle_id", pa.string()),
    ]
)

FACT_BANK_ACCOUNT = pa.schema(
    [
        ("employee_id", pa.string()),
        ("effective_from", DATE),
        ("effective_to", DATE),
        ("iban", pa.string()),
        ("bank_code", pa.string()),
        ("change_reason", pa.string()),
        ("is_known_benign_share", pa.bool_()),
    ]
)

FACT_SYSTEM_ACTIVITY_MONTHLY = pa.schema(
    [
        ("employee_id", pa.string()),
        ("period", pa.int32()),
        ("badge_swipes", pa.int32()),
        ("email_count", pa.int32()),
        ("erp_logins", pa.int32()),
        ("vpn_sessions", pa.int32()),
        ("activity_score", pa.float64()),
    ]
)

SCHEMAS: dict[str, pa.Schema] = {
    "dim_region": DIM_REGION,
    "dim_site": DIM_SITE,
    "dim_org_unit": DIM_ORG_UNIT,
    "dim_job": DIM_JOB,
    "dim_grade": DIM_GRADE,
    "dim_allowance": DIM_ALLOWANCE,
    "dim_calendar": DIM_CALENDAR,
    "employee_master": EMPLOYEE_MASTER,
    "fact_payroll_monthly": FACT_PAYROLL_MONTHLY,
    "fact_payroll_allowance": FACT_PAYROLL_ALLOWANCE,
    "fact_assignment_history": FACT_ASSIGNMENT_HISTORY,
    "fact_attendance_monthly": FACT_ATTENDANCE_MONTHLY,
    "fact_bank_account": FACT_BANK_ACCOUNT,
    "fact_system_activity_monthly": FACT_SYSTEM_ACTIVITY_MONTHLY,
}
