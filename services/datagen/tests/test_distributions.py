"""The population has to be skewed the way a real workforce is.

A uniform population makes every anomaly trivially separable, so these
tolerances are not cosmetic: if the nationality mix flattened or the grade
pyramid inverted, the evaluation in later phases would be measuring an easier
problem than the one the platform exists to solve.

Tolerances are wide because the slice is 1,000 employees; they are set to catch
a distribution that has broken, not to pin down sampling noise.
"""

from __future__ import annotations

import duckdb
import pytest
from datagen.integrity import connect


@pytest.fixture(scope="module")
def con(slice_lake) -> duckdb.DuckDBPyConnection:
    connection = connect(slice_lake)
    yield connection
    connection.close()


def share(con, sql: str) -> dict[str, float]:
    return {row[0]: float(row[1]) for row in con.execute(sql).fetchall()}


def test_nationality_mix_is_saudi_majority(con):
    mix = share(
        con,
        "SELECT nationality_class, count(*) / (SELECT count(*) FROM employee_master) "
        "FROM employee_master GROUP BY 1",
    )
    assert 0.50 <= mix["saudi"] <= 0.72, mix
    assert 0.24 <= mix["expat"] <= 0.45, mix
    assert 0.01 <= mix["gcc"] <= 0.10, mix


def test_saudization_is_higher_in_the_office_than_in_the_field(con):
    """`nationality_class` has to be a real analytical dimension, not noise."""
    rows = con.execute(
        "SELECT s.site_class IN ('hq','office','training') AS corporate, "
        "avg(CASE WHEN e.nationality_class = 'saudi' THEN 1.0 ELSE 0 END) "
        "FROM employee_master e JOIN dim_site s ON s.site_id = e.work_site_id "
        "GROUP BY 1 ORDER BY 1"
    ).fetchall()
    field_share, corporate_share = (float(r[1]) for r in rows)
    assert corporate_share > field_share


def test_grade_pyramid_tapers_at_the_top(con):
    counts = {
        int(g): int(n)
        for g, n in con.execute(
            "SELECT grade, count(*) FROM employee_master GROUP BY 1"
        ).fetchall()
    }
    junior = sum(n for g, n in counts.items() if g <= 8)
    senior = sum(n for g, n in counts.items() if g >= 14)
    assert junior > senior * 4, counts
    assert max(counts) >= 14, "no senior grades at all"


def test_site_assignment_follows_headcount_weight(con):
    """Eastern Province dominance is why every map metric is per 1,000."""
    top = con.execute(
        "SELECT region_code, count(*) FROM employee_master "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 1"
    ).fetchone()
    total = con.execute("SELECT count(*) FROM employee_master").fetchone()[0]
    assert top[0] == "SA-04"
    assert top[1] / total > 0.25, top


def test_tenure_is_right_skewed(con):
    p50, p90, longest = con.execute(
        "SELECT quantile_cont(service_years, 0.5), quantile_cont(service_years, 0.9), "
        "max(service_years) FROM employee_master"
    ).fetchone()
    assert 3.0 <= float(p50) <= 12.0
    assert float(p90) > float(p50)
    assert float(longest) > 20.0


def test_status_mix_is_mostly_active(con):
    mix = share(
        con,
        "SELECT status, count(*) / (SELECT count(*) FROM employee_master) "
        "FROM employee_master GROUP BY 1",
    )
    assert 0.88 <= mix["active"] <= 0.97, mix
    assert 0.01 <= mix.get("terminated", 0) <= 0.10, mix


def test_housing_and_transport_correlate_with_the_site(con):
    """This correlation is what makes A05 and A06 rare *and* plausible."""
    camp = con.execute(
        "SELECT avg(CASE WHEN e.housing_type = 'company_camp_bachelor' THEN 1.0 ELSE 0 END) "
        "FROM employee_master e JOIN dim_site s ON s.site_id = e.work_site_id "
        "WHERE s.camp_available AND NOT s.family_housing_available"
    ).fetchone()[0]
    office = con.execute(
        "SELECT avg(CASE WHEN e.housing_type = 'company_camp_bachelor' THEN 1.0 ELSE 0 END) "
        "FROM employee_master e JOIN dim_site s ON s.site_id = e.work_site_id "
        "WHERE s.site_class IN ('hq','office')"
    ).fetchone()[0]
    assert float(camp) > 0.5
    assert float(office) == 0.0


def test_rotation_only_where_the_site_supports_it(con):
    stray = con.execute(
        "SELECT count(*) FROM employee_master e JOIN dim_site s "
        "ON s.site_id = e.work_site_id WHERE e.work_pattern IN "
        "('rotation_28_28','rotation_14_14') AND NOT s.rotation_supported"
    ).fetchone()[0]
    assert stray == 0


def test_allowance_load_stays_below_the_injection_range(con, policy):
    """B03 injects a 0.70-0.90 ratio; the clean set has to sit under that."""
    ceiling = float(policy.pack.allowance_load["clean_population_ratio_max"])
    worst, median = con.execute(
        "SELECT max(allowance_ratio), median(allowance_ratio) FROM employee_master"
    ).fetchone()
    assert float(worst) <= ceiling + 1e-6
    assert 0.2 <= float(median) <= ceiling


def test_managers_change_over_a_career(con):
    """D05 needs manager changes the clean population produces on its own.

    They arrive two ways: a transfer moves the employee under a different unit,
    and a promotion can outgrow the manager who was two grades above. Without
    them the clean set has no manager-change events, so D05 would have nothing
    to hide among and no precision denominator.
    """
    changes, people = con.execute(
        "WITH moves AS (SELECT employee_id, effective_from, manager_id, "
        "lag(manager_id) OVER (PARTITION BY employee_id ORDER BY effective_from) "
        "AS prior FROM fact_assignment_history) "
        "SELECT count(*), count(DISTINCT employee_id) FROM moves m, "
        "(SELECT min(period) AS lo, max(period) AS hi FROM dim_calendar) w "
        "WHERE m.prior IS NOT NULL AND m.manager_id IS DISTINCT FROM m.prior "
        "AND year(m.effective_from) * 100 + month(m.effective_from) "
        "BETWEEN w.lo AND w.hi"
    ).fetchone()
    total = con.execute("SELECT count(*) FROM employee_master").fetchone()[0]
    assert changes > 0, "no manager changes inside the observation window"
    assert 0.15 < people / total < 0.90, (people, total)


def test_nobody_approves_their_own_assignment(con):
    """Self-approval is C05; the approver is the manager at the time."""
    assert con.execute(
        "SELECT count(*) FROM fact_assignment_history WHERE approved_by = employee_id"
    ).fetchone()[0] == 0
    assert con.execute(
        "SELECT count(DISTINCT approved_by) FROM fact_assignment_history"
    ).fetchone()[0] > 1


def test_realism_noise_is_present_but_never_labelled(con, slice_lake):
    """Data-quality flags are not anomalies; they exist so the two differ."""
    flagged = con.execute(
        "SELECT count(*) FROM employee_master WHERE len(dq_flags) > 0"
    ).fetchone()[0]
    total = con.execute("SELECT count(*) FROM employee_master").fetchone()[0]
    assert 0 < flagged < total * 0.25, flagged
    assert not (slice_lake.lake / "labels_anomaly").exists()
