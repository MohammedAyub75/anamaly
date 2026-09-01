"""Layer 2 -- peer statistics, the expected-salary residual and change-points.

Where layer 1 asks "was a clause broken?", layer 2 asks "is this normal for
somebody like them?", and the whole difficulty is in the second half of that
sentence. A cohort is built for every employee by the fallback ladder in
`policy/fusion.yaml`, walking from the most specific key until it reaches
`min_size` peers, and **the key that was actually used and its size travel into
the evidence** -- so a reviewer reads "compared against 412 peers at grade 12,
Process Ops, plant sites" rather than an unexplained number.

Three families of signal, all set-based DuckDB over the feature store:

1. **Cohort statistics.** Median and MAD, never mean and sigma: outliers are
   what we are looking for and they poison the mean. `robust_z = 0.6745 *
   (x - median) / MAD`, with the MAD = 0 guard from `policy/peer_stats.yaml`.
2. **The expected-salary residual** (`l2_salary.py`), with TreeSHAP attribution
   rendered in SAR. That sentence is the point of the layer.
3. **Change-points** over an employee's own 24-month series -- CUSUM in its
   drawup form, which is two window functions rather than a Python loop, so it
   costs the same at 24M rows as it does at 240k.

Twelve codes: B01-B07 and D01, D02, D05, D06, D07, which is every code
`docs/ANOMALY_CATALOG.md` marks `L2`. Layer 2's findings carry the same shape as
layer 1's, so phase 6 fuses one list rather than two.

Unlike layer 1, layer 2 is not expected to run at 100% precision -- a statistic
is not a fact. What it *is* expected to do is say why, in riyals, every time.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import duckdb

from .l1_rules import SEVERITIES, RuleError, period_label, render
from .l2_salary import SalaryExpectation
from .l2_salary import fit as fit_salary

LAYER = "peer_stats"

# The `peer_context` block of `docs/EVIDENCE_CONTRACT.md`. A detector selects
# these as `peer_<name>`; the emitter lifts them out into the bundle and also
# exposes them to the description template under their bare name.
PEER_FIELDS = (
    "cohort_key",
    "cohort_key_level",
    "cohort_key_fallback_reason",
    "cohort_n",
    "cohort_label",
    "metric",
    "value",
    "cohort_median",
    "cohort_mad",
    "percentile",
    "robust_z",
)

# What every detector query must return beside its own evidence.
REQUIRED_COLUMNS = (
    "employee_id",
    "first_period_paid",
    "last_period_paid",
    "months_paid",
    "monthly_impact",
    "cumulative_impact",
)

# Columns the emitter consumes rather than passes through as evidence.
INTERNAL_COLUMNS = ("attributions_json", "row_severity")

# How a cohort dimension reads in a sentence. The label is what the reviewer
# sees, so it is built from a lookup and never from the column name.
DIM_PHRASE = {
    "grade": "'grade ' || {c}",
    "job_family": "{c}",
    "site_class": "{c} || ' sites'",
    "nationality_class": "{c} || ' staff'",
    "service_band": "{c} || ' service'",
}

# Cohort dimensions that are not strings need their own empty-value default,
# matching `features/sql/06_cohort_stats.sql` exactly -- the keys have to be
# byte-identical or the join finds nothing.
DIM_DEFAULT = {"grade": "-1"}


class L2Error(RuntimeError):
    """A layer-2 detector that cannot run. Fatal, never skipped -- as in layer 1."""


@dataclass
class CohortAssignment:
    """Which rung of the ladder each employee ended up on, and how often."""

    by_level: dict[int, int] = field(default_factory=dict)
    below_min: int = 0
    employees: int = 0
    min_size: int = 30
    levels: tuple[str, ...] = ()

    @property
    def fallback_share(self) -> float:
        """Share of assignments that had to drop at least one dimension."""
        total = sum(self.by_level.values())
        top = self.by_level.get(1, 0)
        return (total - top) / total if total else 0.0

    @property
    def last_rung_share(self) -> float:
        """Heavy reliance on the last rung means the cohort design is wrong."""
        total = sum(self.by_level.values())
        last = self.by_level.get(len(self.levels), 0)
        return last / total if total else 0.0


@dataclass
class L2Result:
    """What one layer-2 pass found, per code and in total."""

    seconds: float
    hits: list[dict[str, Any]] = field(default_factory=list)
    by_code: dict[str, int] = field(default_factory=dict)
    employees_by_code: dict[str, int] = field(default_factory=dict)
    seconds_by_code: dict[str, float] = field(default_factory=dict)
    cohorts: CohortAssignment = field(default_factory=CohortAssignment)
    salary: SalaryExpectation | None = None
    codes: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.hits)

    @property
    def detectors(self) -> dict[str, str]:
        """Code -> the detector label the eval report prints."""
        return {code: "L2 peer" for code in self.codes}


# --------------------------------------------------------------------------
# Generated SQL -- the parts that come from the policy pack
# --------------------------------------------------------------------------


def _dim(name: str) -> str:
    """One cohort dimension, defaulted exactly as the feature build defaults it."""
    default = DIM_DEFAULT.get(name, "'unknown'")
    return f"coalesce({name}, {default})"


def cohort_key_sql(dims: tuple[str, ...]) -> str:
    """The cohort key, byte-identical to `features/sql/06_cohort_stats.sql`."""
    return " || '|' || ".join(f"'{d}=' || {_dim(d)}" for d in dims)


def cohort_label_sql(dims: tuple[str, ...]) -> str:
    """The same cohort as a phrase: `grade 12, Process Ops, plant sites`."""
    parts = [DIM_PHRASE[d].format(c=_dim(d)) for d in dims]
    return "concat_ws(', ', " + ", ".join(parts) + ")"


def allowance_mix_sql(policy, prefix: str = "", positive_only: bool = True) -> str:
    """`Housing 4,200, Transport 800` from the wide allowance columns.

    Generated from the policy pack so a twenty-seventh allowance code needs no
    code change, and labelled from `dim_allowance.name_en` so no raw code
    reaches a reviewer.
    """
    parts = []
    for code in policy.allowance_codes:
        column = f"{prefix}allowance_{code}_amount"
        label = policy.allowance_label(code).replace("'", "''")
        test = f"{column} > 0" if positive_only else f"{column} IS NOT NULL"
        parts.append(
            f"CASE WHEN {test} THEN '{label} ' || "
            f"format('{{:,}}', round({column})::BIGINT) END"
        )
    return "concat_ws(', ', " + ", ".join(parts) + ")"


def added_allowances_window_sql(policy, split: str, source: str) -> str:
    """The allowances a change-point added, read from the long allowance table.

    The wide form below compares two known rows; this compares two *windows*,
    which over the wide table means fifty-two aggregate expressions and DuckDB
    evaluating all of them across the whole series before any join prunes it --
    eight seconds at 10k for five findings. In long format it is one GROUP BY
    over rows already filtered to the flagged employees.
    """
    return f"""
    SELECT x.employee_id,
           string_agg(x.label, ', ' ORDER BY x.label) AS added_allowances
    FROM (
        SELECT fa.employee_id,
               {policy.allowance_label_case('fa.allowance_code')} AS label,
               coalesce(avg(fa.amount) FILTER (WHERE fa.period <  {split}), 0)
                   AS before_amount,
               coalesce(avg(fa.amount) FILTER (WHERE fa.period >= {split}), 0)
                   AS after_amount
        FROM features_allowance fa
        JOIN {source} q USING (employee_id)
        GROUP BY 1, 2
    ) x
    WHERE x.after_amount > x.before_amount + 1
    GROUP BY x.employee_id
"""


def added_allowances_sql(policy, before: str, after: str) -> str:
    """The allowance codes whose amount is higher in one row than in another."""
    parts = []
    for code in policy.allowance_codes:
        label = policy.allowance_label(code).replace("'", "''")
        parts.append(
            f"CASE WHEN ({after.format(code=code)}) > ({before.format(code=code)}) + 1"
            f" THEN '{label}' END"
        )
    return "concat_ws(', ', " + ", ".join(parts) + ")"


# --------------------------------------------------------------------------
# Preparation: cohorts, robust z, the salary model
# --------------------------------------------------------------------------


def prepare(
    con: duckdb.DuckDBPyConnection, policy, *, log=None
) -> tuple[CohortAssignment, SalaryExpectation]:
    """Build everything the twelve detectors share, once per run."""
    con.execute(
        "CREATE OR REPLACE TEMP MACRO period_month(p) AS "
        "((p // 100) * 12 + (p % 100))"
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP VIEW assignment_events AS
        SELECT employee_id, effective_from, change_reason, grade, job_code,
               manager_id, approved_by, base_salary,
               (year(effective_from) * 12 + month(effective_from)) AS month_index,
               (year(effective_from) * 100 + month(effective_from)) AS period
        FROM fact_assignment_history
        """
    )
    # Every month within `assignment_reach` of an assignment record, expanded
    # once. B04 and D06 both ask "was there paperwork near this month?", and
    # asked as `abs(a.month_index - period_month(p)) <= 1` that is a non-equi
    # join DuckDB has to nest-loop over the whole series; expanded, it is an
    # equality anti-join and the stage drops from seconds to milliseconds.
    reach = max(
        int(policy.peer_threshold("B04", "assignment_window_months")), 1
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE assignment_months AS
        SELECT DISTINCT e.employee_id,
               e.month_index + d.delta AS month_index,
               abs(d.delta)            AS distance
        FROM assignment_events e,
             (SELECT unnest(range(-{reach}, {reach} + 1)) AS delta) d
        """
    )
    cohorts = build_cohorts(con, policy, log=log)
    salary = fit_salary(con, policy, log=log)
    return cohorts, salary


def build_cohorts(
    con: duckdb.DuckDBPyConnection, policy, *, log=None
) -> CohortAssignment:
    """Assign every employee to the most specific cohort that reaches `min_size`.

    The ladder is walked per (employee, metric): the first rung with at least
    `min_size` peers wins, and if no rung reaches it the *widest* rung does --
    a comparison against too few peers is worse than a broad one, and the
    evidence records which happened either way.
    """
    ladder = policy.cohort_ladder
    metrics = list(policy.robust["metrics"])
    min_size = policy.cohort_min_size
    robust = policy.robust

    unpivot = ", ".join(metrics)
    dims = ", ".join(sorted({d for level in ladder for d in level}))
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE peer_values AS
        UNPIVOT (SELECT employee_id, {dims}, {unpivot} FROM features_employee)
        ON {unpivot} INTO NAME metric VALUE value
        """
    )

    blocks = []
    top = ladder[0]
    for level, keys in enumerate(ladder, start=1):
        dropped = [d for d in top if d not in keys]
        reason = (
            "NULL"
            if not dropped
            else "'" + " and ".join(dropped)
            + f" dropped to reach n >= {min_size}'"
        )
        blocks.append(
            f"""SELECT employee_id, metric, value,
       {level} AS cohort_level,
       '{"|".join(keys)}' AS cohort_dims,
       {cohort_key_sql(keys)} AS cohort_key,
       {cohort_label_sql(keys)} AS cohort_label,
       {reason} AS cohort_key_fallback_reason
FROM peer_values"""
        )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE peer_keys AS\n"
        + "\nUNION ALL\n".join(blocks)
    )

    floor_ratio = float(robust["mad_floor_ratio"])
    scale = float(robust["scale_factor"])
    min_for_z = int(robust["min_cohort_for_z"])
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE peer_stats AS
        WITH ranked AS (
            SELECT k.*,
                   c.n AS cohort_n,
                   c.median_value,
                   c.mad,
                   c.p01,
                   c.p99,
                   percent_rank() OVER (PARTITION BY k.metric, k.cohort_level,
                                                     k.cohort_key
                                        ORDER BY k.value) * 100 AS percentile,
                   row_number() OVER (
                       PARTITION BY k.employee_id, k.metric
                       ORDER BY (c.n >= {min_size}) DESC,
                                CASE WHEN c.n >= {min_size}
                                     THEN k.cohort_level
                                     ELSE -k.cohort_level END
                   ) AS rung
            FROM peer_keys k
            JOIN cohort_stats c
              ON c.cohort_level = k.cohort_level
             AND c.cohort_key = k.cohort_key
             AND c.metric = k.metric
        )
        SELECT employee_id, metric, value, cohort_level, cohort_dims, cohort_key,
               cohort_label, cohort_key_fallback_reason, cohort_n,
               median_value, mad, p01, p99, percentile,
               greatest(mad, {floor_ratio} * abs(median_value)) AS mad_used,
               CASE WHEN cohort_n >= {min_for_z}
                     AND greatest(mad, {floor_ratio} * abs(median_value)) > 0
                    THEN {scale} * (value - median_value)
                         / greatest(mad, {floor_ratio} * abs(median_value))
               END AS robust_z
        FROM ranked
        WHERE rung = 1
        """
    )

    rows = con.execute(
        f"""
        SELECT cohort_level, count(*),
               count(*) FILTER (WHERE cohort_n < {min_size})
        FROM peer_stats WHERE metric = 'base_salary'
        GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    assignment = CohortAssignment(
        by_level={int(r[0]): int(r[1]) for r in rows},
        below_min=sum(int(r[2]) for r in rows),
        employees=sum(int(r[1]) for r in rows),
        min_size=min_size,
        levels=tuple("|".join(level) for level in ladder),
    )
    if log:
        spread = ", ".join(
            f"L{level}={count:,}" for level, count in sorted(assignment.by_level.items())
        )
        log(f"  cohorts   {spread}; {assignment.below_min:,} below n>={min_size}")
    return assignment


# --------------------------------------------------------------------------
# Window collapsing -- shared with layer 1's definition of a finding
# --------------------------------------------------------------------------


def windowed(inner: str, *, select: str = "", joins: str = "", where: str = "") -> str:
    """Collapse consecutive flagged months into one finding per employee.

    The same gaps-and-islands pass layer 1 uses, for the same reason: a
    condition that holds for fourteen months is one case a reviewer works, not
    fourteen. Evidence is read from the last month of the window -- the state as
    it stands now.
    """
    return f"""
WITH flagged AS ({inner}),
islands AS (
    SELECT *,
           period_index - row_number() OVER (PARTITION BY employee_id
                                             ORDER BY period_index) AS island
    FROM flagged
),
windows AS (
    SELECT employee_id, island,
           min(period)       AS first_period_paid,
           max(period)       AS last_period_paid,
           count(*)          AS months_paid,
           max(period_index) AS last_index
    FROM islands GROUP BY employee_id, island
)
SELECT w.first_period_paid,
       w.last_period_paid,
       w.months_paid,
       i.* EXCLUDE (period, period_index, island){select}
FROM windows w
JOIN islands i
  ON i.employee_id = w.employee_id
 AND i.island = w.island
 AND i.period_index = w.last_index
{joins}
{where}
ORDER BY i.employee_id, w.first_period_paid
"""


def _peer_join(metric: str, alias: str = "ps") -> tuple[str, str]:
    """The peer_context columns, joined for one metric."""
    select = (
        f",\n       {alias}.cohort_key       AS peer_cohort_key"
        f",\n       {alias}.cohort_level     AS peer_cohort_key_level"
        f",\n       {alias}.cohort_key_fallback_reason AS peer_cohort_key_fallback_reason"
        f",\n       {alias}.cohort_label     AS peer_cohort_label"
        f",\n       {alias}.cohort_n         AS peer_cohort_n"
        f",\n       {alias}.metric           AS peer_metric"
        f",\n       {alias}.value            AS peer_value"
        f",\n       {alias}.median_value     AS peer_cohort_median"
        f",\n       {alias}.mad              AS peer_cohort_mad"
        f",\n       {alias}.percentile        AS peer_percentile"
        f",\n       {alias}.robust_z         AS peer_robust_z"
    )
    join = (
        f"LEFT JOIN peer_stats {alias} ON {alias}.employee_id = i.employee_id "
        f"AND {alias}.metric = '{metric}'"
    )
    return select, join


# --------------------------------------------------------------------------
# The twelve detectors. One function per code, each returning its DuckDB SQL.
# --------------------------------------------------------------------------


def sql_B01(policy) -> str:
    """Base salary above the peer group and above the top of the band.

    Two ways in, and both are needed. Above the band ceiling is a fact about
    the approved band; a robust-z outlier at the top of its cohort is a fact
    about the peer group, and an employee can be either without being both.

    The peer route is corroborated by the expected-salary residual, exactly as
    `docs/ANOMALY_CATALOG.md` says: a senior specialist who is genuinely at the
    top of their cohort is *predicted* to be there by grade, service and site,
    and the planted `legit_high_earner` confounder is built to be precisely
    that. Only pay the model cannot account for survives.
    """
    over = 1 + policy.overpayment_tolerance_pct / 100
    residual = float(policy.expected_salary["residual_min_sar"])
    peer_select, peer_join = _peer_join("base_salary")
    inner = f"""
    SELECT p.employee_id, p.period, p.period_index,
           p.asat_base_salary AS base_salary,
           p.band_salary_max, p.band_salary_min, p.asat_grade AS grade,
           p.job_family, p.site_name_en,
           (p.asat_base_salary > p.band_salary_max * {over}) AS above_band_ceiling
    FROM features_period p
    WHERE p.payroll_row_present
      AND p.band_salary_max > 0
      AND (p.asat_base_salary > p.band_salary_max * {over}
           OR p.employee_id IN (
               SELECT ps.employee_id FROM peer_stats ps
               JOIN salary_expectation se USING (employee_id)
               WHERE ps.metric = 'base_salary'
                 -- A cohort that never reached min_size is context in the
                 -- evidence, never the trigger: at grade 19 there are nineteen
                 -- people in the company and the most senior of them is an
                 -- outlier against the other eighteen by definition.
                 AND ps.cohort_n >= {policy.cohort_min_size}
                 AND ps.robust_z >= {policy.robust_z_threshold}
                 AND ps.percentile >= {policy.percentile_flag_high}
                 AND se.unexplained_sar >= {residual}))
    """
    select = (
        peer_select
        + """,
       se.expected_salary,
       se.explained_sar,
       se.unexplained_sar,
       se.attributions_json,
       greatest(i.base_salary
                - greatest(i.band_salary_max, coalesce(ps.median_value, 0)), 0)
           AS monthly_impact,
       greatest(i.base_salary
                - greatest(i.band_salary_max, coalesce(ps.median_value, 0)), 0)
           * w.months_paid AS cumulative_impact"""
    )
    joins = peer_join + "\nLEFT JOIN salary_expectation se ON se.employee_id = i.employee_id"
    return windowed(inner, select=select, joins=joins)


def sql_B02(policy) -> str:
    """Base salary below the band minimum -- underpayment, and a legal exposure."""
    under = 1 - policy.underpayment_tolerance_pct / 100
    peer_select, peer_join = _peer_join("base_salary")
    inner = f"""
    SELECT p.employee_id, p.period, p.period_index,
           p.asat_base_salary AS base_salary,
           p.band_salary_min, p.band_salary_max, p.asat_grade AS grade,
           p.job_family
    FROM features_period p
    WHERE p.payroll_row_present
      AND p.band_salary_min > 0
      AND p.asat_base_salary < p.band_salary_min * {under}
    """
    select = (
        peer_select
        + """,
       (i.band_salary_min - i.base_salary) AS monthly_impact,
       (i.band_salary_min - i.base_salary) * w.months_paid AS cumulative_impact"""
    )
    return windowed(inner, select=select, joins=peer_join)


def sql_B03(policy) -> str:
    """Total allowances above the policy ceiling, read against the cohort norm.

    The ceiling is the trigger and the cohort comparison is the context, which
    is the opposite way round from how the catalogue first stated it -- and the
    catalogue's own note on the injection range says why. An offshore rotation
    worker legitimately stacks six site-driven allowances at a ratio around
    0.60, which is why `legit_rotation_stack` is planted there; a ratio inside
    the clean distribution is genuinely indistinguishable from that, so a robust
    z alone flags the confounder as readily as the anomaly. What is *not*
    ambiguous is the load ceiling in `policy/allowance_rules.yaml`.
    """
    ceiling = policy.hard_ceiling_ratio
    peer_select, peer_join = _peer_join("allowance_ratio")
    inner = f"""
    SELECT p.employee_id, p.period, p.period_index,
           p.allowance_total, p.base_pay, p.allowance_ratio,
           p.allowance_paid_count, p.asat_grade AS grade,
           max(p.period_index) FILTER (WHERE p.payroll_row_present)
               OVER (PARTITION BY p.employee_id) AS last_paid_index,
           {allowance_mix_sql(policy, prefix='p.')} AS allowance_breakdown
    FROM features_period p
    QUALIFY p.payroll_row_present
      AND p.base_pay > 0
      AND p.allowance_ratio > {ceiling}
    """
    select = (
        peer_select
        + f""",
       i.allowance_ratio * 100 AS value_pct,
       coalesce(ps.median_value, 0) * 100 AS median_pct,
       {ceiling * 100} AS ceiling_pct,
       greatest(i.allowance_total - i.base_pay * {ceiling}, 0) AS monthly_impact,
       greatest(i.allowance_total - i.base_pay * {ceiling}, 0) * w.months_paid
           AS cumulative_impact"""
    )
    # The load must still be over the ceiling in the employee's most recent
    # paid month. An allowance stack that crossed the line for three months two
    # years ago and came back under it on its own is what a legitimate rotation
    # worker's record looks like at a posting change -- and it is exactly what
    # `legit_rotation_stack` is planted to check. B03's action is "review each
    # allowance", which is only a sentence about a stack that still exists.
    where = "WHERE w.last_index = i.last_paid_index"
    return windowed(inner, select=select, joins=peer_join, where=where)


def sql_B04(policy) -> str:
    """A salary step with no assignment record either side of it.

    The single strongest compensation-fraud signal in the catalogue: money moved
    with no paperwork. The whole discrimination lives in the anti-join -- a
    legitimate jump of the same size arrives with a promotion row, which is
    exactly what the planted `legit_salary_jump` confounder is built to test.
    """
    jump = policy.peer_threshold("B04", "jump_pct") / 100
    window = int(policy.peer_threshold("B04", "assignment_window_months"))
    peer_select, peer_join = _peer_join("base_salary")
    return f"""
WITH series AS (
    SELECT employee_id, period, period_index, base_pay, asat_grade,
           lag(base_pay)  OVER w AS previous_base,
           lag(asat_grade) OVER w AS previous_grade,
           max(period_index) OVER (PARTITION BY employee_id) AS last_index,
           max(period)       OVER (PARTITION BY employee_id) AS last_period
    FROM features_period
    WHERE payroll_row_present
    WINDOW w AS (PARTITION BY employee_id ORDER BY period)
),
jumps AS (
    SELECT *, base_pay AS new_base,
           (base_pay - previous_base) / previous_base * 100 AS jump_pct
    FROM series
    WHERE previous_base > 0
      AND (base_pay - previous_base) / previous_base > {jump}
),
unexplained AS (
    SELECT j.* FROM jumps j
    WHERE NOT EXISTS (
        SELECT 1 FROM assignment_months a
        WHERE a.employee_id = j.employee_id
          AND a.month_index = period_month(j.period)
          AND a.distance <= {window}
    )
)
SELECT i.employee_id,
       i.period      AS first_period_paid,
       i.last_period AS last_period_paid,
       i.last_index - i.period_index + 1 AS months_paid,
       i.previous_base, i.new_base, i.jump_pct, i.previous_grade,
       i.asat_grade AS grade{peer_select},
       se.expected_salary, se.unexplained_sar, se.attributions_json,
       (i.new_base - i.previous_base) AS monthly_impact,
       (i.new_base - i.previous_base) * (i.last_index - i.period_index + 1)
           AS cumulative_impact
FROM unexplained i
{peer_join}
LEFT JOIN salary_expectation se ON se.employee_id = i.employee_id
ORDER BY i.employee_id, i.period
"""


def sql_B05(policy) -> str:
    """Overtime beyond the legal monthly maximum, or larger than base pay."""
    legal = policy.legal_overtime_hours
    peer_select, peer_join = _peer_join("overtime_ratio")
    inner = f"""
    SELECT p.employee_id, p.period, p.period_index,
           p.overtime_hours, p.overtime_pay, p.base_pay,
           p.attendance_days_worked, p.attendance_overtime_hours,
           {legal} AS legal_max_hours
    FROM features_period p
    WHERE p.payroll_row_present
      AND p.base_pay > 0
      AND (p.overtime_hours > {legal} OR p.overtime_pay > p.base_pay)
    """
    # The recoverable amount is the pay attributable to hours beyond the legal
    # maximum; where the trigger is overtime exceeding base pay instead, it is
    # the excess over base. Whichever is larger is the number to quote.
    excess = (
        f"greatest(i.overtime_pay * greatest(i.overtime_hours - {legal}, 0)"
        f" / nullif(i.overtime_hours, 0),"
        f" greatest(i.overtime_pay - i.base_pay, 0))"
    )
    select = f"""{peer_select},
       {excess} AS monthly_impact,
       {excess} * w.months_paid AS cumulative_impact"""
    return windowed(inner, select=select, joins=peer_join)


def sql_B06(policy) -> str:
    """A bonus the performance record does not support.

    The bonus schedule in `policy/payroll.yaml` is monotone in the rating, so
    the entitlement is computable and the finding is the gap against it. A
    rating that entitles the employee to nothing makes the ratio meaningless,
    so that case falls back to an absolute floor.
    """
    entitled = policy.bonus_entitlement_sql("p.performance_rating_y1", "p.base_pay")
    ratio = policy.peer_threshold("B06", "excess_ratio")
    floor = policy.peer_threshold("B06", "min_pct_of_base_when_unentitled")
    inner = f"""
    SELECT p.employee_id, p.period, p.period_index,
           p.bonus, p.base_pay,
           coalesce(p.performance_rating_y1::VARCHAR, 'none recorded') AS rating,
           concat_ws(', ', p.performance_rating_y3::VARCHAR,
                           p.performance_rating_y2::VARCHAR,
                           p.performance_rating_y1::VARCHAR) AS rating_history,
           round(coalesce({entitled}, 0), 2) AS entitled_bonus
    FROM features_period p
    WHERE p.payroll_row_present
      AND p.bonus > 0
      AND p.base_pay > 0
      -- No rating on record is a gap in the performance file, not a bonus the
      -- record contradicts. It is a different finding and not this one.
      AND p.performance_rating_y1 IS NOT NULL
      AND CASE WHEN coalesce({entitled}, 0) > 0
               THEN p.bonus > {entitled} * {ratio}
               ELSE p.bonus > p.base_pay * {floor} END
    """
    excess = "greatest(i.bonus - coalesce(i.entitled_bonus, 0), 0)"
    select = f""",
       {excess} AS monthly_impact,
       {excess} * w.months_paid AS cumulative_impact"""
    return windowed(inner, select=select)


def sql_B07(policy) -> str:
    """More increments inside twelve months than `band_policy` allows."""
    limit = policy.max_increments_per_12m
    return f"""
WITH events AS (
    SELECT employee_id, month_index, period, base_salary, change_reason, approved_by,
           lag(base_salary) OVER (PARTITION BY employee_id ORDER BY month_index)
               AS previous_salary
    FROM assignment_events
),
increments AS (
    SELECT *,
           count(*) OVER (PARTITION BY employee_id ORDER BY month_index
                          RANGE BETWEEN 11 PRECEDING AND CURRENT ROW) AS in_12m
    FROM events WHERE change_reason = 'increment'
),
peak AS (
    SELECT employee_id, month_index AS end_month, in_12m AS increment_count,
           row_number() OVER (PARTITION BY employee_id
                              ORDER BY in_12m DESC, month_index DESC) AS rn
    FROM increments
),
worst AS (SELECT * FROM peak WHERE rn = 1 AND increment_count > {limit}),
run AS (
    SELECT w.employee_id, w.increment_count,
           min(i.period)  AS first_period_paid,
           max(i.period)  AS last_period_paid,
           arg_min(coalesce(i.previous_salary, i.base_salary), i.month_index)
               AS previous_base,
           arg_max(i.base_salary, i.month_index) AS new_base,
           count(DISTINCT i.approved_by) AS approver_count
    FROM worst w
    JOIN increments i
      ON i.employee_id = w.employee_id
     AND i.month_index BETWEEN w.end_month - 11 AND w.end_month
    GROUP BY 1, 2
)
SELECT r.employee_id, r.first_period_paid, r.last_period_paid,
       period_month(r.last_period_paid) - period_month(r.first_period_paid) + 1
           AS months_paid,
       r.increment_count, r.previous_base, r.new_base, r.approver_count,
       {limit} AS max_increments,
       f.grade, f.job_family, f.org_unit_name_en,
       greatest(r.new_base - r.previous_base, 0) AS monthly_impact,
       greatest(r.new_base - r.previous_base, 0)
           * (period_month(r.last_period_paid)
              - period_month(r.first_period_paid) + 1) AS cumulative_impact
FROM run r
JOIN features_employee f ON f.employee_id = r.employee_id
ORDER BY r.employee_id
"""


def sql_D01(policy) -> str:
    """More grades climbed in twenty-four months than the promotion policy allows."""
    limit = policy.max_grade_jump_per_24m
    return f"""
WITH events AS (
    SELECT employee_id, month_index, period, grade, base_salary, change_reason,
           min(grade) OVER (PARTITION BY employee_id ORDER BY month_index
                            RANGE BETWEEN 23 PRECEDING AND CURRENT ROW)
               AS grade_floor
    FROM assignment_events
),
climbs AS (SELECT *, grade - grade_floor AS grade_delta FROM events),
peak AS (
    SELECT employee_id, month_index AS end_month, grade_delta, grade AS new_grade,
           grade_floor AS previous_grade,
           row_number() OVER (PARTITION BY employee_id
                              ORDER BY grade_delta DESC, month_index DESC) AS rn
    FROM climbs
),
worst AS (SELECT * FROM peak WHERE rn = 1 AND grade_delta > {limit}),
run AS (
    SELECT w.employee_id, w.grade_delta, w.new_grade, w.previous_grade,
           min(e.period) AS first_promotion_period,
           max(e.period) AS last_promotion_period,
           count(*) FILTER (WHERE e.change_reason = 'promotion') AS promotion_count,
           arg_min(e.base_salary, e.month_index) AS previous_base,
           arg_max(e.base_salary, e.month_index) AS new_base
    FROM worst w
    JOIN events e
      ON e.employee_id = w.employee_id
     AND e.month_index BETWEEN w.end_month - 23 AND w.end_month
    GROUP BY 1, 2, 3, 4
)
-- The finding is dated over the months the employee is on payroll, not over
-- the promotions themselves: a career that climbed four grades before the
-- observation window opened is still a grade held today that policy does not
-- support, and a reviewer works it now. The promotion dates stay in the
-- evidence, where the grade timeline belongs.
SELECT r.employee_id,
       f.first_period_paid_any AS first_period_paid,
       f.last_period_paid_any  AS last_period_paid,
       period_month(f.last_period_paid_any)
           - period_month(f.first_period_paid_any) + 1 AS months_paid,
       r.first_promotion_period, r.last_promotion_period,
       period_month(r.last_promotion_period)
           - period_month(r.first_promotion_period) + 1 AS window_months,
       r.grade_delta, r.new_grade, r.previous_grade, r.promotion_count,
       r.previous_base, r.new_base, {limit} AS max_grade_jump,
       f.job_family, f.org_unit_name_en,
       greatest(r.new_base - r.previous_base, 0) AS monthly_impact,
       greatest(r.new_base - r.previous_base, 0)
           * (period_month(f.last_period_paid_any)
              - period_month(f.first_period_paid_any) + 1) AS cumulative_impact
FROM run r
JOIN features_employee f ON f.employee_id = r.employee_id
ORDER BY r.employee_id
"""


def sql_D02(policy) -> str:
    """A stream of retroactive payments -- the classic slow leak.

    Counted over positive adjustments only: a correction that takes money back
    is not the pattern, and the planted `legit_retro_correction` confounder is
    two of them with a matching assignment record.
    """
    limit = policy.max_retro_entries_clean
    peer_select, peer_join = _peer_join("net_mean", alias="ps")
    return f"""
WITH entries AS (
    SELECT employee_id, period, period_index, retro_adjustment, base_pay
    FROM features_period
    WHERE payroll_row_present AND retro_adjustment > 0
),
agg AS (
    SELECT employee_id,
           count(*)                              AS entry_count,
           sum(retro_adjustment)                 AS total_retro,
           max(retro_adjustment)                 AS largest_entry,
           arg_max(period, retro_adjustment)     AS largest_period,
           min(period)                           AS first_period_paid,
           max(period)                           AS last_period_paid
    FROM entries GROUP BY 1
    HAVING count(*) > {limit}
)
SELECT i.employee_id, i.first_period_paid, i.last_period_paid,
       period_month(i.last_period_paid) - period_month(i.first_period_paid) + 1
           AS months_paid,
       i.entry_count, i.largest_entry, i.largest_period,
       {limit} AS max_entries,
       f.org_unit_name_en, f.grade{peer_select},
       i.total_retro / nullif(period_month(i.last_period_paid)
                              - period_month(i.first_period_paid) + 1, 0)
           AS monthly_impact,
       i.total_retro AS cumulative_impact
FROM agg i
JOIN features_employee f ON f.employee_id = i.employee_id
{peer_join}
ORDER BY i.employee_id
"""


def sql_D05(policy) -> str:
    """Allowances appearing within two periods of a change of manager.

    Windows containing a grade change are excluded on purpose: a promotion that
    crosses into a new `grade_entitlements` band adds allowances *by policy* and
    carries a promotion row that explains them, so flagging those would put a
    collusion alert on every promotion.
    """
    window = int(policy.peer_threshold("D05", "manager_change_window_months"))
    step_pct = policy.peer_threshold("D05", "step_pct_of_base") / 100
    added = added_allowances_sql(
        policy, before="c.allowance_{code}_amount", after="p.allowance_{code}_amount"
    )
    peer_select, peer_join = _peer_join("allowance_total_monthly")
    return f"""
WITH series AS (
    SELECT *, lag(asat_manager_id) OVER w AS previous_manager,
              lag(asat_grade)      OVER w AS previous_grade,
              max(period_index)    OVER (PARTITION BY employee_id) AS last_index,
              max(period)          OVER (PARTITION BY employee_id) AS last_period
    FROM features_period
    WHERE payroll_row_present
    WINDOW w AS (PARTITION BY employee_id ORDER BY period)
),
changes AS (
    SELECT * FROM series
    WHERE previous_manager IS NOT NULL
      AND asat_manager_id <> previous_manager
      AND asat_grade = previous_grade
),
steps AS (
    SELECT c.employee_id,
           c.period        AS manager_change_period,
           c.previous_manager, c.asat_manager_id AS new_manager,
           c.period_index  AS change_index,
           c.base_pay, c.asat_grade AS grade,
           p.period        AS step_period,
           p.period_index  AS step_index,
           p.last_index, p.last_period,
           p.allowance_total - c.allowance_total AS step_amount,
           (p.allowance_total - c.allowance_total) / nullif(c.base_pay, 0) * 100
               AS step_pct,
           {added} AS added_allowances,
           row_number() OVER (PARTITION BY c.employee_id, c.period
                              ORDER BY p.period_index) AS rn
    FROM changes c
    JOIN series p
      ON p.employee_id = c.employee_id
     AND p.period_index > c.period_index
     AND p.period_index <= c.period_index + {window}
     AND p.asat_grade = c.asat_grade
    WHERE p.allowance_total - c.allowance_total >= c.base_pay * {step_pct}
)
SELECT i.employee_id,
       i.step_period AS first_period_paid,
       i.last_period AS last_period_paid,
       i.last_index - i.step_index + 1 AS months_paid,
       i.manager_change_period, i.previous_manager, i.new_manager,
       i.step_amount, i.step_pct, i.added_allowances, i.base_pay, i.grade,
       f.org_unit_name_en{peer_select},
       i.step_amount AS monthly_impact,
       i.step_amount * (i.last_index - i.step_index + 1) AS cumulative_impact
FROM steps i
JOIN features_employee f ON f.employee_id = i.employee_id
{peer_join}
WHERE i.rn = 1
ORDER BY i.employee_id, i.step_period
"""


def sql_D06(policy) -> str:
    """A sustained, unexplained step in standing pay -- CUSUM over one employee.

    Measured on standing pay (base plus allowances, less GOSI and loan) rather
    than net: overtime, the bonus month and a retro correction are variation a
    reviewer can already account for. Base pay must not have moved -- a salary
    that moved with no paperwork behind it is B04's finding, not this one.

    Two stages, and the order matters. The **step** locates the month: a single
    month where standing pay rises by more than `step_ratio` with base pay flat
    and no assignment record either side. CUSUM then decides whether it was a
    change or a blip, accumulating `x - baseline - k * spread` over the months
    that follow against the employee's own pre-step baseline and alarming at
    `h * spread`. Running CUSUM alone over the whole series finds the drift but
    dates it badly -- it alarms some months after the step and its reset point
    wanders -- and a change-point a reviewer cannot line up against a payroll
    instruction is not evidence. The step supplies the date; CUSUM supplies the
    proof that it stuck.
    """
    cusum = policy.cusum
    k = float(cusum["k_sigma"])
    h = float(cusum["h_sigma"])
    each_side = int(cusum["min_months_each_side"])
    floor_ratio = float(cusum["spread_floor_ratio"])
    step_ratio = policy.peer_threshold("D06", "step_ratio")
    added = added_allowances_window_sql(policy, "q.change_period", "qualified")
    # `series` is read five times over, so it is projected down to the columns
    # the detector actually reads. `SELECT *` here drags all 168 feature columns
    # through every one of those passes, which at 24M rows is the difference
    # between a stage and a coffee break.
    columns = ", ".join(
        f"allowance_{code}_amount" for code in policy.allowance_codes
    )
    return f"""
WITH series AS (
    SELECT employee_id, period, period_index, standing_pay, base_pay, {columns},
           row_number() OVER w AS seq,
           lag(standing_pay) OVER w AS previous_standing,
           lag(base_pay)     OVER w AS previous_base,
           count(*) OVER (PARTITION BY employee_id) AS months
    FROM features_period
    WHERE payroll_row_present AND standing_pay > 0
    WINDOW w AS (PARTITION BY employee_id ORDER BY period)
),
steps AS (
    SELECT employee_id, seq AS change_seq, period AS change_period,
           row_number() OVER (PARTITION BY employee_id ORDER BY seq) AS rn
    FROM series s
    WHERE previous_standing > 0
      AND (standing_pay - previous_standing) / previous_standing >= {step_ratio}
      AND base_pay = previous_base
      AND seq > {each_side}
      AND months - seq + 1 >= {each_side}
      AND NOT EXISTS (
          SELECT 1 FROM assignment_months a
          WHERE a.employee_id = s.employee_id
            AND a.month_index = period_month(s.period)
            AND a.distance <= 1
      )
),
first_step AS (SELECT * FROM steps WHERE rn = 1),
before_centre AS (
    SELECT s.employee_id, median(s.standing_pay) AS before_median
    FROM series s JOIN first_step c USING (employee_id)
    WHERE s.seq < c.change_seq
    GROUP BY s.employee_id
),
before_spread AS (
    SELECT s.employee_id,
           greatest(median(abs(s.standing_pay - b.before_median)),
                    {floor_ratio} * any_value(b.before_median)) AS spread
    FROM series s
    JOIN first_step c USING (employee_id)
    JOIN before_centre b USING (employee_id)
    WHERE s.seq < c.change_seq
    GROUP BY s.employee_id
),
split AS (
    SELECT s.employee_id, c.change_seq, c.change_period, sp.spread,
           avg(s.standing_pay) FILTER (WHERE s.seq <  c.change_seq) AS before_mean,
           avg(s.standing_pay) FILTER (WHERE s.seq >= c.change_seq) AS after_mean,
           count(*)            FILTER (WHERE s.seq <  c.change_seq) AS before_months,
           count(*)            FILTER (WHERE s.seq >= c.change_seq) AS after_months,
           max(s.period) AS last_period_paid
    FROM series s
    JOIN first_step c USING (employee_id)
    JOIN before_spread sp USING (employee_id)
    GROUP BY s.employee_id, c.change_seq, c.change_period, sp.spread
),
-- CUSUM over the months from the step, against the employee's own pre-step
-- baseline: sum(x - baseline - k * spread), alarming at h * spread. Two window
-- functions rather than a Python loop, so 24M rows cost what 240k cost.
walk AS (
    SELECT s.employee_id, s.seq, p.spread,
           sum(s.standing_pay - p.before_mean - {k} * p.spread)
               OVER (PARTITION BY s.employee_id ORDER BY s.seq
                     ROWS UNBOUNDED PRECEDING) AS statistic
    FROM series s JOIN split p USING (employee_id)
    WHERE s.seq >= p.change_seq
),
alarm AS (
    SELECT employee_id, max(statistic) AS peak, any_value(spread) AS spread
    FROM walk GROUP BY employee_id
),
qualified AS (
    SELECT p.* FROM split p JOIN alarm a USING (employee_id)
    WHERE p.before_months >= {each_side}
      AND p.after_months  >= {each_side}
      AND p.before_mean > 0
      AND a.peak > {h} * a.spread
),
added AS ({added})
SELECT i.employee_id,
       i.change_period AS first_period_paid,
       i.last_period_paid,
       i.after_months  AS months_paid,
       i.before_mean, i.after_mean, i.before_months, i.after_months,
       coalesce(a.added_allowances, 'no new allowance') AS added_allowances,
       f.org_unit_name_en, f.grade,
       (i.after_mean - i.before_mean) AS monthly_impact,
       (i.after_mean - i.before_mean) * i.after_months AS cumulative_impact
FROM qualified i
JOIN features_employee f ON f.employee_id = i.employee_id
LEFT JOIN added a ON a.employee_id = i.employee_id
ORDER BY i.employee_id
"""


def sql_D07(policy) -> str:
    """A whole section drifting upward together -- the collusion signal.

    One person is an anomaly; a section moving together is a scheme, so the
    comparison is at `org_unit_id` level: the unit against its own earlier
    baseline over the employees paid in both windows, and against its sibling
    units at the same level. Every member of a drifting section is a finding,
    because a member left out would be flagged by the section's drift with
    nothing of their own to explain it.
    """
    window = int(policy.peer_threshold("D07", "window_months"))
    drift_ratio = policy.peer_threshold("D07", "drift_ratio")
    min_members = int(policy.peer_threshold("D07", "min_members"))
    member_ratio = policy.peer_threshold("D07", "member_drift_ratio")
    member_share = policy.peer_threshold("D07", "member_share")
    floor_ratio = float(policy.robust["mad_floor_ratio"])
    scale = float(policy.robust["scale_factor"])
    return f"""
WITH span AS (SELECT max(period_index) AS last_index FROM features_period),
halves AS (
    SELECT p.employee_id, p.asat_org_unit_id AS org_unit_id, p.org_unit_name_en,
           p.org_unit_level, p.period, p.period_index, p.allowance_total,
           (p.period_index > s.last_index - {window}) AS recent
    FROM features_period p, span s
    WHERE p.payroll_row_present
),
per_employee AS (
    SELECT employee_id,
           any_value(org_unit_id)     AS org_unit_id,
           any_value(org_unit_name_en) AS org_unit_name_en,
           any_value(org_unit_level)  AS org_unit_level,
           avg(allowance_total) FILTER (WHERE NOT recent) AS before_amount,
           avg(allowance_total) FILTER (WHERE recent)     AS after_amount,
           min(period)          FILTER (WHERE recent)     AS first_recent_period,
           max(period)          FILTER (WHERE recent)     AS last_recent_period
    FROM halves GROUP BY employee_id
),
paid_both AS (
    SELECT * FROM per_employee
    WHERE before_amount IS NOT NULL AND after_amount IS NOT NULL
      AND first_recent_period IS NOT NULL
),
units AS (
    SELECT org_unit_id,
           any_value(org_unit_name_en) AS org_unit_name_en,
           any_value(org_unit_level)   AS org_unit_level,
           count(*)                    AS member_count,
           avg(before_amount)          AS unit_before,
           avg(after_amount)           AS unit_after,
           -- The whole point of D07: a section moving TOGETHER. One manager
           -- with a big legitimate increase drags a unit average as far as a
           -- scheme does, and the two are told apart by how many members moved.
           count(*) FILTER (WHERE before_amount > 0
                              AND after_amount / before_amount >= {member_ratio})
               * 1.0 / count(*)        AS moving_share
    FROM paid_both GROUP BY org_unit_id
),
-- Every unit big enough to have a norm. This is the control group the sibling
-- statistics are taken over, so it is deliberately NOT filtered down to the
-- candidates: a median taken over the drifting units would find nothing odd
-- about them.
sized AS (
    SELECT *, unit_after / nullif(unit_before, 0) AS drift
    FROM units WHERE member_count >= {min_members} AND unit_before > 0
),
sibling_centre AS (
    SELECT org_unit_level, median(drift) AS sibling_drift, count(*) AS sibling_count
    FROM sized GROUP BY org_unit_level
),
sibling_spread AS (
    SELECT s.org_unit_level,
           greatest(median(abs(s.drift - c.sibling_drift)),
                    {floor_ratio}) AS sibling_mad
    FROM sized s JOIN sibling_centre c USING (org_unit_level)
    GROUP BY s.org_unit_level
),
drifting AS (
    SELECT s.*, c.sibling_drift, c.sibling_count, d.sibling_mad,
           {scale} * (s.drift - c.sibling_drift) / d.sibling_mad AS unit_z
    FROM sized s
    JOIN sibling_centre c USING (org_unit_level)
    JOIN sibling_spread d USING (org_unit_level)
    WHERE s.drift >= {drift_ratio}
      AND s.moving_share >= {member_share}
      AND {scale} * (s.drift - c.sibling_drift) / d.sibling_mad
          >= {policy.robust_z_threshold}
)
SELECT e.employee_id,
       e.first_recent_period AS first_period_paid,
       e.last_recent_period  AS last_period_paid,
       {window} AS months_paid,
       d.org_unit_name_en, d.member_count, d.unit_before, d.unit_after,
       (d.drift - 1) * 100 AS drift_pct,
       (d.sibling_drift - 1) * 100 AS sibling_pct,
       d.sibling_count, d.unit_z,
       e.before_amount, e.after_amount,
       greatest(e.after_amount - e.before_amount, 0) AS monthly_impact,
       greatest(e.after_amount - e.before_amount, 0) * {window} AS cumulative_impact
FROM paid_both e
JOIN drifting d USING (org_unit_id)
ORDER BY d.org_unit_id, e.employee_id
"""


DETECTORS = {
    "B01": sql_B01,
    "B02": sql_B02,
    "B03": sql_B03,
    "B04": sql_B04,
    "B05": sql_B05,
    "B06": sql_B06,
    "B07": sql_B07,
    "D01": sql_D01,
    "D02": sql_D02,
    "D05": sql_D05,
    "D06": sql_D06,
    "D07": sql_D07,
}


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _evidence(row: dict[str, Any], code: str, config: dict) -> str:
    """The evidence bundle's raw material: fields, peer context, attributions."""
    fields = {
        k: v
        for k, v in row.items()
        if not k.startswith("peer_") and k not in INTERNAL_COLUMNS
    }
    payload: dict[str, Any] = {"anomaly_code": code, "fields": fields}
    peer = {
        name: row.get(f"peer_{name}")
        for name in PEER_FIELDS
        if f"peer_{name}" in row
    }
    if peer and peer.get("cohort_key"):
        peer["employee_value"] = peer.pop("value", None)
        payload["peer_context"] = peer
    if row.get("attributions_json"):
        payload["feature_attributions"] = json.loads(row["attributions_json"])
    payload["metric"] = config.get("metric")
    return json.dumps(payload, default=str)


def _attribution_text(payload: str | None, top: int = 3) -> str:
    """`grade adds SAR 2,100, type of site adds SAR 900` -- the SHAP split, said.

    The model's per-driver contributions are already in riyals, so the sentence
    a reviewer reads is the attribution itself rather than a description of one.
    """
    if not payload:
        return "no single driver stands out"
    parts = []
    for item in json.loads(payload)[:top]:
        verb = "adds" if item["contribution"] >= 0 else "reduces by"
        parts.append(
            f"{item['label_en'].lower()} {verb} SAR {abs(item['contribution']):,.0f}"
        )
    return ", ".join(parts) or "no single driver stands out"


def _context(row: dict[str, Any]) -> dict[str, Any]:
    """What a description template can name: every column, peer fields unprefixed."""
    context = dict(row)
    for key, value in row.items():
        if key.startswith("peer_"):
            context.setdefault(key[len("peer_"):], value)
    context["first_period_label"] = period_label(row.get("first_period_paid"))
    context["last_period_label"] = period_label(row.get("last_period_paid"))
    context["attribution_text"] = _attribution_text(row.get("attributions_json"))
    if "manager_change_period" in row:
        context["manager_change_label"] = period_label(row["manager_change_period"])
    if "largest_period" in row:
        context["largest_period_label"] = period_label(row["largest_period"])
    return context


def run_peer(
    con: duckdb.DuckDBPyConnection,
    policy,
    *,
    codes: list[str] | None = None,
    log=None,
) -> L2Result:
    """Run every enabled layer-2 detector and return its findings.

    Preparation -- cohorts, robust statistics, the expected-salary model -- runs
    once and is shared; each detector is then one DuckDB query over it.
    """
    started = time.perf_counter()
    cohorts, salary = prepare(con, policy, log=log)
    result = L2Result(seconds=0.0, cohorts=cohorts, salary=salary)

    wanted = codes or sorted(DETECTORS)
    unknown = sorted(set(wanted) - set(DETECTORS))
    if unknown:
        raise L2Error(f"no layer-2 detector for {unknown}")

    built: list[str] = []
    for code in wanted:
        config = policy.peer_codes.get(code)
        if config is None:
            raise L2Error(f"peer_stats.yaml has no entry for {code}")
        if not config.get("enabled", True):
            continue
        built.append(code)
        code_started = time.perf_counter()
        sql = DETECTORS[code](policy)
        try:
            rows = con.execute(sql).fetchall()
        except duckdb.Error as exc:
            raise L2Error(f"{code}: detector query failed: {exc}") from exc
        names = [d[0] for d in (con.description or [])]
        missing = sorted(set(REQUIRED_COLUMNS) - set(names))
        if missing:
            raise L2Error(f"{code}: detector query does not return {missing}")

        severity = str(config["severity"])
        if severity not in SEVERITIES:
            raise L2Error(f"peer_stats.yaml: {code} severity {severity!r} unknown")
        employees: set[str] = set()
        for values in rows:
            row = {name: _plain(value) for name, value in zip(names, values)}
            context = _context(row)
            monthly = float(row.get("monthly_impact") or 0.0)
            cumulative = float(row.get("cumulative_impact") or 0.0)
            context["monthly_impact"] = monthly
            context["cumulative_impact"] = cumulative
            employees.add(str(row["employee_id"]))
            try:
                description = render(" ".join(str(config["description"]).split()),
                                     context)
                actions = [render(a, context)
                           for a in config.get("recommended_actions") or []]
            except RuleError as exc:
                raise L2Error(f"{code}: {exc}") from exc
            result.hits.append(
                {
                    "employee_id": row["employee_id"],
                    "anomaly_code": code,
                    "family": code[0],
                    "severity": severity,
                    "rule_name_en": str(config["name_en"]),
                    "rule_name_ar": str(config["name_ar"]),
                    "allowance_code": config.get("allowance_code"),
                    "regulatory_reference": str(config["regulatory_reference"]),
                    "period_from": int(row["first_period_paid"]),
                    "period_to": int(row["last_period_paid"]),
                    "months_flagged": int(row["months_paid"]),
                    "financial_impact_monthly": round(monthly, 2),
                    "financial_impact_cumulative": round(cumulative, 2),
                    "financial_impact_confidence": str(
                        config.get("impact_confidence", "estimated")
                    ),
                    "description": description,
                    "recommended_actions": actions,
                    "evidence_json": _evidence(row, code, config),
                }
            )
        elapsed = time.perf_counter() - code_started
        result.by_code[code] = len(rows)
        result.employees_by_code[code] = len(employees)
        result.seconds_by_code[code] = round(elapsed, 3)
        if log:
            log(f"  {code}  {len(rows):>6} findings  {len(employees):>5} employees"
                f"  {elapsed:6.2f}s")

    result.codes = tuple(built)
    result.seconds = round(time.perf_counter() - started, 3)
    return result
