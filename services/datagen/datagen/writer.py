"""Chunked Parquet writer with explicit types, row-group control and a manifest.

Two decisions worth stating.

**Money travels as int64 minor units (halalas) everywhere inside the generator**
and is only widened to DECIMAL(12,2) here.  `gross` and `net` are stored, not
derived on read, and the gate reconciles them to the cent -- integer arithmetic
is the only way that is true on every row rather than almost every row.

**The schema is declared, never inferred.**  `schemas.py` transcribes
`docs/DATA_DICTIONARY.md`; this writer casts to it and refuses anything that
does not match, so a drifting column shows up as a failed write rather than as a
silently-retyped column three phases later.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import GENERATOR_VERSION
from .config import CHUNK_ROWS, ScaleConfig
from .schemas import SCHEMAS

COMPRESSION = "zstd"
COMPRESSION_LEVEL = 3


def money_array(cents: Sequence[int] | np.ndarray, mask: np.ndarray | None = None) -> pa.Array:
    """Build a DECIMAL(12,2) array from int64 minor units, without going via float.

    Arrow stores decimal128 as little-endian 128-bit words, so the int64 cents
    are widened by hand into (low, high) pairs. Doing it this way keeps the
    conversion exact and vectorised; going through `Decimal` objects is exact but
    allocates one Python object per cell, which is unaffordable at 24M rows.
    """
    values = np.asarray(cents, dtype=np.int64)
    words = np.empty((values.size, 2), dtype=np.int64)
    words[:, 0] = values
    words[:, 1] = np.where(values < 0, -1, 0)  # sign extension into the high word
    buffers: list[pa.Buffer | None] = [None, pa.py_buffer(words.tobytes())]
    if mask is not None:
        valid = np.asarray(mask, dtype=bool)
        buffers[0] = pa.py_buffer(np.packbits(valid, bitorder="little").tobytes())
        null_count = int((~valid).sum())
    else:
        null_count = 0
    return pa.Array.from_buffers(
        pa.decimal128(12, 2), values.size, buffers, null_count=null_count
    )


def money_cents(array: pa.Array | pa.ChunkedArray) -> np.ndarray:
    """The inverse of `money_array`: DECIMAL(12,2) back to int64 minor units.

    Pass 2 reloads written Parquet, edits it and writes it back, so it needs the
    same exact integer view of money the generator worked in. Arrow stores
    decimal128 as little-endian 128-bit words, so the low word of each element
    *is* the halalas -- no float, no Decimal object per cell.
    """
    if isinstance(array, pa.ChunkedArray):
        array = array.combine_chunks()
    words = np.frombuffer(array.buffers()[1], dtype=np.int64)
    low = words[0::2]
    return low[array.offset : array.offset + len(array)].copy()


def write_arrow(arrow: pa.Table, path: Path) -> None:
    """Write one Parquet part with the settings the whole lake is written at.

    Shared with pass 2 so a rewritten part is byte-comparable with the part
    pass 1 produced -- the determinism check compares file bytes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        arrow,
        path,
        compression=COMPRESSION,
        compression_level=COMPRESSION_LEVEL,
        row_group_size=CHUNK_ROWS,
        version="2.6",
        store_schema=True,
        write_statistics=True,
        coerce_timestamps="us",
    )


def _build_column(name: str, dtype: pa.DataType, values: Any, length: int) -> pa.Array:
    if isinstance(values, (pa.Array, pa.ChunkedArray)):
        array = values.combine_chunks() if isinstance(values, pa.ChunkedArray) else values
        if array.type != dtype:
            array = array.cast(dtype)
    elif pa.types.is_decimal(dtype):
        # Bulk money arrives as int64 minor units; the small reference tables
        # carry nullable Decimals straight from the policy pack.
        if isinstance(values, np.ndarray) and values.dtype.kind in "iu" or all(isinstance(v, (int, np.integer)) for v in values):
            array = money_array(values)
        else:
            array = pa.array(values, type=dtype)
    else:
        array = pa.array(values, type=dtype)
    if len(array) != length:
        raise ValueError(f"column {name!r} has {len(array)} rows, expected {length}")
    return array


def build_table(table: str, columns: Mapping[str, Any]) -> pa.Table:
    """Assemble one Arrow table against its declared schema.

    Missing or extra columns are an error rather than a silent reshape: the
    dictionary is the contract and the gate compares against the same schema.
    """
    schema = SCHEMAS[table]
    expected = [f.name for f in schema]
    got = list(columns)
    if got != expected:
        missing = [c for c in expected if c not in got]
        extra = [c for c in got if c not in expected]
        raise ValueError(
            f"{table}: column set does not match docs/DATA_DICTIONARY.md"
            + (f"; missing={missing}" if missing else "")
            + (f"; unexpected={extra}" if extra else "")
            + ("; wrong order" if not missing and not extra else "")
        )
    length = _column_length(columns, expected)
    arrays = [_build_column(f.name, f.type, columns[f.name], length) for f in schema]
    return pa.Table.from_arrays(arrays, schema=schema)


def _column_length(columns: Mapping[str, Any], names: list[str]) -> int:
    for name in names:
        value = columns[name]
        if hasattr(value, "__len__"):
            return len(value)
    raise ValueError("cannot determine row count: no sized column")


@dataclass
class LakeWriter:
    """Writes the lake and accumulates the row counts that go into the manifest."""

    cfg: ScaleConfig
    row_counts: Counter = field(default_factory=Counter)
    _files: list[Path] = field(default_factory=list)

    def write(
        self,
        table: str,
        columns: Mapping[str, Any],
        chunk: int = 0,
        period: int | None = None,
    ) -> int:
        arrow = build_table(table, columns)
        path = self.cfg.part_path(table, chunk, period)
        # Statistics and the writer version are part of the file bytes; both are
        # pinned so two runs with the same seed compare byte-identical.
        write_arrow(arrow, path)
        self.row_counts[table] += arrow.num_rows
        self._files.append(path)
        return arrow.num_rows

    # ---------------------------------------------------------------- manifest

    def manifest(
        self,
        policy_digest: Mapping[str, str],
        generated_at: datetime | None = None,
        injection: Mapping[str, Any] | None = None,
        row_counts: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        """The `manifest.json` payload; shape fixed by docs/DATA_DICTIONARY.md section 3."""
        stamp = generated_at or datetime.now(timezone.utc)
        return {
            "generator_version": GENERATOR_VERSION,
            "seed": self.cfg.seed,
            "scale": self.cfg.scale,
            "employee_count": self.cfg.employees,
            "period_from": self.cfg.period_from,
            "period_to": self.cfg.period_to,
            "period_count": self.cfg.periods,
            "reference_date": self.cfg.reference_date.isoformat(),
            "noise": self.cfg.noise,
            "generated_at": stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "row_counts": {
                k: int(v)
                for k, v in sorted((row_counts or self.row_counts).items())
            },
            # Pass 2 fills this in. A run with `--no-inject` leaves the zeroes,
            # which the eval harness reads as "no ground truth in this lake".
            "injection": dict(injection) if injection is not None else {
                "target_anomaly_rate": 0.0,
                "employees_with_anomaly": 0,
                "by_code": {},
                "confounders": {},
            },
            "policy_digest": dict(policy_digest),
        }

    def write_manifest(
        self,
        policy_digest: Mapping[str, str],
        generated_at: datetime | None = None,
        injection: Mapping[str, Any] | None = None,
        row_counts: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        payload = self.manifest(policy_digest, generated_at, injection, row_counts)
        self.cfg.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.manifest_path.write_text(
            json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8"
        )
        return payload


def clear_lake(cfg: ScaleConfig, tables: Iterable[str] | None = None) -> None:
    """Remove a previous run's Parquet so a regeneration cannot leave orphans behind."""
    import shutil

    for table in tables or SCHEMAS:
        directory = cfg.table_dir(table)
        if directory.exists():
            shutil.rmtree(directory)
    if cfg.manifest_path.exists():
        cfg.manifest_path.unlink()
