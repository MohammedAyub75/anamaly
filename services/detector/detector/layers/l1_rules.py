"""Layer 1 -- the declarative rule engine.

Rules are `policy/rules/*.yaml` in the shape `A01_*.yaml` defines. Adding a
policy is adding a file; there is no code change and no branch in here that
knows any particular code's name.

The engine does four things (docs/specs/detector.md, layer 1):

1. Loads and validates every rule file, failing loudly on a malformed one. A
   silently skipped rule is a silent 0% recall, which is the most expensive bug
   this project can have, so *every* failure here is fatal rather than a warning.
2. Compiles each `sql_predicate` into a DuckDB SELECT over the feature store,
   with `exclusions` applied as `AND NOT (...)`.
3. Emits one hit row per (employee, rule, period window) -- consecutive flagged
   months collapsed into one finding -- with the `evidence_fields` values
   attached and the `description_template` rendered.
4. Computes `financial_impact` from the rule's `monthly_expr` / `cumulative_expr`.

Layer 1 emits 100%-precision hits. A rule that produces a false positive is a
bug in the rule, not a tuning opportunity.
"""

from __future__ import annotations

import json
import re
import string
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import yaml

RULES_DIRNAME = "rules"

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM")

# `docs/EVIDENCE_CONTRACT.md`: rule-derived impact is exact, model-derived is
# estimated. A rule may still declare `estimated` where the money it names is a
# reconstruction rather than a line in the payroll run.
CONFIDENCES = ("exact", "estimated", "unknown")
FAMILIES = ("A", "B", "C", "D")

CODE_RE = re.compile(r"^[ABCD]\d{2}$")

REQUIRED_KEYS = (
    "id",
    "family",
    "name_en",
    "name_ar",
    "severity",
    "regulatory_reference",
    "enabled",
    "sql_predicate",
    "evidence_fields",
    "description_template",
    "financial_impact",
    "recommended_actions",
)

# Fields the engine itself supplies for every rule, so a template and an
# evidence bundle can always speak about the window the rule fired over.
WINDOW_FIELDS = (
    "employee_id",
    "first_period_paid",
    "last_period_paid",
    "months_paid",
    "first_period_label",
    "last_period_label",
)

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


class RuleError(ValueError):
    """A malformed or unexecutable rule file. Always fatal -- never skipped."""


def period_label(period: int | None) -> str:
    """`202403` as `March 2024` -- reviewers read months, not integers."""
    if period is None:
        return "an unrecorded month"
    year, month = divmod(int(period), 100)
    if not 1 <= month <= 12:
        return str(period)
    return f"{_MONTHS[month - 1]} {year}"


def _placeholders(template: str) -> set[str]:
    """The `{field}` names a format template reads, ignoring the format spec."""
    return {
        name.split(".")[0].split("[")[0]
        for _, name, _, _ in string.Formatter().parse(template)
        if name
    }


@dataclass(frozen=True)
class Rule:
    """One policy rule, validated. The YAML is the detector; this is its parse."""

    id: str
    family: str
    name_en: str
    name_ar: str
    severity: str
    regulatory_reference: str
    enabled: bool
    sql_predicate: str
    evidence_fields: tuple[str, ...]
    description_template: str
    monthly_expr: str
    cumulative_expr: str
    recommended_actions: tuple[str, ...]
    exclusions: tuple[str, ...]
    allowance_code: str | None
    # Optional. Some codes carry a severity that depends on the row -- A11 is
    # CRITICAL in a safety-critical post and MEDIUM elsewhere
    # (docs/ANOMALY_CATALOG.md) -- and a single static field cannot say that.
    severity_expr: str | None
    impact_confidence: str
    path: Path

    @classmethod
    def parse(cls, path: Path) -> Rule:
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise RuleError(f"{path.name}: not valid YAML: {exc}") from exc
        if not isinstance(doc, dict):
            raise RuleError(f"{path.name}: expected a mapping at the top level")

        missing = [k for k in REQUIRED_KEYS if k not in doc]
        if missing:
            raise RuleError(f"{path.name}: missing required keys {missing}")

        code = str(doc["id"])
        if not CODE_RE.match(code):
            raise RuleError(f"{path.name}: id {code!r} is not an anomaly code")
        if not path.name.startswith(f"{code}_"):
            raise RuleError(f"{path.name}: filename must start with {code}_")
        if doc["family"] != code[0]:
            raise RuleError(f"{path.name}: family {doc['family']!r} disagrees with id")
        if doc["family"] not in FAMILIES:
            raise RuleError(f"{path.name}: unknown family {doc['family']!r}")
        if doc["severity"] not in SEVERITIES:
            raise RuleError(
                f"{path.name}: severity {doc['severity']!r} not in {SEVERITIES}"
            )

        impact = doc["financial_impact"] or {}
        for key in ("monthly_expr", "cumulative_expr"):
            if not impact.get(key):
                raise RuleError(f"{path.name}: financial_impact.{key} is required")
        confidence = str(impact.get("confidence", "exact"))
        if confidence not in CONFIDENCES:
            raise RuleError(
                f"{path.name}: financial_impact.confidence {confidence!r} "
                f"not in {CONFIDENCES}"
            )

        evidence = tuple(doc["evidence_fields"] or ())
        if not evidence:
            raise RuleError(f"{path.name}: evidence_fields must not be empty")
        actions = tuple(doc["recommended_actions"] or ())
        if not 1 <= len(actions) <= 5:
            raise RuleError(f"{path.name}: recommended_actions must hold 1-5 entries")

        available = set(evidence) | set(WINDOW_FIELDS)
        for label, text in [("description_template", doc["description_template"])] + [
            (f"recommended_actions[{i}]", a) for i, a in enumerate(actions)
        ]:
            unknown = sorted(_placeholders(str(text)) - available)
            if unknown:
                raise RuleError(
                    f"{path.name}: {label} reads {unknown}, which is neither an "
                    "evidence field nor supplied by the engine"
                )

        return cls(
            id=code,
            family=str(doc["family"]),
            name_en=str(doc["name_en"]),
            name_ar=str(doc["name_ar"]),
            severity=str(doc["severity"]),
            regulatory_reference=str(doc["regulatory_reference"]),
            enabled=bool(doc["enabled"]),
            sql_predicate=str(doc["sql_predicate"]).strip(),
            evidence_fields=evidence,
            description_template=" ".join(str(doc["description_template"]).split()),
            monthly_expr=str(impact["monthly_expr"]).strip(),
            cumulative_expr=str(impact["cumulative_expr"]).strip(),
            recommended_actions=actions,
            exclusions=tuple(doc.get("exclusions") or ()),
            allowance_code=doc.get("allowance_code"),
            severity_expr=(str(doc["severity_expr"]).strip()
                           if doc.get("severity_expr") else None),
            impact_confidence=confidence,
            path=path,
        )

    # ----------------------------------------------------------------- SQL

    @property
    def where(self) -> str:
        """The predicate with exclusions applied.

        An exclusion that cannot be evaluated -- a null on one side -- must not
        exclude. `NOT (a AND NULL)` is NULL, and a NULL in a WHERE clause drops
        the row, so an unrelated missing field would silently eat a true
        positive. `coalesce(..., FALSE)` says what is meant: excluded only when
        the legitimate case is positively established.
        """
        clauses = [f"({self.sql_predicate})"]
        clauses += [f"NOT coalesce(({e}), FALSE)" for e in self.exclusions]
        return "\n      AND ".join(clauses)

    def select(self, table: str = "features_period") -> str:
        """One rule compiled to a DuckDB SELECT over the feature store.

        Consecutive flagged months are collapsed into one finding by the
        gaps-and-islands trick on `period_index`: a rule that fires for fourteen
        months is one case a reviewer works, not fourteen.
        """
        # `first_period_paid` and friends are supplied by the window step
        # below, so a rule that names them as evidence must not also try to
        # select them from the feature table.
        columns = sorted(set(self.evidence_fields) - set(WINDOW_FIELDS))
        evidence = "".join(f",\n           {c}" for c in columns)
        severity = (
            f",\n           ({self.severity_expr}) AS row_severity"
            if self.severity_expr
            else ""
        )
        return f"""
WITH hits AS (
    SELECT employee_id,
           period,
           period_index{evidence}{severity}
    FROM {table}
    WHERE {self.where}
),
islands AS (
    SELECT *,
           period_index - row_number() OVER (PARTITION BY employee_id
                                             ORDER BY period_index) AS island
    FROM hits
),
windows AS (
    SELECT employee_id,
           island,
           min(period)       AS first_period_paid,
           max(period)       AS last_period_paid,
           count(*)          AS months_paid,
           max(period_index) AS last_index
    FROM islands
    GROUP BY employee_id, island
)
SELECT w.first_period_paid,
       w.last_period_paid,
       w.months_paid,
       i.*  EXCLUDE (period, period_index, island),
       ({self.monthly_expr})    AS financial_impact_monthly,
       ({self.cumulative_expr}) AS financial_impact_cumulative
FROM windows w
JOIN islands i
  ON i.employee_id = w.employee_id
 AND i.island = w.island
 AND i.period_index = w.last_index
ORDER BY i.employee_id, w.first_period_paid
"""


@dataclass
class RuleSet:
    """Every rule under `policy/rules/`, loaded once and validated as a set."""

    rules: tuple[Rule, ...]
    root: Path

    @classmethod
    def load(cls, policy_root: str | Path = "policy") -> RuleSet:
        root = Path(policy_root) / RULES_DIRNAME
        if not root.is_dir():
            raise RuleError(f"no rule directory at {root}")
        paths = sorted(root.glob("*.yaml"))
        if not paths:
            raise RuleError(f"no rule files in {root}")
        rules = tuple(Rule.parse(p) for p in paths)
        seen: dict[str, Path] = {}
        for rule in rules:
            if rule.id in seen:
                raise RuleError(
                    f"two rules claim {rule.id}: {seen[rule.id].name}, {rule.path.name}"
                )
            seen[rule.id] = rule.path
        return cls(rules=rules, root=root)

    @property
    def enabled(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.enabled)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(r.id for r in self.rules)

    def by_code(self, code: str) -> Rule:
        for rule in self.rules:
            if rule.id == code:
                return rule
        raise KeyError(code)

    def check_columns(self, columns: list[str]) -> None:
        """Every evidence field must be a real feature column."""
        available = set(columns)
        for rule in self.rules:
            unknown = sorted(set(rule.evidence_fields) - available - set(WINDOW_FIELDS))
            if unknown:
                raise RuleError(
                    f"{rule.path.name}: evidence_fields name columns the feature "
                    f"store does not have: {unknown}. Add them in features/sql/."
                )

    def check_executable(self, con: duckdb.DuckDBPyConnection) -> None:
        """Bind every rule against the feature store without running it.

        A typo in a predicate is a binder error here rather than a permanently
        empty recall row six steps later.
        """
        for rule in self.enabled:
            try:
                con.execute(f"SELECT 1 FROM features_period WHERE {rule.where} LIMIT 0")
            except duckdb.Error as exc:
                raise RuleError(f"{rule.path.name}: predicate will not bind: {exc}") from exc
            for label, expr in (
                ("financial_impact.monthly_expr", rule.monthly_expr),
                ("severity_expr", rule.severity_expr),
            ):
                if not expr:
                    continue
                try:
                    con.execute(f"SELECT ({expr}) FROM features_period LIMIT 0")
                except duckdb.Error as exc:
                    raise RuleError(
                        f"{rule.path.name}: {label} will not bind: {exc}"
                    ) from exc


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """A value a format template and a JSON dump can both handle."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def render(template: str, row: dict[str, Any]) -> str:
    """Fill a rule's template from a hit row.

    Null-safe on purpose: a template is user-facing text, and an evidence field
    that happens to be null must read as a gap in the record rather than crash
    the run or print `None` at a reviewer.
    """
    safe = {
        k: ("not recorded" if v is None else _plain(v))
        for k, v in row.items()
    }
    try:
        return template.format(**safe)
    except (ValueError, TypeError, KeyError) as exc:
        raise RuleError(f"cannot render {template!r}: {exc}") from exc


@dataclass
class L1Result:
    """What one layer-1 pass found, per rule and in total."""

    seconds: float
    hits: list[dict[str, Any]] = field(default_factory=list)
    by_code: dict[str, int] = field(default_factory=dict)
    employees_by_code: dict[str, int] = field(default_factory=dict)
    seconds_by_code: dict[str, float] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.hits)


def run_rules(
    con: duckdb.DuckDBPyConnection,
    ruleset: RuleSet,
    *,
    table: str = "features_period",
    log=None,
) -> L1Result:
    """Execute every enabled rule and return the hit rows, evidence rendered."""
    started = time.perf_counter()
    result = L1Result(seconds=0.0)
    for rule in ruleset.enabled:
        rule_started = time.perf_counter()
        rows = con.execute(rule.select(table)).fetchall()
        names = [d[0] for d in (con.description or [])]
        employees: set[str] = set()
        for values in rows:
            row = {name: _plain(value) for name, value in zip(names, values)}
            row["first_period_label"] = period_label(row.get("first_period_paid"))
            row["last_period_label"] = period_label(row.get("last_period_paid"))
            employees.add(str(row["employee_id"]))
            evidence = {k: row.get(k) for k in rule.evidence_fields}
            severity = str(row.get("row_severity") or rule.severity)
            if severity not in SEVERITIES:
                raise RuleError(
                    f"{rule.path.name}: severity_expr returned {severity!r}"
                )
            result.hits.append(
                {
                    "employee_id": row["employee_id"],
                    "anomaly_code": rule.id,
                    "family": rule.family,
                    "severity": severity,
                    "rule_name_en": rule.name_en,
                    "rule_name_ar": rule.name_ar,
                    "allowance_code": rule.allowance_code,
                    "regulatory_reference": rule.regulatory_reference,
                    "period_from": int(row["first_period_paid"]),
                    "period_to": int(row["last_period_paid"]),
                    "months_flagged": int(row["months_paid"]),
                    "financial_impact_monthly": float(
                        row.get("financial_impact_monthly") or 0.0
                    ),
                    "financial_impact_cumulative": float(
                        row.get("financial_impact_cumulative") or 0.0
                    ),
                    "financial_impact_confidence": rule.impact_confidence,
                    "description": render(rule.description_template, row),
                    "recommended_actions": [
                        render(a, row) for a in rule.recommended_actions
                    ],
                    "evidence_json": json.dumps(evidence, default=str),
                }
            )
        elapsed = time.perf_counter() - rule_started
        result.by_code[rule.id] = len(rows)
        result.employees_by_code[rule.id] = len(employees)
        result.seconds_by_code[rule.id] = round(elapsed, 3)
        if log:
            log(f"  {rule.id}  {len(rows):>6} windows  {len(employees):>5} employees"
                f"  {elapsed:6.2f}s")
    result.seconds = round(time.perf_counter() - started, 3)
    return result
