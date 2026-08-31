"""Check digits, both directions.

C01 groups on the IBAN and C02 groups on the national id, so these columns are
detector inputs rather than decoration: a generator emitting merely
well-shaped digits would leave the integrity suite checking nothing.
"""

from __future__ import annotations

import numpy as np
from datagen.identifiers import (
    IBAN_LENGTH,
    badge_numbers,
    employee_ids,
    ibans,
    iqama_numbers,
    is_valid_iban,
    is_valid_saudi_id,
    national_ids,
)


def test_national_ids_are_valid_and_start_with_one():
    values = national_ids(np.arange(1, 500))
    assert all(is_valid_saudi_id(v) for v in values)
    assert all(v.startswith("1") and len(v) == 10 for v in values)
    assert len(set(values)) == len(values)


def test_iqama_numbers_are_valid_and_start_with_two():
    values = iqama_numbers(np.arange(1, 500))
    assert all(is_valid_saudi_id(v) for v in values)
    assert all(v.startswith("2") for v in values)


def test_a_tampered_check_digit_is_rejected():
    value = national_ids(np.array([12345]))[0]
    tampered = value[:9] + str((int(value[9]) + 1) % 10)
    assert not is_valid_saudi_id(tampered)
    assert not is_valid_saudi_id(value[:9])
    assert not is_valid_saudi_id("3" + value[1:])


def test_ibans_pass_mod_97():
    codes = np.array(["RJHI", "NCBK", "SABB", "ALBI"] * 50, dtype=object)
    values = ibans(codes, np.arange(1, len(codes) + 1))
    assert all(len(v) == IBAN_LENGTH for v in values)
    assert all(is_valid_iban(v) for v in values)
    assert len(set(values)) == len(values)


def test_a_tampered_iban_is_rejected():
    value = ibans(np.array(["RJHI"], dtype=object), np.array([7]))[0]
    swapped = "9" if value[10] != "9" else "8"
    assert not is_valid_iban(value[:10] + swapped + value[11:])
    assert not is_valid_iban(value[:-1])
    assert not is_valid_iban("GB" + value[2:])


def test_employee_and_badge_ids_are_dense_and_unique():
    ids = employee_ids(0, 100)
    assert ids[0] == "E00000001" and ids[-1] == "E00000100"
    assert len(set(badge_numbers(np.arange(1, 101)))) == 100
