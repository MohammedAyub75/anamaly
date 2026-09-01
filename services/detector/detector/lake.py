"""DuckDB access to the Parquet lake and the feature store.

The ground-truth tables are kept out of the default connection on purpose.
`docs/ANOMALY_CATALOG.md` states that `labels_anomaly` is never a detector
input, and the cheapest way to keep a promise like that is to make it
structurally impossible: `connect()` -- the connection every feature build,
rule and layer uses -- simply has no view named `labels_anomaly`, so a query
that reaches for it fails with a binder error instead of silently scoring 100%.
Only `connect_labels()`, which lives behind `detector.eval`, can see them.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import duckdb

from .config import DetectorConfig

# Everything the detector may read. Deliberately not derived from the lake
# directory listing: a new table appearing there must be an explicit decision.
RAW_TABLES: tuple[str, ...] = (
    "dim_region",
    "dim_site",
    "dim_calendar",
    "dim_grade",
    "dim_allowance",
    "dim_job",
    "dim_org_unit",
    "employee_master",
    "fact_assignment_history",
    "fact_bank_account",
    "fact_payroll_monthly",
    "fact_payroll_allowance",
    "fact_attendance_monthly",
    "fact_system_activity_monthly",
)

# Ground truth. Readable by the evaluation harness and by nothing else.
LABEL_TABLES: tuple[str, ...] = ("labels_anomaly", "labels_confounder")

# Feature-store tables, written by `detector.features.build`.
FEATURE_TABLES: tuple[str, ...] = (
    "features_employee",
    "features_period",
    "features_allowance",
    "allowance_history",
    "cohort_stats",
)


def _view(con: duckdb.DuckDBPyConnection, name: str, glob: str) -> None:
    con.execute(
        f"CREATE OR REPLACE VIEW {name} AS "
        f"SELECT * FROM read_parquet('{glob}', hive_partitioning=false)"
    )


def connect(
    cfg: DetectorConfig,
    *,
    features: bool = False,
    threads: int | None = None,
    memory_limit: str | None = None,
    policy=None,
) -> duckdb.DuckDBPyConnection:
    """A connection with one view per raw table, and no view over ground truth.

    `policy` supplies the engine budget from `policy/runtime.yaml` when the
    caller does not override it: a memory limit and a temp directory to spill
    into.  At 1m a hash join over 24 million payroll rows will not fit in RAM
    on a 16 GB machine, and an engine told its budget spills to disk and
    finishes, where an engine left to guess is killed by the OS.
    """
    con = duckdb.connect()
    if threads is None and policy is not None:
        threads = policy.duckdb_threads
    if memory_limit is None and policy is not None:
        memory_limit = policy.duckdb_memory_limit
    if threads is not None:
        con.execute(f"SET threads TO {int(threads)}")
    if memory_limit is not None:
        con.execute(f"SET memory_limit = '{memory_limit}'")
    if policy is not None and policy.duckdb_temp_directory:
        spill = Path(policy.duckdb_temp_directory)
        spill.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory = '{str(spill).replace(chr(92), '/')}'")
    for table in RAW_TABLES:
        _view(con, table, cfg.raw_glob(table))
    if features:
        attach_features(con, cfg)
    return con


def attach_features(
    con: duckdb.DuckDBPyConnection,
    cfg: DetectorConfig,
    tables: Iterable[str] = FEATURE_TABLES,
) -> None:
    """Add views over the feature store to an existing connection."""
    for table in tables:
        if cfg.feature_dir(table).exists():
            _view(con, table, cfg.feature_glob(table))


def connect_labels(cfg: DetectorConfig, *, policy=None) -> duckdb.DuckDBPyConnection:
    """The evaluation harness's connection: raw + features + ground truth.

    Importing this anywhere outside `detector.eval` is a bug, and the phase-3
    gate greps for exactly that.
    """
    con = connect(cfg, features=True, policy=policy)
    for table in LABEL_TABLES:
        _view(con, table, cfg.raw_glob(table))
    return con


def scalar(con: duckdb.DuckDBPyConnection, sql: str, default: int = 0) -> int:
    row = con.execute(sql).fetchone()
    return int(row[0]) if row and row[0] is not None else default


def table_rows(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return scalar(con, f"SELECT count(*) FROM {table}")
