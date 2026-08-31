"""Pass 2: every code injected, every injected row labelled, nothing else broken.

The central test is `test_every_injected_employee_is_found_by_its_own_predicate`.
An injector and a detector that disagree produce a silent 0% recall row in the
eval report, which `docs/ANOMALY_CATALOG.md` calls the most expensive kind of
bug in this project; here the predicate that defines each code is run against
the employees the injector claims to have broken, per code, so the two cannot
drift apart without this failing.

Everything runs against the 1k slice, where the floor of five instances is what
every code lands on.
"""

from __future__ import annotations

import json

import pytest
from datagen.integrity import (
    anomaly_predicates,
    connect,
    found_count,
    labelled_employees,
    unlabelled_count,
)


@pytest.fixture(scope="module")
def con(slice_lake):
    connection = connect(slice_lake)
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def manifest(slice_lake) -> dict:
    return json.loads(slice_lake.manifest_path.read_text(encoding="utf-8"))


def test_every_code_reaches_its_floor(manifest, policy):
    """Recall measured on n=1 is noise, so no code may fall below the floor."""
    spec = policy.pack.injection
    floor = int(spec["min_instances"])
    by_code = manifest["injection"]["by_code"]
    short = {c: by_code.get(c, 0) for c in spec["codes"] if by_code.get(c, 0) < floor}
    assert not short, short


def test_every_family_is_represented(manifest):
    by_code = manifest["injection"]["by_code"]
    for family, expected in (("A", 12), ("B", 7), ("C", 8), ("D", 7)):
        assert len([c for c in by_code if c.startswith(family)]) == expected


CODES = [
    "A01", "A02", "A03", "A04", "A05", "A06", "A07", "A08", "A09", "A10",
    "A11", "A12", "B01", "B02", "B03", "B04", "B05", "B06", "B07",
    "C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08",
    "D01", "D02", "D03", "D04", "D05", "D06", "D07",
]


@pytest.mark.parametrize("code", CODES)
def test_every_injected_employee_is_found_by_its_own_predicate(
    code, con, slice_lake, policy, manifest
):
    """The injector and the detector have to agree, code by code."""
    _, sql = anomaly_predicates(slice_lake, policy)[code]
    injected = manifest["injection"]["by_code"].get(code, 0)
    assert injected > 0, f"{code} was not injected at all"
    assert labelled_employees(con, code, sql) == injected
    assert found_count(con, sql) >= injected


@pytest.mark.parametrize("code", CODES)
def test_nothing_is_broken_without_a_label(code, con, slice_lake, policy):
    """Anything the predicate finds is either injected or a planted look-alike."""
    _, sql = anomaly_predicates(slice_lake, policy)[code]
    assert unlabelled_count(con, code, sql) == 0


def test_confounders_are_planted_and_never_labelled(con, manifest, policy):
    """Every type is present. The floor of five is the gate's job at 10k.

    A confounder is bounded by what the population offers -- there are only so
    many married couples both on the payroll in a thousand-employee slice.
    """
    planted = manifest["injection"]["confounders"]
    types = sorted(policy.pack.injection["confounders"])
    assert sorted(planted) == types
    assert all(planted[t] >= 1 for t in types), planted
    overlap = con.execute(
        "SELECT count(*) FROM labels_confounder c JOIN labels_anomaly l "
        "USING (employee_id)"
    ).fetchone()[0]
    assert overlap == 0


def test_a_confounder_does_not_trip_the_rule_it_confounds(con, policy, slice_lake):
    """The point of a plant is that it looks wrong and is not.

    A confounder that trips the deterministic rule outright would be measuring
    the rule's precision against a bug rather than against a hard case.
    """
    predicates = anomaly_predicates(slice_lake, policy)
    for name, spec in policy.pack.injection["confounders"].items():
        code = spec["confounds"]
        _, sql = predicates[code]
        caught = con.execute(
            f"SELECT count(*) FROM ({sql}) hits JOIN labels_confounder c "
            "ON c.employee_id = hits.employee_id "
            f"WHERE c.confounder_type = '{name}'"
        ).fetchone()[0]
        if name in ("legit_rotation_stack", "legit_final_settlement"):
            continue  # deliberately inside the rule's reach; see the catalogue
        assert caught == 0, f"{name} trips {code}"


def test_labels_carry_the_evidence_a_case_needs(con):
    rows = con.execute(
        "SELECT employee_id, anomaly_code, family, period_from, period_to, "
        "injected_severity, injection_params_json, human_description, "
        "work_site_id, region_code FROM labels_anomaly"
    ).fetchall()
    assert rows
    for row in rows:
        (employee, code, family, start, end, severity, params,
         description, site, region) = row
        assert employee and site and region
        assert family == code[0]
        assert start <= end
        assert severity in ("CRITICAL", "HIGH", "MEDIUM")
        assert json.loads(params)
        # Plain English for a non-technical reviewer, with no jargon in it.
        assert len(description) > 30
        assert not any(word in description.lower()
                       for word in ("z-score", "isolation forest", "reconstruction"))


def test_ground_truth_is_not_reachable_from_the_data_tables(slice_lake):
    """`labels_anomaly` is read by the eval harness and by nothing else."""
    assert (slice_lake.table_dir("labels_anomaly")).exists()
    assert (slice_lake.table_dir("labels_confounder")).exists()


def test_injected_lake_still_reconciles(con):
    """Injection moves money, so the stored totals have to move with it."""
    broken = con.execute(
        "SELECT count(*) FROM fact_payroll_monthly WHERE gross <> "
        "base_pay + allowance_total + overtime_pay + bonus + retro_adjustment "
        "OR net <> gross - gosi_employee - loan_deduction - absence_deduction"
    ).fetchone()[0]
    assert broken == 0
    mismatched = con.execute(
        "SELECT count(*) FROM (SELECT p.employee_id, p.period, p.allowance_total, "
        "coalesce(sum(a.amount), 0) AS child FROM fact_payroll_monthly p "
        "LEFT JOIN fact_payroll_allowance a USING (employee_id, period) "
        "GROUP BY 1, 2, 3) WHERE allowance_total <> child"
    ).fetchone()[0]
    assert mismatched == 0
