"""Check-digit-valid Saudi identifiers: national ID, iqama, IBAN, badge.

These have to be genuinely valid, not merely well-shaped. C01 (shared IBAN) and
C02 (duplicate national ID) are detected by grouping on these columns, and the
phase-1 gate verifies every check digit -- a generator that emitted random
digits would make the integrity suite meaningless and would leave the UI unable
to show a reviewer a plausible masked account number.
"""

from __future__ import annotations

import numpy as np

# Saudi IBANs are SA + 2 check digits + 2-digit bank code + 18-digit account.
IBAN_LENGTH = 24
_IBAN_COUNTRY = "SA"
# ISO 13616 letter values: A=10 ... Z=35. Only S and A are needed for SA.
_LETTER_VALUES = {"S": "28", "A": "10"}

# The two-digit clearing code that fronts the account number, per bank.
BANK_NUMERIC = {
    "RJHI": "80", "NCBK": "10", "RIBL": "20", "SABB": "45", "ALBI": "05",
    "BSFR": "55", "ARNB": "30", "INMA": "05", "ALJZ": "60", "SIBC": "65",
}


def _luhn_check_digit(digits: np.ndarray) -> np.ndarray:
    """Luhn check digit over an (n, 9) digit matrix; Saudi IDs use this scheme.

    Odd positions counting from the left are doubled and their digits summed,
    which is the variant the Saudi national ID and iqama both use.
    """
    doubled = digits[:, ::2] * 2
    doubled = np.where(doubled > 9, doubled - 9, doubled)
    total = doubled.sum(axis=1) + digits[:, 1::2].sum(axis=1)
    return (10 - (total % 10)) % 10


def _id_from_serial(serials: np.ndarray, leading: int) -> np.ndarray:
    """Build 10-digit IDs: a leading class digit, 8 serial digits, a check digit."""
    body = np.empty((serials.size, 9), dtype=np.int64)
    body[:, 0] = leading
    remainder = serials.astype(np.int64) % 100_000_000
    for position in range(8, 0, -1):
        body[:, position] = remainder % 10
        remainder //= 10
    check = _luhn_check_digit(body)
    full = np.concatenate([body, check[:, None]], axis=1)
    return np.array(["".join(map(str, row)) for row in full], dtype=object)


def national_ids(serials: np.ndarray) -> np.ndarray:
    """Saudi nationals: 10 digits starting with 1."""
    return _id_from_serial(serials, leading=1)


def iqama_numbers(serials: np.ndarray) -> np.ndarray:
    """Residents: 10 digits starting with 2."""
    return _id_from_serial(serials, leading=2)


def is_valid_saudi_id(value: str) -> bool:
    """True when `value` is a 10-digit ID starting 1 or 2 with a correct check digit."""
    if not (isinstance(value, str) and len(value) == 10 and value.isdigit()):
        return False
    if value[0] not in ("1", "2"):
        return False
    digits = np.array([[int(c) for c in value[:9]]], dtype=np.int64)
    return int(_luhn_check_digit(digits)[0]) == int(value[9])


def _mod97(text: str) -> int:
    """MOD-97-10 over a possibly very long numeric string, without big integers."""
    remainder = 0
    for char in text:
        remainder = (remainder * 10 + int(char)) % 97
    return remainder


def _iban_check_digits(bban: str) -> str:
    rearranged = bban + "".join(_LETTER_VALUES[c] for c in _IBAN_COUNTRY) + "00"
    return f"{98 - _mod97(rearranged):02d}"


def ibans(bank_codes: np.ndarray, serials: np.ndarray) -> np.ndarray:
    """Valid Saudi IBANs: bank clearing code plus an 18-digit account number."""
    out = np.empty(len(serials), dtype=object)
    for index, (bank, serial) in enumerate(zip(bank_codes, serials, strict=True)):
        account = f"{BANK_NUMERIC[str(bank)]}{int(serial) % 10**18:018d}"
        out[index] = f"{_IBAN_COUNTRY}{_iban_check_digits(account)}{account}"
    return out


def is_valid_iban(value: str) -> bool:
    """MOD-97 validation, per ISO 13616."""
    if not (isinstance(value, str) and len(value) == IBAN_LENGTH):
        return False
    if not value.startswith(_IBAN_COUNTRY) or not value[2:].isdigit():
        return False
    rearranged = value[4:] + "".join(_LETTER_VALUES[c] for c in value[:2]) + value[2:4]
    return _mod97(rearranged) == 1


def badge_numbers(serials: np.ndarray) -> np.ndarray:
    """Physical badge numbers; the join key to the access-control feed."""
    return np.array([f"B{int(s) % 10**8:08d}" for s in serials], dtype=object)


def employee_ids(start: int, count: int) -> np.ndarray:
    """`E########` -- dense and ordered, so a chunk is a contiguous id range."""
    return np.array([f"E{n:08d}" for n in range(start + 1, start + count + 1)], dtype=object)


def passport_numbers(country: np.ndarray, serials: np.ndarray) -> np.ndarray:
    """Two country letters plus seven digits. No check digit exists to verify."""
    return np.array(
        [f"{str(c)[:2]}{int(s) % 10**7:07d}" for c, s in zip(country, serials, strict=True)],
        dtype=object,
    )
