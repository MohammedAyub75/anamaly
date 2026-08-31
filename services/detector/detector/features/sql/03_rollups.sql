-- Feature block 3 -- 24-month roll-ups per employee.
--
-- Mean, standard deviation, trend slope and the largest month-over-month jump
-- for each money series, plus per-allowance duration and level. These are what
-- layer 2 compares an employee against their own history with, and what dates
-- an anomaly window: a step is only a step relative to the months around it.
--
-- Computed only over months the employee was actually paid in. A gap is not a
-- zero -- averaging an unpaid month in would drag every leaver's mean down and
-- manufacture a change-point out of the termination itself.

CREATE OR REPLACE TEMP TABLE rollups AS
WITH series AS (
    SELECT employee_id,
           period,
           period_index,
           base_pay,
           allowance_total,
           overtime_pay,
           net,
           standing_pay,
           allowance_ratio,
           lag(base_pay)        OVER w AS prior_base_pay,
           lag(allowance_total) OVER w AS prior_allowance_total,
           lag(overtime_pay)    OVER w AS prior_overtime_pay,
           lag(net)             OVER w AS prior_net,
           lag(standing_pay)    OVER w AS prior_standing_pay,
           lag(allowance_ratio) OVER w AS prior_allowance_ratio
    FROM period_features
    WHERE payroll_row_present
    WINDOW w AS (PARTITION BY employee_id ORDER BY period)
)
SELECT employee_id,
       count(*)                                          AS periods_paid,
       min(period)                                       AS first_period_paid_any,
       max(period)                                       AS last_period_paid_any,

       avg(base_pay)                                     AS base_pay_mean,
       stddev_samp(base_pay)                             AS base_pay_std,
       regr_slope(base_pay, period_index)                AS base_pay_slope,
       max(abs(base_pay - prior_base_pay))               AS base_pay_max_jump,
       max(CASE WHEN prior_base_pay > 0
                THEN abs(base_pay - prior_base_pay) / prior_base_pay END)
                                                         AS base_pay_max_jump_pct,

       avg(allowance_total)                              AS allowance_total_mean,
       stddev_samp(allowance_total)                      AS allowance_total_std,
       regr_slope(allowance_total, period_index)         AS allowance_total_slope,
       max(abs(allowance_total - prior_allowance_total)) AS allowance_total_max_jump,

       avg(overtime_pay)                                 AS overtime_pay_mean,
       stddev_samp(overtime_pay)                         AS overtime_pay_std,
       regr_slope(overtime_pay, period_index)            AS overtime_pay_slope,
       max(abs(overtime_pay - prior_overtime_pay))       AS overtime_pay_max_jump,

       avg(net)                                          AS net_mean,
       stddev_samp(net)                                  AS net_std,
       regr_slope(net, period_index)                     AS net_slope,
       max(abs(net - prior_net))                         AS net_max_jump,

       avg(standing_pay)                                 AS standing_pay_mean,
       stddev_samp(standing_pay)                         AS standing_pay_std,
       regr_slope(standing_pay, period_index)            AS standing_pay_slope,
       max(abs(standing_pay - prior_standing_pay))       AS standing_pay_max_jump,
       max(CASE WHEN prior_standing_pay > 0
                THEN abs(standing_pay - prior_standing_pay) / prior_standing_pay END)
                                                         AS standing_pay_max_jump_pct,

       avg(allowance_ratio)                              AS allowance_ratio_mean,
       stddev_samp(allowance_ratio)                      AS allowance_ratio_std,
       regr_slope(allowance_ratio, period_index)         AS allowance_ratio_slope,
       max(abs(allowance_ratio - prior_allowance_ratio)) AS allowance_ratio_max_jump
FROM series
GROUP BY employee_id;

-- Per allowance code: how long it has run and at what level. A monthly
-- entitlement is a flat line by construction, so a slope and a standard
-- deviation over it carry no information -- duration, level and the size of the
-- one step that started it are the facts a reviewer asks about.
CREATE OR REPLACE TEMP TABLE allowance_rollups AS
WITH series AS (
    SELECT employee_id,
           allowance_code,
           period,
           amount,
           lag(amount) OVER (PARTITION BY employee_id, allowance_code
                             ORDER BY period) AS prior_amount
    FROM allowance_features
    WHERE amount > 0
)
SELECT employee_id,
       allowance_code,
       count(*)                                  AS months_paid,
       min(period)                               AS first_period_paid,
       max(period)                               AS last_period_paid,
       avg(amount)                               AS amount_mean,
       max(amount)                               AS amount_max,
       sum(amount)                               AS amount_cumulative,
       coalesce(max(abs(amount - prior_amount)), 0) AS amount_max_jump
FROM series
GROUP BY employee_id, allowance_code;
