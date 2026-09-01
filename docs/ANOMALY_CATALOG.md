# ANOMALY_CATALOG.md

**The single most important file in the repo.** Every anomaly code is defined once, here, with its
injection logic and its detection logic side by side. A code whose injector and detector drift apart
produces a silent 0% recall row in the eval report, which is the most expensive kind of bug in this
project.

**34 codes** across four families (A: 12, B: 7, C: 8, D: 7). `docs/PLAN.md` says "~30"; the worked
catalogue came out at 34. The number in the plan was indicative — 34 is the contract.

## Rules of engagement

- **Every code needs three things**: an injector in `services/datagen`, a detector (a rule, a
  statistic, or an ML signal), and a row in the eval report. Adding one without the others is what
  the `add-anomaly-rule` skill exists to prevent.
- **`labels_anomaly` is never a detector input.** It is read only by the eval harness.
- **Injection rate floor.** Rates below are percentages of the population, but every code is
  injected at least **5 times at any scale**. At 10k, 0.01% would be one employee, and recall
  measured on n=1 is noise, not a signal.
- **Confounders are unlabelled by design** (§5). They are legitimate and must not be flagged
  CRITICAL. `labels_confounder.is_known_benign_share` and friends exist for the eval harness only.
- **Detector column** names the layer that is expected to catch it: `L1` declarative rule,
  `L2` peer statistics, `L3` unsupervised ML / graph, `L4` fusion-only.

Total injected rate ≈ **2.75%** of employees across all codes; with ~10% of flagged employees
carrying more than one code, ≈ **2.5%** of the population carries ≥ 1 anomaly, per `docs/PLAN.md` §2.4.

---

## Family A — Entitlement / policy violations

Deterministic ground truth. These are **facts**, not probabilities: a clause in
`policy/allowance_rules.yaml` is broken and the exact clause can be cited to the reviewer. Family A
is expected at **100% precision and 100% recall** (phase-3 gate).

### A01 — Remote-site allowance at an ineligible site
**Severity** HIGH · **Rate** 0.22% · **Detector** L1 · **Rule** `policy/rules/A01_*.yaml`
*The flagship case: an employee posted to Dhahran HQ drawing a remote-site allowance.*
- **Injection**: pick employees at sites where `remote_allowance_eligible = false` or
  `site_class ∈ {hq, office, medical, training}`; pay `REMOTE_SITE` at the tier-2/3 table rate for a
  window of 6–24 months.
- **Detection**: `allowance_REMOTE_SITE_amount > 0 AND NOT site_remote_allowance_eligible`.
- **Evidence**: `work_site_id`, `site_name_en`, `site_class`, `site_remote_allowance_eligible`,
  monthly amount, `first_period_paid`, `months_paid`.
- **Actions**: suspend the allowance; confirm posting with Division HR; raise a recovery case.

### A02 — Hardship allowance at a tier-0 site
**Severity** HIGH · **Rate** 0.18% · **Detector** L1
- **Injection**: pay `HARDSHIP` to employees whose site has `hardship_tier = 0`.
- **Detection**: `allowance_HARDSHIP_amount > 0 AND site_hardship_tier = 0`.
- **Evidence**: site id/name, `hardship_tier`, amount, months paid.
- **Actions**: suspend; verify the site's hardship classification; recover.

### A03 — Offshore allowance for an onshore assignment
**Severity** HIGH · **Rate** 0.04% · **Detector** L1
- **Injection**: pay `OFFSHORE` where `site_class ≠ 'offshore'`.
- **Detection**: `allowance_OFFSHORE_amount > 0 AND site_class <> 'offshore'`.
- **Evidence**: site class, `offshore_eligible`, amount, months.
- **Actions**: suspend; confirm assignment records; recover.

### A04 — School/family assistance without qualifying dependents
**Severity** MEDIUM · **Rate** 0.15% · **Detector** L1
- **Injection**: pay `SCHOOL_ASSIST`/`FAMILY` where `dependents_count = 0` or
  `dependents_in_kingdom = 0`.
- **Detection**: allowance present AND (`dependents_count = 0` OR `dependents_in_kingdom = 0`).
- **Evidence**: dependents counts, marital status, amount, months.
- **Actions**: request updated dependent declaration; suspend pending proof; recover if unproven.

### A05 — Housing allowance while company-housed
**Severity** HIGH · **Rate** 0.14% · **Detector** L1
- **Injection**: pay `HOUSING` where `housing_type ∈ {company_camp_bachelor, company_family_housing}`.
- **Detection**: mutual-exclusion clause in `allowance_rules.yaml`.
- **Evidence**: `housing_type`, site `camp_available`, amount (25% of base — usually large), months.
- **Actions**: suspend; confirm accommodation assignment; recover — high financial impact.

### A06 — Transport allowance while on a company bus route
**Severity** MEDIUM · **Rate** 0.16% · **Detector** L1
- **Injection**: pay `TRANSPORT`/`FUEL` where `transport_mode = 'company_bus'`.
- **Detection**: mutual-exclusion clause; `company_bus_route_id` is the corroborating evidence.
- **Evidence**: `transport_mode`, `company_bus_route_id`, amount, months.
- **Actions**: suspend; confirm route assignment; recover.

### A07 — Allowance amount outside the policy table
**Severity** MEDIUM · **Rate** 0.11% · **Detector** L1
- **Injection**: pay a legitimately-entitled allowance at 1.3×–2.5× the policy amount for that
  grade/site.
- **Detection**: recompute the entitled amount from `amount_basis` and compare; tolerance 1 SAR.
  **Only where the recomputed amount is above zero.** An allowance the employee has no claim to at
  all recomputes to zero, and that is an eligibility breach with its own code (A01-A06, A09, A10);
  counting it here as well would put two codes on one row and split its recall between them.
- **Evidence**: expected vs actual amount, the basis used, grade, site tier.
- **Actions**: correct to the policy amount; recover the difference; check for a payroll-master
  override.

### A08 — Grade outside the job code's permitted band
**Severity** HIGH · **Rate** 0.07% · **Detector** L1
- **Injection**: move the **job code**, not the grade — a different post in the same job family,
  with the same `safety_critical` status and no higher education minimum, whose permitted band
  excludes the grade held. The equivalent mismatch, and the only form that leaves the salary band
  and the entitlement set untouched: moving the grade instead would drag the band (B01/B02) and the
  `grade_entitlements` gate along with it.
- **Detection**: direct comparison against `dim_job`, over `fact_assignment_history`.
- **Evidence**: grade, job code and title, permitted band, salary implication.
- **Actions**: confirm with Compensation; regrade or reassign the job code.

### A09 — Nationality-restricted benefit paid to an ineligible class
**Severity** HIGH · **Rate** 0.06% · **Detector** L1
- **Injection**: pay `EXPAT_PREMIUM` to a Saudi national, or `SAUDI_DEV_SCHEME` to an expat; or set
  `gosi_class` inconsistent with `nationality_class`.
- **Detection**: eligibility clause plus a `gosi_class` × `nationality_class` cross-check.
- **Evidence**: nationality class, GOSI class, the allowance, amount.
- **Actions**: suspend; correct the GOSI registration; recover — has a regulatory dimension.

### A10 — Rotation allowance without a rotation work pattern
**Severity** MEDIUM · **Rate** 0.08% · **Detector** L1
- **Injection**: pay `ROTATION`/`TRAVEL_TIME` where `work_pattern ∉ {rotation_28_28, rotation_14_14}`.
- **Detection**: eligibility clause; `fact_attendance_monthly.rotation_cycle_id IS NULL` corroborates.
- **Evidence**: work pattern, site `rotation_supported`, absence of rotation cycles, amount.
- **Actions**: suspend; confirm the work pattern with the line supervisor; recover.

### A11 — Qualification or certification below the job's mandatory minimum
**Severity** CRITICAL when `safety_critical`, else MEDIUM · **Rate** 0.06% · **Detector** L1
- **Injection**: set `education_level` below `dim_job.min_education`, or expire a required
  certification while the employee stays in a safety-critical role.
- **Detection**: education ordinal comparison; certification expiry vs the period.
- **Evidence**: job title, `safety_critical`, required vs held certifications with expiry dates.
- **Actions**: **suspend from safety-critical duty**; schedule recertification; review the appointment.
  This is a safety finding first and a pay finding second.

### A12 — Time-limited allowance beyond its permitted duration
**Severity** MEDIUM · **Rate** 0.03% · **Detector** L1
*Two allowances carry a `max_consecutive_months` in `policy/allowance_rules.yaml` and both name A12
in their `violation_codes`: `ACTING_ROLE` (12 months) and `RELOCATION` (6). The code covers both —
a bridging payment that never stopped — and the injector plants the acting-role form.*
- **Injection**: keep `ACTING_ROLE` running 14–30 months past `acting_role_since`
  (policy max: 12).
- **Detection**: `months_between(acting_role_since, period) > max_consecutive_months`, and the same
  test on `RELOCATION` against `months_since_site_change`. The limits come from the allowance
  schedule, resolved into the feature store, never restated in the rule.
- **Evidence**: `acting_role_since`, months elapsed, policy maximum, cumulative amount.
- **Actions**: confirm or end the acting assignment; regularise the position or stop the allowance.

---

## Family B — Compensation outliers vs peer group

Statistical, not deterministic. Cohort is built by the fallback ladder in `policy/fusion.yaml` and
the cohort key used is always recorded in the evidence. Robust z (median/MAD), never mean/σ.
Phase-4 gate: **recall ≥ 85%**.

### B01 — Base salary above the peer P99
**Severity** HIGH · **Rate** 0.14% · **Detector** L2
- **Injection**: raise `base_salary` to 1.35×–1.8× the cohort median, keeping it plausible.
- **Detection**: two routes in, either sufficient. Above the top of the approved band by more than
  `band_policy.overpayment_tolerance_pct` — a fact about the band, and what the injector actually
  does — **or** robust z ≥ 3.5 *and* percentile ≥ 99 within a cohort of at least `min_size`, *and*
  a gap the expected-salary model (HistGradientBoostingRegressor + TreeSHAP) cannot account for.
  The residual corroboration is what leaves `legit_high_earner` alone: a senior specialist is
  predicted to be at the top of their cohort by grade, service and site.
- **Evidence**: cohort key and size, employee value, cohort median, percentile, SHAP attribution in
  SAR ("grade explains +2,100, site +900, unexplained +9,800").
- **Actions**: confirm against the approved band; review the last salary action's authorisation.
- **Confounder**: legitimate senior specialists are planted in the same range with proper
  assignment history — they must not reach CRITICAL.

### B02 — Base salary below the band minimum
**Severity** MEDIUM · **Rate** 0.16% · **Detector** L2
*Under-payment is a real finding, not just a fraud signal — it is a legal exposure.*
- **Injection**: set `base_salary` 5–15% below `dim_grade.salary_min` for the class.
- **Detection**: `base_salary < salary_min × (1 − underpayment_tolerance_pct)`.
- **Evidence**: band minimum, actual, shortfall, months affected, cumulative underpayment.
- **Actions**: raise a pay-correction case; calculate back-pay owed.

### B03 — Allowance load far above the cohort norm
**Severity** MEDIUM · **Rate** 0.12% · **Detector** L2
- **Injection**: push `allowance_ratio` **above `allowance_load.hard_ceiling_ratio`** (0.98–1.20) by
  making the employee genuinely entitled to more — housed by allowance rather than in the camp,
  family resident in the Kingdom, own transport — and paying the larger entitled set. Every
  allowance in the stack passes its own clause; the total is the finding. The range is above the
  ceiling rather than the 0.7–0.9 first written here because phase 1 clamps the clean population at
  `clean_population_ratio_max` (0.88): an injection inside 0.7–0.9 would sit inside the clean
  distribution and be indistinguishable from an ordinary offshore rotation worker.
- **Detection**: the ceiling in `allowance_load.hard_ceiling_ratio` is the trigger and the cohort
  comparison is the context, which is the reverse of how this line first read. The injection note
  above says why: `legit_rotation_stack` sits at ~0.60 by design, and a robust z within cohort
  flags that confounder as readily as the anomaly, because a ratio inside the clean distribution is
  genuinely indistinguishable from a legitimate stack. The breach must also still be live in the
  employee's most recent paid month — a stack that crossed the line for three months and came back
  under it on its own is what a posting change looks like, not a case to work.
- **Evidence**: ratio, cohort median ratio, the contributing allowance breakdown.
- **Actions**: review each allowance's eligibility individually — the total is the symptom.

### B04 — Salary jump with no assignment-history record
**Severity** CRITICAL · **Rate** 0.09% · **Detector** L2 + L1 join
*The single strongest compensation-fraud signal: money moved with no paperwork.*
- **Injection**: step `base_pay` up 12–40% mid-series and deliberately write **no**
  `fact_assignment_history` row.
- **Detection**: month-over-month base change > 8% with no assignment row within ±1 month.
- **Evidence**: before/after salary, jump %, the period, absence of a corresponding record.
- **Actions**: escalate to investigation; obtain the authorising document; freeze further increments.
- **Confounder**: legitimate mid-year jumps **with** a proper promotion record are planted at the
  same magnitude. Precision here is the real test.

### B05 — Overtime exceeding base pay or the legal maximum
**Severity** HIGH · **Rate** 0.10% · **Detector** L2
- **Injection**: set `overtime_hours` to 120–200/month, or `overtime_pay > base_pay`.
- **Detection**: `overtime_pay > base_pay` OR `overtime_hours > legal_monthly_max`.
- **Evidence**: hours, pay, base, ratio, the periods involved, attendance cross-check.
- **Actions**: verify timesheets with the supervisor; check for duplicate claims.

### B06 — Bonus inconsistent with performance history
**Severity** MEDIUM · **Rate** 0.08% · **Detector** L2
- **Injection**: pay a top-decile bonus to employees with ratings of 1–2 across three years.
- **Detection**: the bonus schedule in `policy/payroll.yaml` is monotone in the rating, so the
  entitlement is computable and the finding is the gap against it: paid more than `excess_ratio`
  times what the rating entitles, or — where the rating entitles nothing — more than
  `min_pct_of_base_when_unentitled` of base pay. No rating on record is a gap in the performance
  file rather than a bonus the record contradicts, and is not this finding.
- **Evidence**: three-year ratings, bonus amount, cohort bonus median for that rating.
- **Actions**: confirm the bonus approval; review the rating record for tampering.

### B07 — Increment frequency above policy
**Severity** MEDIUM · **Rate** 0.06% · **Detector** L2
- **Injection**: write 2–4 `increment` rows within 12 months.
- **Detection**: count `change_reason = 'increment'` per rolling 12 months >
  `max_increments_per_12m`.
- **Evidence**: the increment dates, amounts, cumulative increase, approvers.
- **Actions**: review each approval; check whether one approver recurs.

---

## Family C — Identity & payroll fraud

Rare, high severity, mostly graph- or set-based. Phase-5 gate: **recall ≥ 75%** across C and D.

### C01 — IBAN shared across unrelated employees
**Severity** CRITICAL · **Rate** 0.05% · **Detector** L3 graph
- **Injection**: assign one IBAN to 2–3 employees in different org units, with different surnames
  and different dates of birth.
- **Detection**: connected components over `fact_bank_account`, over time, not just current rows.
  Candidates are a DuckDB self-join; `networkx` resolves the components and never sees an employee
  who shares nothing with anybody. Two exclusions, each of which is a *different* finding rather
  than a false positive: a couple who each declare the other as their spouse (the planted
  confounder), and a pair sharing a date of birth and a near-identical name, which is one person on
  the payroll twice — **C06**. **A component is excluded only when *every* pair in it is
  explained**, not when some pair is: a three-person ring containing one married couple is still a
  ring, and accounting for two of its three links says nothing about the third.
- **Evidence**: the IBAN (masked), every employee on it, their org units, total monthly disbursement.
- **Actions**: **hold payroll** on the account; verify identity documents; escalate to investigation.
- **Confounder**: spousal shared accounts (`is_known_benign_share = true`, same surname, declared
  `spouse_employee_id`) are planted and must be suppressed.

### C02 — Duplicate national ID or iqama number
**Severity** CRITICAL · **Rate** 0.03% · **Detector** L3 graph
- **Injection**: reuse one `national_id`/`iqama_no` across 2–3 employee records.
- **Detection**: group-by count > 1 on each identifier, resolved into components the same way C01's
  accounts are, so a record linked by a national ID to one employee and by an iqama to another is
  one finding rather than two. Every record in the group is raised: which of them is the real
  person is the reviewer's question, and the action says so — hold all but the one whose documents
  check out. The financial impact carried is **the record's own pay stream**, `estimated`, rather
  than the zero the injector records: one of these streams is going to stop, and an alert with no
  money on it sorts to the bottom of a queue ranked by exposure.
- **Evidence**: the identifier (masked), the duplicate records, hire dates, both pay streams.
- **Actions**: hold payroll on all but the verified record; escalate.

### C03 — Ghost employee
**Severity** CRITICAL · **Rate** 0.02% · **Detector** L3 (ML + rules)
- **Injection**: an employee paid every month with zero badge swipes, zero ERP logins, zero leave
  variance, and no assignment history after hire.
- **Detection**: zero badge swipes, zero ERP logins and zero `activity_score` for ≥ 6 **consecutive**
  paid periods, **before any termination date**. Two corrections to the first statement of this
  line, both from what the injector actually writes. The absence of assignment rows is *not* a
  condition: the injected ghosts carry a full career history like anybody else, and requiring an
  empty one would have found none of them. And the run must sit before termination, because an
  employee still paid after their leaving date stops badging in for a reason that already has a
  code — **C04** — and without the exclusion this detector reports every leaver a second time.
  The two unsupervised models corroborate rather than trigger: where they also put the record at
  the top of the workforce the finding says so, and where they do not, a dormancy this long is
  still a fact. A model that stayed quiet must never be the reason a ghost is not raised.
- **Evidence**: 24-month activity series, payroll series, empty assignment history, cumulative paid.
- **Actions**: **hold payroll immediately**; physical verification through the line manager; escalate.
- **Confounder**: genuinely low-activity roles (long-term sick leave, field staff without ERP
  accounts) are planted with *some* activity and a leave record.

### C04 — Terminated employee still on payroll
**Severity** CRITICAL · **Rate** 0.04% · **Detector** L1
- **Injection**: keep paying 1–8 months past `termination_date` (beyond the legitimate final
  settlement month).
- **Detection**: `period > termination_date` AND `paid_flag` AND the payment is not `SEVERANCE`.
- **Evidence**: termination date and reason, periods paid after it, cumulative overpayment.
- **Actions**: stop payroll; recover; review the leaver process for that org unit.

### C05 — Self-approval or a manager-hierarchy cycle
**Severity** HIGH · **Rate** 0.02% · **Detector** L3 graph
- **Injection**: set `approved_by = employee_id` on an assignment row, or create a 2–4 node cycle in
  `manager_id`.
- **Detection**: `networkx` cycle detection over the manager graph (small candidate subgraph only —
  the employees the feature build's `manager_cycle_flag` marks, plus their chains up to
  `max_cycle_length`); direct equality test for self-approval. The finding is dated from the period
  of the self-approved record to the last month paid, because what a reviewer works is the money
  that has been paid on the strength of that signature. Both routes are one code and neither
  sentence fits the other, so `policy/graph_ml.yaml` carries a template for each.
- **Evidence**: the cycle path or the self-approved record, the value approved.
- **Actions**: void the approval; correct the hierarchy; review everything that approver signed.

### C06 — Near-duplicate identity
**Severity** HIGH · **Rate** 0.02% · **Detector** L3 graph
- **Injection**: take two existing employees and make the second a near-duplicate of the first —
  the same date of birth, the same IBAN, and a name differing by a single transposition. Two real
  records rather than a manufactured clone, so both pay streams, both careers and both attendance
  histories are genuine.
- **Detection**: blocking on DOB + IBAN, then Jaro-Winkler on `name_en_normalised` ≥ 0.90. **Both
  records are raised**, not just the newer one: the detector cannot know which of two real careers
  is the duplicate, and the action — hold the newer record's payroll — names it from the hire dates
  rather than asserting it. What the reviewer is told is the **edit distance**, not the similarity:
  "the names differ by two letters" is a fact about the records, where "0.94" is a fact about the
  comparison.
- **Evidence**: both records side by side with the differing fields highlighted, both pay streams.
- **Actions**: hold the newer record's payroll; verify documents; merge or terminate.

### C07 — Active payroll with an expired iqama
**Severity** CRITICAL · **Rate** 0.03% · **Detector** L1
- **Injection**: set `iqama_expiry` 2–12 months before the paid period while `status = 'active'`.
- **Detection**: `iqama_expiry < period_end AND status = 'active' AND paid_flag`.
- **Evidence**: expiry date, periods paid after it, nationality class.
- **Actions**: **regulatory exposure — escalate immediately**; suspend pay; renew or offboard.

### C08 — Payroll charged to a cost centre with no org assignment
**Severity** HIGH · **Rate** 0.01% (floor: 5 instances) · **Detector** L1
- **Injection**: write `fact_payroll_monthly.cost_center` different from the employee's org unit's.
- **Detection**: anti-join payroll `cost_center` against `dim_org_unit` via `employee_master`.
- **Evidence**: both cost centres, the owning org units, amounts and periods.
- **Actions**: correct the charge; investigate who redirected it — this is how budgets get hidden.

---

## Family D — Behavioural / temporal drift

Time-series shaped. CUSUM change-point detection over each employee's own 24-month baseline is the
workhorse; the sequence autoencoder is only built if CUSUM proves insufficient (`docs/PLAN.md` §3.4).

### D01 — Promotion velocity outlier
**Severity** MEDIUM · **Rate** 0.07% · **Detector** L2
- **Injection**: 3+ grade increases within 24 months.
- **Detection**: grade delta over a rolling 24 months > `max_grade_jump_per_24m`. The finding is
  dated over the months the employee is on payroll, not over the promotions: a career that climbed
  four grades before the observation window opened is still a grade held today that policy does not
  support, and the reviewer works it now. The promotion dates stay in the evidence.
- **Evidence**: the grade timeline, each promotion's date and approver, peer promotion rate.
- **Actions**: review each promotion's approval; check the approver for a pattern.

### D02 — Repeated retroactive adjustments
**Severity** HIGH · **Rate** 0.09% · **Detector** L2
- **Injection**: 3–6 positive `retro_adjustment` entries across the window to one employee.
- **Detection**: count and sum of non-zero `retro_adjustment` per employee vs cohort.
- **Evidence**: each adjustment with period and amount, cumulative total, cohort norm.
- **Actions**: obtain the justification for each; retro adjustments are the classic slow leak.

### D03 — Leave and overtime claimed in the same period
**Severity** MEDIUM · **Rate** 0.10% · **Detector** L1
- **Injection**: set `days_leave ≥ 15` and `overtime_hours ≥ 40` in the same period.
- **Detection**: join attendance to payroll on (`employee_id`, `period`) and test both conditions.
- **Evidence**: leave days and type, overtime hours and pay, the period.
- **Actions**: verify timesheets; likely a timesheet-approval control failure.

### D04 — Attendance beyond physical maximum
**Severity** MEDIUM · **Rate** 0.08% · **Detector** L1
- **Injection**: `days_worked + days_leave + absence_days > calendar_days`, or hours beyond
  physically possible.
- **Detection**: compare against `dim_calendar.calendar_days`.
- **Evidence**: the day breakdown, calendar days in that month, the excess.
- **Actions**: correct the attendance record; check the source system feed.

### D05 — Allowance mix changing abruptly after a manager change
**Severity** HIGH · **Rate** 0.06% · **Detector** L2 + L4
- **Injection**: **the manager change is injected too.** Split the open assignment interval at a
  mid-window month, hand the employee to their manager's own manager at the same grade, same post
  and same unit, and 1–2 periods later add 2–3 allowances worth ≥ 20% of base with no grade change.
  Pass 1's in-window manager changes all arrive with a promotion — transfers cluster early in a
  career — and D05 deliberately ignores promotions, so the case has to be planted whole.
- **Detection**: change-point in `allowance_total` within 2 periods of a manager change in
  `fact_assignment_history`, **excluding windows containing a grade change**. A promotion that
  crosses into a new `grade_entitlements` band adds allowances *by policy* and carries a promotion
  row that explains them; flagging those would put a collusion alert on every promotion.
- **Evidence**: manager before/after, the allowances added and when, the amount delta, and the
  absence of any grade movement.
- **Actions**: review the new manager's other reports for the same pattern — this is a collusion
  precursor, and it is why **D07** exists.
- **Confounder**: none needs planting, but note the shape of the denominator. Managers change
  legitimately in the clean population — at a
  transfer, and whenever a promotion outgrows the previous manager — so roughly half the workforce
  carries a manager change inside the observation window. That is D05's precision denominator.

### D06 — Personal change-point vs own baseline
**Severity** MEDIUM · **Rate** 0.05% · **Detector** L2 (CUSUM)
- **Injection**: a sustained step in take-home pay from a mid-window month with no assignment record
  within a month either side, built from allowance codes that no family-A rule polices (`SHIFT`,
  `ON_CALL`, `SAFETY`, `SECURITY_CLEARANCE` and the rest of `unowned_allowance_codes` in
  `policy/injection.yaml`), each paid at exactly its policy amount. Money appearing with nothing in
  the record to account for it is the finding; paid as `REMOTE_SITE` it would be A01 instead.
- **Detection**: a step, then CUSUM. The step locates the month — a single month where standing pay
  rises past `step_ratio` — and CUSUM, accumulating against the employee's own pre-step baseline,
  decides whether it was a change or a blip. CUSUM alone over the whole series finds the drift but
  dates it badly: it alarms some months late and its reset point wanders, and a change-point a
  reviewer cannot line up against a payroll instruction is not evidence.
  Measured on the **standing** part of pay — base plus allowances, less GOSI and loan — and only
  where **base pay itself did not move**: overtime, the bonus month and a retro correction are
  variation a reviewer can already account for, and a salary that moved with no paperwork behind it
  is B04's finding, not this one.
- **Evidence**: the 24-month chart with the change-point marked, before/after means, magnitude.
- **Actions**: reconcile against payroll instructions for that period.

### D07 — Cluster drift across a department
**Severity** HIGH · **Rate** 0.03% (applied to whole sections, so it covers more employees than the
rate suggests) · **Detector** L2 aggregate
*The collusion signal — one person is an anomaly, a whole section moving together is a scheme.*
- **Injection**: lift every member of one section by 45–75% of their own allowance load over the
  last six months, introduced in three stages so no individual month is a personal change-point.
  Sections are chosen where every member has room under B03's ceiling, and every member the
  detector would put in the comparison — paid in both the baseline and the recent window — is
  lifted and labelled, because a member left out would be flagged by the section's drift with
  nothing of their own to explain it.
- **Detection**: three conditions, all required. Aggregate robust z at `org_unit_id` level against
  sibling units at the same level; the unit's own earlier baseline over the employees present in
  both windows; and **a majority of members who each moved**. The third is what makes it a section
  finding: one manager with a large legitimate increase drags a unit average exactly as far as a
  scheme does, and only the member count tells the two apart.
- **Evidence**: the section, its members, the org-level comparison, the timeline of the drift.
- **Actions**: audit the section as a unit, not employee by employee; review the section head.

---

## 5. Planted confounders (unlabelled, legitimate)

Injected in pass 2 into `labels_confounder`, and **never** into `labels_anomaly`. Without these the
evaluation is meaningless, because anything unusual would be a true positive.

| Type | What it looks like | Which detector it tests |
|---|---|---|
| `legit_high_earner` | Senior specialist, salary near band max, with correct grade, job code and full assignment history | B01 precision |
| `legit_salary_jump` | 15–30% mid-year jump **with** a proper promotion record and approver | B04 precision |
| `spousal_shared_iban` | Two employees, same IBAN, same surname, `spouse_employee_id` set both ways | C01 precision |
| `low_activity_role` | Genuinely low ERP/badge activity (field or long-term-sick) but with leave records and some activity | C03 precision |
| `legit_final_settlement` | One month of `SEVERANCE` paid after `termination_date` | C04 precision |
| `legit_rotation_stack` | Offshore rotation worker legitimately holding 5 allowances at ~0.6 allowance ratio | B03 precision |
| `legit_retro_correction` | Two retro adjustments correcting a known payroll error, with a matching assignment record | D02 precision |

Each type declares the code whose precision it exists to measure, in `policy/injection.yaml` and
again in `labels_confounder.confounds_code`. A confounder is built to sit *below* the deterministic
rule for that code and *above* the statistical norm: a specialist near the top of the band but
inside it, a jump with its promotion row, a quiet role with real badge entries. Two are deliberately
inside their rule's reach and are suppressed by an exclusion rather than by magnitude —
`spousal_shared_iban` (C01 excludes a declared couple) and `legit_final_settlement` (C04 excludes a
SEVERANCE-only month).

Target: **~0.9% of the population** carries a confounder. The phase-2 gate asserts they exist and
are unlabelled; the eval harness reports how many were incorrectly scored CRITICAL or HIGH.

## 6. Realism safeguards

Beyond confounders, the clean population carries deliberate noise so that "unusual" and "anomalous"
are not the same thing (`docs/PLAN.md` §2.5):

- **Missingness**: nullable fields are null at realistic rates; `manual` source records more so.
- **Name noise**: inconsistent casing, double spaces, transliteration variants — which is exactly
  why `name_en_normalised` exists as a separate column.
- **Date typos**: a small rate of transposed digits in non-key dates.
- **Late postings**: some payroll rows land in the following period's run.
- **Skewed distributions**: nationality mix, the grade pyramid in `policy/grade_bands.yaml`, the
  tenure curve, and Eastern-Province-heavy site headcount.
