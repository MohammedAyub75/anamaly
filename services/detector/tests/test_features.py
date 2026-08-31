"""The feature store: shape, freshness, speed, and what it must never contain."""

from __future__ import annotations

import duckdb
import pytest
from detector.config import DetectorConfig
from detector.features.build import BLOCKS, OUTPUTS, build, cache_key, is_current
from detector.lake import LABEL_TABLES, RAW_TABLES
from detector.policy import DetectorPolicy, DigestMismatch

# Columns later phases and the rule pack are entitled to find. Losing one of
# these is a schema break, not a refactor.
CONTRACT_COLUMNS = (
    "employee_id", "period", "period_index", "period_end_date", "calendar_days",
    "asat_grade", "asat_job_code", "asat_org_unit_id", "asat_site_id",
    "asat_manager_id", "asat_base_salary", "asat_org_cost_center",
    "site_class", "site_hardship_tier", "site_remote_allowance_eligible",
    "previous_site_remote_allowance_eligible", "months_since_site_change",
    "job_min_grade", "job_max_grade", "job_min_education_rank", "job_safety_critical",
    "band_salary_min", "band_salary_max", "band_position",
    "education_rank", "education_below_job_minimum", "certification_expired",
    "gosi_class_expected", "gosi_class_mismatch",
    "acting_months_over_limit", "relocation_months_over_limit",
    "termination_period", "months_since_termination",
    "base_pay", "allowance_total", "net", "standing_pay", "allowance_ratio",
    "paid_cost_center", "paid_flag",
    "attendance_days_total", "activity_score",
    "allowance_offpolicy_count", "allowance_offpolicy_delta_total",
    "non_severance_allowance_count",
)


def test_every_block_produced_a_table(features) -> None:
    for name, _ in OUTPUTS:
        assert features.row_counts.get(name), name
    assert set(features.block_seconds or {}) <= set(BLOCKS)


def test_period_grain_covers_every_employee(cfg: DetectorConfig, con) -> None:
    """One row per employee per month from hire onward -- nobody drops out."""
    employees = con.execute(
        "SELECT count(DISTINCT employee_id) FROM features_period"
    ).fetchone()[0]
    assert employees == cfg.employees
    span = con.execute(
        "SELECT min(period), max(period) FROM features_period"
    ).fetchone()
    assert span == (cfg.period_from, cfg.period_to)


def test_no_duplicate_grain(con) -> None:
    dupes = con.execute(
        "SELECT count(*) FROM (SELECT employee_id, period FROM features_period "
        "GROUP BY 1, 2 HAVING count(*) > 1)"
    ).fetchone()[0]
    assert dupes == 0


def test_contract_columns_are_present(columns: list[str]) -> None:
    missing = sorted(set(CONTRACT_COLUMNS) - set(columns))
    assert not missing, missing


def test_every_allowance_code_has_a_column(
    columns: list[str], policy: DetectorPolicy
) -> None:
    """26 codes today, and adding a 27th must need no code change."""
    for code in policy.allowance_codes:
        assert f"allowance_{code}_amount" in columns, code


def test_as_at_state_is_not_todays_state(con) -> None:
    """The spine must carry history, or every rule silently exonerates transfers."""
    moved = con.execute(
        """
        SELECT count(*) FROM (
            SELECT employee_id FROM features_period
            GROUP BY 1 HAVING count(DISTINCT asat_site_id) > 1
        )
        """
    ).fetchone()[0]
    assert moved > 0


def test_expected_amounts_agree_with_the_policy_resolver(
    con, policy: DetectorPolicy
) -> None:
    """DuckDB's recomputation and policycore's must give the same answer.

    The SQL is generated from the same pack the Python resolver reads, and this
    is the assertion that keeps A07 a second opinion rather than a second bug.
    """
    rows = con.execute(
        """
        SELECT a.allowance_code, a.expected_amount, f.asat_base_salary,
               f.asat_grade, f.site_hardship_tier, f.dependents_in_kingdom
        FROM features_allowance a
        JOIN features_period f USING (employee_id, period)
        WHERE a.expected_amount > 0
        USING SAMPLE 300 ROWS
        """
    ).fetchall()
    assert rows
    for code, expected, salary, grade, tier, dependents in rows:
        allowance = policy.pack.allowances[code]
        resolved = allowance.resolve_amount(
            {
                "base_salary": salary,
                "grade": grade,
                "site.hardship_tier": tier,
                "dependents_in_kingdom": dependents,
            }
        )
        assert abs(float(resolved) - float(expected)) < 0.01, code


def test_cohort_stats_cover_every_ladder_level(con, policy: DetectorPolicy) -> None:
    levels = {
        int(r[0]) for r in con.execute(
            "SELECT DISTINCT cohort_level FROM cohort_stats"
        ).fetchall()
    }
    assert levels == set(range(1, len(policy.cohort_ladder) + 1))
    negative = con.execute(
        "SELECT count(*) FROM cohort_stats WHERE n <= 0 OR mad < 0"
    ).fetchone()[0]
    assert negative == 0


def test_the_detection_connection_cannot_see_ground_truth(con) -> None:
    """The non-negotiable, enforced structurally rather than by convention."""
    for table in LABEL_TABLES:
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT count(*) FROM {table}")


def test_no_feature_column_leaks_a_label(columns: list[str]) -> None:
    leaked = [c for c in columns if "label" in c.lower() or "anomaly" in c.lower()]
    assert not leaked, leaked


def test_benign_share_flag_is_not_a_feature(cfg: DetectorConfig) -> None:
    """`is_known_benign_share` is eval metadata; a detector reading it is told
    the answer (docs/DATA_DICTIONARY.md)."""
    con = duckdb.connect()
    try:
        for name, _ in OUTPUTS:
            found = con.execute(
                f"SELECT * FROM read_parquet('{cfg.feature_glob(name)}',"
                " hive_partitioning=false) LIMIT 0"
            ).description
            assert "is_known_benign_share" not in [c[0] for c in (found or [])], name
    finally:
        con.close()


def test_raw_table_list_matches_the_lake(cfg: DetectorConfig) -> None:
    on_disk = {p.name for p in cfg.lake.iterdir() if p.is_dir()}
    assert set(RAW_TABLES) | set(LABEL_TABLES) == on_disk


def test_build_is_cached_and_the_key_covers_the_sql(
    cfg: DetectorConfig, policy: DetectorPolicy, features
) -> None:
    assert is_current(cfg, policy)
    again = build(cfg, policy)
    assert again.cached
    assert again.row_counts == features.row_counts
    assert cache_key(cfg, policy) == features.cache_key


def test_stale_policy_refuses_to_build(cfg: DetectorConfig, policy: DetectorPolicy) -> None:
    stale = dict(cfg.manifest)
    stale["policy_digest"] = {"sites.yaml": "sha256:not-the-one-you-generated-under"}
    with pytest.raises(DigestMismatch, match="sites.yaml"):
        policy.require_digest(stale)


def test_feature_build_is_within_the_phase_three_budget(features) -> None:
    """docs/specs/detector.md: under 60 seconds at 10k."""
    if features.cached:
        pytest.skip("feature store was reused; nothing was timed")
    assert features.seconds < 60.0, f"{features.seconds:.1f}s"
