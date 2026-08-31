-- Feature block 2 -- rule inputs: the one wide table layer 1 scans.
--
-- Site attributes, housing type, dependents, job band and calendar days joined
-- and denormalised once, so a policy rule is a scan over a single table rather
-- than a five-way join written out seventeen times (docs/specs/detector.md).
--
-- Where a rule would otherwise have to compute something -- an education
-- ordinal, a certification expiry, the GOSI class a nationality implies, an
-- acting role's overrun -- that computation is a column here instead. A rule
-- predicate is a statement of policy; arithmetic inside one is a feature that
-- was not built (.claude/skills/add-anomaly-rule).

-- Where the employee was posted before their last transfer. A01's exclusion
-- needs it: one trailing month of remote-site allowance after a move from an
-- eligible site is a payroll posting lag, not a violation.
CREATE OR REPLACE TEMP TABLE previous_site AS
WITH hist AS (
    SELECT employee_id, effective_from, work_site_id,
           lag(work_site_id) OVER (PARTITION BY employee_id
                                   ORDER BY effective_from) AS prior_site
    FROM fact_assignment_history
),
moves AS (
    SELECT employee_id, effective_from, prior_site,
           row_number() OVER (PARTITION BY employee_id
                              ORDER BY effective_from DESC) AS rn
    FROM hist
    WHERE prior_site IS NOT NULL AND prior_site <> work_site_id
)
SELECT m.employee_id,
       m.prior_site AS previous_site_id,
       st.site_class AS previous_site_class,
       st.hardship_tier AS previous_site_hardship_tier,
       st.remote_allowance_eligible AS previous_site_remote_allowance_eligible,
       st.offshore_eligible AS previous_site_offshore_eligible,
       m.effective_from AS last_site_change_date
FROM moves m
LEFT JOIN dim_site st ON st.site_id = m.prior_site
WHERE m.rn = 1;

CREATE OR REPLACE TEMP TABLE period_features AS
SELECT
    -- ------------------------------------------------------------- keys
    s.employee_id,
    s.period,
    s.period_index,
    s.period_start_date,
    s.period_end_date,
    s.calendar_days,
    s.working_days,
    s.is_ramadan,

    -- ------------------------------------------------------- the person
    e.name_en,
    e.name_ar,
    e.badge_no,
    e.gender,
    e.dob,
    date_diff('year', e.dob, s.period_end_date)          AS age_years,
    e.nationality,
    e.nationality_class,
    e.iqama_expiry,
    e.marital_status,
    e.dependents_count,
    e.dependents_in_kingdom,
    e.spouse_employed_internally,
    e.spouse_employee_id,
    e.education_level,
    $education_rank                                      AS education_rank,
    e.certifications_count,
    e.has_valid_required_certifications,
    e.languages_count,
    e.hire_date,
    e.service_years,
    e.service_band,
    e.employment_type,
    e.contract_type,
    e.status,
    e.termination_date,
    CASE WHEN e.termination_date IS NULL THEN NULL
         ELSE year(e.termination_date) * 100 + month(e.termination_date) END
                                                         AS termination_period,
    -- Whole months from the termination to the end of the paid month. Zero is
    -- the termination month itself, one the statutory settlement month; beyond
    -- that a payment has nothing behind it (docs/ANOMALY_CATALOG.md C04).
    CASE WHEN e.termination_date IS NULL THEN NULL
         ELSE date_diff('month', e.termination_date, s.period_end_date) END
                                                         AS months_since_termination,
    e.housing_type,
    e.transport_mode,
    e.company_bus_route_id,
    e.work_pattern,
    e.rotation_cycle_days,
    e.months_since_site_change,
    e.acting_role_flag,
    e.acting_role_since,
    e.gosi_class,
    $gosi_expected                                       AS gosi_class_expected,
    e.gosi_class <> ($gosi_expected)                     AS gosi_class_mismatch,
    e.payment_method,
    e.payroll_hold_flag,
    e.source_system,
    e.region_code,
    e.pay_grade_step,
    e.performance_rating_y1,
    e.performance_rating_y2,
    e.performance_rating_y3,
    e.bonus_eligible,
    e.last_increment_date,
    e.last_promotion_date,
    e.months_in_grade,

    -- ------------------------------------------- position, as it was then
    s.asat_grade,
    s.asat_job_code,
    s.asat_org_unit_id,
    s.asat_site_id,
    s.asat_site_id                                       AS work_site_id,
    s.asat_manager_id,
    s.asat_base_salary,
    s.asat_change_reason,
    s.asat_effective_from,
    o.cost_center                                        AS asat_org_cost_center,
    o.org_unit_name_en,
    o.business_line,
    o.level                                              AS org_unit_level,
    o.parent_org_unit_id,

    -- ------------------------------------------------------------- site
    st.site_name_en,
    st.site_name_ar,
    st.site_class,
    st.hardship_tier                                     AS site_hardship_tier,
    st.remote_allowance_eligible                         AS site_remote_allowance_eligible,
    st.offshore_eligible                                 AS site_offshore_eligible,
    st.camp_available                                    AS site_camp_available,
    st.family_housing_available                          AS site_family_housing_available,
    st.rotation_supported                                AS site_rotation_supported,
    prev.previous_site_id,
    prev.previous_site_class,
    prev.previous_site_hardship_tier,
    prev.previous_site_remote_allowance_eligible,
    prev.previous_site_offshore_eligible,
    prev.last_site_change_date,

    -- -------------------------------------------------------------- job
    j.job_title_en,
    j.job_family,
    j.min_grade                                          AS job_min_grade,
    j.max_grade                                          AS job_max_grade,
    j.min_education                                      AS job_min_education,
    $job_education_rank                                  AS job_min_education_rank,
    j.safety_critical                                    AS job_safety_critical,
    len(j.required_certifications)                       AS job_required_certification_count,

    -- ------------------------------------------------------- salary band
    g.salary_min                                         AS band_salary_min,
    g.salary_mid                                         AS band_salary_mid,
    g.salary_max                                         AS band_salary_max,
    CASE WHEN g.salary_max > g.salary_min
         THEN (s.asat_base_salary - g.salary_min) / (g.salary_max - g.salary_min)
    END                                                  AS band_position,

    -- ------------------------------------- qualification, resolved for A11
    ($education_rank) < ($job_education_rank)            AS education_below_job_minimum,
    coalesce(j.safety_critical
             AND len(j.required_certifications) > len(e.certifications), FALSE)
                                                         AS required_certification_missing,
    coalesce(j.safety_critical
             AND len(list_filter(e.certifications,
                                 c -> c.expiry <= s.period_end_date)) > 0, FALSE)
                                                         AS certification_expired,

    -- ------------------------------ time-limited allowances, resolved for A12
    CASE WHEN e.acting_role_since IS NULL THEN NULL
         ELSE date_diff('month', e.acting_role_since, s.period_end_date) END
                                                         AS acting_months,
    greatest(0, coalesce(date_diff('month', e.acting_role_since,
                                   s.period_end_date), 0) - $acting_max_months)
                                                         AS acting_months_over_limit,
    greatest(0, coalesce(e.months_since_site_change, 0) - $relocation_max_months)
                                                         AS relocation_months_over_limit,

    -- ---------------------------------------------------------- the money
    p.base_pay,
    p.overtime_hours,
    p.overtime_pay,
    p.bonus,
    p.retro_adjustment,
    p.gosi_employee,
    p.gosi_employer,
    p.loan_deduction,
    p.absence_deduction,
    p.allowance_total,
    p.gross,
    p.net,
    p.cost_center                                        AS paid_cost_center,
    p.payroll_run_id,
    coalesce(p.paid_flag, FALSE)                         AS paid_flag,
    p.employee_id IS NOT NULL                            AS payroll_row_present,
    -- The standing part of pay: what recurs every month once overtime, the
    -- bonus month and a retro correction are set aside. D06 is measured on it.
    p.base_pay + p.allowance_total - p.gosi_employee - p.loan_deduction
                                                         AS standing_pay,
    CASE WHEN p.base_pay > 0 THEN p.allowance_total / p.base_pay END
                                                         AS allowance_ratio,

    -- --------------------------------------------------------- attendance
    att.days_worked                                       AS attendance_days_worked,
    att.days_leave                                        AS attendance_days_leave,
    att.absence_days                                      AS attendance_absence_days,
    att.overtime_hours                                    AS attendance_overtime_hours,
    att.rotation_cycle_id                                 AS attendance_rotation_cycle_id,
    att.days_worked + att.days_leave + att.absence_days     AS attendance_days_total,

    -- ----------------------------------------------------------- activity
    ac.badge_swipes,
    ac.email_count,
    ac.erp_logins,
    ac.vpn_sessions,
    ac.activity_score,

    -- ------------------------------------------ allowances, wide and rolled up
    $allowance_columns
    coalesce(al.allowance_paid_count, 0)                 AS allowance_paid_count,
    coalesce(al.allowance_expected_total, 0)             AS allowance_expected_total,
    coalesce(al.allowance_offpolicy_count, 0)            AS allowance_offpolicy_count,
    al.allowance_offpolicy_codes,
    coalesce(al.allowance_offpolicy_max_delta, 0)        AS allowance_offpolicy_max_delta,
    coalesce(al.allowance_offpolicy_delta_total, 0)      AS allowance_offpolicy_delta_total,
    coalesce(al.non_severance_allowance_count, 0)        AS non_severance_allowance_count

FROM asat s
JOIN employee_master e USING (employee_id)
LEFT JOIN dim_site st ON st.site_id = s.asat_site_id
LEFT JOIN dim_job j ON j.job_code = s.asat_job_code
LEFT JOIN dim_org_unit o ON o.org_unit_id = s.asat_org_unit_id
LEFT JOIN dim_grade g ON g.grade = s.asat_grade
                     AND g.nationality_class = e.nationality_class
LEFT JOIN fact_payroll_monthly p USING (employee_id, period)
LEFT JOIN fact_attendance_monthly att USING (employee_id, period)
LEFT JOIN fact_system_activity_monthly ac USING (employee_id, period)
LEFT JOIN previous_site prev USING (employee_id)
LEFT JOIN allowance_pivot al USING (employee_id, period);
