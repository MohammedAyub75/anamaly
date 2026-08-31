"""Rewriting the lake with pass 2's edits, one Parquet part at a time.

Only parts that actually contain an edited row are rewritten, and each one is
read, patched and written back through the same `build_table` the generator
writes with -- so the schema gate compares the injected lake against exactly the
same contract as the clean one, and a mistyped edit fails the write rather than
retyping a column.

Memory stays bounded because a part is at most `CHUNK_ROWS` rows, which is the
same bound pass 1 generated under.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ..config import PERIOD_PARTITIONED, ScaleConfig
from ..schemas import SCHEMAS
from ..writer import build_table, money_cents, write_arrow
from .model import AllowanceRow, Edits


def _columns(table: pa.Table, name: str) -> Any:
    """One column in the form the edit layer works in: money as int64 halalas."""
    column = table.column(name)
    if pa.types.is_decimal(column.type):
        return money_cents(column)
    return column.to_pylist()


def _employee_chunks(cfg: ScaleConfig) -> dict[str, int]:
    """Which chunk each employee's rows live in, read back off the lake."""
    out: dict[str, int] = {}
    for path in sorted(cfg.table_dir("employee_master").glob("part-*.parquet")):
        chunk = int(path.stem.split("-")[1])
        for employee in pq.read_table(path, columns=["employee_id"]).column(0).to_pylist():
            out[employee] = chunk
    return out


def _rewrite(
    path: Path,
    table_name: str,
    drop: set[tuple],
    patch: dict[tuple, dict[str, Any]],
    append: list[dict[str, Any]],
    key_of,
) -> int:
    """Apply drops, cell patches and appended rows to one part. Returns its rows."""
    arrow = pq.read_table(path) if path.exists() else None
    columns: dict[str, Any] = {}
    schema = SCHEMAS[table_name]

    if arrow is not None and arrow.num_rows:
        keys = key_of(arrow)
        if drop:
            keep = pa.array([k not in drop for k in keys], type=pa.bool_())
            arrow = arrow.filter(keep)
            keys = key_of(arrow)
        edited = {name for values in patch.values() for name in values}
        positions = [i for i, key in enumerate(keys) if key in patch] if patch else []
        for field in schema:
            if field.name in edited and positions:
                values = _columns(arrow, field.name)
                for position in positions:
                    change = patch[keys[position]]
                    if field.name in change:
                        values[position] = change[field.name]
                columns[field.name] = (
                    np.asarray(values, dtype=np.int64)
                    if isinstance(values, np.ndarray) else values
                )
            else:
                columns[field.name] = arrow.column(field.name)
        arrow = build_table(table_name, columns)

    if append:
        extra = build_table(
            table_name,
            {field.name: [row[field.name] for row in append] for field in schema},
        )
        arrow = extra if arrow is None else pa.concat_tables([arrow, extra])

    if arrow is None:
        return 0
    write_arrow(arrow.combine_chunks(), path)
    return arrow.num_rows


def apply_edits(cfg: ScaleConfig, edits: Edits) -> dict[str, int]:
    """Write every edit into the lake. Returns the row count of each table touched."""
    chunk_of = _employee_chunks(cfg)
    counts: dict[str, int] = {}

    def key_employee(arrow: pa.Table) -> list:
        return arrow.column("employee_id").to_pylist()

    def key_employee_period(arrow: pa.Table) -> list:
        return list(zip(arrow.column("employee_id").to_pylist(),
                        arrow.column("period").to_pylist(), strict=True))

    # ---- employee_master: cell patches only -----------------------------
    _by_chunk_table(
        cfg, counts, "employee_master", chunk_of,
        patch={(e,): values for e, values in edits.master.items()},
        key_of=lambda arrow: [(e,) for e in key_employee(arrow)],
        owner=lambda key: key[0],
    )

    # ---- whole-employee replacements ------------------------------------
    for table, replacement in (("fact_assignment_history", edits.history),
                               ("fact_bank_account", edits.bank)):
        _by_chunk_table(
            cfg, counts, table, chunk_of,
            drop={(e,) for e in replacement},
            append={e: rows for e, rows in replacement.items()},
            key_of=lambda arrow: [(e,) for e in key_employee(arrow)],
            owner=lambda key: key[0],
        )

    # ---- period-partitioned facts ---------------------------------------
    allowance_rows: dict[str, list[dict[str, Any]]] = {}
    for (employee, period), rows in edits.allowances.items():
        for row in rows:
            allowance_rows.setdefault(employee, []).append({
                "employee_id": employee, "period": period,
                "allowance_code": row.code, "amount": row.cents,
                "amount_basis": row.basis, "eligibility_snapshot_json": row.snapshot,
            })

    _by_period_table(
        cfg, counts, "fact_payroll_allowance", chunk_of,
        drop=set(edits.allowances),
        append=allowance_rows,
        key_of=key_employee_period,
    )
    fresh: dict[str, list[dict[str, Any]]] = {}
    for (employee, _), row in edits.payroll_new.items():
        fresh.setdefault(employee, []).append(row)
    _by_period_table(
        cfg, counts, "fact_payroll_monthly", chunk_of,
        patch=edits.payroll, append=fresh, key_of=key_employee_period,
    )
    _by_period_table(cfg, counts, "fact_attendance_monthly", chunk_of,
                     patch=edits.attendance, key_of=key_employee_period)
    _by_period_table(cfg, counts, "fact_system_activity_monthly", chunk_of,
                     patch=edits.activity, key_of=key_employee_period)
    return counts


def _by_chunk_table(
    cfg: ScaleConfig,
    counts: dict[str, int],
    table: str,
    chunk_of: dict[str, int],
    key_of,
    owner,
    patch: dict | None = None,
    drop: set | None = None,
    append: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    """Rewrite a single-partition table, part by part."""
    patch = patch or {}
    drop = drop or set()
    append = append or {}
    if not (patch or drop or append):
        return
    total = 0
    for path in sorted(cfg.table_dir(table).glob("part-*.parquet")):
        chunk = int(path.stem.split("-")[1])
        mine_patch = {k: v for k, v in patch.items() if chunk_of.get(owner(k)) == chunk}
        mine_drop = {k for k in drop if chunk_of.get(owner(k)) == chunk}
        mine_append = [row for employee, rows in append.items()
                       if chunk_of.get(employee) == chunk for row in rows]
        if not (mine_patch or mine_drop or mine_append):
            total += pq.read_metadata(path).num_rows
            continue
        total += _rewrite(path, table, mine_drop, mine_patch, mine_append, key_of)
    counts[table] = total


def _by_period_table(
    cfg: ScaleConfig,
    counts: dict[str, int],
    table: str,
    chunk_of: dict[str, int],
    key_of,
    patch: dict | None = None,
    drop: set | None = None,
    append: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    """Rewrite a period-partitioned table, one `period=<n>/part-<chunk>` at a time."""
    patch = patch or {}
    drop = drop or set()
    append = append or {}
    if not (patch or drop or append):
        return
    assert table in PERIOD_PARTITIONED
    targets: dict[tuple[int, int], None] = {}
    for key in (*patch, *drop):
        targets[(chunk_of.get(key[0], 0), key[1])] = None
    for employee, rows in append.items():
        for row in rows:
            targets[(chunk_of.get(employee, 0), row["period"])] = None

    total = 0
    seen: set[Path] = set()
    for chunk, period in targets:
        path = cfg.part_path(table, chunk, period)
        mine_patch = {k: v for k, v in patch.items()
                      if k[1] == period and chunk_of.get(k[0]) == chunk}
        mine_drop = {k for k in drop
                     if k[1] == period and chunk_of.get(k[0]) == chunk}
        mine_append = [row for employee, rows in append.items()
                       if chunk_of.get(employee) == chunk
                       for row in rows if row["period"] == period]
        total += _rewrite(path, table, mine_drop, mine_patch, mine_append, key_of)
        seen.add(path)
    for path in sorted(cfg.table_dir(table).rglob("part-*.parquet")):
        if path not in seen:
            total += pq.read_metadata(path).num_rows
    counts[table] = total


__all__ = ["AllowanceRow", "apply_edits"]
