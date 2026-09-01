"""The feature build: `data/raw/` to `data/features/`, in one DuckDB step.

All SQL, no Python row loops (docs/specs/detector.md). The queries live one file
per feature block in `sql/`; what this module adds is the parts that have to be
*generated* from the policy pack -- the expected-amount recomputation, the wide
allowance pivot, the education ordinal, the cohort ladder -- because those are
policy, and a policy pack with a new allowance code must not need a code change.

Targets: under 60 seconds at 10k, under 10 minutes at 1m x 24 on 24 cores.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

import duckdb

from ..config import ROW_GROUP_ROWS, DetectorConfig
from ..lake import connect
from ..policy import AMOUNT_TOLERANCE_SAR, DetectorPolicy

SQL_DIR = Path(__file__).parent / "sql"

# Intermediates at employee x period grain, written to Parquet as they are
# computed and read back as views rather than held as temp tables (phase 7).
# The value is the feature table the intermediate *is*, or None for one that is
# internal to the build.
STREAMED: dict[str, str | None] = {
    "asat": None,
    "allowance_features": "features_allowance",
    "allowance_pivot": None,
    "period_features": "features_period",
}

# Where the internal ones live while the build runs. Removed at the end: it is
# a spill, not an output.
SCRATCH = "_intermediate"

# Written to data/features/scale=<n>/. `period`-partitioned where the table is
# per-month; single-part where it is per-employee.
PARTITIONED = {"features_period": "period", "features_allowance": "period"}

OUTPUTS: tuple[tuple[str, str], ...] = (
    ("features_period", "period_features"),
    ("features_allowance", "allowance_features"),
    ("features_employee", "employee_features"),
    ("allowance_history", "allowance_rollups"),
    ("cohort_stats", "cohort_stats"),
)

# The blocks, in dependency order. Renaming one is a schema change.
BLOCKS: tuple[str, ...] = (
    "00_asat.sql",
    "01_allowance.sql",
    "02_rule_inputs.sql",
    "03_rollups.sql",
    "04_graph.sql",
    "05_employee_statics.sql",
    "06_cohort_stats.sql",
)


@dataclass
class FeatureBuild:
    """What one feature build produced, for the manifest and the runtime profile."""

    seconds: float
    block_seconds: dict[str, float] = field(default_factory=dict)
    row_counts: dict[str, int] = field(default_factory=dict)
    columns: dict[str, int] = field(default_factory=dict)
    cache_key: str = ""
    cached: bool = False


# --------------------------------------------------------------------------
# Generated SQL -- the parts that come from the policy pack, not from a file
# --------------------------------------------------------------------------


def _pivot_columns(policy: DetectorPolicy) -> str:
    """One `sum(amount) FILTER (...)` per allowance code, in a fixed order."""
    return "".join(
        f"sum(amount) FILTER (WHERE allowance_code = '{code}')"
        f" AS allowance_{code}_amount,\n       "
        for code in policy.allowance_codes
    )


def _allowance_columns(policy: DetectorPolicy) -> str:
    """The same columns again on the wide table, with a paid-nothing default."""
    return "".join(
        f"coalesce(al.allowance_{code}_amount, 0) AS allowance_{code}_amount,\n    "
        for code in policy.allowance_codes
    )


def _cohort_levels(policy: DetectorPolicy) -> str:
    """The five ladder levels from `policy/fusion.yaml` as one UNION ALL."""
    blocks: list[str] = []
    for level, keys in enumerate(policy.cohort_ladder, start=1):
        key_expr = " || '|' || ".join(f"'{k}=' || {k}" for k in keys)
        group = ", ".join(keys)
        blocks.append(
            f"""SELECT {level} AS cohort_level,
       '{"|".join(keys)}' AS cohort_dims,
       k.cohort_key,
       k.metric,
       m.n,
       m.median_value,
       median(abs(k.value - m.median_value)) AS mad,
       m.p01,
       m.p99
FROM (SELECT {key_expr} AS cohort_key, metric, value
      FROM cohort_input WHERE value IS NOT NULL) k
JOIN (SELECT {key_expr} AS cohort_key, metric, count(*) AS n,
             median(value) AS median_value,
             quantile_cont(value, 0.01) AS p01,
             quantile_cont(value, 0.99) AS p99
      FROM cohort_input WHERE value IS NOT NULL
      GROUP BY {group}, metric) m
  USING (cohort_key, metric)
GROUP BY 1, 2, 3, 4, 5, 6, 8, 9"""
        )
    return "\nUNION ALL\n".join(blocks)


def windows(cfg: DetectorConfig, rows_per_write: int) -> list[list[int]]:
    """The window split into groups of months, each about `rows_per_write` rows.

    One group at 10k and 100k -- the whole window, one statement, exactly what
    phases 3 to 6 measured -- and twelve groups of two months at 1m, where a
    single statement cannot hold the result. The size is in *rows* rather than
    months so the same dial means the same thing at every tier.
    """
    per_month = max(cfg.employees, 1)
    months = max(1, int(rows_per_write) // per_month)
    periods = cfg.period_list
    return [periods[i : i + months] for i in range(0, len(periods), months)]


def substitutions(
    policy: DetectorPolicy, periods: list[int] | None = None
) -> dict[str, str]:
    """Every `$placeholder` the SQL blocks expect, resolved from the policy pack."""
    return {
        # Empty for a whole-window build; one month when the build is writing a
        # month at a time. Every period-grained input carries it, so a monthly
        # write reads one partition of each rather than all twenty-four.
        "period_filter": (
            ""
            if not periods
            else "AND period IN (" + ", ".join(str(int(p)) for p in periods) + ")"
        ),
        "expected_case": policy.expected_amount_case(),
        "tolerance": f"{AMOUNT_TOLERANCE_SAR:.2f}",
        "pivot_columns": _pivot_columns(policy),
        "allowance_columns": _allowance_columns(policy),
        "education_rank": policy.education_rank_sql("e.education_level"),
        "job_education_rank": policy.education_rank_sql("j.min_education"),
        "gosi_expected": policy.gosi_class_sql("e.nationality_class"),
        "acting_max_months": str(policy.duration_limit("ACTING_ROLE")),
        "relocation_max_months": str(policy.duration_limit("RELOCATION")),
        "cohort_levels": _cohort_levels(policy),
    }


def render(
    block: str, policy: DetectorPolicy, periods: list[int] | None = None
) -> str:
    """One SQL block with its policy-derived fragments substituted in."""
    text = (SQL_DIR / block).read_text(encoding="utf-8")
    rendered = Template(text).safe_substitute(substitutions(policy, periods))
    if "$" in rendered.replace("$$", ""):
        leftover = [line for line in rendered.splitlines() if "$" in line]
        raise ValueError(f"{block}: unresolved placeholder in {leftover[:3]}")
    return rendered


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


def cache_key(cfg: DetectorConfig, policy: DetectorPolicy) -> str:
    """Stage + input digest + policy digest (docs/specs/detector.md, CLI section).

    The SQL sources are part of the key: editing a feature query must invalidate
    the store, or the next run silently scores against the previous shape.
    """
    import hashlib

    parts = [
        "features",
        cfg.scale,
        str(cfg.manifest.get("generated_at", "")),
        str(cfg.manifest.get("seed", "")),
        json.dumps(policy.digest, sort_keys=True),
    ]
    for block in BLOCKS:
        parts.append((SQL_DIR / block).read_text(encoding="utf-8"))
    return "sha256:" + hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def is_current(cfg: DetectorConfig, policy: DetectorPolicy) -> bool:
    """True when the feature store on disk was built from exactly these inputs."""
    if not cfg.features_manifest.exists():
        return False
    try:
        stored = json.loads(cfg.features_manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if stored.get("cache_key") != cache_key(cfg, policy):
        return False
    return all(cfg.feature_dir(name).exists() for name, _ in OUTPUTS)


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


def statements(sql: str) -> list[str]:
    """One rendered block split into its statements.

    Boundaries are semicolons in *code*, not in comments: the blocks explain
    themselves at length, and "a rule predicate is a statement of policy;
    arithmetic inside one is a feature that was not built" is a sentence, not
    two statements.
    """
    out: list[str] = []
    current: list[str] = []
    for line in sql.splitlines():
        code = line.split("--", 1)[0]
        if ";" in code:
            cut = line.rindex(";", 0, len(code)) + 1
            current.append(line[:cut])
            out.append("\n".join(current))
            current = [line[cut:]] if line[cut:].strip() else []
        else:
            current.append(line)
    if any(part.strip() for part in current):
        out.append("\n".join(current))
    return [part.strip() for part in out if part.strip()]


def split_create(statement: str) -> tuple[str | None, str]:
    """`CREATE OR REPLACE TEMP TABLE x AS <query>` to `("x", "<query>")`."""
    match = re.search(
        r"CREATE\s+OR\s+REPLACE\s+TEMP\s+TABLE\s+(\w+)\s+AS\s",
        statement,
        re.IGNORECASE,
    )
    if not match:
        return None, statement
    return match.group(1), statement[match.end():].strip().rstrip(";")


def _query_of(sql: str, name: str) -> str:
    """The query behind one `CREATE OR REPLACE TEMP TABLE <name> AS` statement."""
    for statement in statements(sql):
        found, query = split_create(statement)
        if found == name:
            return query
    raise KeyError(f"no statement creating {name!r} in this block")


def stream(
    con: duckdb.DuckDBPyConnection,
    cfg: DetectorConfig,
    policy: DetectorPolicy,
    block: str,
    name: str,
) -> None:
    """Materialise one employee x period intermediate to Parquet, not to memory.

    At 1m these are twenty-four million rows and up to a hundred and sixty-five
    columns; DuckDB cannot pin that many blocks and the build dies with an
    out-of-memory error even though every join in it is streamable.  Written
    straight out of the query pipeline and read back as a view, the peak is the
    pipeline rather than the result, and every later block reads only the
    columns it asks for instead of scanning a wide table in RAM.

    Two of the four are the feature tables themselves, so this *is* their write
    -- there is no second copy. The other two are internal, and go to a scratch
    directory the build removes when it finishes.
    """
    output = STREAMED[name]
    target = cfg.feature_dir(output) if output else cfg.features / SCRATCH / name
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    path = str(target).replace("\\", "/")
    # A few months per statement rather than the whole window. The partitioned
    # writer keeps a buffer per thread per partition -- on a 32-thread machine,
    # twenty-four months of a 165-column table is 768 open buffers, and at 1m
    # it runs out of memory before it writes anything. Two months at a time it
    # holds 64, and the filter prunes every input to those months' partitions.
    # At 10k and 100k the whole window is one group, so this is one statement
    # and exactly the plan phases 3 to 6 ran.
    groups = windows(cfg, policy.rows_per_write)
    for group in groups:
        query = _query_of(render(block, policy, group), name)
        # `APPEND` because each group writes different months into the same
        # directory, and DuckDB then insists the file name be unique per write.
        # A single-group build keeps the numbered names phases 3 to 6 wrote.
        pattern = "part-{uuid}" if len(groups) > 1 else "part-{i}"
        con.execute(
            f"COPY (SELECT *, period AS period_part FROM ({query})) TO '{path}' "
            f"(FORMAT PARQUET, PARTITION_BY (period_part), "
            + ("APPEND, " if len(groups) > 1 else "")
            + f"ROW_GROUP_SIZE {ROW_GROUP_ROWS}, "
            + f"FILENAME_PATTERN '{pattern}')"
        )
    con.execute(
        f"CREATE OR REPLACE TEMP VIEW {name} AS SELECT * FROM "
        f"read_parquet('{path}/**/*.parquet', hive_partitioning=false)"
    )


def _write(
    con: duckdb.DuckDBPyConnection, cfg: DetectorConfig, name: str, source: str
) -> None:
    """Materialise one temp table to Parquet, replacing whatever was there."""
    target = cfg.feature_dir(name)
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    path = str(target).replace("\\", "/")
    partition = PARTITIONED.get(name)
    if partition:
        # The partition column is dropped from the file by DuckDB, so it is
        # written twice: once as the directory key, once as a real column, and
        # the reader never has to guess a Hive type back.
        con.execute(
            f"COPY (SELECT *, {partition} AS {partition}_part FROM {source}) "
            f"TO '{path}' (FORMAT PARQUET, PARTITION_BY ({partition}_part), "
            f"ROW_GROUP_SIZE {ROW_GROUP_ROWS}, FILENAME_PATTERN 'part-" + "{i}')"
        )
    else:
        target.mkdir(parents=True, exist_ok=True)
        con.execute(
            f"COPY (SELECT * FROM {source}) TO '{path}/part-0000.parquet' "
            f"(FORMAT PARQUET, ROW_GROUP_SIZE {ROW_GROUP_ROWS})"
        )


def build(
    cfg: DetectorConfig,
    policy: DetectorPolicy,
    *,
    force: bool = False,
    threads: int | None = None,
    log=None,
) -> FeatureBuild:
    """Build the feature store. Returns the runtime profile the eval report shows."""
    policy.require_digest(cfg.manifest)
    key = cache_key(cfg, policy)
    if not force and is_current(cfg, policy):
        stored = json.loads(cfg.features_manifest.read_text(encoding="utf-8"))
        return FeatureBuild(
            seconds=float(stored.get("seconds", 0.0)),
            block_seconds=dict(stored.get("block_seconds") or {}),
            row_counts={k: int(v) for k, v in (stored.get("row_counts") or {}).items()},
            columns={k: int(v) for k, v in (stored.get("columns") or {}).items()},
            cache_key=key,
            cached=True,
        )

    started = time.perf_counter()
    result = FeatureBuild(seconds=0.0, cache_key=key)
    con = connect(cfg, threads=threads, policy=policy)
    # Nothing downstream reads the feature store in file order -- every layer
    # orders what it needs explicitly, and the evidence fingerprint sorts its
    # findings before hashing them. Letting DuckDB write 24m rows in whatever
    # order the threads finish saves a quarter of the write time and a copy of
    # the table (phase 7).
    con.execute("SET preserve_insertion_order = false")
    try:
        for block in BLOCKS:
            block_started = time.perf_counter()
            for statement in statements(render(block, policy)):
                name, _query = split_create(statement)
                if name in STREAMED:
                    stream(con, cfg, policy, block, name)
                else:
                    con.execute(statement)
            elapsed = time.perf_counter() - block_started
            result.block_seconds[block] = round(elapsed, 3)
            if log:
                log(f"  {block:<24} {elapsed:6.2f}s")
        for name, source in OUTPUTS:
            if STREAMED.get(source) != name:
                _write(con, cfg, name, source)
            row = con.execute(f"SELECT count(*) FROM {source}").fetchone()
            result.row_counts[name] = int(row[0]) if row else 0
            result.columns[name] = len(
                con.execute(f"SELECT * FROM {source} LIMIT 0").description or []
            )
    finally:
        con.close()
        scratch = cfg.features / SCRATCH
        if scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)

    result.seconds = round(time.perf_counter() - started, 3)
    cfg.features_manifest.parent.mkdir(parents=True, exist_ok=True)
    cfg.features_manifest.write_text(
        json.dumps(
            {
                "scale": cfg.scale,
                "cache_key": key,
                "seconds": result.seconds,
                "block_seconds": result.block_seconds,
                "row_counts": result.row_counts,
                "columns": result.columns,
                "policy_digest": policy.digest,
                "lake_generated_at": cfg.manifest.get("generated_at"),
                "period_from": cfg.period_from,
                "period_to": cfg.period_to,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return result


def feature_columns(cfg: DetectorConfig, table: str = "features_period") -> list[str]:
    """The column names of a feature table, for rule validation and the gate."""
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT * FROM read_parquet('{cfg.feature_glob(table)}',"
            " hive_partitioning=false) LIMIT 0"
        ).description
        return [name for name, *_ in (rows or [])]
    finally:
        con.close()
