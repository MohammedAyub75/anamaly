"""`fact_bank_account` -- effective-dated IBAN history.

History rather than a current row, because C01 detects accounts shared *over
time*: two employees who held the same IBAN in different years are as
interesting as two who hold it today, and a current-state table would hide that
entirely.

`is_known_benign_share` is written False on every row here. Pass 1 plants no
shared accounts at all; the spousal shares that make C01's precision measurable
are pass 2's job, and this column is metadata for the eval harness only -- a
detector that reads it scores itself.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np

from ..config import ScaleConfig
from ..identifiers import ibans

CHANGE_REASONS = ("initial", "employee_request", "bank_merger", "correction")

# Offset applied to the account serial of a superseded IBAN. Far above any
# serial a real employee draws, so a closed account can never collide with an
# open one and manufacture an unlabelled C01.
_SUPERSEDED_OFFSET = 10**12


def build(
    cfg: ScaleConfig,
    records: list[dict[str, Any]],
    start: int,
    change_draw: np.ndarray,
    reason_draw: np.ndarray,
    share: float,
) -> dict[str, Any]:
    employee_id: list[str] = []
    effective_from: list[date] = []
    effective_to: list[date | None] = []
    iban: list[str] = []
    bank_code: list[str] = []
    change_reason: list[str] = []
    benign: list[bool] = []

    changed = change_draw < share
    previous = ibans(
        np.array([r["bank_code"] for r in records], dtype=object),
        np.arange(start + 1, start + len(records) + 1, dtype=np.int64) * 7919
        + 13
        + _SUPERSEDED_OFFSET,
    )

    for offset, record in enumerate(records):
        opened = record["iban_effective_from"]
        if changed[offset]:
            # The superseded account runs from the original opening date; the
            # current one starts on the change and stays open.
            switch = max(
                opened + timedelta(days=180),
                cfg.window_start + timedelta(days=int(reason_draw[offset] * 500)),
            )
            switch = min(switch, cfg.reference_date - timedelta(days=1))
            if switch > opened:
                employee_id.append(record["employee_id"])
                effective_from.append(opened)
                effective_to.append(switch - timedelta(days=1))
                iban.append(str(previous[offset]))
                bank_code.append(record["bank_code"])
                change_reason.append("initial")
                benign.append(False)
                opened = switch
        employee_id.append(record["employee_id"])
        effective_from.append(opened)
        effective_to.append(None)
        iban.append(record["iban"])
        bank_code.append(record["bank_code"])
        change_reason.append(
            CHANGE_REASONS[1 + int(reason_draw[offset] * 3) % 3]
            if changed[offset]
            else "initial"
        )
        benign.append(False)

    return {
        "employee_id": employee_id,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "iban": iban,
        "bank_code": bank_code,
        "change_reason": change_reason,
        "is_known_benign_share": benign,
    }
