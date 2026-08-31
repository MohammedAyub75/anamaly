"""Shared fixtures. Everything runs at 1k scale so the suite stays under a minute."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
for _path in (ROOT, ROOT / "services" / "datagen"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from datagen.config import ScaleConfig
from datagen.policy import DatagenPolicy

SLICE_EMPLOYEES = 1000
SEED = 42


@pytest.fixture(scope="session")
def policy() -> DatagenPolicy:
    return DatagenPolicy.load(ROOT / "policy")


@pytest.fixture(scope="session")
def slice_lake(tmp_path_factory, policy) -> ScaleConfig:
    """A generated 1,000-employee lake, built once for the whole session."""
    from datagen.pipeline import generate

    out = tmp_path_factory.mktemp("lake")
    cfg = ScaleConfig.build(
        "10k", SEED, policy.population, out=out, employees=SLICE_EMPLOYEES
    )
    generate(cfg, policy)
    return cfg


def build_cfg(policy: DatagenPolicy, out, seed: int = SEED, employees: int = SLICE_EMPLOYEES):
    return ScaleConfig.build("10k", seed, policy.population, out=out, employees=employees)
