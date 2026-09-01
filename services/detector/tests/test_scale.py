"""Phase 7: the things that make a 1m run affordable, tested at 10k.

None of this can be tested at the scale it exists for -- a suite that generates
a million employees is a suite nobody runs.  What *can* be tested is that each
mechanism behaves identically to the code it replaced when it is not binding,
and behaves the way it claims when it is: that the caps leave a small
population untouched, that a sampled model fit is the same fit on two runs,
that a chunked bundle assembly produces exactly what the unchunked one
produced, and that the runtime profile records what a phase gate is entitled to
hold a run to.
"""

from __future__ import annotations

import json
from dataclasses import replace

import numpy as np
import pytest
from detector.features.build import build as build_features
from detector.features.build import windows as build_windows
from detector.lake import connect
from detector.layers.l2_salary import fit as fit_salary
from detector.layers.l3_ml import LevelColumn, _encode, build_matrix, train_autoencoder
from detector.layers.l4_fusion import run_fusion
from detector.policy import DetectorPolicy
from detector.run import PeakMemory, RunResult, load_profiles, record_profile

from policycore import runtime as runtime_pack
from policycore.packs import POLICY_FILES

pytestmark = pytest.mark.usefixtures("features")


def _with_runtime(policy: DetectorPolicy, runtime: dict) -> DetectorPolicy:
    """The same pack with different engineering dials.

    `runtime` is a `cached_property`, so seeding the instance dictionary is the
    documented way to hand one a different answer -- and it keeps the test
    honest about what it is changing: the dials, never the policy.
    """
    other = DetectorPolicy(policy.pack)
    other.__dict__["runtime"] = runtime
    return other


class _Tighter:
    """One policy pack with one dial overridden.

    `expected_salary` is a property on the class rather than a cached one, so
    it cannot be shadowed on an instance; delegating is both simpler and more
    honest about what the test changes.
    """

    def __init__(self, policy: DetectorPolicy, **overrides) -> None:
        self._policy = policy
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._policy, name)


def _comparable(bundle_json: str) -> dict:
    """A bundle without the one field that is genuinely time-dependent."""
    bundle = json.loads(bundle_json)
    bundle["provenance"].pop("scored_at", None)
    return bundle


# --------------------------------------------------------------------------
# The runtime pack
# --------------------------------------------------------------------------


def test_runtime_pack_is_not_digested():
    """Editing a memory limit must not invalidate twenty-four million rows."""
    assert "runtime.yaml" not in POLICY_FILES


def test_runtime_pack_is_optional(tmp_path):
    """A checkout without the file still runs, at every reader's own default."""
    assert runtime_pack.load(tmp_path) == {}
    assert runtime_pack.section({}, "datagen", "injection") == {}
    bare = _with_runtime(DetectorPolicy.load(), {})
    assert bare.duckdb_memory_limit is None
    assert bare.peak_rss_budget_gb == 12.0
    assert bare.bundle_chunk_alerts > 0


def test_runtime_dials_are_read(policy: DetectorPolicy):
    assert policy.duckdb_memory_limit.endswith("GB")
    assert policy.duckdb_temp_directory
    assert policy.peak_rss_budget_gb == 12.0
    assert policy.target_minutes == 15.0
    assert policy.runtime_digest.startswith("sha256:")


def test_runtime_digest_moves_with_the_dials(policy: DetectorPolicy):
    changed = _with_runtime(
        policy, {**policy.runtime, "fusion": {"bundle_chunk_alerts": 7}}
    )
    assert changed.runtime_digest != policy.runtime_digest


# --------------------------------------------------------------------------
# The caps
# --------------------------------------------------------------------------


def test_caps_do_not_bind_at_10k(policy: DetectorPolicy, cfg):
    """Phases 3-6 scored a population smaller than every cap, so the numbers
    they reported stand exactly as they were reported."""
    assert int(policy.autoencoder["max_train_rows"]) >= cfg.employees
    assert int(policy.autoencoder["attribution_max_rows"]) >= cfg.employees
    assert int(policy.expected_salary["attribution_max_rows"]) >= cfg.employees
    assert int(policy.isolation_forest["score_batch_rows"]) >= cfg.employees


def test_every_employee_is_explained_when_the_cap_is_wide(l2, l3):
    assert l3.ml is not None and l3.ml.attributed == l3.ml.rows
    assert l2.salary is not None and l2.salary.attributed == l2.salary.rows


def test_the_salary_cap_keeps_the_widest_gaps(cfg, policy: DetectorPolicy):
    """With the cap binding, the records that keep an attribution are the ones
    with something to explain -- which is where every finding comes from."""
    tight = _Tighter(
        policy,
        expected_salary={**policy.expected_salary, "attribution_max_rows": 200},
    )
    # Its own connection: `salary_expectation` is a registered table, and the
    # session's layer-2 pass is entitled to keep the one it fitted.
    con = connect(cfg, features=True)
    try:
        fitted = fit_salary(con, tight)
        explained, unexplained = con.execute(
            "SELECT min(abs(salary_residual)) "
            "  FILTER (WHERE attributions_json <> ''), "
            "       max(abs(salary_residual)) FILTER (WHERE attributions_json = '') "
            "FROM salary_expectation"
        ).fetchone()
    finally:
        con.close()
    assert fitted.attributed == 200
    assert fitted.rows > fitted.attributed
    assert explained >= unexplained


# --------------------------------------------------------------------------
# The matrix
# --------------------------------------------------------------------------


def test_encode_is_sorted_by_value_not_row_order():
    codes, levels, names = _encode(["plant", "hq", "plant", None])
    assert list(names) == ["", "hq", "plant"]
    assert list(codes) == [2, 1, 2, 0]
    assert levels == 3


def test_level_column_indexes_without_materialising():
    column = LevelColumn(np.array(["hq", "plant"]), np.array([1, 0, 1]))
    assert column[0] == "plant"
    assert len(column) == 3
    assert list(column[1:].codes) == [0, 1]


def test_matrix_values_are_the_matrix(con, policy: DetectorPolicy):
    """An attribution reads the number the model saw, not a second copy of it."""
    matrix = build_matrix(con, policy)
    first = matrix.numeric_names[0]
    assert matrix.values[first][0] == matrix.numeric[0, 0]
    assert isinstance(matrix.values[matrix.categorical_names[0]], LevelColumn)
    assert np.isfinite(matrix.numeric).all()  # every missing value was imputed


def test_the_sampled_fit_is_the_same_fit_twice(con, policy: DetectorPolicy):
    """`max_train_rows` samples from a seeded generator, so two runs over one
    lake fit the same network on the same records -- and score everybody."""
    matrix = build_matrix(con, policy)
    config = {**policy.autoencoder, "epochs": 2, "max_train_rows": 500}
    first, *_ = train_autoencoder(matrix, config, device="cpu")
    second, *_ = train_autoencoder(matrix, config, device="cpu")
    assert first.shape[0] == matrix.rows
    assert np.allclose(first, second)


# --------------------------------------------------------------------------
# The windowed feature build
# --------------------------------------------------------------------------


def test_the_window_is_one_group_at_10k(cfg, policy: DetectorPolicy):
    """The dial is in rows, so a small tier writes exactly as it always did."""
    assert len(build_windows(cfg, policy.rows_per_write)) == 1
    groups = build_windows(cfg, 100_000)
    assert len(groups) > 1
    assert [period for group in groups for period in group] == cfg.period_list


def test_writing_a_few_months_at_a_time_changes_nothing(cfg, policy: DetectorPolicy):
    """A feature store written in groups is the store written in one pass.

    The group size is a memory decision; it must not be able to change a
    number. `period_index` is the one that would break first and silently: it
    is the month's position in the window, everything from the gaps-and-islands
    pass to CUSUM counts on it, and numbering it inside a group would restart it
    at 1 and turn one fourteen-month finding into fourteen.
    """
    def summary() -> tuple:
        con = connect(cfg, features=True, policy=policy)
        try:
            return con.execute(
                "SELECT count(*), sum(period_index), max(period_index), "
                "       round(sum(coalesce(base_pay, 0)), 2), "
                "       count(*) FILTER (WHERE paid_flag) "
                "FROM features_period"
            ).fetchone()
        finally:
            con.close()

    one_pass = summary()
    assert one_pass[2] == cfg.periods
    try:
        build_features(cfg, _Tighter(policy, rows_per_write=100_000), force=True)
        assert summary() == one_pass
    finally:
        build_features(cfg, policy, force=True)


# --------------------------------------------------------------------------
# Chunked bundle assembly
# --------------------------------------------------------------------------


def test_chunking_does_not_change_a_single_bundle(
    con, cfg, policy: DetectorPolicy, l1, l2, l3, ml_scores, l4
):
    """The chunk size is a memory decision, and must be nothing else."""
    chunked = run_fusion(
        con, cfg,
        _with_runtime(policy, {**policy.runtime, "fusion": {"bundle_chunk_alerts": 7}}),
        l1_hits=l1.hits, l2_hits=l2.hits, l3_hits=l3.hits, ml_scores=ml_scores,
    )
    assert [a.alert_id for a in chunked.alerts] == [a.alert_id for a in l4.alerts]
    assert [a.score for a in chunked.alerts] == [a.score for a in l4.alerts]
    assert [_comparable(a.evidence_json) for a in chunked.alerts] == [
        _comparable(a.evidence_json) for a in l4.alerts
    ]


# --------------------------------------------------------------------------
# The runtime profile
# --------------------------------------------------------------------------


def test_peak_memory_samples_the_process():
    with PeakMemory(0.01) as peak:
        ballast = np.zeros(20_000_000, dtype=np.float64)  # 160 MB
        assert ballast.sum() == 0
    assert peak.peak_gb is None or peak.peak_gb > 0.1


def test_profile_records_a_cold_run_even_when_a_stage_was_reused(cfg, tmp_path):
    """A reused stage is recorded under its own name with the time it cost the
    day it really ran, and named in `cached`. That is what makes the total the
    cost of a cold run without rebuilding twenty-four million rows to measure
    work that has not changed -- and what stops anybody reading the wall clock
    of a cached run as the cost of the batch."""
    runs = tmp_path / "runs"
    runs.mkdir()
    local = replace(cfg, runs_root=runs)
    result = RunResult(run_id=local.run_id, scale=local.scale, seconds=12.5)
    result.stage_seconds = {"features (cached)": 99.0, "l1": 3.0}
    record_profile(local, result, peak_rss_gb=1.25)

    profiles = load_profiles(runs)
    assert set(profiles) == {local.scale}
    entry = profiles[local.scale]
    assert entry["stages"] == {"features": 99.0, "l1": 3.0}
    assert entry["cached"] == ["features"]
    assert entry["stage_seconds_total"] == 102.0
    assert entry["seconds"] == 12.5
    assert entry["peak_rss_gb"] == 1.25
    assert entry["policy_digest"] == local.policy_digest
    assert entry["lake_generated_at"] == local.manifest.get("generated_at")
