"""`agg_alerts_by_site_month` -- the pre-aggregated map payload (phase 7).

`docs/API_CONTRACT.md` serves `/analytics/geo` *entirely* from this table: one
small frame per month, no aggregation on request, which is what makes a
24-frame animation over 180 sites smooth.  Computing it here rather than in the
API is the whole point -- at 1m the alternative is scanning 35,000 alerts and
24 million payroll rows every time somebody drags the scrubber.

Three decisions carry the shape:

* **The grain is (period, site, anomaly code)**, because the endpoint filters
  by `family` and `anomaly_code` and a site-month total cannot answer those.
  The site-month total a default frame needs is stored too, as the row whose
  `anomaly_code` is `'*'`; it carries the severity mix and the three worst
  codes, so the default frame is a filter rather than a group-by.
* **An alert counts in every month of its window**, at the site the employee
  was at *that* month.  A twelve-month overpayment is a live finding in each of
  those twelve frames, and an employee who moved sites mid-window takes their
  exposure with them -- otherwise the map shows the anomaly where the person
  ended up rather than where the money went.
* **The denominator is the site's headcount that month**, so the default metric
  is alerts per 1,000 employees (CLAUDE.md).  Eastern Province headcount
  dominance would otherwise turn every heat map into a population map.

Suppressed alerts are excluded from the counts: a dismissed finding is not on
anybody's map.  They stay in `alerts.parquet` -- suppression hides, it never
deletes -- but a frame is a picture of what is open.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import polars as pl

from .config import ROW_GROUP_ROWS, DetectorConfig

AGG_FILE = "agg_alerts_by_site_month.parquet"

# The row that carries a site-month's totals rather than one code's. Kept as a
# value rather than a separate table so a frame is one filtered scan.
TOTAL = "*"

AGG_SCHEMA = {
    "period": pl.Int32,
    "site_id": pl.Utf8,
    "site_name_en": pl.Utf8,
    "site_class": pl.Utf8,
    "region_code": pl.Utf8,
    "latitude": pl.Float64,
    "longitude": pl.Float64,
    "anomaly_code": pl.Utf8,
    "family": pl.Utf8,
    "headcount": pl.Int32,
    "alert_count": pl.Int32,
    "employee_count": pl.Int32,
    "critical_count": pl.Int32,
    "high_count": pl.Int32,
    "medium_count": pl.Int32,
    "watchlist_count": pl.Int32,
    "alerts_per_1000": pl.Float64,
    "financial_exposure_monthly": pl.Float64,
    "financial_exposure_cumulative": pl.Float64,
    "top_codes": pl.List(pl.Utf8),
}


@dataclass
class AggregateResult:
    """One aggregation pass, for the runtime profile and the phase gate."""

    rows: int = 0
    periods: int = 0
    sites: int = 0
    total_rows: int = 0
    alerts_in: int = 0
    seconds: float = 0.0
    path: Path | None = None
    by_period: dict[int, int] = field(default_factory=dict)


def _months_sql(alerts_source: str, window: tuple[int, int]) -> str:
    """One row per alert per month of its window, placed at that month's site.

    `generate_series` expands the window in SQL rather than in Python: at 1m
    this is tens of thousands of alerts times up to twenty-four months, and a
    Python loop over that is the kind of row-at-a-time work the feature build
    exists not to do.
    """
    first, last = window
    return f"""
    WITH live AS (
        SELECT alert_id, employee_id, anomaly_code, family, severity,
               -- Clamped to the run's own window. A finding can be dated from
               -- before the first month the lake carries -- an allowance that
               -- was already running -- and a frame outside the window has no
               -- headcount to be a rate against and no month to be drawn in.
               greatest(period_from, {first}) AS period_from,
               least(period_to, {last})       AS period_to,
               financial_impact_monthly, financial_impact_cumulative,
               -- Months in the window, counted in months rather than by
               -- subtracting two YYYYMM integers: 202501 - 202412 is 89.
               -- `//` and not `/`: DuckDB's `/` is float division, and a float
               -- YYYYMM silently turns the arithmetic below into nonsense.
               ((least(period_to, {last}) // 100) * 12
                 + (least(period_to, {last}) % 100))
                 - ((greatest(period_from, {first}) // 100) * 12
                    + (greatest(period_from, {first}) % 100))
                 + 1 AS window_months
        FROM {alerts_source}
        WHERE NOT suppressed
    ),
    months AS (
        SELECT l.alert_id, l.employee_id, l.anomaly_code, l.family, l.severity,
               CAST(
                   ((((l.period_from // 100) * 12 + (l.period_from % 100) - 1 + p.k)
                     // 12) * 100)
                   + ((((l.period_from // 100) * 12 + (l.period_from % 100) - 1 + p.k)
                       % 12) + 1)
                   AS INTEGER
               ) AS period,
               -- Cumulative exposure spread evenly over the months it was spent
               -- in, so summing a year of frames gives the recovery figure back
               -- rather than a multiple of it.
               l.financial_impact_cumulative
                   / greatest(l.window_months, 1) AS exposure_cumulative,
               l.period_to,
               l.financial_impact_monthly
        FROM live l
        CROSS JOIN LATERAL (
            SELECT unnest(generate_series(0, CAST(l.window_months - 1 AS BIGINT)))
                   AS k
        ) p
    )
    SELECT m.alert_id, m.employee_id, m.anomaly_code, m.family, m.severity,
           m.period, m.exposure_cumulative,
           -- Monthly exposure is what is still going out, so it belongs to the
           -- last month of the window and to no other frame.
           CASE WHEN m.period = m.period_to THEN m.financial_impact_monthly
                ELSE 0 END AS exposure_monthly,
           -- The site the employee was at that month, falling back to where
           -- they are now. A handful of windows reach a month before the
           -- employee's first feature row, and an alert-month that joined to
           -- nothing would quietly leave the map: the total across the frames
           -- would no longer be the queue, which is the one property this
           -- table has to have.
           coalesce(f.asat_site_id, e.work_site_id)  AS site_id,
           coalesce(f.region_code, e.region_code)    AS region_code
    FROM months m
    JOIN features_employee e ON e.employee_id = m.employee_id
    LEFT JOIN features_period f
      ON f.employee_id = m.employee_id AND f.period = m.period
    """


def _aggregate_sql(
    alerts_source: str, top_codes: int, window: tuple[int, int]
) -> str:
    """The whole table in one statement: per-code rows, then the totals row."""
    return f"""
    WITH alert_month AS ({_months_sql(alerts_source, window)}),
    headcount AS (
        SELECT period, asat_site_id AS site_id, count(*) AS headcount
        FROM features_period
        GROUP BY 1, 2
    ),
    by_code AS (
        SELECT period, site_id, anomaly_code, any_value(family) AS family,
               count(*) AS alert_count,
               count(DISTINCT employee_id) AS employee_count,
               count(*) FILTER (WHERE severity = 'CRITICAL') AS critical_count,
               count(*) FILTER (WHERE severity = 'HIGH') AS high_count,
               count(*) FILTER (WHERE severity = 'MEDIUM') AS medium_count,
               count(*) FILTER (WHERE severity = 'WATCHLIST') AS watchlist_count,
               sum(exposure_monthly) AS financial_exposure_monthly,
               sum(exposure_cumulative) AS financial_exposure_cumulative
        FROM alert_month
        GROUP BY 1, 2, 3
    ),
    totals AS (
        SELECT period, site_id, '{TOTAL}' AS anomaly_code, '{TOTAL}' AS family,
               sum(alert_count) AS alert_count,
               sum(employee_count) AS employee_count,
               sum(critical_count) AS critical_count,
               sum(high_count) AS high_count,
               sum(medium_count) AS medium_count,
               sum(watchlist_count) AS watchlist_count,
               sum(financial_exposure_monthly) AS financial_exposure_monthly,
               sum(financial_exposure_cumulative) AS financial_exposure_cumulative,
               -- The codes a reviewer sees in the tooltip, worst first.
               list(anomaly_code ORDER BY alert_count DESC, anomaly_code)
                   [1:{top_codes}] AS top_codes
        FROM by_code
        GROUP BY 1, 2
    ),
    every_row AS (
        SELECT period, site_id, anomaly_code, family, alert_count,
               employee_count, critical_count, high_count, medium_count,
               watchlist_count, financial_exposure_monthly,
               financial_exposure_cumulative, CAST(NULL AS VARCHAR[]) AS top_codes
        FROM by_code
        UNION ALL
        SELECT period, site_id, anomaly_code, family, alert_count,
               employee_count, critical_count, high_count, medium_count,
               watchlist_count, financial_exposure_monthly,
               financial_exposure_cumulative, top_codes
        FROM totals
    )
    SELECT CAST(r.period AS INTEGER)                       AS period,
           r.site_id,
           s.site_name_en,
           s.site_class,
           coalesce(s.region_code, '')                     AS region_code,
           s.latitude,
           s.longitude,
           r.anomaly_code,
           r.family,
           CAST(coalesce(h.headcount, 0) AS INTEGER)       AS headcount,
           CAST(r.alert_count AS INTEGER)                  AS alert_count,
           CAST(r.employee_count AS INTEGER)               AS employee_count,
           CAST(r.critical_count AS INTEGER)               AS critical_count,
           CAST(r.high_count AS INTEGER)                   AS high_count,
           CAST(r.medium_count AS INTEGER)                 AS medium_count,
           CAST(r.watchlist_count AS INTEGER)              AS watchlist_count,
           -- The map's default metric. Zero headcount cannot happen for a site
           -- with an alert on it, but a rate is never divided by a guess.
           CASE WHEN coalesce(h.headcount, 0) > 0
                THEN round(r.alert_count * 1000.0 / h.headcount, 4)
                ELSE 0.0 END                               AS alerts_per_1000,
           round(coalesce(r.financial_exposure_monthly, 0), 2)
                                                           AS financial_exposure_monthly,
           round(coalesce(r.financial_exposure_cumulative, 0), 2)
                                                    AS financial_exposure_cumulative,
           r.top_codes
    FROM every_row r
    LEFT JOIN dim_site s ON s.site_id = r.site_id
    LEFT JOIN headcount h ON h.period = r.period AND h.site_id = r.site_id
    ORDER BY period, site_id, (anomaly_code = '{TOTAL}') DESC, anomaly_code
    """


def build(
    con: duckdb.DuckDBPyConnection,
    cfg: DetectorConfig,
    policy,
    *,
    alerts_path: Path | str | None = None,
    log=None,
) -> AggregateResult:
    """Aggregate one run's queue into the map's monthly frames.

    Reads `alerts.parquet` rather than the in-memory queue: the aggregate is a
    statement about what was written, and phase 8 upserts exactly this file.
    """
    started = time.perf_counter()
    path = Path(alerts_path) if alerts_path else cfg.run_dir / "alerts.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"no alerts to aggregate at {path}; run the fusion stage first"
        )
    source = f"read_parquet('{str(path).replace(chr(92), '/')}')"
    frame = pl.from_arrow(
        con.execute(
            _aggregate_sql(
                source,
                policy.aggregate_top_codes,
                (cfg.period_from, cfg.period_to),
            )
        ).arrow()
    )
    frame = frame.select(
        [pl.col(name).cast(dtype) for name, dtype in AGG_SCHEMA.items()]
    )

    target = cfg.run_dir / AGG_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(target, row_group_size=ROW_GROUP_ROWS)

    totals = frame.filter(pl.col("anomaly_code") == TOTAL)
    result = AggregateResult(
        rows=frame.height,
        periods=frame["period"].n_unique(),
        sites=frame["site_id"].n_unique(),
        total_rows=totals.height,
        alerts_in=int(
            con.execute(
                f"SELECT count(*) FROM {source} WHERE NOT suppressed"
            ).fetchone()[0]
        ),
        seconds=round(time.perf_counter() - started, 3),
        path=target,
        by_period={
            int(period): int(count)
            for period, count in zip(
                totals["period"].unique(maintain_order=True),
                totals.group_by("period", maintain_order=True)
                .agg(pl.col("alert_count").sum())["alert_count"],
            )
        },
    )
    if log:
        log(
            f"  aggregate {result.rows:,} rows over {result.sites} sites and "
            f"{result.periods} months  {result.seconds:.2f}s"
        )
    return result


def read(cfg: DetectorConfig) -> pl.DataFrame:
    """The aggregate as written. Small by construction -- sites times months."""
    return pl.read_parquet(cfg.run_dir / AGG_FILE)
