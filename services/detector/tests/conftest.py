"""Shared fixtures: one 10k lake, one feature build, one layer-1 pass per session.

The feature build is the expensive part, so it happens once and every test reads
the same store. Tests that need ground truth get it through `labels_con`, which
is the only fixture wired to `connect_labels` -- the same separation the product
code keeps.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
for _path in (ROOT, ROOT / "services" / "detector"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from detector.config import DetectorConfig
from detector.eval import harness
from detector.features.build import build, feature_columns
from detector.lake import connect, connect_labels
from detector.layers.l1_rules import RuleSet, run_rules
from detector.layers.l2_peer import run_peer
from detector.layers.l3_graph import run_l3
from detector.layers.l4_fusion import run_fusion
from detector.policy import DetectorPolicy

SCALE = "10k"


@pytest.fixture(scope="session")
def policy() -> DetectorPolicy:
    return DetectorPolicy.load(ROOT / "policy")


@pytest.fixture(scope="session")
def ruleset() -> RuleSet:
    return RuleSet.load(ROOT / "policy")


@pytest.fixture(scope="session")
def cfg(policy: DetectorPolicy) -> DetectorConfig:
    """The 10k lake, generated on demand if this is a fresh clone."""
    lake = ROOT / "data" / "raw"
    manifest = lake / f"scale={SCALE}" / "manifest.json"
    if not manifest.exists():
        pytest.skip(
            "no 10k lake; run `python tasks.py datagen --scale 10k --seed 42` first"
        )
    return DetectorConfig.build(
        SCALE,
        run_id="pytest",
        lake=lake,
        features=ROOT / "data" / "features",
        runs=ROOT / "data" / "runs",
    )


@pytest.fixture(scope="session")
def features(cfg: DetectorConfig, policy: DetectorPolicy):
    return build(cfg, policy)


@pytest.fixture(scope="session")
def columns(cfg: DetectorConfig, features) -> list[str]:
    return feature_columns(cfg)


@pytest.fixture(scope="session")
def con(cfg: DetectorConfig, features):
    """The detection connection: raw + features, and no view over ground truth."""
    connection = connect(cfg, features=True)
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def labels_con(cfg: DetectorConfig, features):
    """The evaluation connection. Only tests and `detector.eval` may hold one."""
    connection = connect_labels(cfg)
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def l1(con, ruleset: RuleSet):
    ruleset.check_executable(con)
    return run_rules(con, ruleset)


@pytest.fixture(scope="session")
def l2(con, policy: DetectorPolicy):
    """One layer-2 pass: cohorts, the salary model and the twelve detectors."""
    return run_peer(con, policy)


@pytest.fixture(scope="session")
def l3(con, policy: DetectorPolicy):
    """One layer-3 pass: both models, the candidate graph and the five codes."""
    return run_l3(con, policy)


@pytest.fixture(scope="session")
def ml_scores(l3) -> dict[str, float]:
    """Layer 3's per-employee score, the input fusion weights as the models."""
    if l3.ml is None or l3.ml.table is None:
        return {}
    return {
        str(row["employee_id"]): float(row["ml_score"])
        for row in l3.ml.table.to_pylist()
    }


@pytest.fixture(scope="session")
def l4(con, cfg: DetectorConfig, policy: DetectorPolicy, l1, l2, l3, ml_scores):
    """One layer-4 pass: the fused, banded, validated queue."""
    return run_fusion(
        con, cfg, policy,
        l1_hits=l1.hits, l2_hits=l2.hits, l3_hits=l3.hits, ml_scores=ml_scores,
    )


@pytest.fixture(scope="session")
def evaluation(cfg: DetectorConfig, ruleset: RuleSet, l1, l2, l3, l4,
               policy: DetectorPolicy):
    """One scored run, all four layers. The queue is part of what is evaluated:
    a detector can have perfect recall and still bury the five things that
    matter under three hundred that do not."""
    return harness.evaluate(cfg, ruleset, l1, l2, l3, l4,
                            policy_digest=policy.digest)
