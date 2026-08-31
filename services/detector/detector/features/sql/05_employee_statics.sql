-- Feature block 5 -- employee statics, assembled.
--
-- One row per employee: the categoricals a model needs encoded, position within
-- the salary band, tenure, age, the education ordinal and the ratios, joined to
-- the 24-month roll-ups and the graph features. This is the matrix layer 3
-- trains on and the grain layer 2 builds cohorts over.
--
-- State is taken from the employee's LAST period in the window rather than from
-- `employee_master`, so that the statics and the period features tell the same
-- story about the same month. An employee who transferred in the final month
-- would otherwise be compared against peers at a site they had already left.

CREATE OR REPLACE TEMP TABLE employee_features AS
WITH latest AS (
    SELECT *
    FROM (
        SELECT pf.*,
               row_number() OVER (PARTITION BY employee_id
                                  ORDER BY period DESC) AS rn
        FROM period_features pf
    )
    WHERE rn = 1
)
SELECT l.employee_id,
       l.period                                     AS as_of_period,

       -- identity and cohort keys
       l.name_en,
       l.name_ar,
       l.badge_no,
       l.gender,
       l.nationality,
       l.nationality_class,
       l.service_band,
       l.employment_type,
       l.contract_type,
       l.status,
       l.source_system,
       l.asat_grade                                 AS grade,
       l.asat_job_code                              AS job_code,
       l.job_family,
       l.job_title_en,
       l.job_safety_critical,
       l.asat_org_unit_id                           AS org_unit_id,
       l.org_unit_name_en,
       l.parent_org_unit_id,
       l.business_line,
       l.asat_org_cost_center                       AS cost_center,
       l.asat_manager_id                            AS manager_id,
       l.work_site_id,
       l.site_name_en,
       l.site_class,
       l.site_hardship_tier,
       l.site_remote_allowance_eligible,
       l.region_code,
       l.work_pattern,
       l.housing_type,
       l.transport_mode,

       -- numeric statics
       l.age_years,
       l.service_years,
       date_diff('month', l.hire_date, l.period_end_date) AS tenure_months,
       l.months_in_grade,
       l.education_rank,
       l.job_min_education_rank,
       l.certifications_count,
       l.languages_count,
       l.dependents_count,
       l.dependents_in_kingdom,
       l.pay_grade_step,
       l.performance_rating_y1,
       l.performance_rating_y2,
       l.performance_rating_y3,
       (coalesce(l.performance_rating_y1, 0) + coalesce(l.performance_rating_y2, 0)
        + coalesce(l.performance_rating_y3, 0))
           / nullif(
               (l.performance_rating_y1 IS NOT NULL)::INT
               + (l.performance_rating_y2 IS NOT NULL)::INT
               + (l.performance_rating_y3 IS NOT NULL)::INT, 0)
                                                    AS performance_rating_mean,

       -- compensation and band position
       l.asat_base_salary                           AS base_salary,
       l.band_salary_min,
       l.band_salary_mid,
       l.band_salary_max,
       l.band_position,
       l.allowance_total                            AS allowance_total_monthly,
       l.allowance_ratio,
       l.allowance_paid_count,
       CASE WHEN l.asat_base_salary > 0
            THEN r.overtime_pay_mean / l.asat_base_salary END
                                                    AS overtime_ratio,

       -- 24-month roll-ups
       r.periods_paid,
       r.first_period_paid_any,
       r.last_period_paid_any,
       r.base_pay_mean, r.base_pay_std, r.base_pay_slope,
       r.base_pay_max_jump, r.base_pay_max_jump_pct,
       r.allowance_total_mean, r.allowance_total_std, r.allowance_total_slope,
       r.allowance_total_max_jump,
       r.overtime_pay_mean, r.overtime_pay_std, r.overtime_pay_slope,
       r.overtime_pay_max_jump,
       r.net_mean, r.net_std, r.net_slope, r.net_max_jump,
       r.standing_pay_mean, r.standing_pay_std, r.standing_pay_slope,
       r.standing_pay_max_jump, r.standing_pay_max_jump_pct,
       r.allowance_ratio_mean, r.allowance_ratio_std, r.allowance_ratio_slope,
       r.allowance_ratio_max_jump,

       -- graph-derived
       gr.iban_cluster_size,
       gr.iban_count,
       gr.identity_cluster_size,
       gr.manager_depth,
       gr.manager_cycle_flag,
       gr.approver_is_self_flag,
       gr.approvals_given,

       -- activity, averaged over the window
       act.activity_score_mean,
       act.badge_swipes_mean,
       act.erp_logins_mean,
       act.silent_paid_periods

FROM latest l
LEFT JOIN rollups r USING (employee_id)
LEFT JOIN graph_features gr USING (employee_id)
LEFT JOIN (
    SELECT employee_id,
           avg(activity_score) AS activity_score_mean,
           avg(badge_swipes)   AS badge_swipes_mean,
           avg(erp_logins)     AS erp_logins_mean,
           -- Months paid with nothing to show for them: no badge, no login.
           -- A count of facts rather than a threshold, so the number means the
           -- same thing whatever phase 5 decides "dormant" is.
           count(*) FILTER (WHERE paid_flag
                              AND coalesce(badge_swipes, 0) = 0
                              AND coalesce(erp_logins, 0) = 0)
                               AS silent_paid_periods
    FROM period_features
    GROUP BY employee_id
) act USING (employee_id);
