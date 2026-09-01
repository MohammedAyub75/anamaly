"""Layer 3 -- the graph checks, and the five findings layer 3 owns.

Family C is about *identity*, and identity is a question about how records
relate to each other rather than about any one record's values. Two employees
paid into one account, one national ID on two records, a reporting line that
closes on itself, two records that are visibly the same person: none of those
is visible from a single row, which is why neither a rule nor a peer statistic
finds them and why this layer exists.

The method is the same for all four, and it is the one `docs/specs/detector.md`
insists on: **candidates are found set-based in DuckDB, and `networkx` only
ever walks the small candidate subgraphs.** A shared account is a self-join
that returns tens of rows at 10k and a few thousand at 1m; the graph built from
them has a few thousand nodes, not a million. Nothing here ever holds the whole
workforce as a graph in memory, and that is a property of the design rather
than an optimisation to add later.

Five codes:

* **C01** one bank account, unrelated employees -- with two exclusions that are
  each a *different* finding rather than a false positive: a couple who declare
  each other (the planted `spousal_shared_iban` confounder), and a pair sharing
  a date of birth and an all-but-identical name, which is one person on the
  payroll twice and is C06.
* **C02** one identity number on more than one record.
* **C03** paid every month with nothing to show for it -- the one code the
  catalogue marks `L3 (ML + rules)`. Its trigger is a dormancy scan and its
  corroboration is the reconstruction gap `l3_ml.py` computed, which is why it
  lives beside the graph codes rather than inside the model module: it is a
  *finding*, and findings are emitted here.
* **C05** an approval signed by the person it benefits, or a manager cycle.
* **C06** two records that look like the same person.

Layer 3's findings carry the same seventeen columns as layer 1's and layer 2's,
so phase 6 fuses one list rather than three.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import duckdb

from .l1_rules import SEVERITIES, RuleError, period_label, render
from .l2_peer import windowed
from .l3_ml import MLScores
from .l3_ml import fit as fit_models

LAYER = "graph"

# The `graph_context` block of `docs/EVIDENCE_CONTRACT.md`. A detector selects
# these as `graph_<name>`; the emitter lifts them into the bundle and also
# exposes them to the description template under their bare name.
GRAPH_FIELDS = (
    "link_type",
    "link_value_masked",
    "component_size",
    "component_class",
    "related_employees",
    "total_monthly_disbursement",
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
INTERNAL_COLUMNS = ("ml_attributions_json", "route", "related_json")

# How a link reads in a sentence. Never the column name, and never the raw
# identifier: the alert is read by people who do not need the whole number.
LINK_LABELS = {
    "shared_iban": "bank account",
    "national_id": "national ID",
    "iqama_no": "iqama number",
}


class L3Error(RuntimeError):
    """A layer-3 detector that cannot run. Fatal, never skipped -- as in layer 1."""


@dataclass
class GraphSummary:
    """What the candidate search found, and how big the subgraphs really were."""

    iban_components: int = 0
    iban_members: int = 0
    identity_components: int = 0
    identity_members: int = 0
    by_class: dict[str, int] = field(default_factory=dict)
    largest_component: int = 0
    cycle_candidates: int = 0
    cycles_found: int = 0
    self_approvals: int = 0
    graph_nodes: int = 0
    seconds: float = 0.0

    @property
    def components(self) -> int:
        return self.iban_components + self.identity_components


@dataclass
class L3Result:
    """What one layer-3 pass found, per code and in total."""

    seconds: float
    hits: list[dict[str, Any]] = field(default_factory=list)
    by_code: dict[str, int] = field(default_factory=dict)
    employees_by_code: dict[str, int] = field(default_factory=dict)
    seconds_by_code: dict[str, float] = field(default_factory=dict)
    # Both are None on a pass loaded back from the stage cache. They describe
    # what the run *did* rather than what it found, and a zeroed summary in the
    # eval report would read as "the graph was empty" rather than as "this pass
    # did not rebuild it" -- which is worse than no section at all.
    graph: GraphSummary | None = None
    ml: MLScores | None = None
    codes: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.hits)

    @property
    def detectors(self) -> dict[str, str]:
        """Code -> the detector label the eval report prints."""
        return {
            code: ("L3 ML + rules" if code == "C03" else "L3 graph")
            for code in self.codes
        }


# --------------------------------------------------------------------------
# Name comparison
# --------------------------------------------------------------------------


def jaro(left: str, right: str) -> float:
    """Jaro similarity. Written out rather than pulled in as a dependency.

    Thirty lines against a package, for a function that runs over a handful of
    candidate pairs per run: the blocking step has already reduced the problem
    to pairs sharing a date of birth *and* a bank account, so this is never on
    a hot path, and the arithmetic is fixed by the definition rather than by a
    library's version.
    """
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    reach = max(max(len(left), len(right)) // 2 - 1, 0)
    left_hits = [False] * len(left)
    right_hits = [False] * len(right)
    matches = 0
    for i, character in enumerate(left):
        for j in range(max(0, i - reach), min(i + reach + 1, len(right))):
            if not right_hits[j] and right[j] == character:
                left_hits[i] = right_hits[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    transpositions, k = 0, 0
    for i, character in enumerate(left):
        if not left_hits[i]:
            continue
        while not right_hits[k]:
            k += 1
        if character != right[k]:
            transpositions += 1
        k += 1
    transpositions //= 2
    return (
        matches / len(left)
        + matches / len(right)
        + (matches - transpositions) / matches
    ) / 3


def jaro_winkler(left: str, right: str, prefix_weight: float, prefix_max: int) -> float:
    """Jaro, with a bonus for a shared prefix -- the C06 name comparison.

    The prefix bonus is what makes it the right measure for names: a
    transposition in the middle of a surname leaves the beginning intact, and
    that is exactly the shape of a manufactured near-duplicate.
    """
    base = jaro(left, right)
    prefix = 0
    for a, b in zip(left[:prefix_max], right[:prefix_max]):
        if a != b:
            break
        prefix += 1
    return base + prefix * prefix_weight * (1 - base)


# --------------------------------------------------------------------------
# Candidate components -- DuckDB finds them, networkx walks them
# --------------------------------------------------------------------------


def _components(edges: list[tuple[str, str]]) -> dict[str, int]:
    """Connected components over the candidate edges only.

    `networkx` never sees an employee who shares nothing with anybody, which is
    the overwhelming majority of them: at 10k this graph has 24 nodes.
    """
    import networkx as nx

    graph = nx.Graph()
    graph.add_edges_from(edges)
    assignment: dict[str, int] = {}
    for index, component in enumerate(
        sorted(nx.connected_components(graph), key=lambda c: sorted(c)), start=1
    ):
        for node in component:
            assignment[node] = index
    return assignment


def find_cycles(edges: list[tuple[str, str]], max_length: int) -> list[list[str]]:
    """Cycles in a manager graph, over the candidate subgraph only.

    Directed: `(employee, manager)`. A cycle here means somebody reports,
    through however many steps, to a person who reports back to them -- so no
    one in the chain has an approver outside it.
    """
    import networkx as nx

    graph = nx.DiGraph()
    graph.add_edges_from(edges)
    out: list[list[str]] = []
    for cycle in nx.simple_cycles(graph, length_bound=max_length):
        if len(cycle) >= 2:
            # Rotate to a stable starting point so two runs name the same chain
            # the same way; the wording a reviewer reads must not depend on
            # dictionary ordering.
            start = cycle.index(min(cycle))
            out.append(cycle[start:] + cycle[:start])
    return sorted(out, key=lambda c: (len(c), c))


def _mask(value: str | None, visible: int) -> str:
    if not value:
        return "not recorded"
    text = str(value)
    return text[-visible:] if len(text) > visible else text


def prepare(
    con: duckdb.DuckDBPyConnection, policy, *, log=None
) -> tuple[GraphSummary, MLScores]:
    """Build everything the five detectors share, once per run.

    Order matters: the models are fitted first because C03 reads `ml_scores`,
    and the candidate components are built second because they are cheap.
    """
    ml = fit_models(con, policy, log=log)
    summary = build_components(con, policy, log=log)
    return summary, ml


def build_components(
    con: duckdb.DuckDBPyConnection, policy, *, log=None
) -> GraphSummary:
    """Find the candidate links, resolve them into components, and classify them.

    Classification is what keeps C01 and C06 apart and what leaves the planted
    spousal accounts alone. A component is:

    * `spousal` when every pair in it declares the other as a spouse -- a joint
      account, and not a finding at all;
    * `near_duplicate` when every pair shares a date of birth and a name the
      comparison cannot tell apart -- one person on the payroll twice, which is
      C06;
    * `unrelated` otherwise, which is C01.

    "Every pair", not "some pair": a three-person ring containing one married
    couple is still a ring, and explaining away two of its three links does not
    explain the third.
    """
    started = time.perf_counter()
    config = policy.graph
    visible = int(config["mask_visible_digits"])
    threshold = float(config["name_similarity_threshold"])
    prefix_weight = float(config["jaro_winkler_prefix_weight"])
    prefix_max = int(config["jaro_winkler_prefix_max"])
    summary = GraphSummary()

    # --------------------------------------------------------------- linking
    # Bank accounts are matched over history rather than over the current row:
    # an account shared for six months and then changed is still a shared
    # account (docs/DATA_DICTIONARY.md, fact_bank_account).
    iban_edges = con.execute(
        """
        SELECT DISTINCT a.employee_id, b.employee_id, a.iban
        FROM fact_bank_account a
        JOIN fact_bank_account b ON b.iban = a.iban
                               AND b.employee_id > a.employee_id
        WHERE a.iban IS NOT NULL
        ORDER BY 3, 1, 2
        """
    ).fetchall()
    identity_edges = con.execute(
        """
        WITH ids AS (
            SELECT employee_id, 'national_id' AS link_type, national_id AS value
            FROM employee_master WHERE national_id IS NOT NULL
            UNION ALL
            SELECT employee_id, 'iqama_no', iqama_no
            FROM employee_master WHERE iqama_no IS NOT NULL
        )
        SELECT DISTINCT a.employee_id, b.employee_id, a.value, a.link_type
        FROM ids a
        JOIN ids b ON b.value = a.value
                  AND b.link_type = a.link_type
                  AND b.employee_id > a.employee_id
        ORDER BY 3, 1, 2
        """
    ).fetchall()

    people = sorted(
        {e for row in iban_edges for e in row[:2]}
        | {e for row in identity_edges for e in row[:2]}
    )
    summary.graph_nodes = len(people)
    attributes: dict[str, dict[str, Any]] = {}
    if people:
        placeholders = ", ".join("?" for _ in people)
        rows = con.execute(
            f"""
            SELECT e.employee_id, e.name_en_normalised, e.dob,
                   e.spouse_employee_id
            FROM employee_master e
            WHERE e.employee_id IN ({placeholders})
            """,
            people,
        ).fetchall()
        names = [d[0] for d in (con.description or [])]
        attributes = {
            row[0]: dict(zip(names, row)) for row in rows
        }

    def _spouses(left: str, right: str) -> bool:
        a, b = attributes.get(left, {}), attributes.get(right, {})
        return (
            a.get("spouse_employee_id") == right
            and b.get("spouse_employee_id") == left
        )

    def _similarity(left: str, right: str) -> float:
        a = str(attributes.get(left, {}).get("name_en_normalised") or "")
        b = str(attributes.get(right, {}).get("name_en_normalised") or "")
        return jaro_winkler(a, b, prefix_weight, prefix_max)

    def _near_duplicate(left: str, right: str) -> bool:
        a, b = attributes.get(left, {}), attributes.get(right, {})
        if a.get("dob") is None or a.get("dob") != b.get("dob"):
            return False
        return _similarity(left, right) >= threshold

    # ------------------------------------------------------------ components
    rows: list[tuple] = []
    minimum = int(config["min_component_size"])
    for kind, edges in (("shared_iban", iban_edges), ("identity", identity_edges)):
        pairs = [(row[0], row[1]) for row in edges]
        link_value = {}
        link_type = {}
        for row in edges:
            for member in row[:2]:
                link_value[member] = row[2]
                link_type[member] = "shared_iban" if kind == "shared_iban" else row[3]
        assignment = _components(pairs)
        grouped: dict[int, list[str]] = {}
        for member, component in assignment.items():
            grouped.setdefault(component, []).append(member)
        for component, members in sorted(grouped.items()):
            members = sorted(members)
            if len(members) < minimum:
                continue
            couples = [
                (a, b) for i, a in enumerate(members) for b in members[i + 1 :]
            ]
            if all(_spouses(a, b) for a, b in couples):
                component_class = "spousal"
            elif all(_near_duplicate(a, b) for a, b in couples):
                component_class = "near_duplicate"
            else:
                component_class = "unrelated"
            summary.by_class[component_class] = (
                summary.by_class.get(component_class, 0) + 1
            )
            summary.largest_component = max(summary.largest_component, len(members))
            if kind == "shared_iban":
                summary.iban_components += 1
                summary.iban_members += len(members)
            else:
                summary.identity_components += 1
                summary.identity_members += len(members)
            for member in members:
                others = [m for m in members if m != member]
                closest = (
                    max(others, key=lambda o: (_similarity(member, o), o))
                    if others
                    else None
                )
                rows.append(
                    (
                        f"{kind}-{component:06d}",
                        kind,
                        link_type.get(member, kind),
                        str(link_value.get(member) or ""),
                        _mask(link_value.get(member), visible),
                        len(members),
                        component_class,
                        member,
                        closest,
                        round(_similarity(member, closest), 4) if closest else None,
                    )
                )

    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE graph_components (
            component_id VARCHAR, link_kind VARCHAR, link_type VARCHAR,
            link_value VARCHAR, link_value_masked VARCHAR, component_size INTEGER,
            component_class VARCHAR, employee_id VARCHAR,
            closest_employee_id VARCHAR, name_similarity DOUBLE
        )
        """
    )
    if rows:
        con.executemany(
            "INSERT INTO graph_components VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    # ---------------------------------------------------------------- cycles
    # `manager_cycle_flag` is computed set-based by the feature build; the
    # candidate node set is those employees plus their chains, bounded by
    # `max_cycle_length`. That is the subgraph networkx is handed -- never the
    # workforce.
    max_length = int(config["max_cycle_length"])
    candidates = [
        row[0]
        for row in con.execute(
            "SELECT employee_id FROM features_employee "
            "WHERE manager_cycle_flag ORDER BY employee_id"
        ).fetchall()
    ]
    summary.cycle_candidates = len(candidates)
    cycles: list[list[str]] = []
    if candidates:
        placeholders = ", ".join("?" for _ in candidates)
        edges = con.execute(
            f"""
            WITH RECURSIVE reach(seed, node, depth) AS (
                SELECT employee_id, employee_id, 0
                FROM employee_master WHERE employee_id IN ({placeholders})
                UNION
                SELECT r.seed, e.manager_id, r.depth + 1
                FROM reach r
                JOIN employee_master e ON e.employee_id = r.node
                WHERE e.manager_id IS NOT NULL AND r.depth < {max_length}
            )
            SELECT DISTINCT e.employee_id, e.manager_id
            FROM employee_master e
            WHERE e.manager_id IS NOT NULL
              AND e.employee_id IN (SELECT node FROM reach WHERE node IS NOT NULL)
            ORDER BY 1, 2
            """,
            candidates,
        ).fetchall()
        cycles = find_cycles([(a, b) for a, b in edges], max_length)
    summary.cycles_found = len(cycles)

    con.execute(
        "CREATE OR REPLACE TEMP TABLE manager_cycles ("
        "employee_id VARCHAR, cycle_id INTEGER, cycle_length INTEGER, "
        "cycle_path VARCHAR)"
    )
    if cycles:
        con.executemany(
            "INSERT INTO manager_cycles VALUES (?, ?, ?, ?)",
            [
                (member, index, len(cycle), " -> ".join([*cycle, cycle[0]]))
                for index, cycle in enumerate(cycles, start=1)
                for member in cycle
            ],
        )

    summary.self_approvals = int(
        con.execute(
            "SELECT count(DISTINCT employee_id) FROM fact_assignment_history "
            "WHERE approved_by = employee_id"
        ).fetchone()[0]
    )
    summary.seconds = round(time.perf_counter() - started, 3)
    if log:
        classes = ", ".join(
            f"{name} {count}" for name, count in sorted(summary.by_class.items())
        )
        log(
            f"  graph     {summary.components} components over "
            f"{summary.graph_nodes:,} linked employees ({classes}); "
            f"{summary.cycles_found} manager cycles, "
            f"{summary.self_approvals} self-approvals  {summary.seconds:.2f}s"
        )
    return summary


# --------------------------------------------------------------------------
# Shared SQL
# --------------------------------------------------------------------------


def monthly_net_sql(alias: str = "p") -> str:
    """The employee's own pay stream in their most recent paid month."""
    return f"""
    SELECT employee_id,
           min(period)                        AS first_paid,
           max(period)                        AS last_paid,
           count(*)                           AS months_paid,
           avg({alias}.net)                   AS monthly_net,
           sum({alias}.net)                   AS cumulative_net
    FROM features_period {alias}
    WHERE {alias}.paid_flag
    GROUP BY employee_id
    """


def related_json_sql() -> str:
    """The other members of a component, as the evidence bundle carries them."""
    return """
    SELECT c.component_id,
           to_json(list(struct_pack(
               employee_id := c.employee_id,
               name_en := e.name_en,
               org_unit_name_en := f.org_unit_name_en,
               site_name_en := f.site_name_en,
               monthly_net := round(coalesce(n.monthly_net, 0), 2)
           ) ORDER BY c.employee_id))          AS related_json,
           string_agg(e.name_en || ' (' || coalesce(f.org_unit_name_en, 'no unit')
                      || ')', ', ' ORDER BY c.employee_id) AS related_summary,
           sum(coalesce(n.monthly_net, 0))     AS total_monthly_disbursement,
           min(n.first_paid)                   AS component_first_paid,
           max(n.last_paid)                    AS component_last_paid
    FROM graph_components c
    JOIN employee_master e USING (employee_id)
    LEFT JOIN features_employee f USING (employee_id)
    LEFT JOIN net_by_employee n ON n.employee_id = c.employee_id
    GROUP BY c.component_id
    """


# --------------------------------------------------------------------------
# The five detectors. One function per code, each returning its DuckDB SQL.
# --------------------------------------------------------------------------


def sql_C01(policy) -> str:
    """One bank account, several employees, no declared relationship between them.

    The exclusions are done in `build_components`, where the whole component is
    in view: a component is only left alone when *every* link in it is
    explained, because explaining two links of a three-person ring says nothing
    about the third.
    """
    return """
    SELECT c.employee_id,
           n.first_paid                        AS first_period_paid,
           n.last_paid                         AS last_period_paid,
           n.months_paid                       AS months_paid,
           c.link_type                         AS graph_link_type,
           c.link_value_masked                 AS graph_link_value_masked,
           c.component_size                    AS graph_component_size,
           c.component_class                   AS graph_component_class,
           r.related_json,
           r.related_summary,
           round(r.total_monthly_disbursement, 2)
                                               AS graph_total_monthly_disbursement,
           f.org_unit_name_en,
           f.site_name_en,
           round(coalesce(n.monthly_net, 0), 2)  AS monthly_impact,
           round(coalesce(n.cumulative_net, 0), 2) AS cumulative_impact
    FROM graph_components c
    JOIN net_by_employee n ON n.employee_id = c.employee_id
    JOIN component_context r ON r.component_id = c.component_id
    LEFT JOIN features_employee f ON f.employee_id = c.employee_id
    WHERE c.link_kind = 'shared_iban'
      AND c.component_class = 'unrelated'
    ORDER BY c.component_id, c.employee_id
    """


def sql_C02(policy) -> str:
    """One identity number on more than one employee record.

    No exclusion and no judgement: a national ID belongs to one person, so a
    second record carrying it is wrong however it got there. Which of the
    records is the real one is the reviewer's question, not the detector's,
    which is why both are raised and the action is "hold all but the one whose
    documents check out".
    """
    return """
    SELECT c.employee_id,
           n.first_paid                        AS first_period_paid,
           n.last_paid                         AS last_period_paid,
           n.months_paid                       AS months_paid,
           c.link_type                         AS graph_link_type,
           c.link_value_masked                 AS graph_link_value_masked,
           c.component_size                    AS graph_component_size,
           c.component_class                   AS graph_component_class,
           r.related_json,
           r.related_summary,
           round(r.total_monthly_disbursement, 2)
                                               AS graph_total_monthly_disbursement,
           e.hire_date,
           f.org_unit_name_en,
           round(coalesce(n.monthly_net, 0), 2)  AS monthly_impact,
           round(coalesce(n.cumulative_net, 0), 2) AS cumulative_impact
    FROM graph_components c
    JOIN net_by_employee n ON n.employee_id = c.employee_id
    JOIN component_context r ON r.component_id = c.component_id
    JOIN employee_master e ON e.employee_id = c.employee_id
    LEFT JOIN features_employee f ON f.employee_id = c.employee_id
    WHERE c.link_kind = 'identity'
    ORDER BY c.component_id, c.employee_id
    """


def sql_C03(policy) -> str:
    """Paid month after month with no badge entry and no system login.

    Two things make this a finding rather than a quiet role. The run has to be
    **consecutive** -- a gaps-and-islands pass, the same one layers 1 and 2
    use -- because a fortnight of leave is not a ghost. And it has to be
    **before termination**: an employee still being paid after their leaving
    date stops showing activity for a reason that already has a code, and C04
    owns it. Without that exclusion this detector reports every leaver twice
    over, which is the difference between an alert and a duplicate.

    The planted `low_activity_role` confounder is the other half of the test: a
    field worker without an ERP account still badges in, so the run never
    starts.
    """
    months = int(policy.graph_threshold("C03", "min_silent_months"))
    corroborates = float(policy.graph_threshold("C03", "corroboration_score"))
    inner = """
    SELECT p.employee_id, p.period, p.period_index,
           p.net, p.base_pay, p.status, p.termination_date,
           p.job_title_en, p.org_unit_name_en, p.site_name_en
    FROM features_period p
    WHERE p.paid_flag
      AND coalesce(p.badge_swipes, 0) = 0
      AND coalesce(p.erp_logins, 0) = 0
      AND coalesce(p.activity_score, 0) = 0
      AND (p.termination_period IS NULL OR p.period <= p.termination_period)
    """
    select = f""",
       round(i.net, 2)                          AS monthly_impact,
       round(i.net * w.months_paid, 2)          AS cumulative_impact,
       round(coalesce(m.ml_score, 0), 1)        AS ml_score,
       m.ml_attributions_json,
       CASE WHEN coalesce(m.ml_score, 0) >= {corroborates}
            THEN 'The wider pay and attendance pattern on this record is '
                 || 'unlike almost any other in the workforce.'
            ELSE 'Not one of those months carries a badge entry, a system '
                 || 'login or any other recorded activity.' END AS corroboration"""
    joins = "LEFT JOIN ml_scores m ON m.employee_id = i.employee_id"
    return windowed(
        inner, select=select, joins=joins, where=f"WHERE w.months_paid >= {months}"
    )


def sql_C05(policy) -> str:
    """An approval signed by the person it benefits, or a reporting-line cycle.

    Two routes into one code, and the sentences they produce read nothing like
    each other, which is why `graph_ml.yaml` carries a template for each. The
    self-approval half is a direct equality test over the assignment record;
    the cycle half is the only place in the layer where the subgraph handed to
    networkx is directed.
    """
    return """
    WITH self_approved AS (
        SELECT a.employee_id,
               a.effective_from,
               a.change_reason,
               a.base_salary,
               a.approved_by,
               (year(a.effective_from) * 100 + month(a.effective_from)) AS from_period,
               row_number() OVER (PARTITION BY a.employee_id
                                  ORDER BY a.effective_from DESC) AS recency
        FROM fact_assignment_history a
        WHERE a.approved_by = a.employee_id
    )
    SELECT s.employee_id,
           'self_approval'                     AS route,
           greatest(s.from_period, n.first_paid)  AS first_period_paid,
           n.last_paid                         AS last_period_paid,
           greatest(
               date_diff('month',
                         make_date(s.from_period // 100, s.from_period % 100, 1),
                         make_date(n.last_paid // 100, n.last_paid % 100, 1)) + 1,
               1)                              AS months_paid,
           s.effective_from,
           s.change_reason,
           replace(s.change_reason, '_', ' ')  AS change_reason_label,
           round(s.base_salary, 2)             AS new_base_salary,
           s.approved_by,
           f.job_title_en,
           f.org_unit_name_en,
           round(s.base_salary, 2)             AS monthly_impact,
           round(s.base_salary * greatest(
               date_diff('month',
                         make_date(s.from_period // 100, s.from_period % 100, 1),
                         make_date(n.last_paid // 100, n.last_paid % 100, 1)) + 1,
               1), 2)                          AS cumulative_impact
    FROM self_approved s
    JOIN net_by_employee n ON n.employee_id = s.employee_id
    LEFT JOIN features_employee f ON f.employee_id = s.employee_id
    WHERE s.recency = 1

    UNION ALL BY NAME

    SELECT c.employee_id,
           'manager_cycle'                     AS route,
           n.first_paid                        AS first_period_paid,
           n.last_paid                         AS last_period_paid,
           n.months_paid                       AS months_paid,
           c.cycle_path,
           c.cycle_length,
           f.job_title_en,
           f.org_unit_name_en,
           0.0                                 AS monthly_impact,
           0.0                                 AS cumulative_impact
    FROM manager_cycles c
    JOIN net_by_employee n ON n.employee_id = c.employee_id
    LEFT JOIN features_employee f ON f.employee_id = c.employee_id
    ORDER BY 1
    """


def sql_C06(policy) -> str:
    """Two records that look like the same person, both being paid.

    Blocked on date of birth and bank account, then compared on the name -- so
    the comparison only ever runs over pairs that already share two facts a
    coincidence does not produce. `levenshtein` gives the reviewer the sentence
    they want: not a similarity, but *how many letters* differ.
    """
    return """
    SELECT c.employee_id,
           n.first_paid                        AS first_period_paid,
           n.last_paid                         AS last_period_paid,
           n.months_paid                       AS months_paid,
           c.link_type                         AS graph_link_type,
           c.link_value_masked                 AS graph_link_value_masked,
           c.component_size                    AS graph_component_size,
           c.component_class                   AS graph_component_class,
           r.related_json,
           r.related_summary,
           round(r.total_monthly_disbursement, 2)
                                               AS graph_total_monthly_disbursement,
           e.name_en,
           e.dob,
           e.hire_date,
           c.closest_employee_id               AS other_employee_id,
           o.name_en                           AS other_name_en,
           levenshtein(upper(e.name_en), upper(o.name_en)) AS name_edit_distance,
           round(coalesce(oth.monthly_net, 0), 2) AS other_monthly_net,
           CASE WHEN e.hire_date > o.hire_date THEN e.employee_id
                ELSE o.employee_id END         AS newer_employee_id,
           f.org_unit_name_en,
           round(coalesce(n.monthly_net, 0), 2)  AS monthly_impact,
           round(coalesce(n.cumulative_net, 0), 2) AS cumulative_impact
    FROM graph_components c
    JOIN net_by_employee n ON n.employee_id = c.employee_id
    JOIN component_context r ON r.component_id = c.component_id
    JOIN employee_master e ON e.employee_id = c.employee_id
    JOIN employee_master o ON o.employee_id = c.closest_employee_id
    LEFT JOIN net_by_employee oth ON oth.employee_id = c.closest_employee_id
    LEFT JOIN features_employee f ON f.employee_id = c.employee_id
    WHERE c.link_kind = 'shared_iban'
      AND c.component_class = 'near_duplicate'
    ORDER BY c.component_id, c.employee_id
    """


DETECTORS: dict[str, Any] = {
    "C01": sql_C01,
    "C02": sql_C02,
    "C03": sql_C03,
    "C05": sql_C05,
    "C06": sql_C06,
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
    """The evidence bundle's raw material: fields, graph context, attributions."""
    fields = {
        k: v
        for k, v in row.items()
        if not k.startswith("graph_") and k not in INTERNAL_COLUMNS
    }
    payload: dict[str, Any] = {"anomaly_code": code, "fields": fields}
    context = {
        name: row.get(f"graph_{name}")
        for name in GRAPH_FIELDS
        if f"graph_{name}" in row
    }
    if row.get("related_json"):
        context["related_employees"] = json.loads(row["related_json"])
    if context:
        payload["graph_context"] = context
    if row.get("ml_attributions_json"):
        payload["feature_attributions"] = json.loads(row["ml_attributions_json"])
    payload["metric"] = config.get("metric")
    return json.dumps(payload, default=str)


def _context(row: dict[str, Any]) -> dict[str, Any]:
    """What a description template can name: every column, graph fields unprefixed."""
    context = dict(row)
    for key, value in row.items():
        if key.startswith("graph_"):
            context.setdefault(key[len("graph_"):], value)
    context["first_period_label"] = period_label(row.get("first_period_paid"))
    context["last_period_label"] = period_label(row.get("last_period_paid"))
    # A window can be one month long -- a self-approval signed in the last
    # period of the lake is the common case -- and "1 months" is the kind of
    # detail that makes a reviewer distrust the rest of the sentence.
    months = int(row.get("months_paid") or 0)
    context["months_paid_label"] = f"{months} month" + ("" if months == 1 else "s")
    link_type = row.get("graph_link_type")
    context["identifier_label"] = LINK_LABELS.get(str(link_type), "identity number")
    context["link_label"] = context["identifier_label"]
    return context


def _wording(config: dict, row: dict[str, Any], code: str) -> tuple[str, list[str]]:
    """The template pair for this row -- one code may take more than one route."""
    routes = config.get("routes") or {}
    if routes:
        route = str(row.get("route") or "")
        if route not in routes:
            raise L3Error(
                f"graph_ml.yaml: {code} has no wording for route {route!r}; "
                f"declared routes are {sorted(routes)}"
            )
        block = routes[route]
    else:
        block = config
    if "description" not in block:
        raise L3Error(f"graph_ml.yaml: {code} has no description")
    return str(block["description"]), list(block.get("recommended_actions") or [])


def run_l3(
    con: duckdb.DuckDBPyConnection,
    policy,
    *,
    codes: list[str] | None = None,
    log=None,
) -> L3Result:
    """Run every enabled layer-3 detector and return its findings.

    Preparation -- both models, then the candidate components -- runs once and
    is shared; each detector is then one DuckDB query over it.
    """
    started = time.perf_counter()
    summary, ml = prepare(con, policy, log=log)
    result = L3Result(seconds=0.0, graph=summary, ml=ml)

    con.execute(
        f"CREATE OR REPLACE TEMP TABLE net_by_employee AS {monthly_net_sql()}"
    )
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE component_context AS {related_json_sql()}"
    )

    wanted = codes or sorted(DETECTORS)
    unknown = sorted(set(wanted) - set(DETECTORS))
    if unknown:
        raise L3Error(f"no layer-3 detector for {unknown}")

    built: list[str] = []
    for code in wanted:
        config = policy.graph_codes.get(code)
        if config is None:
            raise L3Error(f"graph_ml.yaml has no entry for {code}")
        if not config.get("enabled", True):
            continue
        built.append(code)
        code_started = time.perf_counter()
        sql = DETECTORS[code](policy)
        try:
            rows = con.execute(sql).fetchall()
        except duckdb.Error as exc:
            raise L3Error(f"{code}: detector query failed: {exc}") from exc
        names = [d[0] for d in (con.description or [])]
        missing = sorted(set(REQUIRED_COLUMNS) - set(names))
        if missing:
            raise L3Error(f"{code}: detector query does not return {missing}")

        severity = str(config["severity"])
        if severity not in SEVERITIES:
            raise L3Error(f"graph_ml.yaml: {code} severity {severity!r} unknown")
        employees: set[str] = set()
        for values in rows:
            row = {name: _plain(value) for name, value in zip(names, values)}
            context = _context(row)
            monthly = float(row.get("monthly_impact") or 0.0)
            cumulative = float(row.get("cumulative_impact") or 0.0)
            context["monthly_impact"] = monthly
            context["cumulative_impact"] = cumulative
            employees.add(str(row["employee_id"]))
            description_template, action_templates = _wording(config, row, code)
            try:
                description = render(
                    " ".join(description_template.split()), context
                )
                actions = [render(a, context) for a in action_templates]
            except RuleError as exc:
                raise L3Error(f"{code}: {exc}") from exc
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
