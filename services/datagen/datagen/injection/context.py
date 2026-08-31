"""Pass 2's working set: the lake rows an injector needs, and the mutators.

Everything an injector does goes through here, for three reasons.

**Arithmetic stays true.**  `set_allowances` and `set_payroll` recompute
`allowance_total`, GOSI, `gross` and `net` exactly the way `facts/payroll.py`
computed them in pass 1, so the integrity gate reconciles to the cent on an
injected row as well as on a clean one.

**Codes stay disjoint.**  `guard_step` refuses a mutation whose side effect
would be a *different* anomaly code -- a pay step big enough to read as a D06
change-point, an allowance load over B03's ceiling, an allowance appearing
beside a manager change and so reading as D05.  An unlabelled collision is an
unlabelled anomaly, which is the one thing pass 2 must never produce.

**Loading is batched.**  An injector picks a candidate pool with SQL, calls
`ensure()` once, and then works in memory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np

from policycore.packs import Allowance

from .. import entitlement as ent
from ..config import ScaleConfig, period_add, period_diff, period_last_day, period_of
from ..policy import DatagenPolicy
from ..schemas import SCHEMAS
from .model import AllowanceRow, Confounder, Edits, Key, Label

# Columns loaded per table. Deliberately explicit: a wide `SELECT *` on
# employee_master would pull a hundred columns per victim for no reason.
HISTORY_COLUMNS = (
    "employee_id", "effective_from", "effective_to", "grade", "job_code",
    "org_unit_id", "work_site_id", "manager_id", "base_salary", "change_reason",
    "approved_by",
)
PAYROLL_COLUMNS = (
    "employee_id", "period", "base_pay", "overtime_hours", "overtime_pay",
    "bonus", "retro_adjustment", "gosi_employee", "gosi_employer",
    "loan_deduction", "absence_deduction", "allowance_total", "gross", "net",
    "cost_center", "payroll_run_id", "paid_flag",
)
BANK_COLUMNS = (
    "employee_id", "effective_from", "effective_to", "iban", "bank_code",
    "change_reason", "is_known_benign_share",
)
ATTENDANCE_COLUMNS = (
    "employee_id", "period", "days_worked", "days_leave", "leave_type_breakdown",
    "overtime_hours", "absence_days", "rotation_cycle_id",
)
ACTIVITY_COLUMNS = (
    "employee_id", "period", "badge_swipes", "email_count", "erp_logins",
    "vpn_sessions", "activity_score",
)
# `record_created_at` / `record_updated_at` are generation metadata and no
# injector reads them. They are left out because a timezone-aware timestamp
# costs DuckDB an optional dependency to hand back to Python, and skipping two
# columns is cheaper than adding one.
SKIP_COLUMNS = frozenset({"record_created_at", "record_updated_at"})

MONEY_COLUMNS = frozenset(
    {"base_salary", "base_pay", "overtime_pay", "bonus", "retro_adjustment",
     "gosi_employee", "gosi_employer", "loan_deduction", "absence_deduction",
     "allowance_total", "gross", "net", "amount", "allowance_total_monthly"}
)


def _cents(value: Any) -> Any:
    """DECIMAL(12,2) out of DuckDB -> int64 minor units, exactly."""
    return None if value is None else int(value.scaleb(2))


@dataclass
class JobSpec:
    """The `dim_job` facts an injector reasons about."""

    job_code: str
    job_family: str
    min_grade: int
    max_grade: int
    min_education: str
    safety_critical: bool
    required_certifications: tuple[str, ...]


class Context:
    """The lake, the policy, the working copies and the guards."""

    def __init__(self, cfg: ScaleConfig, policy: DatagenPolicy, con, streams) -> None:
        self.cfg = cfg
        self.policy = policy
        self.pack = policy.pack
        self.con = con
        self.streams = streams
        self.spec: dict[str, Any] = self.pack.injection
        self.guards: dict[str, Any] = self.spec["guards"]
        self.periods: list[int] = cfg.period_list
        self.edits = Edits()
        self.labels: list[Label] = []
        self.confounders: list[Confounder] = []
        self.taken: set[str] = set()

        self.jobs: dict[str, JobSpec] = {
            row[0]: JobSpec(row[0], row[3], int(row[1]), int(row[2]), row[4],
                            bool(row[5]), tuple(row[6] or ()))
            for row in con.execute(
                "SELECT job_code, min_grade, max_grade, job_family, min_education, "
                "safety_critical, required_certifications FROM dim_job"
            ).fetchall()
        }
        self.cost_center_by_unit: dict[str, str] = dict(
            con.execute("SELECT org_unit_id, cost_center FROM dim_org_unit").fetchall()
        )
        gosi = self.pack.payroll["gosi"]
        self.gosi_ceiling = int(float(gosi["contributory_ceiling"]) * 100)
        self.gosi_rates = policy.gosi_rates

        self._master: dict[str, dict[str, Any]] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._payroll: dict[str, dict[int, dict[str, Any]]] = {}
        self._allow: dict[Key, list[AllowanceRow]] = {}
        self._bank: dict[str, list[dict[str, Any]]] = {}
        self._attend: dict[Key, dict[str, Any]] = {}
        self._activity: dict[Key, dict[str, Any]] = {}
        self._loaded: set[str] = set()
        self._master_columns = ", ".join(
            field.name for field in SCHEMAS["employee_master"]
            if field.name not in SKIP_COLUMNS
        )

    # ------------------------------------------------------------------ seeds

    def rng(self, name: str) -> np.random.Generator:
        """One independent stream per injector, keyed by name, never by order."""
        return self.streams.table("labels_anomaly").field(name, 0)

    def code_spec(self, code: str) -> dict[str, Any]:
        return self.spec["codes"][code]

    def target(self, code: str) -> int:
        """How many instances this code wants, floor included."""
        rate = float(self.code_spec(code)["rate"])
        return max(int(self.spec["min_instances"]), round(rate * self.cfg.employees))

    def confounder_target(self, name: str) -> int:
        spec = self.spec["confounders"][name]
        return max(int(self.spec["min_instances"]),
                   round(float(spec["rate"]) * self.cfg.employees))

    # -------------------------------------------------------------- selection

    def candidates(self, sql: str) -> list[str]:
        """Employee ids from a selection query, already-claimed ones removed."""
        rows = self.con.execute(sql).fetchall()
        return [r[0] for r in rows if r[0] not in self.taken]

    def pick(self, code: str, pool: list[str], count: int) -> list[str]:
        """A deterministic sample of `pool`, in a stable order."""
        if not pool or count <= 0:
            return []
        ordered = sorted(pool)
        rng = self.rng(f"pick:{code}")
        index = rng.permutation(len(ordered))[: min(count, len(ordered))]
        return [ordered[i] for i in sorted(index)]

    def claim(self, employee_id: str) -> None:
        self.taken.add(employee_id)

    def window(self, code: str, rng: np.random.Generator) -> tuple[int, int]:
        """A window of whole months ending at the last period of the run.

        Anchored to the end because that is the case a reviewer is asked about:
        the allowance is still being paid. A window ending mid-series would also
        put a downward step in the pay line, which is D06's finding, not this one.
        """
        low, high = self.code_spec(code)["window_months"]
        months = int(rng.integers(int(low), int(high) + 1))
        months = max(1, min(months, self.cfg.periods))
        return period_add(self.cfg.period_to, -(months - 1)), self.cfg.period_to

    # ---------------------------------------------------------------- loading

    def ensure(self, ids: list[str]) -> None:
        """Load the working copies for these employees, one query per table."""
        wanted = [i for i in dict.fromkeys(ids) if i not in self._loaded]
        if not wanted:
            return
        self._loaded.update(wanted)
        clause = "employee_id IN (" + ", ".join(f"'{i}'" for i in wanted) + ")"

        for row in self._rows("employee_master", self._master_columns, clause):
            self._master[row["employee_id"]] = row
        for row in self._rows("fact_assignment_history",
                              ", ".join(HISTORY_COLUMNS), clause,
                              order="employee_id, effective_from"):
            self._history.setdefault(row["employee_id"], []).append(row)
        for row in self._rows("fact_payroll_monthly", ", ".join(PAYROLL_COLUMNS),
                              clause, order="employee_id, period"):
            self._payroll.setdefault(row["employee_id"], {})[row["period"]] = row
        for row in self._rows("fact_payroll_allowance",
                              "employee_id, period, allowance_code, amount, "
                              "amount_basis, eligibility_snapshot_json", clause,
                              order="employee_id, period, allowance_code"):
            self._allow.setdefault((row["employee_id"], row["period"]), []).append(
                AllowanceRow(row["allowance_code"], row["amount"],
                             row["amount_basis"], row["eligibility_snapshot_json"])
            )
        for row in self._rows("fact_bank_account", ", ".join(BANK_COLUMNS), clause,
                              order="employee_id, effective_from"):
            self._bank.setdefault(row["employee_id"], []).append(row)
        for row in self._rows("fact_attendance_monthly", ", ".join(ATTENDANCE_COLUMNS),
                              clause, order="employee_id, period"):
            self._attend[(row["employee_id"], row["period"])] = row
        for row in self._rows("fact_system_activity_monthly",
                              ", ".join(ACTIVITY_COLUMNS), clause,
                              order="employee_id, period"):
            self._activity[(row["employee_id"], row["period"])] = row

    def _rows(self, table: str, columns: str, where: str, order: str = "") -> list[dict]:
        sql = f"SELECT {columns} FROM {table} WHERE {where}"
        if order:
            sql += f" ORDER BY {order}"
        cursor = self.con.execute(sql)
        names = [d[0] for d in cursor.description]
        out = []
        for values in cursor.fetchall():
            row = dict(zip(names, values, strict=True))
            for name in names:
                if name in MONEY_COLUMNS:
                    row[name] = _cents(row[name])
            out.append(row)
        return out

    # ---------------------------------------------------------------- reading

    def master(self, employee_id: str) -> dict[str, Any]:
        return self._master[employee_id]

    def history(self, employee_id: str) -> list[dict[str, Any]]:
        return self._history.get(employee_id, [])

    def payroll(self, employee_id: str) -> dict[int, dict[str, Any]]:
        return self._payroll.get(employee_id, {})

    def payroll_row(self, employee_id: str, period: int) -> dict[str, Any] | None:
        return self._payroll.get(employee_id, {}).get(period)

    def allowances(self, employee_id: str, period: int) -> list[AllowanceRow]:
        return self._allow.get((employee_id, period), [])

    def bank(self, employee_id: str) -> list[dict[str, Any]]:
        return self._bank.get(employee_id, [])

    def attendance(self, employee_id: str, period: int) -> dict[str, Any] | None:
        return self._attend.get((employee_id, period))

    def activity(self, employee_id: str, period: int) -> dict[str, Any] | None:
        return self._activity.get((employee_id, period))

    def paid_periods(self, employee_id: str, window: tuple[int, int]) -> list[int]:
        """Periods inside `window` where this employee actually drew base pay."""
        rows = self.payroll(employee_id)
        return [p for p in self.periods
                if window[0] <= p <= window[1] and p in rows and rows[p]["base_pay"] > 0]

    def standing(self, employee_id: str, period: int) -> int:
        """Base plus allowances less the standing deductions -- D06's series."""
        row = self.payroll_row(employee_id, period)
        if row is None:
            return 0
        return (row["base_pay"] + row["allowance_total"]
                - row["gosi_employee"] - row["loan_deduction"])

    # ------------------------------------------------------------ as-at state

    def interval_at(self, employee_id: str, period: int) -> dict[str, Any] | None:
        """The assignment interval in force during `period`."""
        end = period_last_day(period)
        current = None
        for row in self.history(employee_id):
            if row["effective_from"] <= end:
                current = row
            else:
                break
        return current

    def site_change_date(self, employee_id: str, upto: date) -> date | None:
        """The last transfer that actually moved the employee's site."""
        found = None
        previous = None
        for row in self.history(employee_id):
            if (previous is not None and row["work_site_id"] != previous
                    and row["effective_from"] <= upto):
                found = row["effective_from"]
            previous = row["work_site_id"]
        return found

    def feature_row(self, employee_id: str, period: int) -> dict[str, Any]:
        """The employee as they were in `period`, ready for clause evaluation.

        The same shape `facts/payroll.py` built in pass 1, rebuilt from the lake
        rather than from the generator's in-memory career: pass 2 only ever sees
        what was actually written.
        """
        record = dict(self.master(employee_id))
        interval = self.interval_at(employee_id, period)
        if interval is not None:
            record["grade"] = interval["grade"]
            record["base_salary"] = interval["base_salary"]
        record["service_years"] = max(
            0.0,
            float(record["service_years"]) - period_diff(self.cfg.period_to, period) / 12.0,
        )
        moved = self.site_change_date(employee_id, period_last_day(period))
        record["months_since_site_change"] = (
            period_diff(period, period_of(moved)) if moved is not None else 999
        )
        terminated = record["termination_date"]
        record["status"] = (
            "terminated" if terminated is not None and period > period_of(terminated)
            else ("active" if record["status"] == "terminated" else record["status"])
        )
        acting = record["acting_role_since"]
        record["acting_role_flag"] = bool(
            record["acting_role_flag"] and acting is not None and period_of(acting) <= period
        )
        job = self.jobs[interval["job_code"] if interval else record["job_code"]]
        site = self.pack.sites_by_id[
            interval["work_site_id"] if interval else record["work_site_id"]
        ]
        return ent.feature_row(record, site, job.safety_critical)

    def policy_amount(self, code: str, employee_id: str, period: int) -> int:
        """What the policy table says this allowance pays that employee, in cents."""
        allowance: Allowance = self.pack.allowances[code]
        return int(allowance.resolve_amount(self.feature_row(employee_id, period)) * 100)

    def snapshot(self, code: str, employee_id: str, period: int) -> str:
        """The eligibility fields frozen at payment time -- the reviewer's evidence."""
        return ent.snapshot_json(self.pack.allowances[code],
                                 self.feature_row(employee_id, period))

    # ----------------------------------------------------------------- guards

    def manager_change_periods(self, employee_id: str) -> set[int]:
        """Periods carrying a manager change that came with no promotion.

        Exactly what D05 looks for, so an injector adding allowances near one
        would be planting an unlabelled D05.
        """
        out: set[int] = set()
        previous = None
        for row in self.history(employee_id):
            if (previous is not None and row["manager_id"] != previous["manager_id"]
                    and row["grade"] == previous["grade"]):
                out.add(period_of(row["effective_from"]))
            previous = row
        return out

    def guard_step(self, employee_id: str, window: tuple[int, int], delta: int) -> bool:
        """May this employee absorb `delta` more allowance from `window[0]` on?

        Three ways they may not: the step is large enough to read as a D06
        change-point, the resulting allowance load reaches B03's ceiling, or a
        manager change sits close enough to make it look like D05.
        """
        periods = self.paid_periods(employee_id, window)
        if not periods:
            return False
        first = periods[0]
        prior = period_add(first, -1)
        if self.payroll_row(employee_id, prior) is not None:
            standing = self.standing(employee_id, prior)
            if standing <= 0:
                return False
            if delta > float(self.guards["max_step_ratio_of_standing"]) * standing:
                return False
        clearance = int(self.guards["manager_change_clearance"])
        if any(abs(period_diff(p, first)) <= clearance
               for p in self.manager_change_periods(employee_id)):
            return False
        ceiling = float(self.guards["max_allowance_ratio"])
        for period in periods:
            row = self.payroll_row(employee_id, period)
            if row["allowance_total"] + delta > ceiling * row["base_pay"]:
                return False
        return True

    def ratio_ok(self, employee_id: str, periods: list[int], codes: list[str]) -> bool:
        """Would paying `codes` keep the allowance load clear of B03's ceiling?

        Checked in every period, not only the last: base pay moves with
        increments, so a stack that is comfortable in the final month can be
        over the ceiling three months earlier -- and B03 is a row-level test.
        """
        ceiling = float(self.guards["max_allowance_ratio"])
        for period in periods:
            row = self.payroll_row(employee_id, period)
            if row is None or row["base_pay"] <= 0:
                return False
            extra = sum(self.policy_amount(code, employee_id, period) for code in codes)
            if row["allowance_total"] + extra > ceiling * row["base_pay"]:
                return False
        return True

    # --------------------------------------------------------------- mutators

    def set_master(self, employee_id: str, **columns: Any) -> None:
        record = self._master[employee_id]
        record.update(columns)
        self.edits.master.setdefault(employee_id, {}).update(columns)

    def set_history(self, employee_id: str, rows: list[dict[str, Any]]) -> None:
        self._history[employee_id] = rows
        self.edits.history[employee_id] = rows

    def set_bank(self, employee_id: str, rows: list[dict[str, Any]]) -> None:
        self._bank[employee_id] = rows
        self.edits.bank[employee_id] = rows

    def set_attendance(self, employee_id: str, period: int, **columns: Any) -> None:
        row = self._attend[(employee_id, period)]
        row.update(columns)
        self.edits.attendance[(employee_id, period)] = row

    def set_activity(self, employee_id: str, period: int, **columns: Any) -> None:
        row = self._activity[(employee_id, period)]
        row.update(columns)
        self.edits.activity[(employee_id, period)] = row

    def set_allowances(
        self, employee_id: str, period: int, rows: list[AllowanceRow]
    ) -> None:
        """Replace an employee-period's allowance rows and redo the money."""
        self._allow[(employee_id, period)] = rows
        self.edits.allowances[(employee_id, period)] = rows
        self._recompute(employee_id, period)

    def add_allowance(
        self, employee_id: str, period: int, code: str, cents: int,
        snapshot: str | None = None,
    ) -> None:
        rows = [r for r in self.allowances(employee_id, period) if r.code != code]
        rows.append(
            AllowanceRow(code, cents, self.pack.allowances[code].amount_basis,
                         snapshot if snapshot is not None
                         else self.snapshot(code, employee_id, period))
        )
        rows.sort(key=lambda r: r.code)
        self.set_allowances(employee_id, period, rows)

    def set_payroll(self, employee_id: str, period: int, **columns: Any) -> None:
        row = self._payroll[employee_id][period]
        row.update(columns)
        self.edits.payroll[(employee_id, period)] = row
        self._recompute(employee_id, period)

    def add_payroll_row(self, row: dict[str, Any]) -> None:
        """A payroll row for a period that had none -- C04's overpayment months."""
        self._payroll.setdefault(row["employee_id"], {})[row["period"]] = row
        self.edits.payroll_new[(row["employee_id"], row["period"])] = row

    def _recompute(self, employee_id: str, period: int) -> None:
        """Re-derive the stored totals exactly as `facts/payroll.py` does."""
        row = self._payroll.get(employee_id, {}).get(period)
        if row is None:
            return
        rows = self.allowances(employee_id, period)
        total = sum(r.cents for r in rows)
        housing = next((r.cents for r in rows if r.code == "HOUSING"), 0)
        row["allowance_total"] = total
        # A settlement row carries no base pay and no contribution; pass 1
        # writes zeroes there and pass 2 must not invent one.
        if row["base_pay"] > 0:
            contributory = min(row["base_pay"] + housing, self.gosi_ceiling)
            employee_pct, employer_pct = self.gosi_rates[
                self.master(employee_id)["gosi_class"]
            ]
            row["gosi_employee"] = round(contributory * employee_pct / 100)
            row["gosi_employer"] = round(contributory * employer_pct / 100)
        row["gross"] = (row["base_pay"] + total + row["overtime_pay"]
                        + row["bonus"] + row["retro_adjustment"])
        row["net"] = (row["gross"] - row["gosi_employee"] - row["loan_deduction"]
                      - row["absence_deduction"])
        if (employee_id, period) not in self.edits.payroll_new:
            self.edits.payroll[(employee_id, period)] = row

    # ----------------------------------------------------------------- labels

    def label(
        self, employee_id: str, code: str, window: tuple[int, int],
        impact: int, description: str, **params: Any,
    ) -> None:
        record = self.master(employee_id)
        self.labels.append(
            Label(
                employee_id=employee_id,
                anomaly_code=code,
                period_from=window[0],
                period_to=window[1],
                injected_severity=str(self.code_spec(code)["severity"]),
                params=params,
                human_description=description,
                work_site_id=record["work_site_id"],
                region_code=record["region_code"],
                expected_monthly_impact=int(impact),
            )
        )
        self.claim(employee_id)

    def confound(
        self, employee_id: str, name: str, window: tuple[int, int],
        impact: int, description: str, **params: Any,
    ) -> None:
        record = self.master(employee_id)
        self.confounders.append(
            Confounder(
                employee_id=employee_id,
                confounder_type=name,
                confounds_code=str(self.spec["confounders"][name]["confounds"]),
                period_from=window[0],
                period_to=window[1],
                params=params,
                human_description=description,
                work_site_id=record["work_site_id"],
                region_code=record["region_code"],
                expected_monthly_impact=int(impact),
            )
        )
        self.claim(employee_id)


def sar(cents: int) -> str:
    """SAR for a human-readable description -- no jargon near a reviewer."""
    return f"{cents / 100:,.0f} SAR"


def params_json(params: dict[str, Any]) -> str:
    return json.dumps(params, sort_keys=True, default=str, ensure_ascii=False)
