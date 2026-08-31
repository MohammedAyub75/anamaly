"""The validation suite behind `validate` and the phase-1 gate.

The headline check is the last one: **every one of the 34 anomaly-code
predicates from `docs/ANOMALY_CATALOG.md`, evaluated against the clean dataset,
must return zero rows** -- reported per code, so a leak is visible as "A05: 12"
rather than as an unhelpful total.  If pass 1 leaks a violation, that violation
is an unlabelled anomaly and every recall figure downstream is wrong.

The predicates are written in SQL over the Parquet lake rather than reusing the
Python that generated it.  That is the point: a check that called the same
resolver would only prove the resolver agrees with itself.  DuckDB recomputes
the expected allowance amount from `policy/allowance_rules.yaml` independently,
joins the as-at assignment state with an ASOF join, and counts.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from policycore.packs import EDUCATION_ORDER, NATIONALITY_CLASSES

from .config import ScaleConfig, period_last_day
from .identifiers import is_valid_iban, is_valid_saudi_id
from .policy import DatagenPolicy
from .schemas import SCHEMAS

# Enumerations the lake must never stray outside of. Transcribed from
# docs/DATA_DICTIONARY.md; a value outside these is a generator bug.
ENUMS: dict[tuple[str, str], tuple[str, ...]] = {
    ("employee_master", "gender"): ("M", "F"),
    ("employee_master", "nationality_class"): NATIONALITY_CLASSES,
    ("employee_master", "marital_status"): ("single", "married", "divorced", "widowed"),
    ("employee_master", "education_level"): EDUCATION_ORDER,
    ("employee_master", "employment_type"): ("direct", "contractor", "secondee", "trainee"),
    ("employee_master", "contract_type"): ("permanent", "fixed_term", "temporary"),
    ("employee_master", "status"): ("active", "terminated", "suspended", "on_leave"),
    ("employee_master", "termination_reason"): (
        "resignation", "retirement", "end_of_contract", "dismissal",
    ),
    ("employee_master", "work_pattern"): (
        "regular", "rotation_28_28", "rotation_14_14", "shift", "remote", "hybrid",
    ),
    ("employee_master", "housing_type"): (
        "company_camp_bachelor", "company_family_housing", "allowance", "own",
    ),
    ("employee_master", "transport_mode"): ("company_bus", "allowance", "own"),
    ("employee_master", "payment_method"): ("bank_transfer", "cash", "cheque"),
    ("employee_master", "source_system"): ("sap_hr", "legacy_hr", "manual"),
    ("employee_master", "gosi_class"): ("saudi_full", "gcc_bilateral", "expat_hazards"),
    ("employee_master", "currency"): ("SAR",),
    ("employee_master", "service_band"): ("0-2", "2-5", "5-10", "10-20", "20+"),
    ("fact_assignment_history", "change_reason"): (
        "hire", "promotion", "transfer", "regrade", "increment", "acting",
        "return_from_acting", "termination",
    ),
    ("fact_bank_account", "change_reason"): (
        "initial", "employee_request", "bank_merger", "correction",
    ),
    ("fact_payroll_allowance", "amount_basis"): (
        "fixed", "pct_of_base", "grade_table", "site_table",
    ),
}

ROTATION_PATTERNS = ("rotation_28_28", "rotation_14_14")

# Thresholds that only exist as anomaly-injection magnitudes in the catalogue,
# not as policy dials. Named here so the predicates read as the catalogue does.
B04_JUMP_PCT = 8.0          # B04: month-over-month base change
D03_LEAVE_DAYS = 15         # D03: leave claimed in the same period as overtime
D03_OVERTIME_HOURS = 40
D05_ALLOWANCE_STEP = 0.20   # D05: allowance step as a share of base
D06_NET_STEP = 0.25         # D06: unexplained step change in net pay
D07_DRIFT = 1.25            # D07: section allowance load vs its own baseline
D07_MIN_MEMBERS = 8
C03_DORMANT_SCORE = 0.02
C03_DORMANT_PERIODS = 6
D02_RETRO_COUNT = 3
C06_NAME_SIMILARITY = 0.90


@dataclass
class Check:
    """One gate row."""

    name: str
    ok: bool
    detail: str


@dataclass
class Report:
    checks: list[Check]

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, bool(ok), detail))


# --------------------------------------------------------------------------
# Lake access
# --------------------------------------------------------------------------


def _glob(cfg: ScaleConfig, table: str) -> str:
    return str(cfg.table_dir(table) / "**" / "*.parquet").replace("\\", "/")


def connect(cfg: ScaleConfig) -> duckdb.DuckDBPyConnection:
    """A connection with one view per table, over the Parquet parts."""
    con = duckdb.connect()
    for table in SCHEMAS:
        con.execute(
            f"CREATE VIEW {table} AS SELECT * FROM read_parquet("
            + f"'{_glob(cfg, table)}', hive_partitioning=false)"
        )
    con.execute(
        """
        CREATE VIEW asat AS
        SELECT p.employee_id, p.period, p.base_pay, p.allowance_total, p.gross, p.net,
               p.paid_flag, p.cost_center AS paid_cost_center,
               h.grade AS asat_grade, h.work_site_id AS asat_site,
               h.org_unit_id AS asat_org, h.base_salary AS asat_salary
        FROM fact_payroll_monthly p
        ASOF JOIN fact_assignment_history h
          ON p.employee_id = h.employee_id
         AND last_day(make_date(p.period // 100, p.period % 100, 1)) >= h.effective_from
        """
    )
    return con


def _count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = con.execute(sql).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


# --------------------------------------------------------------------------
# Expected-amount SQL, generated from the policy pack
# --------------------------------------------------------------------------


def expected_amount_sql(policy: DatagenPolicy) -> str:
    """A CASE expression recomputing each allowance's policy amount in SQL.

    Generated from `policy/allowance_rules.yaml` rather than hard-coded, and
    evaluated by DuckDB rather than by the Python that wrote the rows -- so A07
    is a genuine second opinion on every amount in the lake.
    """
    branches: list[str] = []
    for code, allowance in sorted(policy.pack.allowances.items()):
        if allowance.amount_basis == "fixed":
            if allowance.per_dependent:
                cap = allowance.max_dependents or 99
                value = f"{allowance.amount} * least(e.dependents_in_kingdom, {cap})"
            else:
                value = f"{allowance.amount}"
        elif allowance.amount_basis == "pct_of_base":
            value = f"round(s.asat_salary * {allowance.rate_pct} / 100, 2)"
            if allowance.cap is not None:
                value = f"least({value}, {allowance.cap})"
        elif allowance.amount_basis == "grade_table":
            whens = " ".join(
                f"WHEN {g} THEN {v}" for g, v in sorted(allowance.grade_table.items())
            )
            value = f"CASE s.asat_grade {whens} ELSE 0 END"
        else:
            whens = " ".join(
                f"WHEN {t} THEN {v}" for t, v in sorted(allowance.site_table.items())
            )
            value = f"CASE st.hardship_tier {whens} ELSE 0 END"
        branches.append(f"WHEN '{code}' THEN {value}")
    return "CASE a.allowance_code " + " ".join(branches) + " ELSE NULL END"


def _education_case(column: str) -> str:
    whens = " ".join(f"WHEN '{level}' THEN {rank}" for rank, level in enumerate(EDUCATION_ORDER))
    return f"CASE {column} {whens} ELSE -1 END"


# --------------------------------------------------------------------------
# The 34 anomaly-code predicates
# --------------------------------------------------------------------------


def anomaly_predicates(cfg: ScaleConfig, policy: DatagenPolicy) -> dict[str, tuple[str, str]]:
    """`{code: (label, sql)}`; each query yields the `(employee_id, period)` it finds.

    Rows rather than a count, because pass 2 exists: the phase-1 gate asks how
    many of these are *unlabelled*, and the phase-2 gate asks whether every
    injected employee is among them. A bare count could answer neither.

    `period` is NULL where the finding is about the employee rather than about a
    month -- a duplicate identity, a grade outside its band, a manager cycle.
    """
    pack = policy.pack
    band = policy.pack.band_policy
    load = policy.pack.allowance_load
    overtime = policy.payroll["overtime"]
    expected = expected_amount_sql(policy)
    rotation = ", ".join(f"'{p}'" for p in ROTATION_PATTERNS)
    gosi_pairs = " ".join(
        f"WHEN '{k}' THEN '{v}'" for k, v in sorted(pack.gosi_class_by_nationality.items())
    )
    acting_max = pack.allowances["ACTING_ROLE"].max_consecutive_months
    relocation_max = pack.allowances["RELOCATION"].max_consecutive_months
    last_period_end = period_last_day(cfg.period_to)

    paid = """
        fact_payroll_allowance a
        JOIN asat s USING (employee_id, period)
        JOIN employee_master e ON e.employee_id = a.employee_id
        JOIN dim_site st ON st.site_id = s.asat_site
    """
    found = "SELECT a.employee_id, a.period FROM " + paid

    return {
        "A01": (
            "remote-site allowance at an ineligible site",
            f"{found} WHERE a.allowance_code = 'REMOTE_SITE' "
            + "AND a.amount > 0 AND (NOT st.remote_allowance_eligible OR st.site_class "
            + "IN ('hq','office','medical','training'))",
        ),
        "A02": (
            "hardship allowance at a tier-0 site",
            f"{found} WHERE a.allowance_code = 'HARDSHIP' "
            + "AND a.amount > 0 AND st.hardship_tier = 0",
        ),
        "A03": (
            "offshore allowance onshore",
            f"{found} WHERE a.allowance_code = 'OFFSHORE' "
            + "AND a.amount > 0 AND st.site_class <> 'offshore'",
        ),
        "A04": (
            "family assistance with no dependents",
            f"{found} WHERE a.allowance_code IN "
            + "('SCHOOL_ASSIST','FAMILY') AND a.amount > 0 AND "
            + "(e.dependents_count = 0 OR e.dependents_in_kingdom = 0)",
        ),
        "A05": (
            "housing allowance while company-housed",
            f"{found} WHERE a.allowance_code = 'HOUSING' "
            + "AND a.amount > 0 AND e.housing_type IN "
            + "('company_camp_bachelor','company_family_housing')",
        ),
        "A06": (
            "transport allowance on a company bus",
            f"{found} WHERE a.allowance_code IN ('TRANSPORT','FUEL') "
            + "AND a.amount > 0 AND e.transport_mode = 'company_bus'",
        ),
        "A07": (
            "amount outside the policy table",
            # Only where the allowance is payable at all: paying something the
            # employee has no claim to is an eligibility breach with its own
            # code, and counting it here as well would put two codes on one row.
            f"{found} WHERE ({expected}) > 0 AND abs(a.amount - ({expected})) > 1.00",
        ),
        "A08": (
            "grade outside the job band",
            "SELECT DISTINCT h.employee_id, NULL::INTEGER AS period "
            + "FROM fact_assignment_history h JOIN dim_job j "
            + "USING (job_code) WHERE h.grade < j.min_grade OR h.grade > j.max_grade",
        ),
        "A09": (
            "nationality-restricted benefit misapplied",
            f"{found} WHERE a.amount > 0 AND "
            + "((a.allowance_code = 'EXPAT_PREMIUM' AND e.nationality_class <> 'expat') "
            + "OR (a.allowance_code = 'SAUDI_DEV_SCHEME' AND e.nationality_class <> 'saudi')) "
            + "UNION ALL SELECT e.employee_id, NULL::INTEGER FROM employee_master e "
            + f"WHERE e.gosi_class <> (CASE e.nationality_class {gosi_pairs} ELSE 'unknown' END)",
        ),
        "A10": (
            "rotation allowance without a rotation pattern",
            f"{found} WHERE a.allowance_code IN "
            + f"('ROTATION','TRAVEL_TIME') AND a.amount > 0 AND e.work_pattern NOT IN ({rotation})",
        ),
        "A11": (
            "qualification below the job minimum",
            "SELECT e.employee_id, NULL::INTEGER AS period FROM employee_master e "
            + "JOIN dim_job j USING (job_code) "
            + f"WHERE {_education_case('e.education_level')} < {_education_case('j.min_education')} "
            + "UNION ALL SELECT e.employee_id, NULL::INTEGER FROM employee_master e "
            + "JOIN dim_job j USING (job_code) WHERE j.safety_critical AND ("
            + "len(j.required_certifications) > len(e.certifications) OR "
            + f"len(list_filter(e.certifications, c -> c.expiry <= DATE '{last_period_end}')) > 0)",
        ),
        "A12": (
            "time-limited allowance beyond its maximum",
            f"{found} WHERE a.amount > 0 AND ("
            + "(a.allowance_code = 'ACTING_ROLE' AND (e.acting_role_since IS NULL OR "
            + "date_diff('month', e.acting_role_since, "
            + f"last_day(make_date(a.period // 100, a.period % 100, 1))) > {acting_max})) OR "
            + "(a.allowance_code = 'RELOCATION' AND e.months_since_site_change > "
            + f"{relocation_max}))",
        ),
        "B01": (
            "salary above the band maximum",
            "SELECT e.employee_id, NULL::INTEGER AS period FROM employee_master e "
            + "JOIN dim_grade g ON g.grade = e.grade "
            + "AND g.nationality_class = e.nationality_class "
            + f"WHERE e.base_salary > g.salary_max * {1 + float(band['overpayment_tolerance_pct']) / 100}",
        ),
        "B02": (
            "salary below the band minimum",
            "SELECT e.employee_id, NULL::INTEGER AS period FROM employee_master e "
            + "JOIN dim_grade g ON g.grade = e.grade "
            + "AND g.nationality_class = e.nationality_class "
            + f"WHERE e.base_salary < g.salary_min * {1 - float(band['underpayment_tolerance_pct']) / 100}",
        ),
        "B03": (
            "allowance load above the hard ceiling",
            "SELECT employee_id, NULL::INTEGER AS period FROM employee_master "
            + f"WHERE allowance_ratio > {load['hard_ceiling_ratio']} "
            + "UNION ALL SELECT employee_id, period FROM fact_payroll_monthly "
            + f"WHERE base_pay > 0 AND allowance_total > base_pay * {load['hard_ceiling_ratio']}",
        ),
        "B04": (
            "salary jump with no assignment record",
            "WITH steps AS (SELECT employee_id, period, base_pay, "
            + "lag(base_pay) OVER (PARTITION BY employee_id ORDER BY period) AS prior "
            + "FROM fact_payroll_monthly WHERE base_pay > 0) "
            + "SELECT s.employee_id, s.period FROM steps s WHERE s.prior IS NOT NULL "
            + f"AND s.prior > 0 AND abs(s.base_pay - s.prior) > s.prior * {B04_JUMP_PCT / 100} "
            + "AND NOT EXISTS (SELECT 1 FROM fact_assignment_history h "
            + "WHERE h.employee_id = s.employee_id AND h.effective_from BETWEEN "
            + "make_date(s.period // 100, s.period % 100, 1) - INTERVAL 1 MONTH AND "
            + "last_day(make_date(s.period // 100, s.period % 100, 1)) + INTERVAL 1 MONTH)",
        ),
        "B05": (
            "overtime beyond base pay or the legal maximum",
            "SELECT employee_id, period FROM fact_payroll_monthly "
            + "WHERE overtime_pay > base_pay "
            + f"OR overtime_hours > {overtime['legal_monthly_max_hours']}",
        ),
        "B06": (
            "top-decile bonus on a bottom rating",
            "WITH cut AS (SELECT quantile_cont(bonus, 0.9) AS threshold "
            + "FROM fact_payroll_monthly WHERE bonus > 0) "
            + "SELECT p.employee_id, p.period FROM fact_payroll_monthly p "
            + "JOIN employee_master e USING (employee_id), cut "
            + "WHERE p.bonus > 0 AND p.bonus >= cut.threshold "
            + "AND coalesce(e.performance_rating_y1, 3) <= 2 "
            + "AND coalesce(e.performance_rating_y2, 3) <= 2 "
            + "AND coalesce(e.performance_rating_y3, 3) <= 2",
        ),
        "B07": (
            "increments above the policy frequency",
            "WITH inc AS (SELECT employee_id, effective_from FROM fact_assignment_history "
            + "WHERE change_reason = 'increment') "
            + "SELECT DISTINCT a.employee_id, NULL::INTEGER AS period FROM inc a "
            + "JOIN inc b ON a.employee_id = b.employee_id "
            + "AND b.effective_from > a.effective_from AND b.effective_from < "
            + "a.effective_from + INTERVAL 12 MONTH",
        ),
        "C01": (
            "IBAN shared across employees",
            # A declared married couple sharing a family account is legitimate,
            # and one person on the payroll twice is C06 -- a different finding
            # with a different remedy. What is left is what C01 means: unrelated
            # employees paid into one account.
            "WITH shared AS (SELECT iban FROM fact_bank_account "
            + "GROUP BY iban HAVING count(DISTINCT employee_id) > 1), "
            + "pairs AS (SELECT DISTINCT l.employee_id AS left_id, r.employee_id AS right_id "
            + "FROM fact_bank_account l JOIN fact_bank_account r "
            + "ON l.iban = r.iban AND l.employee_id < r.employee_id "
            + "JOIN shared ON shared.iban = l.iban), "
            + "unrelated AS (SELECT p.* FROM pairs p "
            + "JOIN employee_master a ON a.employee_id = p.left_id "
            + "JOIN employee_master b ON b.employee_id = p.right_id "
            + "WHERE NOT (a.spouse_employee_id IS NOT DISTINCT FROM b.employee_id "
            + "AND b.spouse_employee_id IS NOT DISTINCT FROM a.employee_id) "
            + "AND NOT (a.dob = b.dob AND jaro_winkler_similarity("
            + f"a.name_en_normalised, b.name_en_normalised) >= {C06_NAME_SIMILARITY})) "
            + "SELECT left_id AS employee_id, NULL::INTEGER AS period FROM unrelated "
            + "UNION ALL SELECT right_id, NULL::INTEGER FROM unrelated",
        ),
        "C02": (
            "duplicate national id or iqama",
            "SELECT e.employee_id, NULL::INTEGER AS period FROM employee_master e "
            + "JOIN (SELECT national_id AS id FROM employee_master "
            + "WHERE national_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1 "
            + "UNION ALL SELECT iqama_no FROM employee_master "
            + "WHERE iqama_no IS NOT NULL GROUP BY 1 HAVING count(*) > 1) d "
            + "ON d.id = e.national_id OR d.id = e.iqama_no",
        ),
        "C03": (
            "ghost employee",
            "WITH dormant AS (SELECT a.employee_id, count(*) AS quiet "
            + "FROM fact_system_activity_monthly a JOIN fact_payroll_monthly p "
            + "USING (employee_id, period) "
            + f"WHERE a.activity_score < {C03_DORMANT_SCORE} AND p.paid_flag GROUP BY 1) "
            + "SELECT employee_id, NULL::INTEGER AS period FROM dormant "
            + f"WHERE quiet >= {C03_DORMANT_PERIODS}",
        ),
        "C04": (
            "terminated employee still on payroll",
            "SELECT p.employee_id, p.period FROM fact_payroll_monthly p "
            + "JOIN employee_master e USING (employee_id) "
            + "WHERE e.termination_date IS NOT NULL AND p.paid_flag "
            + "AND p.period > (year(e.termination_date) * 100 + month(e.termination_date)) "
            + "AND (p.base_pay > 0 OR EXISTS (SELECT 1 FROM fact_payroll_allowance a "
            + "WHERE a.employee_id = p.employee_id AND a.period = p.period "
            + "AND a.allowance_code <> 'SEVERANCE'))",
        ),
        "C05": (
            "self-approval or a manager cycle",
            "SELECT DISTINCT employee_id, NULL::INTEGER AS period "
            + "FROM fact_assignment_history WHERE approved_by = employee_id "
            + "UNION ALL (WITH RECURSIVE walk(root, node, depth) AS ("
            + "SELECT employee_id, manager_id, 1 FROM employee_master "
            + "WHERE manager_id IS NOT NULL UNION ALL "
            + "SELECT w.root, e.manager_id, w.depth + 1 FROM walk w "
            + "JOIN employee_master e ON e.employee_id = w.node "
            + "WHERE w.node IS NOT NULL AND w.node <> w.root AND w.depth < 30) "
            + "SELECT DISTINCT root, NULL::INTEGER FROM walk WHERE node = root)",
        ),
        "C06": (
            "near-duplicate identity",
            "WITH twins AS (SELECT a.employee_id AS left_id, b.employee_id AS right_id "
            + "FROM employee_master a JOIN employee_master b "
            + "ON a.employee_id < b.employee_id AND a.dob = b.dob "
            + "AND (a.iban = b.iban OR a.national_id = b.national_id "
            + "OR a.iqama_no = b.iqama_no) "
            + "WHERE jaro_winkler_similarity(a.name_en_normalised, b.name_en_normalised) "
            + f">= {C06_NAME_SIMILARITY}) "
            + "SELECT left_id AS employee_id, NULL::INTEGER AS period FROM twins "
            + "UNION ALL SELECT right_id, NULL::INTEGER FROM twins",
        ),
        "C07": (
            "active payroll with an expired iqama",
            "SELECT p.employee_id, p.period FROM fact_payroll_monthly p "
            + "JOIN employee_master e USING (employee_id) "
            + "WHERE e.iqama_expiry IS NOT NULL AND e.status = 'active' "
            + "AND p.paid_flag AND e.iqama_expiry < "
            + "last_day(make_date(p.period // 100, p.period % 100, 1))",
        ),
        "C08": (
            "payroll charged to a foreign cost centre",
            "SELECT s.employee_id, s.period FROM asat s JOIN dim_org_unit o "
            + "ON o.org_unit_id = s.asat_org WHERE s.paid_cost_center <> o.cost_center",
        ),
        "D01": (
            "promotion velocity outlier",
            "WITH g AS (SELECT employee_id, effective_from, grade FROM fact_assignment_history) "
            + "SELECT DISTINCT a.employee_id, NULL::INTEGER AS period FROM g a "
            + "JOIN g b ON a.employee_id = b.employee_id "
            + "AND b.effective_from > a.effective_from AND b.effective_from <= "
            + "a.effective_from + INTERVAL 24 MONTH "
            + f"WHERE b.grade - a.grade > {int(band['max_grade_jump_per_24m'])}",
        ),
        "D02": (
            "repeated retroactive adjustments",
            "SELECT employee_id, NULL::INTEGER AS period FROM fact_payroll_monthly "
            + f"WHERE retro_adjustment > 0 GROUP BY 1 HAVING count(*) >= {D02_RETRO_COUNT}",
        ),
        "D03": (
            "leave and overtime in the same period",
            "SELECT employee_id, period FROM fact_attendance_monthly "
            + f"WHERE days_leave >= {D03_LEAVE_DAYS} AND overtime_hours >= {D03_OVERTIME_HOURS}",
        ),
        "D04": (
            "attendance beyond the physical maximum",
            "SELECT a.employee_id, a.period FROM fact_attendance_monthly a "
            + "JOIN dim_calendar c USING (period) WHERE a.days_worked + a.days_leave "
            + "+ a.absence_days > c.calendar_days",
        ),
        "D05": (
            "allowance step after a manager change",
            # A promotion is excluded: crossing into a new grade band adds
            # entitlements by policy, and the promotion row explains the step.
            # What D05 looks for is allowances appearing after a manager change
            # with nothing in the record that accounts for them.
            "WITH moves AS (SELECT employee_id, effective_from, manager_id, grade, "
            + "lag(manager_id) OVER (PARTITION BY employee_id ORDER BY effective_from) "
            + "AS prior_manager, "
            + "lag(grade) OVER (PARTITION BY employee_id ORDER BY effective_from) "
            + "AS prior_grade FROM fact_assignment_history), "
            + "step AS (SELECT employee_id, period, base_pay, allowance_total, "
            + "lag(allowance_total) OVER (PARTITION BY employee_id ORDER BY period) AS prior "
            + "FROM fact_payroll_monthly) "
            + "SELECT DISTINCT s.employee_id, s.period FROM step s "
            + "JOIN moves m ON m.employee_id = s.employee_id "
            + "AND m.prior_manager IS NOT NULL "
            + "AND m.manager_id IS DISTINCT FROM m.prior_manager "
            + "AND m.grade = m.prior_grade "
            + "AND m.effective_from BETWEEN "
            + "make_date(s.period // 100, s.period % 100, 1) - INTERVAL 2 MONTH AND "
            + "last_day(make_date(s.period // 100, s.period % 100, 1)) "
            + "WHERE s.prior IS NOT NULL AND s.base_pay > 0 "
            + f"AND s.allowance_total - s.prior >= s.base_pay * {D05_ALLOWANCE_STEP} "
            + "AND NOT EXISTS (SELECT 1 FROM moves g WHERE g.employee_id = s.employee_id "
            + "AND g.grade IS DISTINCT FROM g.prior_grade AND g.effective_from BETWEEN "
            + "make_date(s.period // 100, s.period % 100, 1) - INTERVAL 2 MONTH AND "
            + "last_day(make_date(s.period // 100, s.period % 100, 1)))",
        ),
        "D06": (
            "unexplained personal change-point",
            # Measured on the standing part of net pay -- base plus allowances,
            # less the standing deductions. Overtime, the bonus month, a retro
            # correction and an absence deduction are all *explained* variation
            # that a reviewer can already account for.
            #
            # A month where base pay itself moved is excluded for the same
            # reason: a salary that changed is a visible cause, and a salary that
            # changed with no paperwork behind it is B04's finding, not this one.
            "WITH step AS (SELECT employee_id, period, base_pay, "
            + "lag(base_pay) OVER (PARTITION BY employee_id ORDER BY period) AS prior_base, "
            + "base_pay + allowance_total - gosi_employee - loan_deduction AS standing, "
            + "lag(base_pay + allowance_total - gosi_employee - loan_deduction) "
            + "OVER (PARTITION BY employee_id ORDER BY period) AS prior "
            + "FROM fact_payroll_monthly WHERE base_pay > 0) "
            + "SELECT s.employee_id, s.period FROM step s WHERE s.prior IS NOT NULL "
            + "AND s.prior > 0 AND s.base_pay = s.prior_base "
            + f"AND abs(s.standing - s.prior) > s.prior * {D06_NET_STEP} "
            + "AND NOT EXISTS (SELECT 1 FROM fact_assignment_history h "
            + "WHERE h.employee_id = s.employee_id AND h.effective_from BETWEEN "
            + "make_date(s.period // 100, s.period % 100, 1) - INTERVAL 1 MONTH AND "
            + "last_day(make_date(s.period // 100, s.period % 100, 1)) + INTERVAL 1 MONTH)",
        ),
        "D07": (
            "section-wide allowance drift",
            # Compared against the unit's own earlier baseline, over the
            # employees present in BOTH windows, and on allowance load as a
            # share of base. A raw monthly total drifts whenever the section
            # hires or loses somebody, which is turnover, not a scheme.
            "WITH ranked AS (SELECT period, row_number() OVER (ORDER BY period) AS rn, "
            + "count(*) OVER () AS total FROM dim_calendar), "
            + "load AS (SELECT e.org_unit_id, p.employee_id, r.rn, r.total, "
            + "p.allowance_total / p.base_pay AS ratio "
            + "FROM fact_payroll_monthly p JOIN employee_master e USING (employee_id) "
            + "JOIN ranked r ON r.period = p.period WHERE p.base_pay > 0), "
            + "tagged AS (SELECT *, rn <= 12 AS baseline, rn > total - 6 AS recent FROM load), "
            + "stable AS (SELECT org_unit_id, employee_id FROM tagged GROUP BY 1, 2 "
            + "HAVING count(*) FILTER (WHERE baseline) > 0 "
            + "AND count(*) FILTER (WHERE recent) > 0), "
            + "unit AS (SELECT t.org_unit_id, "
            + "avg(t.ratio) FILTER (WHERE t.baseline) AS base_ratio, "
            + "avg(t.ratio) FILTER (WHERE t.recent) AS recent_ratio, "
            + "count(DISTINCT t.employee_id) AS members "
            + "FROM tagged t JOIN stable s USING (org_unit_id, employee_id) GROUP BY 1) "
            + "SELECT DISTINCT s.employee_id, NULL::INTEGER AS period FROM stable s "
            + f"JOIN unit u USING (org_unit_id) WHERE u.members >= {D07_MIN_MEMBERS} "
            + f"AND u.base_ratio > 0 AND u.recent_ratio > u.base_ratio * {D07_DRIFT}",
        ),
    }


def found_count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    """How many rows a predicate finds, labelled or not."""
    return _count(con, f"SELECT count(*) FROM ({sql}) hits")


def unlabelled_count(con: duckdb.DuckDBPyConnection, code: str, sql: str) -> int:
    """Predicate hits that no ground-truth row accounts for.

    This is the invariant both gates rest on. Before pass 2 the label tables are
    empty and it reduces to "the clean population contains no policy violation
    at all", which is what phase 1 asserted. After pass 2 it says the stronger
    thing: every violation in the lake is one pass 2 wrote down, either as an
    injected anomaly or as a planted legitimate look-alike.
    """
    return _count(
        con,
        f"SELECT count(*) FROM ({sql}) hits WHERE NOT EXISTS ("
        + "SELECT 1 FROM labels_anomaly l WHERE l.employee_id = hits.employee_id "
        + f"AND l.anomaly_code = '{code}' AND (hits.period IS NULL "
        + "OR hits.period BETWEEN l.period_from AND l.period_to)) "
        + "AND NOT EXISTS (SELECT 1 FROM labels_confounder c "
        + f"WHERE c.employee_id = hits.employee_id AND c.confounds_code = '{code}')",
    )


def labelled_employees(con: duckdb.DuckDBPyConnection, code: str, sql: str) -> int:
    """Injected employees this predicate actually finds -- recall against truth."""
    return _count(
        con,
        "SELECT count(*) FROM (SELECT employee_id FROM labels_anomaly "
        + f"WHERE anomaly_code = '{code}') l WHERE EXISTS ("
        + f"SELECT 1 FROM ({sql}) hits WHERE hits.employee_id = l.employee_id)",
    )


def label_filter(code: str, column: str = "e.employee_id") -> str:
    """SQL excluding employees deliberately injected with `code`.

    A handful of the domain rules police exactly what an anomaly code breaks --
    a salary inside its band is B01/B02, a grade inside its job band is A08, a
    month with more days than it has is D04. Those rules stay absolute for the
    rest of the population and defer to the ground truth for the injected rows.
    """
    return (f"NOT EXISTS (SELECT 1 FROM labels_anomaly l WHERE l.employee_id = {column} "
            + f"AND l.anomaly_code IN ({', '.join(repr(c) for c in code.split(','))}))")


# --------------------------------------------------------------------------
# The suite
# --------------------------------------------------------------------------


def run(
    cfg: ScaleConfig,
    policy: DatagenPolicy | None = None,
    include_determinism: bool = True,
    include_git: bool = True,
) -> Report:
    policy = policy or DatagenPolicy.load()
    report = Report(checks=[])
    if not cfg.manifest_path.exists():
        report.add("dataset present", False, f"no manifest at {cfg.manifest_path}")
        return report
    manifest = json.loads(cfg.manifest_path.read_text(encoding="utf-8"))
    con = connect(cfg)
    try:
        _check_schema(cfg, report)
        _check_row_counts(con, cfg, manifest, report)
        _check_referential(con, report)
        _check_domain(con, cfg, policy, report)
        _check_arithmetic(con, report)
        _check_policy_digest(policy, manifest, report)
        _check_anomaly_predicates(con, cfg, policy, report)
    finally:
        con.close()
    if include_determinism:
        _check_determinism(cfg, policy, report)
    if include_git:
        _check_git(report)
    return report


def _check_schema(cfg: ScaleConfig, report: Report) -> None:
    """Names, order and types must match docs/DATA_DICTIONARY.md exactly."""
    mismatched: list[str] = []
    for table, schema in SCHEMAS.items():
        parts = sorted(cfg.table_dir(table).rglob("*.parquet"))
        if not parts:
            mismatched.append(f"{table}(no files)")
            continue
        written = pq.read_schema(parts[0])
        if list(written.names) != list(schema.names):
            mismatched.append(f"{table}(columns)")
        elif any(a.type != b.type for a, b in zip(written, schema, strict=True)):
            bad = [a.name for a, b in zip(written, schema, strict=True) if a.type != b.type]
            mismatched.append(f"{table}({','.join(bad)})")
    report.add(
        "schema matches dictionary",
        not mismatched,
        f"{len(SCHEMAS)} tables" if not mismatched else f"mismatched={mismatched[:4]}",
    )


def _check_row_counts(
    con: duckdb.DuckDBPyConnection, cfg: ScaleConfig, manifest: dict, report: Report
) -> None:
    declared = manifest["row_counts"]
    actual = {table: _count(con, f"SELECT count(*) FROM {table}") for table in SCHEMAS}
    wrong = {t: (declared.get(t), actual[t]) for t in SCHEMAS if declared.get(t) != actual[t]}
    report.add(
        "row counts match manifest",
        not wrong,
        f"{sum(actual.values()):,} rows" if not wrong else f"mismatched={wrong}",
    )
    report.add(
        "employee count matches manifest",
        actual["employee_master"] == manifest["employee_count"],
        f"{actual['employee_master']:,} employees",
    )
    duplicates = _count(
        con,
        "SELECT count(*) FROM (SELECT employee_id, period FROM fact_payroll_monthly "
        + "GROUP BY 1, 2 HAVING count(*) > 1)",
    )
    # A payroll series must be unbroken between an employee's first and last
    # paid period: the one legitimate post-termination payment is the following
    # month's settlement, so there is never a hole to allow for.
    gaps = _count(
        con,
        """
        SELECT count(*) FROM (
          WITH span AS (
            SELECT employee_id, min(period) AS lo, max(period) AS hi,
                   count(*) AS paid_rows
            FROM fact_payroll_monthly GROUP BY 1)
          SELECT s.employee_id FROM span s
          JOIN dim_calendar c ON c.period BETWEEN s.lo AND s.hi
          GROUP BY s.employee_id, s.paid_rows HAVING count(*) <> s.paid_rows)
        """,
    )
    report.add(
        "payroll one row per active period",
        duplicates == 0 and gaps == 0,
        "no duplicates, no gaps" if not (duplicates or gaps)
        else f"duplicates={duplicates}, gaps={gaps}",
    )


def _check_referential(con: duckdb.DuckDBPyConnection, report: Report) -> None:
    orphans: dict[str, int] = {}
    checks = [
        ("employee_master.work_site_id", "employee_master e LEFT JOIN dim_site d "
         + "ON d.site_id = e.work_site_id WHERE d.site_id IS NULL"),
        ("employee_master.org_unit_id", "employee_master e LEFT JOIN dim_org_unit d "
         + "ON d.org_unit_id = e.org_unit_id WHERE d.org_unit_id IS NULL"),
        ("employee_master.job_code", "employee_master e LEFT JOIN dim_job d "
         + "ON d.job_code = e.job_code WHERE d.job_code IS NULL"),
        ("employee_master.manager_id", "employee_master e LEFT JOIN employee_master m "
         + "ON m.employee_id = e.manager_id WHERE e.manager_id IS NOT NULL "
         + "AND m.employee_id IS NULL"),
        ("employee_master.spouse_employee_id", "employee_master e LEFT JOIN employee_master s "
         + "ON s.employee_id = e.spouse_employee_id WHERE e.spouse_employee_id IS NOT NULL "
         + "AND s.employee_id IS NULL"),
        ("dim_site.region_code", "dim_site s LEFT JOIN dim_region r "
         + "ON r.region_code = s.region_code WHERE r.region_code IS NULL"),
        ("dim_org_unit.parent", "dim_org_unit o LEFT JOIN dim_org_unit p "
         + "ON p.org_unit_id = o.parent_org_unit_id WHERE o.parent_org_unit_id IS NOT NULL "
         + "AND p.org_unit_id IS NULL"),
        ("fact_payroll_monthly.employee_id", "fact_payroll_monthly f LEFT JOIN employee_master e "
         + "USING (employee_id) WHERE e.employee_id IS NULL"),
        ("fact_payroll_allowance.parent", "fact_payroll_allowance a LEFT JOIN "
         + "fact_payroll_monthly p USING (employee_id, period) WHERE p.employee_id IS NULL"),
        ("fact_payroll_allowance.code", "fact_payroll_allowance a LEFT JOIN dim_allowance d "
         + "ON d.allowance_code = a.allowance_code WHERE d.allowance_code IS NULL"),
        ("fact_assignment_history.employee_id", "fact_assignment_history f "
         + "LEFT JOIN employee_master e USING (employee_id) WHERE e.employee_id IS NULL"),
        ("fact_assignment_history.manager_id", "fact_assignment_history f "
         + "LEFT JOIN employee_master m ON m.employee_id = f.manager_id "
         + "WHERE f.manager_id IS NOT NULL AND m.employee_id IS NULL"),
        ("fact_assignment_history.approved_by", "fact_assignment_history f "
         + "LEFT JOIN employee_master a ON a.employee_id = f.approved_by "
         + "WHERE f.approved_by IS NOT NULL AND a.employee_id IS NULL"),
        ("fact_assignment_history.org_unit_id", "fact_assignment_history f "
         + "LEFT JOIN dim_org_unit o USING (org_unit_id) WHERE o.org_unit_id IS NULL"),
        ("fact_attendance_monthly.employee_id", "fact_attendance_monthly f "
         + "LEFT JOIN employee_master e USING (employee_id) WHERE e.employee_id IS NULL"),
        ("fact_bank_account.employee_id", "fact_bank_account f "
         + "LEFT JOIN employee_master e USING (employee_id) WHERE e.employee_id IS NULL"),
        ("fact_system_activity_monthly.employee_id", "fact_system_activity_monthly f "
         + "LEFT JOIN employee_master e USING (employee_id) WHERE e.employee_id IS NULL"),
    ]
    for name, sql in checks:
        found = _count(con, f"SELECT count(*) FROM {sql}")
        if found:
            orphans[name] = found
    report.add(
        "no orphan foreign keys",
        not orphans,
        f"{len(checks)} relationships" if not orphans else f"orphans={orphans}",
    )

    overlaps = _count(
        con,
        "SELECT count(*) FROM (SELECT employee_id, effective_from, effective_to, "
        + "lead(effective_from) OVER (PARTITION BY employee_id ORDER BY effective_from) AS nxt "
        + "FROM fact_assignment_history) WHERE nxt IS NOT NULL "
        + "AND (effective_to IS NULL OR effective_to + INTERVAL 1 DAY <> nxt)",
    )
    open_rows = _count(
        con,
        "SELECT count(*) FROM (SELECT employee_id, count(*) FILTER (WHERE effective_to IS NULL) "
        + "AS open FROM fact_assignment_history GROUP BY 1) WHERE open <> 1",
    )
    starts = _count(
        con,
        "SELECT count(*) FROM (SELECT employee_id, min(effective_from) AS first "
        + "FROM fact_assignment_history GROUP BY 1) f JOIN employee_master e USING (employee_id) "
        + "WHERE f.first <> e.hire_date",
    )
    report.add(
        "assignment intervals contiguous",
        not (overlaps or open_rows or starts),
        "no gaps or overlaps" if not (overlaps or open_rows or starts)
        else f"gaps={overlaps}, open={open_rows}, bad_start={starts}",
    )

    drift = _count(
        con,
        "SELECT count(*) FROM employee_master e JOIN fact_assignment_history h "
        + "USING (employee_id) WHERE h.effective_to IS NULL AND ("
        + "e.grade <> h.grade OR e.job_code <> h.job_code "
        + "OR e.org_unit_id <> h.org_unit_id OR e.work_site_id <> h.work_site_id "
        + "OR e.base_salary <> h.base_salary "
        + "OR e.manager_id IS DISTINCT FROM h.manager_id)",
    )
    report.add(
        "master row is the open interval",
        drift == 0,
        "grade, job, unit, site, salary, manager agree" if not drift
        else f"drifted={drift}",
    )

    unrooted = _count(
        con,
        "WITH RECURSIVE up(unit, node, depth) AS ("
        + "SELECT org_unit_id, parent_org_unit_id, 1 FROM dim_org_unit UNION ALL "
        + "SELECT u.unit, o.parent_org_unit_id, u.depth + 1 FROM up u "
        + "JOIN dim_org_unit o ON o.org_unit_id = u.node WHERE u.node IS NOT NULL AND u.depth < 12) "
        + "SELECT count(*) FROM (SELECT unit FROM up GROUP BY unit "
        + "HAVING count(*) FILTER (WHERE node IS NULL) = 0)",
    )
    report.add(
        "org chains reach level 1",
        unrooted == 0,
        "every unit rooted" if not unrooted else f"unrooted={unrooted}",
    )


def _check_domain(
    con: duckdb.DuckDBPyConnection,
    cfg: ScaleConfig,
    policy: DatagenPolicy,
    report: Report,
) -> None:
    bad_enum: dict[str, int] = {}
    for (table, column), allowed in ENUMS.items():
        values = ", ".join(f"'{v}'" for v in allowed)
        found = _count(
            con,
            f"SELECT count(*) FROM {table} WHERE {column} IS NOT NULL "
            + f"AND {column} NOT IN ({values})",
        )
        if found:
            bad_enum[f"{table}.{column}"] = found
    report.add(
        "enum values in domain",
        not bad_enum,
        f"{len(ENUMS)} columns" if not bad_enum else f"invalid={bad_enum}",
    )

    ibans = [row[0] for row in con.execute(
        "SELECT DISTINCT iban FROM fact_bank_account UNION "
        + "SELECT DISTINCT iban FROM employee_master"
    ).fetchall()]
    bad_iban = [v for v in ibans if not is_valid_iban(v)]
    report.add(
        "IBAN check digits (MOD-97)",
        not bad_iban,
        f"{len(ibans):,} accounts" if not bad_iban else f"invalid={bad_iban[:3]}",
    )

    ids = [row[0] for row in con.execute(
        "SELECT national_id FROM employee_master WHERE national_id IS NOT NULL UNION ALL "
        + "SELECT iqama_no FROM employee_master WHERE iqama_no IS NOT NULL"
    ).fetchall()]
    bad_ids = [v for v in ids if not is_valid_saudi_id(v)]
    report.add(
        "national id / iqama check digits",
        not bad_ids,
        f"{len(ids):,} identifiers" if not bad_ids else f"invalid={bad_ids[:3]}",
    )

    outside = _sites_outside_region(policy)
    report.add(
        "site coordinates inside region",
        not outside,
        f"{len(policy.pack.sites)} sites" if not outside else f"outside={outside[:4]}",
    )

    violations: dict[str, int] = {}
    domain_rules = {
        "dependents_in_kingdom": "SELECT count(*) FROM employee_master "
        + "WHERE dependents_in_kingdom > dependents_count",
        "attendance_days": "SELECT count(*) FROM fact_attendance_monthly a "
        + "JOIN dim_calendar c USING (period) "
        + "WHERE a.days_worked + a.days_leave + a.absence_days > c.calendar_days "
        + f"AND {label_filter('D04', 'a.employee_id')}",
        # Three of these rules police exactly what an anomaly code breaks, so
        # they hold for the population and defer to the ground truth for the
        # rows pass 2 broke on purpose.
        "grade_in_job_band": "SELECT count(*) FROM employee_master e JOIN dim_job j "
        + "USING (job_code) WHERE (e.grade < j.min_grade OR e.grade > j.max_grade) "
        + f"AND {label_filter('A08')}",
        "salary_in_band": "SELECT count(*) FROM employee_master e JOIN dim_grade g "
        + "ON g.grade = e.grade AND g.nationality_class = e.nationality_class "
        + "WHERE (e.base_salary < g.salary_min OR e.base_salary > g.salary_max) "
        + f"AND {label_filter('B01,B02')}",
        "termination_consistency": "SELECT count(*) FROM employee_master "
        + "WHERE (status = 'terminated') <> (termination_date IS NOT NULL)",
        "bus_route_consistency": "SELECT count(*) FROM employee_master "
        + "WHERE (transport_mode = 'company_bus') <> (company_bus_route_id IS NOT NULL)",
        "rotation_cycle_consistency": "SELECT count(*) FROM employee_master "
        + "WHERE (work_pattern IN ('rotation_28_28','rotation_14_14')) "
        + "<> (rotation_cycle_days IS NOT NULL)",
        "acting_since_consistency": "SELECT count(*) FROM employee_master "
        + "WHERE acting_role_flag <> (acting_role_since IS NOT NULL)",
        "identity_by_nationality": "SELECT count(*) FROM employee_master WHERE "
        + "(nationality_class = 'saudi') <> (national_id IS NOT NULL) "
        + "OR (nationality_class = 'saudi') = (iqama_no IS NOT NULL)",
        "period_window": "SELECT count(*) FROM fact_payroll_monthly p "
        + "LEFT JOIN dim_calendar c USING (period) WHERE c.period IS NULL",
    }
    for name, sql in domain_rules.items():
        found = _count(con, sql)
        if found:
            violations[name] = found
    report.add(
        "domain rules hold",
        not violations,
        f"{len(domain_rules)} rules" if not violations else f"violated={violations}",
    )


def _check_arithmetic(con: duckdb.DuckDBPyConnection, report: Report) -> None:
    gross = _count(
        con,
        "SELECT count(*) FROM fact_payroll_monthly WHERE gross <> "
        + "base_pay + allowance_total + overtime_pay + bonus + retro_adjustment",
    )
    net = _count(
        con,
        "SELECT count(*) FROM fact_payroll_monthly WHERE net <> "
        + "gross - gosi_employee - loan_deduction - absence_deduction",
    )
    report.add(
        "gross and net reconcile",
        not (gross or net),
        "to the cent" if not (gross or net) else f"gross={gross}, net={net}",
    )

    mismatch = _count(
        con,
        "SELECT count(*) FROM (SELECT p.employee_id, p.period, p.allowance_total, "
        + "coalesce(sum(a.amount), 0) AS child FROM fact_payroll_monthly p "
        + "LEFT JOIN fact_payroll_allowance a USING (employee_id, period) "
        + "GROUP BY 1, 2, 3) WHERE allowance_total <> child",
    )
    report.add(
        "allowance_total matches child rows",
        mismatch == 0,
        "every employee-period" if not mismatch else f"mismatched={mismatch}",
    )

    negative = _count(
        con, "SELECT count(*) FROM fact_payroll_allowance WHERE amount <= 0"
    )
    report.add(
        "no zero or negative allowance rows",
        negative == 0,
        "all positive" if not negative else f"non-positive={negative}",
    )


def _check_policy_digest(policy: DatagenPolicy, manifest: dict, report: Report) -> None:
    """A manifest digest that no longer matches means the lake is stale."""
    stale = sorted(
        name
        for name, digest in policy.pack.digest.items()
        if manifest.get("policy_digest", {}).get(name) != digest
    )
    report.add(
        "policy digest current",
        not stale,
        f"{len(policy.pack.digest)} packs" if not stale else f"stale={stale}",
    )


def _check_anomaly_predicates(
    con: duckdb.DuckDBPyConnection,
    cfg: ScaleConfig,
    policy: DatagenPolicy,
    report: Report,
) -> None:
    """The headline check: all 34 codes, each reported separately.

    What must be zero is the *unlabelled* count. On a clean lake the label
    tables are empty and that is the same assertion phase 1 made; on an injected
    one it says every violation present is one pass 2 wrote down.
    """
    for code, (label, sql) in anomaly_predicates(cfg, policy).items():
        try:
            leaked = unlabelled_count(con, code, sql)
        except duckdb.Error as exc:  # pragma: no cover - surfaces a broken predicate
            report.add(f"{code} {label}", False, f"query failed: {exc}")
            continue
        report.add(f"{code} {label}", leaked == 0,
                   "0 unlabelled" if not leaked else f"{leaked} UNLABELLED")


def _check_determinism(cfg: ScaleConfig, policy: DatagenPolicy, report: Report) -> None:
    """The same seed must reproduce byte-identical Parquet."""
    import hashlib
    import tempfile

    from .pipeline import generate

    def digest(root: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in sorted(root.rglob("*.parquet")):
            out[str(path.relative_to(root)).replace("\\", "/")] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        return out

    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
        slice_cfg = {
            "scale": cfg.scale,
            "seed": cfg.seed,
            "population": policy.population,
            "periods": cfg.periods,
            "reference_date": cfg.reference_date,
            "employees": 1000,
            "noise": cfg.noise,
        }
        generate(ScaleConfig.build(out=first, **slice_cfg), policy)
        generate(ScaleConfig.build(out=second, **slice_cfg), policy)
        left, right = digest(Path(first)), digest(Path(second))
        same = left == right

        third = ScaleConfig.build(out=second, **{**slice_cfg, "seed": cfg.seed + 1})
        generate(third, policy)
        different = digest(Path(second)) != left

    report.add(
        "same seed, identical bytes",
        same,
        f"{len(left)} files" if same
        else f"differing={sorted(k for k in left if left[k] != right.get(k))[:3]}",
    )
    report.add(
        "different seed, different data",
        different,
        "seed changes output" if different else "seed had no effect",
    )


def _check_git(report: Report) -> None:
    """Nothing under `data/` may ever be visible to git."""
    proc = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, check=False
    )
    leaked = [
        line[3:].strip().strip('"')
        for line in proc.stdout.splitlines()
        if line[3:].strip().strip('"').startswith("data/")
    ]
    report.add(
        "no data/ in git status",
        not leaked,
        "lake invisible to git" if not leaked else f"leaked={leaked[:3]}",
    )


def _sites_outside_region(policy: DatagenPolicy) -> list[str]:
    """Ray-casting point-in-polygon against the bundled region boundaries."""
    path = Path("policy/geo/sa_regions.geojson")
    if not path.exists():  # pragma: no cover - phase-0 gate covers this
        return ["sa_regions.geojson missing"]
    features = json.loads(path.read_text(encoding="utf-8"))["features"]
    polygons: dict[str, list[list[tuple[float, float]]]] = {}
    for feature in features:
        code = feature["properties"]["region_code"]
        geometry = feature["geometry"]
        rings = (
            geometry["coordinates"]
            if geometry["type"] == "Polygon"
            else [ring for part in geometry["coordinates"] for ring in part]
        )
        polygons[code] = [[(float(x), float(y)) for x, y in ring] for ring in rings]

    outside: list[str] = []
    for site in policy.pack.sites:
        rings = polygons.get(site.region_code, [])
        if not any(_inside(site.longitude, site.latitude, ring) for ring in rings):
            outside.append(site.site_id)
    return outside


def _inside(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    hit = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def summarise(cfg: ScaleConfig) -> list[tuple[str, str]]:
    """A compact row-count and distribution summary. Never row data."""
    con = connect(cfg)
    try:
        rows: list[tuple[str, str]] = []
        for table in SCHEMAS:
            rows.append((table, f"{_count(con, f'SELECT count(*) FROM {table}'):,}"))
        for label, sql in (
            ("nationality mix", "SELECT string_agg(nationality_class || ' ' || pct, ', ') "
             + "FROM (SELECT nationality_class, round(100.0 * count(*) / sum(count(*)) "
             + "OVER (), 1) AS pct FROM employee_master GROUP BY 1 ORDER BY 2 DESC)"),
            ("top regions", "SELECT string_agg(region_code || ' ' || n, ', ') FROM "
             + "(SELECT region_code, count(*) AS n FROM employee_master GROUP BY 1 "
             + "ORDER BY 2 DESC LIMIT 5)"),
            ("grade p50/p90", "SELECT quantile_cont(grade, 0.5) || ' / ' || "
             + "quantile_cont(grade, 0.9) FROM employee_master"),
            ("median salary", "SELECT round(median(base_salary), 0) || ' SAR' "
             + "FROM employee_master"),
            ("allowance ratio p50/max", "SELECT round(median(allowance_ratio), 3) || ' / ' "
             + "|| round(max(allowance_ratio), 3) FROM employee_master"),
            ("status mix", "SELECT string_agg(status || ' ' || pct, ', ') FROM "
             + "(SELECT status, round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct "
             + "FROM employee_master GROUP BY 1 ORDER BY 2 DESC)"),
        ):
            rows.append((label, str(con.execute(sql).fetchone()[0])))
        return rows
    finally:
        con.close()
