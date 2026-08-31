# DATA_DICTIONARY.md

**Authoritative.** Every table, column, type, domain and business meaning in the Parquet lake.
If the code and this document disagree, one of them is a bug — fix both in the same commit.

Conventions used throughout:

- All money is **SAR**, **monthly**, stored as `DECIMAL(12,2)`. Never annual.
- `period` is `INT32` in `YYYYMM` form (e.g. `202408`). 24 consecutive months per run.
- Dates are `DATE`; timestamps are `TIMESTAMP` (UTC, generation-time only, never a business date).
- `NULL` is meaningful and is used for realistic missingness. Columns marked **not null** never are.
- Layout: `data/raw/scale=<10k|100k|1m>/<table>/` as partitioned Parquet, 100k-row row-groups.
- Facts partition by `period`; `employee_master` and dimensions are single-partition.

---

## 1. Reference / dimension tables

### `dim_region` — 13 rows

Source: `policy/sites.yaml` → `regions`, plus `policy/geo/sa_regions.geojson`.

| Column | Type | Domain | Meaning |
|---|---|---|---|
| `region_code` | VARCHAR(5) | ISO 3166-2:SA, **PK** | `SA-01`…`SA-12`, `SA-14`. `SA-13` is unassigned and must not appear. |
| `region_name_en` | VARCHAR | not null | English region name. |
| `region_name_ar` | VARCHAR | not null | Arabic region name. |
| `centroid_lat` | DOUBLE | 16.0–32.5 | Label anchor for the map. |
| `centroid_lon` | DOUBLE | 34.0–56.0 | Label anchor for the map. |
| `site_count` | INT32 | ≥ 1 | Derived; every region carries at least one site by gate rule. |
| `headcount_weight_total` | DOUBLE | > 0 | Sum of site weights; the denominator for per-1,000 map metrics. |

### `dim_site` — 180 rows

Source: `policy/sites.yaml` → `sites`, with booleans resolved from `class_defaults`.

| Column | Type | Domain | Meaning |
|---|---|---|---|
| `site_id` | VARCHAR(12) | **PK** | e.g. `EP-HQ-DHA`. |
| `site_name_en` / `site_name_ar` | VARCHAR | not null | Bilingual name; the UI renders both. |
| `city` | VARCHAR | not null | Nearest city / locality. |
| `region_code` | VARCHAR(5) | **FK** → `dim_region` | |
| `latitude` / `longitude` | DOUBLE | inside the region polygon | Verified by the phase-0 gate. |
| `site_class` | ENUM | `hq`, `plant`, `refinery`, `offshore`, `drilling_camp`, `terminal`, `office`, `depot`, `training`, `medical`, `pump_station` | Drives eligibility for several allowances. |
| `hardship_tier` | INT8 | 0–3 | 0 = none. The whole basis of **A02**. |
| `remote_allowance_eligible` | BOOLEAN | | The whole basis of **A01**. |
| `offshore_eligible` | BOOLEAN | | Basis of **A03**. |
| `camp_available` | BOOLEAN | | Company messing/accommodation on site → interacts with `HOUSING`, `MEAL` (**A05**). |
| `family_housing_available` | BOOLEAN | | Family status can be posted here. |
| `rotation_supported` | BOOLEAN | | Basis of **A10**. |
| `headcount_weight` | DOUBLE | > 0 | Relative staffing weight; deliberately Eastern-Province-heavy. |

### `dim_org_unit` — ~12,000 rows

Five levels: Business Line → Admin Area → Division → Department → Section.

| Column | Type | Domain | Meaning |
|---|---|---|---|
| `org_unit_id` | VARCHAR(12) | **PK** | |
| `org_unit_name_en` / `_ar` | VARCHAR | not null | |
| `level` | INT8 | 1–5 | 1 = Business Line, 5 = Section. |
| `parent_org_unit_id` | VARCHAR(12) | FK → self, null at level 1 | Acyclic by construction. |
| `business_line` | VARCHAR | not null | Denormalised level-1 name, for fast grouping. |
| `cost_center` | VARCHAR(10) | not null, unique | Joins to payroll; basis of **C08**. |
| `head_employee_id` | VARCHAR(10) | FK → `employee_master`, nullable | The unit's manager. |
| `primary_site_id` | VARCHAR(12) | FK → `dim_site` | Where the unit is based. |

### `dim_job` — ~1,200 rows

| Column | Type | Domain | Meaning |
|---|---|---|---|
| `job_code` | VARCHAR(10) | **PK** | |
| `job_title_en` / `_ar` | VARCHAR | not null | |
| `job_family` | ENUM | Drilling, Reservoir, Process Ops, Maintenance, HSE, IT, Finance, HR, Procurement, Medical, Security | Also a peer-cohort key. |
| `min_grade` / `max_grade` | INT8 | 1–20, `min ≤ max` | Basis of **A08**. |
| `min_education` | ENUM | `secondary`, `diploma`, `bachelor`, `master`, `doctorate` | Basis of **A11**. |
| `required_certifications` | LIST\<VARCHAR\> | possibly empty | Basis of **A11**. |
| `safety_critical` | BOOLEAN | | Gates `ON_CALL`, `SAFETY`, `CERT_PREMIUM`; expired certs here are severe. |

### `dim_grade` — 60 rows (20 grades × 3 nationality classes)

Materialised from `policy/grade_bands.yaml`; the multiplier form is resolved at build time.

| Column | Type | Domain | Meaning |
|---|---|---|---|
| `grade` | INT8 | 1–20, **PK** part | |
| `nationality_class` | ENUM | `saudi`, `gcc`, `expat`, **PK** part | |
| `salary_min` / `salary_mid` / `salary_max` | DECIMAL(12,2) | monthly SAR | Basis of **B01**/**B02**. |
| `step_count` | INT8 | 12 | |
| `step_increment_pct` | DOUBLE | 2.4 | |
| `entitled_allowance_codes` | LIST\<VARCHAR\> | | Grade gate for **A07**. |
| `gosi_class` | ENUM | `saudi_full`, `gcc_bilateral`, `expat_hazards` | Basis of **A09**. |

### `dim_allowance` — 26 rows

Materialised from `policy/allowance_rules.yaml`.

| Column | Type | Domain | Meaning |
|---|---|---|---|
| `allowance_code` | VARCHAR(20) | **PK** | `HOUSING`, `REMOTE_SITE`, … |
| `name_en` / `name_ar` | VARCHAR | not null | Reviewer-facing label. |
| `amount_basis` | ENUM | `fixed`, `pct_of_base`, `grade_table`, `site_table` | How the amount is computed. |
| `amount` / `rate_pct` / `cap` | DECIMAL(12,2) / DOUBLE | nullable | Populated per basis. |
| `eligibility_rule_id` | VARCHAR(10) | nullable | Rule in `policy/rules/` that polices it. |
| `violation_codes` | LIST\<VARCHAR\> | | Anomaly codes this allowance can produce. |
| `regulatory_reference` | VARCHAR | not null | Cited verbatim in the evidence bundle. |
| `one_off` | BOOLEAN | default false | `SEVERANCE` only. |

### `dim_calendar` — 24 rows (one per period)

| Column | Type | Domain | Meaning |
|---|---|---|---|
| `period` | INT32 | `YYYYMM`, **PK** | |
| `year` / `month` | INT32 / INT8 | | |
| `hijri_year` / `hijri_month` | INT32 / INT8 | | Gregorian↔Hijri mapping. |
| `calendar_days` | INT8 | 28–31 | Ceiling for **D04**. |
| `working_days` | INT8 | | Net of weekends and holidays. |
| `public_holiday_days` | INT8 | | Saudi public holidays in the month. |
| `is_ramadan` | BOOLEAN | | Reduced statutory hours apply. |
| `ramadan_overlap_days` | INT8 | 0–30 | Partial-month Ramadan handling. |

---

## 2. Fact tables

### `employee_master` — 1 row per employee (~100 columns)

**PK** `employee_id`. This is the widest table and the join hub for everything else.

**Identity**

| Column | Type | Domain | Meaning |
|---|---|---|---|
| `employee_id` | VARCHAR(10) | **PK**, `E########` | |
| `badge_no` | VARCHAR(10) | unique | Physical badge; joins to activity data. |
| `name_en` / `name_ar` | VARCHAR | not null | Bilingual. Deliberately carries casing/spacing noise. |
| `name_en_normalised` | VARCHAR | not null | Upper, punctuation-stripped; the **C06** fuzzy-match key. |
| `gender` | ENUM | `M`, `F` | |
| `dob` | DATE | age 18–65 at hire | Component of the **C06** identity key. |
| `nationality` | VARCHAR(3) | ISO 3166-1 alpha-3 | |
| `nationality_class` | ENUM | `saudi`, `gcc`, `expat` | Drives bands, GOSI, eligibility. |
| `national_id` | VARCHAR(10) | Saudi only, else null | 10 digits starting `1`. Basis of **C02**. |
| `iqama_no` | VARCHAR(10) | expat/GCC only, else null | 10 digits starting `2`. Basis of **C02**/**C07**. |
| `iqama_expiry` | DATE | nullable | Basis of **C07**. |
| `passport_no` | VARCHAR(12) | nullable | |
| `passport_expiry` | DATE | nullable | |

**Personal**

| Column | Type | Domain | Meaning |
|---|---|---|---|
| `marital_status` | ENUM | `single`, `married`, `divorced`, `widowed` | |
| `dependents_count` | INT8 | 0–12 | |
| `dependents_in_kingdom` | INT8 | ≤ `dependents_count` | Basis of **A04**. |
| `spouse_employed_internally` | BOOLEAN | | Blocks duplicate `FAMILY`; a legitimate shared-IBAN confounder. |
| `spouse_employee_id` | VARCHAR(10) | nullable, FK → self | Set only when the above is true. |

**Qualification**

| Column | Type | Domain | Meaning |
|---|---|---|---|
| `education_level` | ENUM | `secondary`…`doctorate` | Compared to `dim_job.min_education` (**A11**). |
| `degree_field` | VARCHAR | nullable | |
| `institution` | VARCHAR | nullable | |
| `graduation_year` | INT32 | nullable, ≥ `year(dob)+17` | |
| `certifications` | LIST\<STRUCT\<code, issued, expiry\>\> | possibly empty | |
| `certifications_count` | INT8 | | Denormalised for fast rules. |
| `has_valid_required_certifications` | BOOLEAN | | Precomputed **A11** input. |
| `languages` | LIST\<VARCHAR\> | | |
| `languages_count` | INT8 | ≥ 1 | Gates `LANGUAGE`. |
| `training_hours_ytd` | INT32 | 0–400 | |

**Employment**

| Column | Type | Domain | Meaning |
|---|---|---|---|
| `hire_date` | DATE | not null | |
| `service_years` | DOUBLE | ≥ 0 | Derived at the run's reference date. |
| `service_band` | ENUM | `0-2`, `2-5`, `5-10`, `10-20`, `20+` | Peer-cohort key. |
| `employment_type` | ENUM | `direct`, `contractor`, `secondee`, `trainee` | |
| `contract_type` | ENUM | `permanent`, `fixed_term`, `temporary` | |
| `status` | ENUM | `active`, `terminated`, `suspended`, `on_leave` | Basis of **C04**. |
| `termination_date` | DATE | non-null iff `status = 'terminated'` | Basis of **C04**. |
| `termination_reason` | ENUM | `resignation`, `retirement`, `end_of_contract`, `dismissal`, nullable | |
| `probation_end` | DATE | nullable | |
| `rehire_flag` | BOOLEAN | | |

**Position**

| Column | Type | Domain | Meaning |
|---|---|---|---|
| `grade` | INT8 | 1–20 | |
| `pay_grade_step` | INT8 | 1–12 | |
| `job_code` | VARCHAR(10) | FK → `dim_job` | |
| `job_family` | VARCHAR | denormalised | Peer-cohort key. |
| `org_unit_id` | VARCHAR(12) | FK → `dim_org_unit` | |
| `cost_center` | VARCHAR(10) | denormalised | Must match payroll (**C08**). |
| `manager_id` | VARCHAR(10) | FK → self, nullable at the top | Acyclic in the clean set (**C05**). |
| `position_id` | VARCHAR(12) | unique among active | |
| `acting_role_flag` | BOOLEAN | | |
| `acting_role_since` | DATE | non-null iff flag | Basis of **A12**. |

**Location**

| Column | Type | Domain | Meaning |
|---|---|---|---|
| `work_site_id` | VARCHAR(12) | FK → `dim_site` | |
| `region_code` | VARCHAR(5) | denormalised | Present on every alert for region × month aggregation. |
| `residence_city` | VARCHAR | | |
| `work_pattern` | ENUM | `regular`, `rotation_28_28`, `rotation_14_14`, `shift`, `remote`, `hybrid` | Basis of **A10**. |
| `rotation_cycle_days` | INT8 | null unless rotational | |
| `housing_type` | ENUM | `company_camp_bachelor`, `company_family_housing`, `allowance`, `own` | Basis of **A05**. |
| `transport_mode` | ENUM | `company_bus`, `allowance`, `own` | Basis of **A06**. |
| `company_bus_route_id` | VARCHAR(8) | non-null iff `company_bus` | Evidence field for **A06**. |
| `remote_work_approved_flag` | BOOLEAN | | |
| `months_since_site_change` | INT16 | | Suppresses posting-lag false positives. |

**Compensation**

| Column | Type | Domain | Meaning |
|---|---|---|---|
| `base_salary` | DECIMAL(12,2) | within band in the clean set | Basis of **B01**/**B02**. |
| `currency` | VARCHAR(3) | `SAR` | |
| `last_increment_date` | DATE | nullable | Basis of **B07**. |
| `last_promotion_date` | DATE | nullable | Basis of **D01**. |
| `months_in_grade` | INT16 | | |
| `performance_rating_y1/_y2/_y3` | INT8 | 1–5, nullable for new hires | Basis of **B06**. |
| `bonus_eligible` | BOOLEAN | | |
| `gosi_class` | ENUM | `saudi_full`, `gcc_bilateral`, `expat_hazards` | Must agree with `nationality_class` (**A09**). |

**Banking**

| Column | Type | Domain | Meaning |
|---|---|---|---|
| `bank_code` | VARCHAR(4) | | |
| `iban` | VARCHAR(24) | `SA` + 22 digits, valid MOD-97 | Basis of **C01**. |
| `iban_effective_from` | DATE | | |
| `payment_method` | ENUM | `bank_transfer`, `cash`, `cheque` | Non-transfer at scale is itself suspicious. |
| `payroll_hold_flag` | BOOLEAN | | |

**Derived allowance flags** — 26 columns, `has_<CODE>` BOOLEAN, one per `dim_allowance.allowance_code`
(`has_HOUSING`, `has_REMOTE_SITE`, …). Plus `allowance_total_monthly` DECIMAL(12,2) and
`allowance_ratio` DOUBLE (= `allowance_total_monthly / base_salary`, basis of **B03**).

**Data quality**

| Column | Type | Meaning |
|---|---|---|
| `source_system` | ENUM `sap_hr`, `legacy_hr`, `manual` | Manual entries carry more noise, realistically. |
| `record_created_at` / `record_updated_at` | TIMESTAMP | Generation-time metadata only. |
| `dq_flags` | LIST\<VARCHAR\> | Injected data-quality issues (`name_casing`, `date_typo`, `missing_field`). Not anomalies. |

### `fact_payroll_monthly` — employee × 24 months

**PK** (`employee_id`, `period`). ~24M rows at 1m scale. Partitioned by `period`.

| Column | Type | Meaning |
|---|---|---|
| `employee_id` | VARCHAR(10) | FK → `employee_master` |
| `period` | INT32 | `YYYYMM` |
| `base_pay` | DECIMAL(12,2) | Monthly base actually paid. |
| `overtime_hours` | DOUBLE | Basis of **B05**/**D03**. |
| `overtime_pay` | DECIMAL(12,2) | Basis of **B05**. |
| `bonus` | DECIMAL(12,2) | Basis of **B06**. |
| `retro_adjustment` | DECIMAL(12,2) | Signed. Repeated positives → **D02**. |
| `gosi_employee` / `gosi_employer` | DECIMAL(12,2) | Rate follows `gosi_class`. |
| `loan_deduction` | DECIMAL(12,2) | |
| `absence_deduction` | DECIMAL(12,2) | |
| `allowance_total` | DECIMAL(12,2) | Sum of the child rows; kept for fast aggregates. |
| `gross` | DECIMAL(12,2) | `base_pay + allowance_total + overtime_pay + bonus + retro_adjustment` |
| `net` | DECIMAL(12,2) | `gross − gosi_employee − loan_deduction − absence_deduction` |
| `cost_center` | VARCHAR(10) | Must match the employee's org assignment (**C08**). |
| `payroll_run_id` | VARCHAR(16) | |
| `paid_flag` | BOOLEAN | False = posted but not disbursed. |

`gross` and `net` are stored, not computed on read — the arithmetic must be reproducible for an
auditor, and a mismatch between the parts and the total is itself a finding.

### `fact_payroll_allowance` — long format, the child of the above

**PK** (`employee_id`, `period`, `allowance_code`). Long format is deliberate (`docs/PLAN.md` §2.3):
adding an allowance code must never mean a schema migration. The wide view is built in DuckDB.

| Column | Type | Meaning |
|---|---|---|
| `employee_id`, `period` | | FK → `fact_payroll_monthly` |
| `allowance_code` | VARCHAR(20) | FK → `dim_allowance` |
| `amount` | DECIMAL(12,2) | > 0 |
| `amount_basis` | ENUM | Copied from the dimension at payment time. |
| `eligibility_snapshot_json` | VARCHAR | The field values eligibility was judged on, frozen at payment time — this is what lets an alert say *why it was payable then*. |

### `fact_assignment_history`

**PK** (`employee_id`, `effective_from`). Every grade/job/org/site change.

| Column | Type | Meaning |
|---|---|---|
| `employee_id` | VARCHAR(10) | |
| `effective_from` / `effective_to` | DATE | `effective_to` null = current. No gaps, no overlaps. |
| `grade`, `job_code`, `org_unit_id`, `work_site_id`, `manager_id` | | State during the interval. |
| `base_salary` | DECIMAL(12,2) | Salary at the start of the interval. |
| `change_reason` | ENUM | `hire`, `promotion`, `transfer`, `regrade`, `increment`, `acting`, `return_from_acting`, `termination` |
| `approved_by` | VARCHAR(10) | FK → `employee_master`. Self-approval is **C05**. |

**A salary change with no row here is B04** — that is the entire point of this table.

### `fact_attendance_monthly`

**PK** (`employee_id`, `period`).

| Column | Type | Meaning |
|---|---|---|
| `days_worked` | INT8 | ≤ `dim_calendar.calendar_days` (**D04**). |
| `days_leave` | INT8 | |
| `leave_type_breakdown` | MAP\<VARCHAR, INT8\> | `annual`, `sick`, `hajj`, `unpaid`, `emergency`. |
| `overtime_hours` | DOUBLE | Must reconcile with payroll (**D03**). |
| `absence_days` | INT8 | Unauthorised. |
| `rotation_cycle_id` | VARCHAR(12) | nullable. |
| `days_worked + days_leave + absence_days` | | ≤ `calendar_days`; a breach is **D04**. |

### `fact_bank_account`

**PK** (`employee_id`, `effective_from`). IBAN history with effective dating, so shared-account
detection works **over time** and not just on the current row.

| Column | Type | Meaning |
|---|---|---|
| `employee_id`, `effective_from`, `effective_to` | | |
| `iban`, `bank_code` | | |
| `change_reason` | ENUM | `initial`, `employee_request`, `bank_merger`, `correction` |
| `is_known_benign_share` | BOOLEAN | **Metadata only — never a feature.** Marks planted legitimate shares (e.g. spousal). Used solely by the eval harness to measure false-positive suppression. Leaking this into the detector invalidates the evaluation. |

### `fact_system_activity_monthly`

**PK** (`employee_id`, `period`). Light proxy signals that make ghost employees detectable.

| Column | Type | Meaning |
|---|---|---|
| `badge_swipes` | INT32 | 0 for a full period on an active employee is loud. |
| `email_count` | INT32 | |
| `erp_logins` | INT32 | |
| `vpn_sessions` | INT32 | |
| `activity_score` | DOUBLE | 0–1 normalised composite; the **C03** headline number. |

### `labels_anomaly` — ground truth

**PK** (`employee_id`, `anomaly_code`, `period_from`). Written by datagen pass 2 only.

| Column | Type | Meaning |
|---|---|---|
| `employee_id` | VARCHAR(10) | |
| `anomaly_code` | VARCHAR(3) | `A01`…`D07` |
| `family` | ENUM `A`,`B`,`C`,`D` | |
| `period_from` / `period_to` | INT32 | The window the anomaly is active. |
| `injected_severity` | ENUM | `CRITICAL`, `HIGH`, `MEDIUM` |
| `injection_params_json` | VARCHAR | Exact parameters used, so a case is reproducible. |
| `human_description` | VARCHAR | Plain English, for the eval report. |
| `work_site_id` / `region_code` | | Denormalised so region × month aggregation needs no join. |
| `expected_monthly_impact` | DECIMAL(12,2) | Ground-truth financial impact, for scoring the estimate. |

**This table is never an input to any detector.** It is read only by the eval harness. A detector
that reads `labels_anomaly` scores 100% and is worthless.

### `labels_confounder`

Same shape, `anomaly_code` replaced by `confounder_type`. Planted **legitimate** oddities — senior
specialists paid above the band, mid-year jumps *with* proper assignment records, spousal shared
IBANs. These must **not** be labelled anomalies and must **not** be scored CRITICAL. Measuring that
is the false-positive half of the evaluation.

---

## 3. `manifest.json`

Written to `data/raw/scale=<n>/manifest.json` on every generation. The eval harness and the phase
gates read it; it is what makes a run reproducible.

```json
{ "generator_version": "1.0.0", "seed": 42, "scale": "10k",
  "employee_count": 10000, "period_from": 202409, "period_to": 202608, "period_count": 24,
  "generated_at": "2026-08-31T09:14:22Z",
  "row_counts": { "employee_master": 10000, "fact_payroll_monthly": 240000, "...": 0 },
  "injection": { "target_anomaly_rate": 0.025, "employees_with_anomaly": 250,
                 "by_code": { "A01": 34, "A02": 21 },
                 "confounders": { "legit_high_earner": 40 } },
  "policy_digest": { "sites.yaml": "sha256:…", "allowance_rules.yaml": "sha256:…" } }
```

`policy_digest` matters: if a policy file changed but the data did not regenerate, the detector is
being evaluated against stale ground truth. The eval harness fails loudly on a digest mismatch.

---

## 4. Referential integrity (asserted by `verify 1`)

- Every FK resolves; no orphans in any fact table.
- `employee_master` × `dim_calendar` = exactly `fact_payroll_monthly` row count (no gaps, no dupes)
  for employees active in that period.
- `fact_assignment_history` intervals per employee are contiguous and non-overlapping, starting at
  `hire_date`.
- `manager_id` graph is acyclic; every `org_unit_id` chain terminates at a level-1 unit.
- Every IBAN passes MOD-97; every `national_id`/`iqama_no` passes its check digit.
- `gross` and `net` reconcile with their components to the stored cent.
- **Zero policy violations** in the clean set: every paid allowance satisfies its
  `policy/allowance_rules.yaml` eligibility clause. This is the phase-1 gate.
