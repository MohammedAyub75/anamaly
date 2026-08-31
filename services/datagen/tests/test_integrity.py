"""The full integrity suite at 1k scale, plus the leak detector itself.

The phase-1 gate runs these checks at 10k; running them here at 1k catches a
break in seconds rather than after a half-minute generation.  The last test is
the important one: it corrupts a clean lake on purpose and asserts the suite
notices, because a check that cannot fail is not a check.
"""

from __future__ import annotations

import json

import pytest
from datagen.integrity import (
    anomaly_predicates,
    connect,
    found_count,
    run,
    summarise,
    unlabelled_count,
)


@pytest.fixture(scope="module")
def report(slice_lake, policy):
    return run(slice_lake, policy, include_determinism=False, include_git=False)


def test_the_clean_slice_passes_every_check(report):
    failures = [f"{c.name}: {c.detail}" for c in report.checks if not c.ok]
    assert not failures, failures


def test_all_thirty_four_anomaly_codes_are_checked(slice_lake, policy):
    codes = set(anomaly_predicates(slice_lake, policy))
    assert len(codes) == 34
    for family, expected in (("A", 12), ("B", 7), ("C", 8), ("D", 7)):
        assert len([c for c in codes if c.startswith(family)]) == expected


def test_every_code_is_reported_separately(report):
    """A leak has to be visible by code, not buried in a total."""
    reported = {
        c.name.split()[0]
        for c in report.checks
        if c.name[:1] in "ABCD" and c.name[1:3].isdigit()
    }
    assert len(reported) == 34


def test_a_planted_violation_is_detected(slice_lake, policy):
    """Break A02 by hand and confirm the predicate notices.

    Without this the suite could be passing because the queries are wrong
    rather than because the data is accounted for.
    """
    con = connect(slice_lake)
    try:
        _, sql = anomaly_predicates(slice_lake, policy)["A02"]
        assert unlabelled_count(con, "A02", sql) == 0
        assert found_count(con, sql) > 0, "A02 was injected, so it must be found"
        # Every site becomes tier 0, so the HARDSHIP rows already in the lake
        # are all violations of the same clause A02 polices.
        con.execute(
            "CREATE TEMP VIEW site_zero AS "
            "SELECT * REPLACE (0::TINYINT AS hardship_tier) FROM dim_site"
        )
        tampered = sql.replace("JOIN dim_site st", "JOIN site_zero st")
        assert tampered != sql
        assert unlabelled_count(con, "A02", tampered) > 0
    finally:
        con.close()


def test_manifest_declares_the_policy_digest(slice_lake, policy):
    manifest = json.loads(slice_lake.manifest_path.read_text(encoding="utf-8"))
    assert manifest["policy_digest"] == policy.pack.digest
    assert manifest["seed"] == slice_lake.seed
    assert manifest["period_count"] == slice_lake.periods
    assert manifest["injection"]["employees_with_anomaly"] > 0
    assert len(manifest["injection"]["by_code"]) >= 30


def test_summary_prints_counts_not_rows(slice_lake):
    rows = summarise(slice_lake)
    assert any(label == "employee_master" for label, _ in rows)
    assert all(len(str(value)) < 200 for _, value in rows)
