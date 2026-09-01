"""`agg_alerts_by_site_month`: the map's frames, and what they must add up to.

The map is the one screen that shows a number nobody can check by opening an
alert, so the arithmetic behind it is asserted here rather than trusted: a
site-month total is exactly the codes underneath it, a year of frames adds up
to the recovery figure rather than a multiple of it, the rate is a rate, and a
dismissed finding is on nobody's map.

The window expansion is tested separately because it is the one piece of
arithmetic with a trap in it: `period` is `YYYYMM`, so counting months by
subtracting two of them makes December to January eighty-nine months long.
"""

from __future__ import annotations

import polars as pl
import pytest
from detector.aggregate import AGG_SCHEMA, TOTAL, build, read
from detector.run import write_alerts

pytestmark = pytest.mark.usefixtures("features")


@pytest.fixture(scope="module")
def aggregated(con, cfg, policy, l4):
    """One aggregation of the real 10k queue, written where a run would."""
    write_alerts(cfg, l4)
    result = build(con, cfg, policy)
    return result, read(cfg)


def test_the_shape_is_the_contract(aggregated):
    _result, frame = aggregated
    assert list(frame.columns) == list(AGG_SCHEMA)
    assert frame.height > 0


def test_one_total_row_per_site_month(aggregated):
    _result, frame = aggregated
    totals = frame.filter(pl.col("anomaly_code") == TOTAL)
    assert totals.height == totals.select(["period", "site_id"]).n_unique()
    assert totals.height == frame.select(["period", "site_id"]).n_unique()


def test_a_total_is_the_sum_of_its_codes(aggregated):
    _result, frame = aggregated
    parts = (
        frame.filter(pl.col("anomaly_code") != TOTAL)
        .group_by(["period", "site_id"])
        .agg(pl.col("alert_count").sum().alias("parts"))
    )
    joined = frame.filter(pl.col("anomaly_code") == TOTAL).join(
        parts, on=["period", "site_id"], how="left"
    )
    assert (joined["alert_count"] == joined["parts"]).all()


def test_a_frame_covers_the_whole_window(aggregated, cfg):
    _result, frame = aggregated
    assert sorted(frame["period"].unique().to_list()) == cfg.period_list


def test_exposure_is_conserved(aggregated, l4):
    """A year of frames adds up to the recovery figure, not a multiple of it."""
    _result, frame = aggregated
    totals = frame.filter(pl.col("anomaly_code") == TOTAL)
    queue = sum(
        a.financial_impact_cumulative for a in l4.alerts if not a.suppressed
    )
    assert totals["financial_exposure_cumulative"].sum() == pytest.approx(
        queue, abs=1.0
    )
    live_monthly = sum(
        a.financial_impact_monthly for a in l4.alerts if not a.suppressed
    )
    assert totals["financial_exposure_monthly"].sum() == pytest.approx(
        live_monthly, abs=1.0
    )


def test_the_metric_is_a_rate(aggregated):
    """Alerts per 1,000 employees, never a count: Eastern Province headcount
    would otherwise turn every heat map into a population map."""
    _result, frame = aggregated
    assert (frame["headcount"] > 0).all()
    rate = frame["alert_count"] * 1000.0 / frame["headcount"]
    assert ((rate - frame["alerts_per_1000"]).abs() < 0.001).all()


def test_every_site_can_be_drawn(aggregated):
    _result, frame = aggregated
    assert frame["latitude"].null_count() == 0
    assert frame["longitude"].null_count() == 0
    assert (frame["region_code"].str.len_chars() > 0).all()


def test_top_codes_ride_on_the_total_row_only(aggregated, policy):
    _result, frame = aggregated
    totals = frame.filter(pl.col("anomaly_code") == TOTAL)
    assert totals["top_codes"].null_count() == 0
    assert totals["top_codes"].list.len().max() <= policy.aggregate_top_codes
    codes = frame.filter(pl.col("anomaly_code") != TOTAL)
    assert codes["top_codes"].null_count() == codes.height


def test_a_dismissed_finding_is_on_nobody_map(con, cfg, policy, l4, tmp_path):
    """Suppression hides an alert from the map without deleting it from the
    queue -- the row stays in `alerts.parquet` and the frame does not count it."""
    everything = build(con, cfg, policy)
    before = read(cfg).filter(pl.col("anomaly_code") == TOTAL)["alert_count"].sum()

    hidden = l4.alerts[0]
    queue = pl.read_parquet(cfg.run_dir / "alerts.parquet")
    dismissed = queue.with_columns(
        suppressed=pl.when(pl.col("alert_id") == hidden.alert_id)
        .then(True)
        .otherwise(pl.col("suppressed"))
    )
    path = tmp_path / "alerts.parquet"
    dismissed.write_parquet(path)

    without = build(con, cfg, policy, alerts_path=path)
    after = read(cfg).filter(pl.col("anomaly_code") == TOTAL)["alert_count"].sum()
    months = (
        ((hidden.period_to // 100) * 12 + hidden.period_to % 100)
        - ((hidden.period_from // 100) * 12 + hidden.period_from % 100)
        + 1
    )
    assert without.alerts_in == everything.alerts_in - 1
    assert after == before - months
    # Hidden, not deleted: the queue still carries every row it carried.
    assert dismissed.height == queue.height
    # Put the run directory back the way the other tests expect to find it.
    build(con, cfg, policy)


def test_a_window_across_a_year_end_is_counted_in_months(con, cfg, policy, tmp_path):
    """December to February is three months, not ninety-one: `period` is
    `YYYYMM`, and subtracting two of them is not month arithmetic."""
    frame = pl.read_parquet(cfg.run_dir / "alerts.parquet")
    row = frame.filter(pl.col("period_from") <= 202412).head(1)
    if row.height == 0:  # pragma: no cover - the 10k lake always has one
        pytest.skip("no alert spanning a year end in this run")
    one = row.with_columns(
        period_from=pl.lit(202412, pl.Int32),
        period_to=pl.lit(202502, pl.Int32),
        months_flagged=pl.lit(3, pl.Int32),
        suppressed=pl.lit(False),
    )
    path = tmp_path / "one.parquet"
    one.write_parquet(path)
    build(con, cfg, policy, alerts_path=path)
    frames = read(cfg).filter(pl.col("anomaly_code") == TOTAL)
    assert sorted(frames["period"].to_list()) == [202412, 202501, 202502]
    build(con, cfg, policy)
