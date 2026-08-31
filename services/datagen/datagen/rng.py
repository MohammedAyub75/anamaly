"""The determinism contract: one independent random stream per table and field.

Getting this subtly wrong is the classic way a synthetic dataset stops being
reproducible.  Two rules, both enforced here rather than by convention:

1. **One stream per table**, spawned from a single root ``SeedSequence`` keyed by
   the table's position in ``TABLE_NAMES``.  Generating ``dim_job`` must not
   shift the numbers ``employee_master`` draws, or adding a column anywhere
   silently changes the whole dataset.
2. **One child stream per field per chunk**, derived from the table stream by a
   stable hash of the field name and the chunk index -- never by draw order.  So
   chunk size does not affect output, and adding a field does not move any other
   field's numbers.

Nothing else in the generator may call ``numpy.random`` at module level, use the
stdlib ``random``, read ``datetime.now()`` or depend on set iteration order.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

from .config import CHUNK_ROWS, TABLE_NAMES


def _key(text: str) -> int:
    """A stable 64-bit key for a field name.

    ``hash()`` is salted per process, so it would make runs irreproducible
    across invocations -- which is exactly the bug this module exists to stop.
    """
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big")


@dataclass(frozen=True)
class TableStreams:
    """The random streams belonging to one table."""

    table: str
    seq: np.random.SeedSequence

    def field(self, name: str, chunk: int = 0) -> np.random.Generator:
        """An independent generator for one field of one chunk."""
        child = np.random.SeedSequence(
            entropy=self.seq.entropy,
            spawn_key=(*self.seq.spawn_key, _key(name), int(chunk)),
        )
        return np.random.default_rng(child)

    def row_chunk(self, start_row: int) -> int:
        """Which chunk a global row index falls in. Fixed size, so scale-stable."""
        return start_row // CHUNK_ROWS


class StreamRegistry:
    """Every stream in a run, all descended from a single seed."""

    def __init__(self, seed: int) -> None:
        self.seed = int(seed)
        self._root = np.random.SeedSequence(self.seed)
        spawned = self._root.spawn(len(TABLE_NAMES))
        self._tables = {
            name: TableStreams(table=name, seq=seq)
            for name, seq in zip(TABLE_NAMES, spawned, strict=True)
        }

    def table(self, name: str) -> TableStreams:
        try:
            return self._tables[name]
        except KeyError:  # pragma: no cover - programmer error guard
            raise KeyError(
                f"{name!r} is not in datagen.config.TABLE_NAMES; add it there so it "
                "gets its own stream"
            ) from None


def weighted_choice(
    rng: np.random.Generator, values: list, weights: list[float], size: int
) -> np.ndarray:
    """Sample `size` values with the given weights.

    Uses uniform draws plus ``searchsorted`` rather than ``Generator.choice`` so
    that the first *n* draws of a size-*m* call equal the first *n* draws of a
    size-*n* call: that prefix stability is what makes a 1k run a genuine slice
    of a 10k run.
    """
    weight_array = np.asarray(weights, dtype=np.float64)
    cumulative = np.cumsum(weight_array)
    cumulative /= cumulative[-1]
    picks = np.searchsorted(cumulative, rng.random(size), side="right")
    picks = np.clip(picks, 0, len(values) - 1)
    return np.asarray(values, dtype=object)[picks]


def weighted_index(
    rng: np.random.Generator, weights: np.ndarray, size: int
) -> np.ndarray:
    """As `weighted_choice`, but returns positions instead of values."""
    cumulative = np.cumsum(np.asarray(weights, dtype=np.float64))
    cumulative /= cumulative[-1]
    picks = np.searchsorted(cumulative, rng.random(size), side="right")
    return np.clip(picks, 0, len(weights) - 1).astype(np.int64)
