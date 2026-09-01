# PHASE 4 — layer 2 peer statistics, the expected-salary model, SHAP

**Status**: PASSED   **Date**: 2026-09-01   **Tag**: `phase-04`

Layer 2 exists. Twelve codes — B01–B07 and D01, D02, D05, D06, D07, which is every code the
catalogue marks `L2` — run as set-based DuckDB over the phase-3 feature store, on cohorts built by
the fallback ladder, corroborated by a gradient-boosted expected-salary model whose TreeSHAP
attribution is rendered **in riyals**. **All twelve reach 100% recall and 100% window agreement at
10k; family B precision is 99%.** No planted confounder is flagged by the code it exists to test,
and layer 1 has not moved.

The headline number for the next phase: **29 of 34 codes now have a detector.** The five left are
C01, C02, C03, C05 and C06 — the graph and ML codes phase 5 owns.

The headline decision: **layer 2 is not held to layer 1's precision, deliberately.** A rule quotes
a broken clause, so a false positive is a bug in the rule. A statistic says "this is unusual for
somebody like them", which is a judgement — and tuning it to 100% against an injected set would
only mean fitting the detector to what we planted.

## What was built

**`policy/peer_stats.yaml`** (new pack, added to `policycore.POLICY_FILES`) — the layer-2 dials:
robust-statistic guards, the expected-salary model's hyperparameters and driver list, the CUSUM
constants, and one block per code carrying severity, regulatory reference, thresholds, the
description a reviewer reads and the actions to take. Numbers that already live in another pack
(`band_policy`, `allowance_load`, `payroll.overtime`, `payroll.bonus`, `peer_cohort`) are read from
there and never restated.

**`services/detector/detector/layers/l2_peer.py`** — cohort assignment down the ladder, robust z
with the `MAD = 0` guard, true-cohort percentiles, the twelve detectors (one DuckDB query each), a
window collapser sharing layer 1's definition of a finding, and the emitter that renders templates
and builds `evidence_json`.

**`services/detector/detector/layers/l2_salary.py`** — `HistGradientBoostingRegressor` over seven
legitimate drivers, TreeSHAP attribution with a deterministic fallback, and the additivity check
that makes the reviewer's sentence true to the riyal.

**`detector/policy.py`** — `peer_stats` / `robust` / `expected_salary` / `cusum` / `peer_codes`
accessors, `peer_threshold()`, the cross-pack numbers layer 2 reads, `bonus_entitlement_sql()` and
`allowance_label_case()`.

**`detector/run.py`** — the `l2` stage: cached on `features key + layer2_digest + run_id`, writing
`data/runs/run_id=<id>/l2_hits.parquet`. `RunResult.findings` returns both layers as one list.

**`detector/eval/harness.py`** — scores every layer's findings together; `built` is now the union of
rule codes and layer-2 codes, and the report carries the cohort ladder distribution and the model
summary. **`report.py`** gains section 2b ("What layer 2 compared against") and family-B rows in the
gate summary.

**`tasks.py`** — `verify 4` (31 checks), `--stages features,l1,l2`.

**Tests** — `services/detector/tests/test_l2.py` (39 new; 316 passing repo-wide, ~54s):
parametrised injector-vs-detector agreement per code, the cohort-key identity between the feature
build and the detector, additivity of the attributions, evidence completeness, and an ML-jargon
scan. `test_eval.py`'s phase-3 assertions were split so layer 1 keeps its absolute standard while
layer 2 gets a floor.

## Public interfaces added

```python
from detector.layers.l2_peer import run_peer, L2Result, L2Error, CohortAssignment
from detector.layers.l2_peer import DETECTORS, PEER_FIELDS, REQUIRED_COLUMNS
from detector.layers.l2_peer import prepare, build_cohorts, windowed
from detector.layers.l2_peer import cohort_key_sql, cohort_label_sql
from detector.layers.l2_peer import allowance_mix_sql, added_allowances_sql
from detector.layers.l2_peer import added_allowances_window_sql

run_peer(con, policy, codes=None, log=None) -> L2Result
  # .hits .by_code .employees_by_code .seconds_by_code .codes .detectors
  # .cohorts: CohortAssignment(.by_level .below_min .min_size .levels
  #                            .fallback_share .last_rung_share)
  # .salary:  SalaryExpectation
DETECTORS: dict[str, Callable[[policy], str]]   # code -> its DuckDB SQL

from detector.layers.l2_salary import fit, additive_gap, SalaryExpectation, driver_label
fit(con, policy, table="features_employee", log=None) -> SalaryExpectation
  # .drivers .rows .baseline .method .mae .median_abs_residual .seconds .table
  # registers TEMP TABLE salary_expectation(employee_id, expected_salary,
  #   salary_residual, explained_sar, unexplained_sar, attribution_baseline,
  #   attributions_json)
additive_gap(expectation) -> float   # worst riyal by which the split fails to add up

from detector.policy import DetectorPolicy
pol.peer_stats / .robust / .expected_salary / .cusum / .peer_codes
pol.peer_threshold(code, name) / .peer_cohort / .robust_z_threshold
pol.percentile_flag_high / .percentile_flag_low
pol.overpayment_tolerance_pct / .underpayment_tolerance_pct
pol.max_increments_per_12m / .max_grade_jump_per_24m / .legal_overtime_hours
pol.bonus_pct_by_rating / .max_retro_entries_clean
pol.allowance_label(code) / .allowance_label_case(col) / .bonus_entitlement_sql(rating, base)

from detector.run import run, layer2_digest, read_peer_hits, write_hits, L2_HITS_FILE
run(cfg, pol, rs, stages="features,l1,l2") -> RunResult   # .l2 .l2_hits_path .findings

from detector.eval import harness
harness.evaluate(cfg, ruleset, l1, l2=None, planned=None, runtime=None,
                 policy_digest=None, rule_digest="") -> EvalReport   # + .cohorts .salary
```

```
python tasks.py detect --scale 10k [--stages features,l1,l2]
python tasks.py verify 4
```

**Layer-2 output** — `data/runs/run_id=<id>/l2_hits.parquet`, **the same seventeen columns as
`l1_hits.parquet`**, so phase 6 fuses one list. `evidence_json` is richer than a rule's flat map:

```json
{ "anomaly_code": "B01", "metric": "base_salary",
  "fields": { ... every column the detector selected ... },
  "peer_context": { "cohort_key": "grade=12|job_family=Process Ops",
                    "cohort_key_level": 4, "cohort_key_fallback_reason":
                    "site_class and nationality_class and service_band dropped to reach n >= 30",
                    "cohort_label": "grade 12, Process Ops", "cohort_n": 221,
                    "metric": "base_salary", "employee_value": 24367.0,
                    "cohort_median": 18740.0, "cohort_mad": 1180.0,
                    "percentile": 99.5, "robust_z": 3.9 },
  "feature_attributions": [ { "feature": "grade", "label_en": "Grade",
                              "contribution": 2100.0, "direction": "increases",
                              "value": 12 } ] }
```

**`policy/peer_stats.yaml` shape**: `robust` (scale factor, `mad_floor_ratio`, `min_cohort_for_z`,
metric list), `expected_salary` (target, drivers, hyperparameters, `attribution_top_n`,
`attribution_min_sar`, `residual_min_sar`), `cusum` (`k_sigma`, `h_sigma`,
`min_months_each_side`, `spread_floor_ratio`), and `codes.<CODE>` with `name_en`, `name_ar`,
`severity`, `regulatory_reference`, `enabled`, `metric`, `impact_confidence`, `thresholds`,
`description`, `recommended_actions`.

## Verify output

```
Phase 4 gate — layer 2 peer stats + expected salary
-----------------------------------------------------------------------
  ok    peer detectors present                 12 codes, every code the catalogue marks L2
  ok    every detector enabled                 a disabled detector is a silent 0% recall row
  ok    cohort ladder resolves                 L1=324, L2=1,361, L3=2,019, L4=5,200, L5=1,096
  ok    cohorts reach n >= 30                  9,943/10,000 at n>=30; 57 short on the last rung, recorded in the evidence and never a trigger
  ok    cohort design holds                    11% fall all the way to grade alone, against a 30% ceiling -- above it the ladder is wrong, not the detector
  ok    expected salary model fitted           10,000 employees, 7 legitimate drivers, median gap SAR 834
  ok    attributions add up                    treeshap: expected pay = baseline + every driver's share, to within SAR 0.01
  ok    layer 2 under 120s                     159 findings in 6.60s
  ok    layer 2 is deterministic               a second pass finds the same cases with the same wording
  ok    B01 l2 peer                            14 injected, 14 found, 15 raised, 93% precision, 100% window
  ok    B02 l2 peer                            16 injected, 16 found, 16 raised, 100% precision, 100% window
  ok    B03 l2 peer                            12 injected, 12 found, 12 raised, 100% precision, 100% window
  ok    B04 l2 peer                            9 injected, 9 found, 9 raised, 100% precision, 100% window
  ok    B05 l2 peer                            10 injected, 10 found, 11 raised, 100% precision, 100% window
  ok    B06 l2 peer                            8 injected, 8 found, 16 raised, 100% precision, 100% window
  ok    B07 l2 peer                            6 injected, 6 found, 6 raised, 100% precision, 100% window
  ok    D01 l2 peer                            7 injected, 7 found, 7 raised, 100% precision, 100% window
  ok    D02 l2 peer                            9 injected, 9 found, 9 raised, 100% precision, 100% window
  ok    D05 l2 peer                            6 injected, 6 found, 6 raised, 100% precision, 100% window
  ok    D06 l2 peer                            5 injected, 5 found, 5 raised, 100% precision, 100% window
  ok    D07 l2 peer                            47 injected, 47 found, 47 raised, 100% precision, 100% window
  ok    family B recall >= 85%                 100% across 7 codes -- the phase-4 gate
  ok    family B precision                     99% -- a statistic is not a fact, so this is a floor, not the 100% layer 1 owes
  ok    layer-2 family D recall                100% across the 5 family-D codes layer 2 owns
  ok    peer evidence names its cohort         78/78 peer findings carry the cohort key and its size -- a comparison whose basis the reviewer cannot see is not evidence
  ok    salary findings carry SAR attribution  24 findings split the gap driver by driver, in riyals
  ok    no ML jargon reaches the reviewer      159 descriptions and their actions, against 16 banned terms
  ok    confounders not flagged                7 types, 90 employees, none flagged by the code they exist to test
  ok    layer 1 has not regressed              family A still 100% recall and 100% precision over 166 findings
  ok    no zero-recall detector                29/34 codes have a detector, 5 owned by phases 5-6
  ok    eval report written                    docs/EVAL_REPORT.md, 34 code rows
-----------------------------------------------------------------------
PASS — phase 4
```

`verify 1` (54/54), `verify 2` (44/44) and `verify 3` (34/34) all still pass against the
regenerated lake. Test suites: 316 passed, 2 skipped repo-wide in 54s. `ruff check` clean.

Layer 2 at 10k: **6.6s for 159 findings across 12 codes**, of which 4.7s is fitting the
expected-salary model. Cumulative financial impact **SAR 5.02M** on top of layer 1's SAR 7.89M;
severity mix 9 CRITICAL / 88 HIGH / 62 MEDIUM (each code's declared severity — phase 6 fuses and
re-bands).

## Decisions made

1. **A new policy pack rather than more of `fusion.yaml`.** `peer_stats.yaml` joined
   `policycore.POLICY_FILES`, so it is digested like every other pack. A pack the *generator* never
   reads still belongs in the digest, because it decides what a *detector* does and an alert scored
   under a superseded policy must be visibly stale. Adding a pack does not invalidate an existing
   lake — a digest name the manifest has never seen is not a mismatch — but `datagen`'s integrity
   check compares the whole map, so **the 10k lake was regenerated** (36.6s, all 343 labels and 90
   confounders identical) and `verify 1`/`2`/`3` re-run. Documented in `DATA_DICTIONARY.md`.
2. **Layer 2 is Python with YAML dials, not a second rule format.** A rule *is* one SQL predicate,
   which is why `policy/rules/*.yaml` can be the whole detector. A peer statistic needs a cohort, a
   robust centre and a spread, or a model residual, or a change-point over 24 months. Faking that in
   a declarative file would have produced a worse rule engine and a worse layer 2.
3. **Layer 2 owns twelve codes, not seven.** The phase table promises family B; the catalogue marks
   D01, D02, D05, D06 and D07 as `L2` as well, and the spec's own layer-2 section names CUSUM for
   D06. Same reasoning as phase 3's seventeen: leaving them out would have put five peer-statistical
   findings into the ML phase.
4. **The cohort below `min_size` is context, never a trigger.** B01's peer route requires
   `cohort_n >= 30`. At grade 19 there are nineteen people in the company and the most senior of
   them is an outlier against the other eighteen by construction; that is the spec's "every cohort
   n ≥ 30 or documented fallback" made operational rather than merely reported.
5. **Where no rung reaches `min_size`, the widest rung wins**, not the most specific. A comparison
   against four peers is worse than one against everyone at that grade, and the evidence records
   which happened either way.
6. **Percentiles are computed over the true cohort, not over the employees assigned to it.** Members
   of a level-4 cohort include employees who were themselves assigned to level 1, so ranking only
   the assignees would quote a percentile against a different population than the `cohort_n` beside
   it.
7. **B03 is triggered by the load ceiling, with the cohort comparison as context** — the reverse of
   the catalogue's first wording. `legit_rotation_stack` is planted at ~0.60 precisely because that
   is indistinguishable from a legitimate six-allowance offshore stack, and a robust z flags it as
   readily as the anomaly (it did: 4 of 12 confounders, at 46% precision). The ceiling in
   `allowance_rules.yaml` is not ambiguous. **The breach must also still be live** in the employee's
   most recent paid month — two confounders crossed the line for three months at a posting change
   and came back under it on their own, and B03's action ("review each allowance") is only a
   sentence about a stack that still exists. Catalogue updated.
8. **B01's peer route is corroborated by the model residual**, as the catalogue always said and the
   first implementation did not do. Adding it removed the false positives and left
   `legit_high_earner` alone at 16/16.
9. **D06 is a step, then CUSUM — not CUSUM alone.** CUSUM over the whole 24-month series scored
   60% recall and 19% precision: it alarms some months after the change and its reset point wanders,
   so the change-point did not line up with the injected month. Detecting the step first (one month,
   standing pay up past `step_ratio`, base flat, no assignment record either side) and using CUSUM
   against the employee's own pre-step baseline to prove it *stuck* gives 5/5 at 100%. A
   change-point a reviewer cannot line up against a payroll instruction is not evidence. Catalogue
   updated.
10. **D07 requires the section to move together.** Unit drift alone put a five-person unit at 1.61
    above a labelled section at 1.58 — 43% precision. Requiring 60% of members to have each moved
    20% separates perfectly (labelled sections: 100% of members moved; the worst clean unit: 23%).
    One manager with a large legitimate increase drags a unit average exactly as far as a scheme
    does. Catalogue updated.
11. **D01 is dated over the payroll window, not over the promotions.** The injector rewrites a
    career that often climbed before the observation window opened, so promotion-dated findings
    missed the label window entirely (29% agreement). The grade held today is what a reviewer works;
    the promotion dates stay in the evidence. Catalogue updated.
12. **CUSUM is two window functions.** The reset-at-zero statistic equals
    `P(i) − min_{j≤i} P(j)`, which is a cumulative sum and a cumulative min — set-based, so it costs
    the same shape at 24M rows as at 240k. No Python loop anywhere in the layer.
13. **TreeSHAP works on `HistGradientBoostingRegressor` here** (shap 0.52 / sklearn 1.9) and
    reconstructs the prediction to SAR 0.01. A deterministic baseline-substitution fallback with the
    same additive property is kept for the day it does not, and which one ran is recorded in the
    report rather than guessed at — but the attribution is never allowed to be the reason a run
    fails.
14. **Categoricals are ordinal-encoded rather than declared native to the model.** Native
    categorical splits are bitsets that TreeSHAP handles poorly, and the attribution is the product
    here — one number per business driver — not the last percent of model fit.
15. **B06 reads the entitlement from the bonus schedule** rather than a bonus percentile. The
    schedule in `payroll.yaml` is monotone in the rating, so the entitlement is computable and the
    finding is the gap against it. A bonus with **no** rating on record is excluded: that is a gap in
    the performance file, a different finding.
16. **The financial impact of an L2 finding is `estimated`, except B02, B06 and D02.** A shortfall
    against the band minimum, a bonus above a computable entitlement and a sum of retro payroll
    lines are arithmetic on lines that exist; the rest are reconstructions.

## Known gaps / deferred

1. **One unaccounted finding.** B01 raises 15 for 14 injected. The extra is a grade-5 IT employee
   at 11,100 against a cohort median of 9,645 whose pay the model cannot account for — inside the
   band, so not a rule breach, but a genuine peer outlier. Left in: suppressing it would mean
   tuning until the detector only fires on what we planted.
2. **B06 raises 16 findings for 8 employees** — both bonus months in the window flag, because the
   injector rewrites three years of ratings and the earlier bonus is inconsistent with the rewritten
   record too. Both windows overlap the label, so precision and window agreement are 100%, but a
   reviewer would rather see one case with two payments than two cases. Phase 6's fusion should
   collapse repeated annual events per (employee, code).
3. **The bonus entitlement is always taken from `performance_rating_y1`.** For the older of the two
   bonus months the applicable rating is arguably `y2`; the clean population shows no false positive
   either way, so no year-mapping was invented. If a future lake pays bonuses on the rating of their
   own year, this is the line to change.
4. **`python -m detector score` is still layer 1 only.** The what-if path rewrites one employee's
   feature row and re-runs the rules; layer 2 needs a cohort and a fitted model, so a what-if
   against peer statistics is a design question phase 6 or 8 should answer, not a missing branch.
5. **Runtime is 10k-shaped.** Layer 2 is 6.6s of which 4.7s is the model fit — which is
   `O(employees)` and will be minutes at 1m, on one core. Two detectors were rewritten during this
   phase purely for scale: D06's before/after allowance diff over the 26 wide columns cost 8s at 10k
   until it moved to the long `features_allowance` table (0.13s), and B04/D06's
   `abs(month_index - period) <= 1` anti-join was expanded into an equality join. `peer_keys` is
   5 rungs × 6 metrics × the population, so 30M rows at 1m for the percentile window. Phase 7 owns
   all of it; the 100k tier is where a superlinear stage will first show.
6. **`cohort_stats` carries six metrics and layer 2 uses four.** `net_mean` and `band_position` are
   computed and joined but only ever reported as context. Harmless, and the long format means adding
   a metric is not a migration.
7. **No `evidence/` package and no `evidence_v1.json` yet.** Layer-2 `evidence_json` is built to the
   shape `EVIDENCE_CONTRACT.md` specifies for `peer_context` and `feature_attributions`, but nothing
   validates it against a schema until phase 6.
8. **Severity is whatever `peer_stats.yaml` declares.** 9 CRITICAL against a scaled budget of 5 is
   expected and meaningless until phase 6 fuses scores and auto-tunes the bands.
9. **`scikit-learn` and `shap` had to be installed** into the working environment; they were already
   declared in `requirements.txt` from phase 0 but never resolved. A fresh clone that runs
   `pip install -r requirements.txt` is fine.

## Start here (next session)

Read exactly these three files:

1. `CLAUDE.md`
2. `docs/specs/detector.md`
3. `docs/handoff/PHASE_04.md` (this file)

First command to run:

```
python tasks.py verify 4
```

then build phase 5 — layer 3: isolation forest, the tabular autoencoder, and the graph checks.
**The five codes at 0% recall are the work**: C01 (shared IBAN), C02 (duplicate national ID), C03
(ghost employee), C05 (self-approval / manager cycle) and C06 (near-duplicate identity). Four of the
five are graph-shaped, and `features_employee` already carries `iban_cluster_size`,
`identity_cluster_size`, `manager_depth`, `manager_cycle_flag`, `approver_is_self_flag` and
`silent_paid_periods` from phase 3 — build the candidate components in DuckDB and hand only the
small subgraphs to `networkx`, never a 1M-node graph.

Three things phase 5 will want to know. **The layer contract is `L2Result`'s shape** — hits with the
seventeen `l1_hits.parquet` columns and an `evidence_json`; copy it and phase 6 fuses one list.
**`spousal_shared_iban` is C01's confounder and is not currently flagged by anything**, so C01's
graph pass must keep it that way — the exclusion is a declared spouse both ways, and the catalogue
says a shared IBAN with a shared date of birth is C06 rather than a false positive. And **the
`prepare()` / per-code-SQL / shared-emitter pattern in `l2_peer.py` is worth copying** for
`l3_graph.py`: one preparation pass, one query per code, one place that renders wording and builds
evidence.

## Contract doc changes

- **`docs/ANOMALY_CATALOG.md`** — detection lines rewritten for **B01** (two routes, with the
  residual corroboration and the `min_size` requirement), **B03** (ceiling is the trigger, cohort is
  context, breach must be live), **B06** (entitlement from the bonus schedule; no rating is not this
  finding), **D01** (dated over the payroll window), **D06** (step, then CUSUM, and why CUSUM alone
  dates it badly) and **D07** (member-consistency as a third required condition). Each says why, so
  the next reader does not re-derive it.
- **`docs/specs/detector.md`** — module layout gains `l2_salary.py`; a new "What phase 4 actually
  wrote" section covering the twelve codes, `peer_stats.yaml` and why layer 2 is not a second rule
  format, cohort assignment and the below-`min_size` rule, the precision standard, the four
  detectors that diverge from the catalogue's first statement, the CUSUM form, and the runtime.
- **`docs/DATA_DICTIONARY.md`** — `policy_digest` now covers eight packs; why a pack the generator
  never reads still belongs in the digest, and that adding one does not invalidate a lake but does
  mean regenerating.
- **`docs/EVIDENCE_CONTRACT.md`**, **`docs/API_CONTRACT.md`** — unchanged. Layer 2 writes
  `peer_context` and `feature_attributions` in the shape already specified there; nothing validates
  it against `evidence_v1.json` until phase 6.
- **`docs/EVAL_REPORT.md`** — regenerated; new section 2b, "What layer 2 compared against".
