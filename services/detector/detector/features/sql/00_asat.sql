-- Feature block 0 -- the as-at spine.
--
-- One row per (employee, period) for every month from the employee's hire
-- onward, carrying the assignment state **as it was in that month** rather than
-- as it is now.  Everything else in the feature store hangs off this: a rule
-- that asks "was this site remote-eligible when the allowance was paid" is
-- asking about the as-at row, and answering it from `employee_master` would
-- silently exonerate anyone who has since transferred.
--
-- Built from every employee x every period rather than from the payroll rows,
-- so that a finding about an employee who was not paid in a month -- a ghost
-- with a gap, a leaver still on the books -- still has a row to be found on.

CREATE OR REPLACE TEMP TABLE asat AS
WITH periods AS (
    SELECT period,
           calendar_days,
           working_days,
           is_ramadan,
           row_number() OVER (ORDER BY period) AS period_index,
           last_day(make_date(period // 100, period % 100, 1)) AS period_end_date,
           make_date(period // 100, period % 100, 1) AS period_start_date
    FROM dim_calendar
),
spine AS (
    SELECT e.employee_id, p.*
    FROM employee_master e
    CROSS JOIN periods p
    WHERE p.period >= year(e.hire_date) * 100 + month(e.hire_date)
),
-- The interval in force at the end of the month. ASOF picks the latest
-- `effective_from` at or before that date, which is exactly the as-at rule.
resolved AS (
    SELECT s.*,
           h.grade          AS asat_grade,
           h.job_code       AS asat_job_code,
           h.org_unit_id    AS asat_org_unit_id,
           h.work_site_id   AS asat_site_id,
           h.manager_id     AS asat_manager_id,
           h.base_salary    AS asat_base_salary,
           h.change_reason  AS asat_change_reason,
           h.effective_from AS asat_effective_from
    FROM spine s
    ASOF LEFT JOIN fact_assignment_history h
      ON s.employee_id = h.employee_id
     AND s.period_end_date >= h.effective_from
)
SELECT r.employee_id,
       r.period,
       r.period_index,
       r.period_start_date,
       r.period_end_date,
       r.calendar_days,
       r.working_days,
       r.is_ramadan,
       -- Fall back to the master row only where history has nothing to say,
       -- which the phase-1 integrity suite asserts never happens.
       coalesce(r.asat_grade, e.grade)                 AS asat_grade,
       coalesce(r.asat_job_code, e.job_code)           AS asat_job_code,
       coalesce(r.asat_org_unit_id, e.org_unit_id)     AS asat_org_unit_id,
       coalesce(r.asat_site_id, e.work_site_id)        AS asat_site_id,
       coalesce(r.asat_manager_id, e.manager_id)       AS asat_manager_id,
       coalesce(r.asat_base_salary, e.base_salary)     AS asat_base_salary,
       r.asat_change_reason,
       r.asat_effective_from,
       e.nationality_class,
       e.dependents_in_kingdom,
       st.hardship_tier                                AS site_hardship_tier
FROM resolved r
JOIN employee_master e USING (employee_id)
LEFT JOIN dim_site st ON st.site_id = coalesce(r.asat_site_id, e.work_site_id);
