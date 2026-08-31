-- Feature block 1 -- allowances in long format, with the policy amount recomputed.
--
-- Long rather than wide (docs/DATA_DICTIONARY.md: adding an allowance code must
-- never mean a schema migration), and this is the table an A07 alert quotes:
-- expected against actual, per code, with the basis the amount was computed on.
--
-- `expected_amount` is recomputed by DuckDB from policy/allowance_rules.yaml,
-- not read back from the payroll row. That is the whole point of A07 -- an
-- independent second opinion on every amount paid. A code the employee has no
-- claim to at all recomputes to zero, and that is an eligibility breach with
-- its own code, so an off-policy amount is asserted only where the entitled
-- amount is above zero (docs/ANOMALY_CATALOG.md A07).

CREATE OR REPLACE TEMP TABLE allowance_features AS
SELECT a.employee_id,
       a.period,
       s.period_index,
       a.allowance_code,
       a.amount,
       a.amount_basis,
       $expected_case AS expected_amount,
       a.amount - ($expected_case) AS amount_delta,
       (($expected_case) > 0 AND abs(a.amount - ($expected_case)) > $tolerance)
           AS off_policy_amount,
       a.eligibility_snapshot_json
FROM fact_payroll_allowance a
JOIN asat s USING (employee_id, period);

-- The per-month roll-up the wide table carries. Kept here beside the long form
-- so the two can never disagree about what "off policy" counted.
CREATE OR REPLACE TEMP TABLE allowance_pivot AS
SELECT employee_id,
       period,
       $pivot_columns
       count(*) FILTER (WHERE amount > 0) AS allowance_paid_count,
       sum(expected_amount) AS allowance_expected_total,
       count(*) FILTER (WHERE off_policy_amount) AS allowance_offpolicy_count,
       list(allowance_code ORDER BY allowance_code)
           FILTER (WHERE off_policy_amount) AS allowance_offpolicy_codes,
       max(abs(amount_delta)) FILTER (WHERE off_policy_amount)
           AS allowance_offpolicy_max_delta,
       sum(amount_delta) FILTER (WHERE off_policy_amount)
           AS allowance_offpolicy_delta_total,
       count(*) FILTER (WHERE amount > 0 AND allowance_code <> 'SEVERANCE')
           AS non_severance_allowance_count
FROM allowance_features
GROUP BY employee_id, period;
