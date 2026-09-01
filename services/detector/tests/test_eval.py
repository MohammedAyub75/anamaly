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
    """Recall and dating are absolute; precision is where the two layers differ.

    A layer-1 rule quotes a broken clause, so a false positive is a bug in the
    rule. A layer-2 statistic says "this is unusual for somebody like them",
    which is a judgement, and holding it to 100% would mean tuning until the
    detector only fires on what we already planted.
    """
    for row in evaluation.implemented:
        assert row.injected, row.code
        assert row.recall == 1.0, f"{row.code} recall {row.recall}"
        assert row.window_rate == 1.0, f"{row.code} window agreement {row.window_rate}"
        floor = 1.0 if row.detector == "L1 rule" else 0.75
        assert row.precision >= floor, f"{row.code} precision {row.precision}"


def test_layer_1_accounts_for_every_finding_it_raises(evaluation) -> None:
    """Unchanged from phase 3: a rule hit with no ground truth behind it is a bug."""
    rules = [row for row in evaluation.implemented if row.detector == "L1 rule"]
    assert rules
    assert sum(row.hits - row.true_positives for row in rules) == 0
    assert not evaluation.zero_recall


def test_unaccounted_findings_stay_rare(evaluation) -> None:
    """Layer 2 may be wrong occasionally; it may not be wrong often."""
    raised = sum(row.hits for row in evaluation.codes)
    assert evaluation.unlabelled_hits / raised <= 0.02


def test_every_code_has_a_detector(evaluation) -> None:
    """All 34 codes appear, and after phase 5 none of them is still waiting.

    Until phase 5 this asserted the opposite -- that the five unbuilt codes said
    which phase owned them, so a missing detector read as "not built" rather
    than as a silent failure. There is nothing left to be pending, and the
    stronger assertion is now the useful one.
    """
    assert len(evaluation.codes) == 34
    assert not evaluation.pending, sorted(row.code for row in evaluation.pending)
    assert len(evaluation.implemented) == 34


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
        "## 4. Alerts after fusion",
        "## 5. Runtime profile",
    ):
        assert heading in text
    # Recall before confounders before precision, as the run-eval skill reads it.
    assert text.index("## 1.") < text.index("## 2.") < text.index("## 3.")
    for code in [f"A{n:02d}" for n in range(1, 13)]:
        assert re.search(rf"\| {code} \|", text), code


def test_report_reports_the_queue(evaluation) -> None:
    """Section 4 is about alerts, not findings: precision per band is the number
    a reviewer feels, and one overall figure hides which band is wrong."""
    queue = evaluation.alerts
    assert queue is not None
    assert queue.alerts <= queue.findings
    assert queue.validated == queue.alerts
    assert queue.budget_ok, queue.by_severity
    assert not queue.critical_confounders
    text = report.render(evaluation)
    assert "Lowest score" in text
    assert f"{queue.alerts:,} alerts" in text


def test_report_writes_where_the_skill_says(evaluation, tmp_path: Path) -> None:
    path = report.write(evaluation, tmp_path / "EVAL_REPORT.md")
    assert path.exists()
    assert report.REPORT_PATH == "docs/EVAL_REPORT.md"


def test_planned_map_covers_every_code(evaluation) -> None:
    assert {row.code for row in evaluation.codes} == set(report.PLANNED)
