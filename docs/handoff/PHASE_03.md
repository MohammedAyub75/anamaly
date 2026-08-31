# PHASE 3 — feature store, layer-1 rule engine, evaluation harness

**Status**: PASSED   **Date**: 2026-09-01   **Tag**: `phase-03`

The detector exists. A DuckDB feature build turns the lake into five feature tables, seventeen
declarative rules run over the widest of them, and the evaluation harness scores the result against
the injected ground truth. **Family A runs at 100% recall and 100% precision, and so do the five
non-family-A codes the catalogue also assigns to layer 1.** No confounder is flagged, and no finding
is raised that ground truth does not account for.

The other headline is structural: **`lake.connect()` has no view named `labels_anomaly`**. The
non-negotiable is enforced by the connection rather than by discipline — a detector that reaches for
ground truth gets a binder error, not a 100% score.

## What was built

**`services/detector/detector/`** (was one empty `__init__.py`)
- `config.py` — `DetectorConfig`, built from the lake's `manifest.json`. The detector does not import
  `datagen`; the manifest is the contract between the two services.
- `lake.py` — DuckDB views over the lake and the feature store. `connect()` for detection,
  `connect_labels()` for the harness, and nothing else may hold the second.
- `policy.py` — `DetectorPolicy`: the SQL forms the feature build needs (expected-amount CASE,
  education ordinal, GOSI cross-check, cohort ladder) generated from the shared `policycore` pack,
  plus `require_digest()`, which refuses to score against stale ground truth.
- `features/build.py` + `features/sql/00..06_*.sql` — one file per feature block, executed in
  dependency order. The policy-derived fragments (`$expected_case`, `$pivot_columns`,
  `$cohort_levels`, …) are generated in Python and substituted in, so a 27th allowance code needs no
  code change. Stage cache keyed on lake + policy digest + the SQL sources themselves.
- `layers/l1_rules.py` — `Rule`/`RuleSet` (load, validate, bind) and `run_rules` (compile, execute,
  collapse into windows, render templates, compute impact).
- `eval/harness.py` — per-code recall and precision, window agreement, confounder analysis,
  precision@k, budget, warnings. The only reader of `labels_*`.
- `eval/report.py` — renders `docs/EVAL_REPORT.md` in the order `.claude/skills/run-eval` says to
  read it.
- `run.py` — the batch orchestrator with per-stage caching; writes
  `data/runs/run_id=<id>/l1_hits.parquet`.
- `__main__.py` — `build-features`, `run`, `score`, `eval`, `rules`.

**`policy/rules/`** — 16 new rule files (A02–A12, C04, C07, C08, D03, D04) beside the phase-0
reference A01. Seventeen rules, no code that knows any code's name.

**`tasks.py`** — `verify 3`; the `detect` and `eval` verbs wired to the detector CLI;
`services/detector` on `SERVICE_PATHS`.

**Tests** — `services/detector/tests/` (87 passing, ~35s): the rule pack, the feature store, a
parametrised injector-vs-detector agreement test per layer-1 code, and the harness boundary.
`conftest.py` at the repo root gained `services/detector`.

**Contract docs** — `docs/specs/detector.md`, `docs/ANOMALY_CATALOG.md`, `docs/DATA_DICTIONARY.md`,
`services/detector/README.md`. See Contract doc changes.

## Public interfaces added

```python
from detector.config import DetectorConfig, period_add, period_diff, period_last_day
cfg = DetectorConfig.build("10k", run_id=None, lake="data/raw",
                           features="data/features", runs="data/runs")
cfg.employees / .period_from / .period_to / .period_list / .policy_digest
cfg.has_ground_truth          # False on a --no-inject lake
cfg.scaled(500)               # a 1m budget scaled linearly to this tier
cfg.raw_glob(t) / .feature_glob(t) / .feature_dir(t) / .features_manifest / .run_dir

from detector.lake import connect, connect_labels, attach_features
from detector.lake import RAW_TABLES, LABEL_TABLES, FEATURE_TABLES
connect(cfg, features=False, threads=None, memory_limit=None)   # no labels_* view
connect_labels(cfg)                                             # eval only

from detector.policy import DetectorPolicy, DigestMismatch, AMOUNT_TOLERANCE_SAR
pol = DetectorPolicy.load("policy")
pol.allowance_codes / .cohort_ladder / .cohort_min_size / .hard_ceiling_ratio
pol.expected_amount_sql(code) / .expected_amount_case() / .education_rank_sql(col)
pol.gosi_class_sql(col) / .duration_limit(code) / .require_digest(manifest)

from detector.features.build import build, cache_key, is_current, feature_columns
build(cfg, pol, force=False, threads=None, log=None) -> FeatureBuild
  # .seconds .block_seconds .row_counts .columns .cache_key .cached

from detector.layers.l1_rules import RuleSet, Rule, RuleError, run_rules, render, period_label
rs = RuleSet.load("policy")        # .rules .enabled .codes .by_code(code)
rs.check_columns(feature_columns(cfg))   # evidence fields must be real columns
rs.check_executable(con)                 # every predicate binds, or RuleError
rule.where / rule.select(table="features_period")
run_rules(con, rs, table="features_period", log=None) -> L1Result
  # .hits .by_code .employees_by_code .seconds_by_code .total

from detector.run import run, rule_digest, resolve_stages, read_hits, write_hits, STAGES, BUILT
run(cfg, pol, rs, stages="features,l1", force=False, threads=None, log=None) -> RunResult

from detector.eval import harness, report
harness.evaluate(cfg, rs, l1, planned=None, runtime=None,
                 policy_digest=None, rule_digest="") -> EvalReport
  # .codes[CodeScore] .confounders .precision_at .implemented .pending .zero_recall
  # CodeScore: .code .family .detector .built .injected .detected .hits
  #            .true_positives .recall .precision .window_rate
report.write(scored, "docs/EVAL_REPORT.md") / report.render / report.summary_rows / report.PLANNED
```

```
python tasks.py detect --scale 10k [--run-id X] [--stages features,l1] [--force]
python tasks.py eval   --scale 10k [--run-id X] [--force]
python tasks.py verify 3
python -m detector build-features|run|score|eval|rules      # the real CLI
python -m detector score --employee-id E00042317 --what-if "housing_type='allowance'"
```

**Feature store** — `data/features/scale=<n>/`, described in full in `docs/specs/detector.md`:

| Table | Grain | Rows at 10k | Cols |
|---|---|---:|---:|
| `features_period` | employee × period (from hire onward) | 239,773 | 168 |
| `features_allowance` | employee × period × allowance code | 1,407,830 | 10 |
| `features_employee` | employee | 10,000 | 97 |
| `allowance_history` | employee × allowance code | 64,159 | 9 |
| `cohort_stats` | ladder level × cohort key × metric | 38,599 | 9 |

**Rule file format** gained two optional fields, both additive: `severity_expr` (a SQL expression
returning a severity, for A11's safety-critical split) and `financial_impact.confidence`
(`exact` default / `estimated` / `unknown`). The engine supplies `first_period_paid`,
`last_period_paid`, `months_paid` and their `..._label` forms to every template.

**Layer-1 output** — `data/runs/run_id=<id>/l1_hits.parquet`: `employee_id, anomaly_code, family,
severity, rule_name_en, rule_name_ar, allowance_code, regulatory_reference, period_from, period_to,
months_flagged, financial_impact_monthly, financial_impact_cumulative,
financial_impact_confidence, description, recommended_actions, evidence_json`.

## Verify output

```
Phase 3 gate — features + layer 1 rules + eval
--------------------------------------------------------------
  ok    family A rules present        17 rules loaded, A01-A12 complete
  ok    every rule enabled            a disabled rule is a silent 0% recall row
  ok    feature build under 60s       5.5s for 239,773 period rows
  ok    feature tables written        features_period=239,773, features_allowance=1,407,830, features_employee=10,000, allowance_history=64,159, cohort_stats=38,599
  ok    no label leaks into features  168 columns, none derived from ground truth
  ok    evidence fields exist         every rule's evidence resolves to a feature column
  ok    detector cannot see labels    labels_anomaly and labels_confounder are not in scope
  ok    rules compile to SQL          17 predicates bind over the feature store
  ok    layer 1 under 60s             166 findings in 0.74s
  ok    layer 1 is deterministic      a second pass finds the same cases with the same wording
  ok    A01 l1 rule                   22 injected, 22 found, 22 raised, 100% precision, 100% window
  ok    A02 l1 rule                   18 injected, 18 found, 18 raised, 100% precision, 100% window
  ok    A03 l1 rule                   5 injected, 5 found, 5 raised, 100% precision, 100% window
  ok    A04 l1 rule                   15 injected, 15 found, 15 raised, 100% precision, 100% window
  ok    A05 l1 rule                   14 injected, 14 found, 14 raised, 100% precision, 100% window
  ok    A06 l1 rule                   16 injected, 16 found, 16 raised, 100% precision, 100% window
  ok    A07 l1 rule                   11 injected, 11 found, 11 raised, 100% precision, 100% window
  ok    A08 l1 rule                   7 injected, 7 found, 7 raised, 100% precision, 100% window
  ok    A09 l1 rule                   6 injected, 6 found, 6 raised, 100% precision, 100% window
  ok    A10 l1 rule                   8 injected, 8 found, 8 raised, 100% precision, 100% window
  ok    A11 l1 rule                   6 injected, 6 found, 6 raised, 100% precision, 100% window
  ok    A12 l1 rule                   5 injected, 5 found, 5 raised, 100% precision, 100% window
  ok    C04 l1 rule                   5 injected, 5 found, 5 raised, 100% precision, 100% window
  ok    C07 l1 rule                   5 injected, 5 found, 5 raised, 100% precision, 100% window
  ok    C08 l1 rule                   5 injected, 5 found, 5 raised, 100% precision, 100% window
  ok    D03 l1 rule                   10 injected, 10 found, 10 raised, 100% precision, 100% window
  ok    D04 l1 rule                   8 injected, 8 found, 8 raised, 100% precision, 100% window
  ok    family A recall               100% across 12 codes
  ok    family A precision            100% -- a family-A false positive is a bug in the rule, not a tuning opportunity
  ok    no unaccounted findings       166 findings, every one matched to ground truth
  ok    no zero-recall detector       17/34 codes have a detector, 17 owned by phases 4-6
  ok    confounders not flagged       7 types, 90 employees, none flagged
  ok    precision@100                 100%
  ok    eval report written           docs/EVAL_REPORT.md, 34 code rows
--------------------------------------------------------------
PASS — phase 3
```

`verify 1` (54/54) and `verify 2` (44/44) also still pass against the regenerated lake. Test suites:
datagen 166 passed / 1 skipped, detector 87 passed. `ruff check` clean across the repo.

Feature build **5.5s** at 10k against a 60s budget; layer 1 **0.74s** for 17 rules over 239,773 rows.
Cumulative financial impact across the 166 findings: **SAR 7.89M**.

## Decisions made

1. **The detector does not import `datagen`.** Everything it needs about a lake is in
   `manifest.json`, which `docs/DATA_DICTIONARY.md` §3 already defines as the contract. The cost is
   four duplicated period-arithmetic functions; the benefit is that either service can be rewritten
   without the other noticing, and a lake copied to another machine carries its own description.
   `policycore` remains the single home for anything that is *policy*.
2. **`labels_anomaly` is excluded structurally, not by convention.** `connect()` simply has no such
   view. This was the cheapest possible way to keep the loudest promise in the project, and it turned
   "we must remember not to" into "the query does not compile". `test_eval.py` additionally greps the
   package: no module outside `detector/eval/` may even name `connect_labels`.
3. **Layer 1 owns seventeen codes, not twelve.** The phase table promises family A; the catalogue
   marks C04, C07, C08, D03 and D04 as `L1` too. Building them now cost five YAML files and no code,
   and leaving them for phase 4 would have put five deterministic findings into a statistical layer.
   They are gated to the same 100/100 standard.
4. **A rule predicate is a statement of policy; arithmetic in one is a feature that was not built.**
   Education ordinals, certification expiry, the GOSI class a nationality implies, an acting role's
   overrun against `max_consecutive_months`, the off-policy allowance count and its SAR total are all
   columns in `features_period`. A12 therefore reads `acting_months_over_limit > 0` and the number 12
   stays in `allowance_rules.yaml`, where a customer can change it.
5. **Hits are windows, not months.** Consecutive flagged periods collapse into one finding per
   (employee, rule) by gaps-and-islands on `period_index`. A01 firing for eighteen months is one case
   a reviewer works. `months_paid` then falls out as the multiplier for `cumulative_expr`, and the
   same definition serves every rule — A01's months of ineligible allowance, C04's months paid after
   leaving, A08's months out of band.
6. **Exclusions compile to `AND NOT coalesce((clause), FALSE)`.** `NOT (a AND NULL)` is NULL and a
   NULL in a `WHERE` drops the row, so a plain `AND NOT (…)` would let an unrelated missing field
   silently eat a true positive — a recall bug that would have shown up as an unexplained 95% and
   been very hard to find. A row is excluded only when the legitimate case is positively established.
7. **`features_period` is employee × period from hire, not one row per payroll row.** Building it
   from `fact_payroll_monthly` would have meant a month somebody was *not* paid has no row to be
   found on, which is exactly the shape of a C03 gap. The cost is 5,481 extra rows at 10k.
8. **`features_employee` takes its state from the employee's last period in the window**, not from
   `employee_master`. Otherwise an employee who transferred in the final month would be compared in
   phase 4 against peers at a site they had already left.
9. **Per-allowance roll-ups carry duration, level and largest step — not mean/std/slope/max-jump.**
   The spec's block 3 says all four for "each allowance"; a monthly entitlement is a flat line by
   construction and a slope over it is noise. 78 informative columns instead of 104 mostly-zero ones.
   **`docs/specs/detector.md` was updated** rather than the code bent to match.
10. **Two optional rule fields were added.** `severity_expr` because the catalogue says A11 is
    CRITICAL in a safety-critical post and MEDIUM elsewhere, which one static field cannot express;
    `financial_impact.confidence` because `EVIDENCE_CONTRACT.md` already distinguishes exact from
    estimated and D04's impact is a reconstruction. Both are additive and documented in the spec.
11. **A08 and A11 report a financial impact of zero.** A grade outside its job band is a
    classification error — the salary is inside the band for the grade actually held, so there is
    nothing to recover until Compensation decides which of the two is wrong. A11 is a safety finding
    first. `EVIDENCE_CONTRACT.md` allows `monthly: 0`, never null. It does mean both rank last in a
    queue ordered by impact, which phase 6 should think about.
12. **The rule pack is not in `policy_digest`, deliberately.** Editing A05's wording must not force a
    24M-row regeneration. The eval report carries its own `rule_digest`, and the layer-1 stage cache
    keys on it. See gap 1 for what this costs.
13. **The 10k lake was regenerated in this session.** `PolicyPack._rule_id_for` resolves
    `dim_allowance.eligibility_rule_id` by globbing `policy/rules/<code>_*.yaml`, so adding sixteen
    rule files changed that column from 1 populated row to 25 — a real divergence between the lake
    and the pack, with no digest change to announce it. Regenerating took 37s and left every other
    table and all 343 labels byte-identical; `verify 1` and `verify 2` confirm it.

## Known gaps / deferred

1. **`policy/rules/` is a generation input that `policy_digest` does not cover** (decision 12/13).
   Adding a rule file silently changes `dim_allowance.eligibility_rule_id` in the next generated
   lake. Documented in `DATA_DICTIONARY.md`; the practical rule is *regenerate after a phase that
   adds rules*. Phase 7 may want a separate `rule_digest` in the manifest.
2. **Precision is measured against the injected population only.** Every rule found exactly its
   injected set and nothing else, which is the correct result on a lake whose clean half is certified
   violation-free — but it means layer 1's precision has not yet been tested against messy real data.
   The confounders exercise it in the right direction and none was flagged.
3. **A04, A05, A06, A07, A08, A09, A10, A11, A12, C04, C07, C08, D03 and D04 carry no exclusions.**
   Each file says why in a comment; several genuinely have no legitimate twin in this data model
   (A04's would need an effective date on the dependent declaration, which the schema does not have).
   A01, A02 and A03 carry posting-lag exclusions, and all three were verified not to cost recall.
4. **The alert-budget section of the eval report is a baseline, not an adherence check.** Severity is
   whatever each rule declares; 16 CRITICAL against a scaled budget of 5 is expected and meaningless
   until phase 6 fuses scores and auto-tunes the bands.
5. **`features_period` is 168 columns at 239,773 rows and this is 10k-shaped.** At 1m it is 24M rows
   of the same width, and `COPY … PARTITION_BY` writes every partition in one pass. Phase 7 owns it.
   The 5.5s here says nothing about 1m; the 100k tier is where a superlinear stage will first show.
6. **`cohort_stats` computes MAD but nothing guards `MAD = 0`.** That guard belongs with the
   `robust_z` that uses it, in phase 4. Level-1 cohorts are frequently tiny (five keys); the fallback
   ladder is the answer, and phase 4 should log the distribution of levels actually used.
7. **No `evidence/` package, no `evidence_v1.json`, no `llm/`.** Phases 6 and 12. Layer-1 hits carry
   `evidence_json` — a flat map of the rule's `evidence_fields` — which is the raw material for the
   bundle, not the bundle.
8. **`python -m detector` needs `services/detector` on the path**, exactly as `python -m datagen`
   does. `tasks.py` handles it; a bare invocation from the repo root does not.

## Start here (next session)

Read exactly these three files:

1. `CLAUDE.md`
2. `docs/specs/detector.md`
3. `docs/handoff/PHASE_03.md` (this file)

First command to run:

```
python tasks.py verify 3
```

then build phase 4 — layer 2 peer statistics, the expected-salary model and SHAP. `cohort_stats` and
`features_employee` are already built and are what the cohort ladder walks; `policy/fusion.yaml`
`peer_cohort` holds the ladder, `min_size` and `robust_z_threshold`.

Three things phase 4 will want to know. **The seven codes at 0% recall in family B are the work** —
the eval report's per-code table is the feedback loop, and B01/B02 in particular are nearly
deterministic (`band_salary_min`/`band_salary_max` and `band_policy` tolerances are already columns),
so they should not need a model at all. **`labels_anomaly` is unreachable from `connect()`** — if a
query fails to bind on a `labels_` name, that is the guard working, not a bug. And **gap 1 in
`PHASE_02.md` is now phase 4's to close**: D05's precision denominator is thin because every in-window
manager change in the clean population comes with a promotion, which D05 excludes by design.

## Contract doc changes

- **`docs/specs/detector.md`** — module layout gains `lake.py` and `policy.py`; a new "What phase 3
  actually wrote" section documenting the five feature tables, their grain and partitioning, and the
  two deliberate deviations from the block list (per-allowance roll-up stats, and computed columns
  in place of arithmetic in predicates); a new "Ground truth is out of scope, structurally" section;
  the layer-1 section gains the seventeen-code scope, window collapsing, null-safe exclusions, the
  two new optional rule fields, and the `l1_hits.parquet` output.
- **`docs/ANOMALY_CATALOG.md`** — A12 renamed from "Acting-role allowance beyond the permitted
  duration" to "Time-limited allowance beyond its permitted duration". `RELOCATION` also carries
  `max_consecutive_months` and also names A12 in its `violation_codes`, so the code has always
  covered both; the entry only named one. Detection restated to say the limits come from the
  allowance schedule.
- **`docs/DATA_DICTIONARY.md`** — `dim_allowance.eligibility_rule_id` documented as filling in as
  rule files land, and `policy/rules/` called out as a generation input that `policy_digest`
  deliberately does not cover, with the reason.
- **`docs/EVIDENCE_CONTRACT.md`**, **`docs/API_CONTRACT.md`** — unchanged, not touched by this phase.
- **`docs/EVAL_REPORT.md`** — new, generated on every `eval` run.
