"""Dimension builders.

Dimensions are generated first: they are small, everything joins to them, and
the facts read them rather than re-deriving anything.  Each module returns a
column mapping in the exact order `datagen.schemas` declares, so the writer can
cast straight to the documented types.
"""

from __future__ import annotations

__all__ = ["allowance", "calendar", "grade", "job", "org_unit", "region", "site"]
