"""Fact builders.

Facts are generated per employee chunk and, for the monthly tables, per period
inside that chunk, so nothing bigger than one chunk-period is ever held in
memory.  At 1m scale `fact_payroll_monthly` is ~24M rows; holding it would cost
more than the whole rest of the run.
"""

from __future__ import annotations

__all__ = ["activity", "assignment", "attendance", "banking", "employee", "payroll"]
