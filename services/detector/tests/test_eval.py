"""The evaluation harness, and the boundary that keeps it the only reader of truth."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from detector.eval import report
from detector.eval.harness import evaluate
from detector.layers.l1_rules import L1Result, RuleSet

DETECTOR_SRC = Path(__file__).resolve().parents[1] / "detector"


def test_only_the_eval_package_reads_ground_truth() -> None:
    """`labels_anomaly` is never a detector input. This is that promise, tested.

    Everything is allowed to *name* the tables -- `lake.py` has to declare them
    -- but only `detector/eval/` may open the connection that can see them.
    """
    offenders = []
    for path in DETECTOR_SRC.rglob("*.py"):
        if path.parent.name == "eval" or path.name == "lake.py":
            continue
        if "connect_labels" in path.read_text(encoding="utf-8"):
            offenders.append(path.relative_to(DETECTOR_SRC).as_posix())
    assert not offenders, f"these modules can see ground truth: {offenders}"


def test_family_a_is_perfect(evaluation) -> None:
    """The phase-3 gate: family A is deterministic, so anything below 100% is a bug."""
    assert evaluation.family_recall("A") == 1.0
    assert evaluation.family_precision("A") == 1.0


def test_every_built_detector_finds_its_injections(evaluation) -> None:
    for row in evaluation.implemented:
        assert row.injected, row.code
        assert row.recall == 1.0, f"{row.code} recall {row.recall}"
        assert row.precision == 1.0, f"{row.code} precision {row.precision}"
        assert row.window_rate == 1.0, f"{row.code} window agreement {row.window_rate}"


def test_no_finding_is_unaccounted_for(evaluation) -> None:
    assert evaluation.unlabelled_hits == 0
    assert not evaluation.zero_recall


def test_pending_codes_are_reported_not_hidden(evaluation) -> None:
    """All 34 codes appear; the seventeen without a detector say which phase owns them."""
    assert len(evaluation.codes) == 34
    pending = {row.code for row in evaluation.pending}
    assert len(pending) == 17
    for row in evaluation.pending:
        assert "phase" in row.detector, row.code


def test_confounders_are_left_alone(evaluation) -> None:
    """The false-positive half. A layer-1 rule must not fire on a planted look-alike."""
    assert evaluation.confounders
    for row in evaluation.confounders:
        assert row.planted > 0, row.confounder_type
        assert row.flagged_critical == 0, row.confounder_type
        assert row.flagged_by_its_code == 0, row.confounder_type


def test_precision_at_depth_is_reported(evaluation) -> None:
    assert evaluation.precision_at[100] == 1.0


def test_a_no_ground_truth_lake_is_refused(cfg, ruleset: RuleSet) -> None:
    blank = type(cfg)(
        scale=cfg.scale, lake_root=cfg.lake_root, features_root=cfg.features_root,
        runs_root=cfg.runs_root, run_id=cfg.run_id,
        manifest={**cfg.manifest, "injection": {"by_code": {}}},
    )
    with pytest.raises(RuntimeError, match="no ground truth"):
        evaluate(blank, ruleset, L1Result(seconds=0.0))


def test_report_renders_the_reading_order(evaluation) -> None:
    text = report.render(evaluation)
    for heading in (
        "## 1. Per-anomaly-code recall",
        "## 2. Confounder false positives",
        "## 3. Precision at depth",
        "## 4. Alert budget",
        "## 5. Runtime profile",
    ):
        assert heading in text
    # Recall before confounders before precision, as the run-eval skill reads it.
    assert text.index("## 1.") < text.index("## 2.") < text.index("## 3.")
    for code in [f"A{n:02d}" for n in range(1, 13)]:
        assert re.search(rf"\| {code} \|", text), code


def test_report_writes_where_the_skill_says(evaluation, tmp_path: Path) -> None:
    path = report.write(evaluation, tmp_path / "EVAL_REPORT.md")
    assert path.exists()
    assert report.REPORT_PATH == "docs/EVAL_REPORT.md"


def test_planned_map_covers_every_code(evaluation) -> None:
    assert {row.code for row in evaluation.codes} == set(report.PLANNED)
