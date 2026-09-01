"""Layer 3: for each code, the injector and the detector must agree.

Parametrised over `policy/graph_ml.yaml` rather than written out five times,
exactly as the layer-1 and layer-2 suites are parametrised over their packs:
adding a detector adds its test, and a detector that finds nothing fails here
instead of showing up as a quiet 0% row in the eval report weeks later.

Three things are tested here that no other suite can reach. The **classifier
that separates C01 from C06 and from a joint account** -- the one place in the
system where "not a finding" and "a different finding" are different answers.
The **candidate subgraph**: the design promise is that networkx never sees the
workforce, and a promise nothing measures is a comment. And the **CPU path**,
because the machine that would find a hard CUDA dependency is the one nobody
runs the suite on.
"""

from __future__ import annotations

import json

import pytest
from detector.layers.l3_graph import (
    DETECTORS,
    GRAPH_FIELDS,
    REQUIRED_COLUMNS,
    find_cycles,
    jaro_winkler,
)
from detector.layers.l3_ml import build_matrix, train_autoencoder

pytestmark = pytest.mark.usefixtures("features")

CODES = sorted(DETECTORS)

# The same list the phase gate enforces, kept here so a new template cannot
# slip one in between gate runs. The evidence bundle is user-facing (CLAUDE.md).
JARGON = (
    "z-score", "robust z", "sigma", "isolation forest", "autoencoder",
    "reconstruction", "residual", "shap", "percentile", "outlier", "cusum",
    "anomaly score", "embedding", "neural",
)


@pytest.fixture(scope="module")
def truth(labels_con):
    """Ground truth as `{code: {employee_id: (from, to)}}`. Tests only."""
    rows = labels_con.execute(
        "SELECT anomaly_code, employee_id, min(period_from), max(period_to) "
        "FROM labels_anomaly GROUP BY 1, 2"
    ).fetchall()
    out: dict[str, dict[str, tuple[int, int]]] = {}
    for code, employee, start, end in rows:
        out.setdefault(code, {})[employee] = (int(start), int(end))
    return out


@pytest.fixture(scope="module")
def found(l3):
    out: dict[str, dict[str, tuple[int, int]]] = {}
    for hit in l3.hits:
        out.setdefault(hit["anomaly_code"], {})[hit["employee_id"]] = (
            hit["period_from"], hit["period_to"]
        )
    return out


@pytest.fixture(scope="module")
def confounders(labels_con):
    rows = labels_con.execute(
        "SELECT employee_id, confounder_type, confounds_code FROM labels_confounder"
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


@pytest.fixture(scope="module")
def bundles(l3):
    return [(h, json.loads(h["evidence_json"])) for h in l3.hits]


# --------------------------------------------------------------------- codes


@pytest.mark.parametrize("code", CODES)
def test_every_code_is_injected(code, truth):
    """A detector for a code nothing injects cannot be scored at all."""
    assert truth.get(code), f"{code} has a detector but no injected instances"


@pytest.mark.parametrize("code", CODES)
def test_detector_finds_every_injected_case(code, truth, found):
    missed = sorted(set(truth[code]) - set(found.get(code, {})))
    assert not missed, (
        f"{code}: {len(missed)} injected employees not found ({missed[:5]}); "
        "reconcile the injector and the detector in docs/ANOMALY_CATALOG.md "
        "before touching a threshold"
    )


@pytest.mark.parametrize("code", CODES)
def test_detector_dates_the_finding(code, truth, found):
    """Landing on the right employee with the wrong months is half an answer."""
    disagreed = [
        employee
        for employee, (start, end) in found.get(code, {}).items()
        if employee in truth[code]
        and not (start <= truth[code][employee][1] and end >= truth[code][employee][0])
    ]
    assert not disagreed, f"{code}: window disagrees for {disagreed[:5]}"


@pytest.mark.parametrize("code", CODES)
def test_precision_is_not_a_shrug(code, truth, found):
    """An identity finding names records, so a false one is a bug, not a judgement."""
    raised = found.get(code, {})
    correct = len(set(raised) & set(truth[code]))
    assert raised, f"{code} raised nothing"
    assert correct / len(raised) >= 0.90, (
        f"{code}: {len(raised) - correct} of {len(raised)} findings are on "
        "employees the injector never touched"
    )


@pytest.mark.parametrize("code", CODES)
def test_detector_returns_the_required_columns(code, con, policy):
    """Every detector owes the emitter the same six columns, whatever it computes."""
    con.execute(f"CREATE OR REPLACE TEMP VIEW _probe AS {DETECTORS[code](policy)}")
    names = {row[0] for row in con.execute("DESCRIBE _probe").fetchall()}
    assert set(REQUIRED_COLUMNS) <= names, (
        f"{code} does not return {sorted(set(REQUIRED_COLUMNS) - names)}"
    )


def test_confounders_are_left_alone(l3, confounders):
    """The planted legitimate look-alikes this phase owns must stay unflagged.

    `spousal_shared_iban` is C01's confounder and `low_activity_role` is C03's;
    both are indistinguishable from the anomaly on the trigger alone, which is
    the entire point of planting them.
    """
    fell_for = sorted(
        (confounders[hit["employee_id"]][0], hit["anomaly_code"])
        for hit in l3.hits
        if hit["employee_id"] in confounders
        and hit["anomaly_code"] == confounders[hit["employee_id"]][1]
    )
    assert not fell_for, f"layer 3 flagged its own confounders: {fell_for}"


# --------------------------------------------------------------------- graph


def test_the_graph_is_a_candidate_subgraph(l3, cfg):
    """networkx must never be handed the workforce -- that is the design, not a tuning."""
    assert l3.graph.graph_nodes < cfg.employees * 0.05, (
        f"{l3.graph.graph_nodes:,} of {cfg.employees:,} employees ended up in "
        "the graph; the candidate search is not pruning"
    )


def test_every_component_is_classified(l3):
    counted = sum(l3.graph.by_class.values())
    assert counted == l3.graph.components
    assert set(l3.graph.by_class) <= {"unrelated", "spousal", "near_duplicate"}


def test_shared_accounts_split_three_ways(l3, con, labels_con):
    """Every shared account is C01, C06 or nothing, and never two of them.

    The three outcomes are decided once, in `build_components`, over the whole
    component: a couple who declare each other is not a finding, a shared date
    of birth with an all-but-identical name is C06, and anything else is C01.
    """
    members = {
        row[0]: row[1]
        for row in con.execute(
            "SELECT employee_id, component_class FROM graph_components "
            "WHERE link_kind = 'shared_iban'"
        ).fetchall()
    }
    assert members, "no shared accounts found at all"
    by_class: dict[str, set[str]] = {}
    for employee, kind in members.items():
        by_class.setdefault(kind, set()).add(employee)

    labels = {
        row[0]: row[1]
        for row in labels_con.execute(
            "SELECT employee_id, anomaly_code FROM labels_anomaly "
            "WHERE anomaly_code IN ('C01', 'C06')"
        ).fetchall()
    }
    benign = {
        row[0]
        for row in labels_con.execute(
            "SELECT employee_id FROM labels_confounder "
            "WHERE confounder_type = 'spousal_shared_iban'"
        ).fetchall()
    }
    assert by_class.get("unrelated", set()) == {
        e for e, c in labels.items() if c == "C01"
    }
    assert by_class.get("near_duplicate", set()) == {
        e for e, c in labels.items() if c == "C06"
    }
    assert by_class.get("spousal", set()) == benign


def test_cycle_detection_finds_a_known_chain():
    """No cycle is planted at 10k, so the finder is checked against one we build."""
    assert find_cycles([("a", "b"), ("b", "c"), ("c", "a"), ("d", "a")], 6) == [
        ["a", "b", "c"]
    ]
    assert find_cycles([("a", "b"), ("b", "c")], 6) == []


def test_long_chains_are_not_cycles():
    """A deep hierarchy is not a cycle, and the length bound is what says so."""
    chain = [(chr(97 + i), chr(98 + i)) for i in range(8)]
    assert find_cycles([*chain, ("i", "a")], 6) == []


@pytest.mark.parametrize(
    ("left", "right", "at_least"),
    [
        ("ABDULLAH ZIYAD AL SUBAIE", "ABDULALH ZIYAD AL SUBAIE", 0.90),
        ("AISHA RAKAN AL DOSSARY", "AISHA RKAAN AL DOSSARY", 0.90),
        ("KHALID SULTAN AL DOSSARY", "KHALID SULTAN LA DOSSARY", 0.90),
    ],
)
def test_near_duplicate_names_score_above_the_threshold(left, right, at_least, policy):
    similarity = jaro_winkler(
        left, right,
        float(policy.graph["jaro_winkler_prefix_weight"]),
        int(policy.graph["jaro_winkler_prefix_max"]),
    )
    assert similarity >= at_least


def test_unrelated_names_score_below_the_threshold(policy):
    """The comparison has to be able to say no, or the blocking is doing all the work."""
    similarity = jaro_winkler(
        "RAKAN SULTAN AL AMRI", "KAMRAN BADR KHAN",
        float(policy.graph["jaro_winkler_prefix_weight"]),
        int(policy.graph["jaro_winkler_prefix_max"]),
    )
    assert similarity < float(policy.graph["name_similarity_threshold"])


# ------------------------------------------------------------------ evidence


def test_linked_findings_name_the_other_records(bundles):
    """A link the reviewer cannot see the other end of is not evidence."""
    linked = [b for _, b in bundles if b.get("graph_context")]
    assert linked
    for context in (b["graph_context"] for b in linked):
        assert set(context) <= set(GRAPH_FIELDS)
        assert context.get("link_value_masked")
        assert context.get("related_employees")
        for record in context["related_employees"]:
            assert record["employee_id"]
            assert record["name_en"]


def test_identifiers_never_reach_the_reviewer_whole(l3, bundles, con):
    """The masked form is what a description quotes, and the whole number is not in it."""
    ibans = {
        row[0]
        for row in con.execute(
            "SELECT DISTINCT link_value FROM graph_components"
        ).fetchall()
    }
    for hit, _ in bundles:
        for text in [hit["description"], *hit["recommended_actions"]]:
            assert not any(value and value in text for value in ibans), (
                f"{hit['anomaly_code']} quotes a whole identifier: {text[:80]}"
            )


def test_ghost_findings_carry_a_model_attribution(bundles):
    """C03's trigger is the attendance record; the models say what else does not fit."""
    ghosts = [b for h, b in bundles if h["anomaly_code"] == "C03"]
    assert ghosts
    for bundle in ghosts:
        attributions = bundle.get("feature_attributions") or []
        assert attributions, "a ghost finding with no attribution explains nothing"
        for item in attributions:
            # `feature` is the internal name and is never displayed; `label_en`
            # is what the UI renders, so it has to read as English rather than
            # as a column (docs/EVIDENCE_CONTRACT.md).
            assert "_" not in item["label_en"]
            assert item["direction"] in {"increases", "reduces", "unexpected"}


def test_no_jargon_reaches_the_reviewer(l3):
    offenders = sorted({
        word
        for hit in l3.hits
        for text in [hit["description"], *hit["recommended_actions"]]
        for word in JARGON
        if word in text.lower()
    })
    assert not offenders, f"layer 3 wording contains {offenders}"


def test_every_finding_carries_an_impact_and_a_confidence(l3):
    for hit in l3.hits:
        assert hit["financial_impact_monthly"] is not None
        assert hit["financial_impact_confidence"] in {"exact", "estimated", "unknown"}
        assert hit["recommended_actions"]


# ---------------------------------------------------------------- the models


def test_every_employee_is_scored(l3, cfg):
    assert l3.ml is not None
    assert l3.ml.rows == cfg.employees


def test_scores_are_ranks_within_the_population(l3):
    scores = [row["ml_score"] for row in l3.ml.table.to_pylist()]
    assert min(scores) >= 0.0
    assert max(scores) <= 100.0


def test_the_models_rank_the_injected_set_above_the_rest(evaluation):
    """"It scored everybody the same" is the failure the recall table cannot catch."""
    separation = evaluation.ml
    assert separation is not None
    assert separation.labelled_median > separation.population_median
    assert separation.lift >= 2.0


def test_the_matrix_excludes_identifiers(con, policy):
    """An employee id carries no signal, and a name carries only row order."""
    matrix = build_matrix(con, policy)
    assert "employee_id" not in matrix.names
    assert "name_en" not in matrix.names
    assert matrix.rows > 0
    assert matrix.categorical.shape[1] == len(matrix.categorical_names)


def test_the_cpu_path_works(con, policy):
    """A hard CUDA dependency is only ever found on the machine without a GPU."""
    from dataclasses import replace

    matrix = build_matrix(con, policy)
    take = min(1000, matrix.rows)
    small = replace(
        matrix,
        employees=matrix.employees[:take],
        numeric=matrix.numeric[:take],
        categorical=matrix.categorical[:take],
        values={name: column[:take] for name, column in matrix.values.items()},
    )
    gap, above, device, _loss, epochs = train_autoencoder(
        small, {**policy.autoencoder, "epochs": 2}, device="cpu"
    )
    assert device == "cpu"
    assert epochs == 2
    assert gap.shape == (take, small.features)
    assert above.shape[0] == take
