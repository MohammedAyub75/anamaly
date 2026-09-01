# PHASE 5 — layer 3: isolation forest, the tabular autoencoder, and the graph checks

**Status**: PASSED   **Date**: 2026-09-01   **Tag**: `phase-05`

Layer 3 exists. Five codes — C01, C02, C03, C05 and C06, the five that had no detector after phase
4 — run as candidate-graph plus set-based DuckDB over the phase-3 feature store, alongside two
unsupervised models that score every employee and name no code at all. **All five reach 100%
recall, 100% precision and 100% window agreement at 10k.** No planted confounder is flagged by the
code it exists to test, and layers 1 and 2 have not moved.

The headline number: **34 of 34 codes now have a detector.** The eval report has no "not built" row
left, and every code in it has non-zero recall.

The headline decision: **a shared bank account has three possible answers, not two.** A declared
joint account is *no finding at all*; a shared account with a shared date of birth and an
all-but-identical name is *C06*; anything else is *C01*. That classification is made once, over the
whole component, and it is what leaves the planted `spousal_shared_iban` confounder alone without a
threshold anywhere near it.

## What was built

**`policy/graph_ml.yaml`** (new pack, added to `policycore.POLICY_FILES`) — the layer-3 dials: the
model matrix declared by exclusion, the isolation forest's hyperparameters and its contamination,
the autoencoder's architecture and training settings, the graph thresholds, and one block per code
carrying severity, regulatory reference, thresholds, the description a reviewer reads and the
actions to take. C05 carries **two** description templates because its two routes in — a signature
on a record, a reporting line that closes on itself — are one code and nothing like one sentence.

**`services/detector/detector/layers/l3_ml.py`** — the employee matrix (named by exclusion, median
imputation, median/MAD standardisation), the Isolation Forest, a PyTorch denoising autoencoder with
categorical embeddings and a numeric branch, percentile normalisation of both scores, and the
per-feature attribution built from the reconstruction gap. Registers `TEMP TABLE ml_scores`.

**`services/detector/detector/layers/l3_graph.py`** — Jaro-Winkler written out, connected
components over the candidate edges, the three-way component classifier, directed cycle detection
over the bounded manager subgraph, the five detectors (one DuckDB query each) and the emitter that
renders templates and builds `evidence_json`.

**`detector/policy.py`** — `graph_ml` / `matrix` / `isolation_forest` / `autoencoder` / `graph` /
`graph_codes` accessors and `graph_threshold()`.

**`detector/run.py`** — the `l3` stage: cached on `features key + layer3_digest + run_id`, writing
`data/runs/run_id=<id>/l3_hits.parquet` and `l3_scores.parquet`. `RunResult.findings` returns all
three layers as one list. `BUILT` now covers `features,l1,l2,l3`; only `fusion` is still pending.

**`detector/eval/harness.py`** — scores all three layers together; `MLSeparation` measures whether
the two unsupervised models rank the injected set above the rest of the population, which is the
only way a scorer that produces no code can be held to the same standard as the recall table.
**`report.py`** gains section 2c ("What layer 3 looked at") and family C/D rows in the gate summary.

**`tasks.py`** — `verify 5` (30 checks), `--stages features,l1,l2,l3`.

**Tests** — `services/detector/tests/test_l3.py` (45 new; 361 passing repo-wide, ~64s):
parametrised injector-vs-detector agreement per code, the three-way split of shared accounts
checked against ground truth, the candidate-subgraph size, cycle detection against a known chain,
Jaro-Winkler against the three planted near-duplicates *and* against a pair it must reject, evidence
completeness, an assertion that no whole identifier reaches a description, and the CPU path.
`test_eval.py`'s pending-codes assertion was inverted: nothing is pending any more.

## Public interfaces added

```python
from detector.layers.l3_graph import run_l3, L3Result, L3Error, GraphSummary
from detector.layers.l3_graph import DETECTORS, GRAPH_FIELDS, REQUIRED_COLUMNS
from detector.layers.l3_graph import prepare, build_components
from detector.layers.l3_graph import jaro, jaro_winkler, find_cycles
from detector.layers.l3_graph import monthly_net_sql, related_json_sql

run_l3(con, policy, codes=None, log=None) -> L3Result
  # .hits .by_code .employees_by_code .seconds_by_code .codes .detectors
  # .graph: GraphSummary(.iban_components .identity_components .by_class
  #                      .largest_component .graph_nodes .cycle_candidates
  #                      .cycles_found .self_approvals .components)
  # .ml:    MLScores
  # .graph and .ml are None on a pass loaded from the stage cache
DETECTORS: dict[str, Callable[[policy], str]]   # code -> its DuckDB SQL
find_cycles(edges, max_length) -> list[list[str]]     # (employee, manager) pairs
jaro_winkler(left, right, prefix_weight, prefix_max) -> float

from detector.layers.l3_ml import fit, build_matrix, fit_forest, train_autoencoder
from detector.layers.l3_ml import MLScores, MLError, FeatureMatrix
from detector.layers.l3_ml import resolve_device, feature_label, FEATURE_LABELS
fit(con, policy, log=None) -> MLScores
  # .rows .features .numeric_features .categorical_features .device .used_cuda
  # .cuda_available .contamination .epochs .final_loss .forest_seconds
  # .autoencoder_seconds .seconds .table .trained
  # registers TEMP TABLE ml_scores(employee_id, forest_raw, forest_score,
  #   reconstruction_gap, reconstruction_score, ml_score, ml_attributions_json)
build_matrix(con, policy, log=None) -> FeatureMatrix
  # .employees .numeric .numeric_names .categorical .categorical_names
  # .cardinalities .values .rows .features .names
train_autoencoder(matrix, config, device=None, log=None)
  -> (gap, above_expected, device, final_loss, epochs)
resolve_device("auto"|"cuda"|"cpu") -> (device, cuda_available)

from detector.policy import DetectorPolicy
pol.graph_ml / .matrix / .isolation_forest / .autoencoder / .graph / .graph_codes
pol.graph_threshold(code, name)

from detector.run import run, layer3_digest, read_graph_hits, write_scores
from detector.run import L3_HITS_FILE, L3_SCORES_FILE
run(cfg, pol, rs, stages="features,l1,l2,l3") -> RunResult
  # .l3 .l3_hits_path .l3_scores_path .findings

from detector.eval import harness
harness.evaluate(cfg, ruleset, l1, l2=None, l3=None, planned=None, runtime=None,
                 policy_digest=None, rule_digest="") -> EvalReport   # + .graph .ml
harness.MLSeparation   # .scored .labelled .labelled_median .population_median
                       # .top_decile_recall .top_percent_recall .lift .device
```

```
python tasks.py detect --scale 10k [--stages features,l1,l2,l3]
python tasks.py verify 5
```

**Layer-3 findings** — `data/runs/run_id=<id>/l3_hits.parquet`, **the same seventeen columns as
`l1_hits.parquet` and `l2_hits.parquet`**, so phase 6 fuses one list. `evidence_json` carries a
`graph_context` block in the shape `docs/EVIDENCE_CONTRACT.md` now defines:

```json
{ "anomaly_code": "C01", "metric": "shared_iban",
  "fields": { ... every column the detector selected ... },
  "graph_context": { "link_type": "shared_iban", "link_value_masked": "4281",
                     "component_size": 3, "component_class": "unrelated",
                     "total_monthly_disbursement": 75637.0,
                     "related_employees": [ { "employee_id": "E00003043",
                       "name_en": "Kamran Badr Khan",
                       "org_unit_name_en": "Downstream Manufacturing Section 650",
                       "site_name_en": "Khurais Field Camp",
                       "monthly_net": 18388.42 } ] },
  "feature_attributions": [ { "feature": "silent_paid_periods",
                              "label_en": "Months paid with no badge entry or login",
                              "contribution": 0.31, "direction": "increases",
                              "value": 24 } ] }
```

**Layer-3 scores** — `data/runs/run_id=<id>/l3_scores.parquet`, one row per employee:
`forest_raw`, `forest_score`, `reconstruction_gap`, `reconstruction_score`, `ml_score` (all
percentile ranks 0–100 except the two raw columns) and `ml_attributions_json`. This is what phase 6
weights under `layer_weights.ml_unsupervised`.

**`policy/graph_ml.yaml` shape**: `matrix` (`table`, `exclude`, `categorical`),
`isolation_forest` (`contamination`, `n_estimators`, `max_samples`, `max_features`,
`random_state`, `n_jobs`), `autoencoder` (`device`, `hidden`, `bottleneck`, `embedding_max`,
`epochs`, `batch_size`, `learning_rate`, `weight_decay`, `noise_rate`,
`categorical_loss_weight`, `seed`, `deterministic`, `attribution_top_n`,
`attribution_min_share`), `graph` (`min_component_size`, `max_members_reported`,
`name_similarity_threshold`, `jaro_winkler_prefix_weight`, `jaro_winkler_prefix_max`,
`mask_visible_digits`, `max_cycle_length`), and `codes.<CODE>` with `name_en`, `name_ar`,
`severity`, `regulatory_reference`, `enabled`, `metric`, `impact_confidence`, `thresholds`, and
either `description` + `recommended_actions` or a `routes` map of both (C05).

## Verify output

```
Phase 5 gate — layer 3 isolation forest + autoencoder + graph
--------------------------------------------------------------------------
  ok    graph detectors present                   5 codes, every code the catalogue leaves to phase 5
  ok    every detector enabled                    a disabled detector is a silent 0% recall row
  ok    contamination is set, not 'auto'          2.5% expected anomaly rate, from the catalogue's own estimate and never from the injected counts
  ok    model matrix built                        10,000 employees x 81 features (66 numeric, 15 embedded)
  ok    both models fitted                        isolation forest 0.48s, autoencoder 8.10s over 60 epochs
  ok    CUDA path confirmed                       trained on `cuda`
  ok    CPU path works                            the same net fits on cpu in 0.1s -- slower is fine, a hard CUDA dependency is not
  ok    graph stays a candidate subgraph          30 linked employees of 10,000 in 14 components, largest 3 -- networkx never sees the workforce
  ok    components are classified                 near_duplicate 3, spousal 6, unrelated 5 -- a declared joint account is not a finding and a shared date of birth is C06, not a suppressed C01
  ok    cycle detection works                     0 cycles in this lake and 0 candidates, so the finder is checked against a known chain instead
  ok    layer 3 under 120s                        28 findings in 9.24s
  ok    layer 3 is deterministic                  a second pass finds the same cases with the same wording
  ok    C01 l3                                    6 injected, 6 found, 6 raised, 100% precision, 100% window
  ok    C02 l3                                    6 injected, 6 found, 6 raised, 100% precision, 100% window
  ok    C03 l3                                    5 injected, 5 found, 5 raised, 100% precision, 100% window
  ok    C05 l3                                    5 injected, 5 found, 5 raised, 100% precision, 100% window
  ok    C06 l3                                    6 injected, 6 found, 6 raised, 100% precision, 100% window
  ok    family C recall >= 75%                    100% across all 8 codes -- the phase-5 gate
  ok    family D recall >= 75%                    100% across all 7 codes
  ok    family C precision                        100% -- an identity finding names records, so a false one is a bug rather than a judgement
  ok    the models rank the injected set high     23% of injected employees are in the top tenth (2.3x a random tenth); median 74 against 50 for the population
  ok    graph evidence names the other records    18/18 linked findings list every employee on the account or the identity number -- a link the reviewer cannot see the other end of is not evidence
  ok    identifiers are masked                    18 findings quote the last 4 digits only
  ok    ghost findings carry a model attribution  5 findings name the columns the models could not account for
  ok    no ML jargon reaches the reviewer         28 descriptions and their actions, against 16 banned terms
  ok    confounders not flagged                   7 types, 90 employees, none flagged by the code they exist to test -- including the spousal accounts and the quiet field roles this phase owns
  ok    layers 1 and 2 have not regressed         family A still 100% recall and 100% precision; family B 100% recall over 325 findings
  ok    every code has a detector                 34/34 codes -- the eval report has no 'not built' row left
  ok    no zero-recall detector                   34 detectors, none finding nothing
  ok    eval report written                       docs/EVAL_REPORT.md, 34 code rows, sections 2b and 2c
--------------------------------------------------------------------------
PASS — phase 5
```

`verify 0` (24/24), `verify 1` (54/54), `verify 2` (44/44), `verify 3` (34/34) and `verify 4`
(31/31) all still pass against the regenerated lake. Test suites: 361 passed, 2 skipped repo-wide in
64s. `ruff check .` clean.

Layer 3 at 10k: **7.4–9.2s for 28 findings across 5 codes**, of which ~6–8s is the autoencoder (60
epochs over 10,000 rows on CUDA), 0.5s the isolation forest and 0.3s the entire graph pass. The five
detectors together cost under 0.1s. Cumulative financial impact **SAR 12.89M** on top of layer 1's
SAR 7.89M and layer 2's SAR 5.02M — C01, C02 and C03 dominate it, because all three quote whole pay
streams rather than a delta. Severity mix from this layer: 17 CRITICAL / 11 HIGH (each code's
declared severity — phase 6 fuses and re-bands).

## Decisions made

1. **A new policy pack rather than more of `peer_stats.yaml`.** `graph_ml.yaml` joined
   `policycore.POLICY_FILES`, so it is digested like every other pack. Same reasoning as phase 4:
   a pack the *generator* never reads still decides what a *detector* does, and an alert scored
   under a superseded policy must be visibly stale. Adding a pack does not invalidate an existing
   lake, but `datagen`'s integrity check compares the whole map, so **the 10k lake was regenerated**
   (36.7s, all 343 labels and 90 confounders identical) and `verify 1`–`4` re-run.
2. **C03 lives in `l3_graph.py`, not `l3_ml.py`.** The spec's module layout puts the models in one
   file and the graph checks in the other, and C03 is neither: its trigger is a dormancy scan and
   its corroboration is the reconstruction gap. It is a *finding*, and every finding in this layer
   is emitted by one emitter, so it sits with the other four and reads `ml_scores` across the file
   boundary — the same shape as `l2_peer.py` reading `l2_salary.py`. The spec's module layout was
   updated to say so.
3. **The models produce no anomaly code, and are held to a different measure.** They cannot appear
   in the recall table, but *"it scored everybody the same"* is exactly the failure that table
   catches for a rule. Section 2c reports **top-decile recall**: 23% of the injected set sits in the
   top tenth of the ranking, 2.3× a random tenth. That is a floor in the gate, not a target — the
   models exist to corroborate and to catch what no rule was written for, and tuning them against
   the injected set would defeat both purposes.
4. **`ml_score` is the mean of the two percentile ranks, not the max.** Two models agreeing is the
   signal; one model shouting alone is exactly what `corroboration_bonus` in `fusion.yaml` exists to
   price, and taking the max here would pay that bonus twice.
5. **The matrix is declared by exclusion.** `features_employee` grows as later layers need more
   columns, and a hand-maintained include list would silently stop feeding the models the day
   somebody adds a feature. The pack names the identifiers and free text to drop and the
   low-cardinality strings to embed; everything else numeric goes in.
6. **Missing values are imputed to the population median, never to zero.** Zero is a real salary and
   a real allowance count. Imputing to it invents an outlier where the record is merely incomplete,
   and the models would then find the gaps in the data rather than the anomalies in it.
7. **Columns are centred on the median and scaled by the MAD**, for the same reason layer 2 uses
   them: the mean and σ of a column are moved by exactly the records this layer exists to find.
8. **The three-way component classification is made once, over the whole component.** A component
   is excluded only when *every* pair in it is explained, not when some pair is — a three-person
   ring containing one married couple is still a ring, and accounting for two of its three links
   says nothing about the third.
9. **C03 does not require an empty assignment history.** The catalogue's first statement of it said
   "no assignment rows after hire"; the injected ghosts carry 12–13 assignment rows each, like
   anybody else, so that condition would have found none of them. Catalogue updated.
10. **C03 excludes months after a termination date.** Without it, three C04 employees (paid 4–9
    months past their leaving date, and not badging in during them) come back as ghosts. A leaver
    still on payroll has a code already, and reporting them under two is a duplicate rather than
    corroboration. Catalogue updated.
11. **The C03 corroboration is a sentence, not a gate.** Where both models put the record in the top
    decile the description says the wider pattern is unlike almost any other; where they do not, it
    states the attendance fact instead. A dormancy this long is a fact either way, and a model that
    stayed quiet must never be the reason a ghost is not raised.
12. **C02 carries the record's own pay stream as its impact**, `estimated`, rather than the zero the
    injector records. One of the duplicated streams is going to stop, and phase 6 ranks a severity
    band by cumulative impact — an alert with no money on it sorts to the bottom of the queue.
    Catalogue updated.
13. **Both records of a C06 pair are raised.** The detector cannot know which of two real careers is
    the duplicate; the action names the newer record from the hire dates rather than the finding
    asserting it. The reviewer is told the **edit distance**, not the similarity: "the names differ
    by two letters" is a fact about the records, "0.94" is a fact about the comparison.
14. **C05 is dated from the self-approved record to the last month paid**, which is what puts its
    window on the label. The money a reviewer works is what has been paid on the strength of that
    signature, not the day it was signed.
15. **Jaro-Winkler is written out rather than added as a dependency.** Thirty lines against a
    package, for a function that only ever runs over pairs already sharing a date of birth *and* a
    bank account. It is never on a hot path, and the arithmetic is fixed by the definition rather
    than by a library's version. Tested against the three planted near-duplicates and against a pair
    it has to reject.
16. **Determinism is asserted, not assumed.** `torch.manual_seed`, `cudnn.deterministic`,
    `use_deterministic_algorithms(warn_only=True)` and a seeded permutation generator; the gate runs
    layer 3 twice and compares every description. `warn_only` because a kernel with no deterministic
    implementation must not fail the run.
17. **`L3Result.graph` and `.ml` are None on a cached pass.** They describe what the run *did*
    rather than what it found. A zeroed summary in the eval report would read as "the graph was
    empty" rather than as "this pass did not rebuild it", which is worse than omitting the section.

## Known gaps / deferred

1. **No manager cycle exists at 10k.** `injection.yaml` gives C05 five instances and the generator
   plants all five as self-approvals, so the cycle route has never fired on real data. It is covered
   by unit tests against a constructed chain (a 3-cycle with a tail, a path with no cycle, and a
   chain longer than `max_cycle_length`), and the gate says plainly that it is checked that way.
   If a future lake plants a cycle, the wording template is already there and untested against real
   evidence.
2. **The autoencoder's per-feature attribution is a share, not a currency.** Layer 2's attributions
   are in riyals because the model predicts riyals; a reconstruction gap has no unit, so
   `contribution` is that column's share of the record's total gap. `EVIDENCE_CONTRACT.md` now says
   which layers mean which, but the UI will have to render the two differently.
3. **`ml_attributions_json` reaches the bundle only for C03.** The other four codes are graph
   findings and the models have nothing to add to them. The scores are still written for every
   employee, so phase 6 can attach them at fusion time if a fused alert wants one.
4. **Top-decile recall is 23%, and 1%-depth recall is 1.5%.** That is a real number for an
   unsupervised model over a population where most anomalies are *policy* violations invisible to a
   distributional model — a remote-site allowance at an HQ site makes a perfectly ordinary-looking
   employee record. It is corroboration, and the gate treats it as a floor rather than a target.
5. **Runtime is 10k-shaped, and the autoencoder is the stage to watch.** 60 epochs over 10,000 rows
   is 6–8s; at 1m it is `O(rows × epochs)` and will be minutes even on CUDA. `batch_size` is already
   4096 per the spec. The isolation forest is `n_jobs=-1` and near-free. The graph pass is
   `O(shared identifiers)`, which does not grow with headcount the way the population does — but
   `build_components` pulls its candidate attributes into Python, so a lake with a genuinely large
   shared-account ring would want that loop revisited. Phase 7 owns all of it.
6. **Model artefacts are not persisted.** Both models are refitted every uncached run rather than
   written to `data/models/`. At 10k that is 9 seconds and not worth a cache; at 1m it will be, and
   `.gitignore` already reserves `data/models/isolation_forest.joblib` and `autoencoder.pt`.
7. **`python -m detector score` is still layer 1 only.** Unchanged from phase 4, and layer 3 makes
   it harder rather than easier: a what-if against a graph component is a question about other
   employees' records, not this one's.
8. **No `evidence/` package and no `evidence_v1.json` yet.** Layer-3 `evidence_json` is built to the
   shape `EVIDENCE_CONTRACT.md` specifies for `graph_context` and `feature_attributions`, but
   nothing validates it against a schema until phase 6.
9. **Severity is whatever `graph_ml.yaml` declares** — 17 CRITICAL and 11 HIGH from this layer.
   Meaningless until phase 6 fuses scores and auto-tunes the bands.
10. **One unaccounted finding remains repo-wide**, unchanged from phase 4: B01's grade-5 IT employee
    at 11,100 against a cohort median of 9,645. Layer 3 added none.

## Start here (next session)

Read exactly these three files:

1. `CLAUDE.md`
2. `docs/specs/detector.md`
3. `docs/handoff/PHASE_05.md` (this file)

First command to run:

```
python tasks.py verify 5
```

then build phase 6 — layer 4: fusion, severity banding, the evidence bundle and financial impact.
**All three layers now write the same seventeen columns**, so `RunResult.findings` is already the
one list to fuse: 166 layer-1 findings, 159 layer-2, 28 layer-3, plus `l3_scores.parquet` carrying
`ml_score` for all 10,000 employees. Four things phase 6 will want to know.

**The percentile ranking of step 1 is already done for layer 3** — `forest_score`,
`reconstruction_score` and `ml_score` are ranks in 0–100, not raw scores. Layers 1 and 2 are not:
their hits carry a severity and an impact, and fusion has to rank them itself.

**`layer_scores` in the evidence bundle wants four keys and layer 3 supplies two of them** —
`ml_unsupervised` from `ml_score`, and `graph`, which has no continuous score at all: a component
either exists or it does not. Treat a graph hit as a floor the way a rule hit is one, or read
`component_size` as the magnitude; either is defensible and the contract does not decide it.

**Three collapse problems are waiting.** B06 raises 16 findings for 8 employees (both bonus months
in the window), C01 and C02 raise one finding per member of a component where a reviewer works the
*component*, and C06 raises both records of a pair. All three are the same shape: repeated or
related findings that should fuse into one case. `graph_context.component_size` and
`related_employees` are already in the bundle to make the second and third possible.

**The alert budget is going to bite.** 353 findings at 10k against a scaled budget of 5 CRITICAL
and 50 HIGH, with 17 CRITICAL from layer 3 alone. Auto-tuning the bands to the budget is step 4 of
the spec's layer-4 list and it is the step that matters most at this scale.

## Contract doc changes

- **`docs/ANOMALY_CATALOG.md`** — detection lines rewritten for **C01** (candidate subgraph, and
  that a component is excluded only when *every* pair in it is explained), **C02** (components
  rather than a bare group-by, every record raised, and the impact divergence), **C03** (no
  assignment-history condition, the termination exclusion, and corroboration that is a sentence
  rather than a gate), **C05** (the bounded candidate subgraph, the dating, and two templates) and
  **C06** (both records raised, and edit distance rather than similarity). Each says why.
- **`docs/EVIDENCE_CONTRACT.md`** — the bundle example gains a **`graph_context`** block, and the
  field-rules table gains a row defining it: `link_value_masked` and `related_employees` mandatory
  when present, the last four digits only, and `component_class` as the record of a decision rather
  than a suppressed alert. The `feature_attributions` row now defines `direction`'s third value
  (`unexpected`, for a categorical) and says which layers report `contribution` in SAR and which as
  a share.
- **`docs/specs/detector.md`** — module layout amended; a new "What phase 5 actually wrote" section
  covering the five codes, `graph_ml.yaml`, the split between findings and scores, the matrix by
  exclusion, the measured graph promise, the three-way classifier, Jaro-Winkler, the CUDA/CPU
  proof, the three divergences and the runtime.
- **`docs/DATA_DICTIONARY.md`** — `policy_digest` now covers nine packs.
- **`docs/API_CONTRACT.md`** — unchanged. Layer 3 adds no endpoint.
- **`docs/EVAL_REPORT.md`** — regenerated; new section 2c, "What layer 3 looked at".
