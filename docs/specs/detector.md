# SPEC — `services/detector`

**Self-contained build spec** for the AI service: feature build, four detection layers, fusion,
evidence, the batch runner, and the evaluation harness. Phases 3–7 and 12 build this.

Contracts this service must satisfy: `docs/ANOMALY_CATALOG.md` (what to detect),
`docs/DATA_DICTIONARY.md` (what it reads), `docs/EVIDENCE_CONTRACT.md` (what it emits).

## Phase map

| Phase | Deliverable | Gate |
|---|---|---|
| 3 | `build_features` + Layer 1 rule engine + eval harness | Family A 100% recall & precision at 10k; features < 60s |
| 4 | Layer 2 peer stats + expected-salary model + SHAP | Family B recall ≥ 85%; every cohort n ≥ 30 or documented fallback |
| 5 | Layer 3 Isolation Forest + autoencoder + graph checks | Family C/D recall ≥ 75%; CUDA path confirmed |
| 6 | Layer 4 fusion, severity, evidence bundle, financial impact | Bundles validate; alert budget within ±20% |
| 7 | Scale-up to 1M + `agg_alerts_by_site_month` | Full 1M run < 15 min, peak RAM < 12 GB |
| 12 | Ollama narrator + grounding check + cache + fallback | Grounding rejects invented figures; UI usable with Ollama stopped |

## Module layout

```
services/detector/
  detector/
    __main__.py         # CLI: build-features, run, score, eval, rules
    config.py           # DetectorConfig: reads the lake's manifest.json
    lake.py             # DuckDB views; connect() cannot see labels_*
    policy.py           # PolicyPack -> the SQL forms the feature build needs
    features/
      build.py          # DuckDB SQL -> data/features/
      sql/*.sql         # the feature queries, one file per feature block
    layers/
      l1_rules.py       # compiles policy/rules/*.yaml to DuckDB SQL
      l2_peer.py        # cohorts, robust z, the twelve L2 detectors, CUSUM
      l2_salary.py      # expected-salary model + TreeSHAP attribution in SAR
      l3_ml.py          # the matrix, isolation forest + tabular autoencoder
      l3_graph.py       # shared IBAN / duplicate ID / manager cycles, and C03
      l4_fusion.py      # scoring, severity, evidence bundle, financial impact
    evidence/
      builder.py
      schemas/evidence_v1.json
    llm/
      provider.py       # the Protocol
      providers/{ollama,anthropic,openai,vllm,null}.py
      narrator.py       # grounding post-check + cache + template fallback
    prompts/*.txt       # versioned, never inline strings
    eval/
      harness.py        # per-code recall, precision@k, confounder analysis
      report.py         # writes docs/EVAL_REPORT.md
    run.py              # the batch orchestrator
  tests/
```

## CLI

```
python -m detector build-features --scale 10k
python -m detector run   --scale 10k --run-id 2026-08 [--stages features,l1,l2,l3,fusion]
python -m detector score --employee-id E00042317 [--what-if key=value]
python -m detector eval  --scale 10k
```

Every stage is independently re-runnable and cached — changing a fusion weight must re-run L4 only,
not rebuild features. Cache key = stage + input digest + `policy_digest`.

## Feature build (phase 3)

One DuckDB step, `data/raw/` → `data/features/`. All SQL, no Python row loops. Target under 10
minutes at 1M × 24 on 24 cores, under 60 seconds at 10k.

Feature blocks:

1. **Employee statics** — encoded categoricals, band position (`(salary − min) / (max − min)`),
   tenure, ratios, age, education ordinal.
2. **Cohort keys and aggregates** — the five fallback levels from `policy/fusion.yaml`, each with
   median, MAD, P01, P99 and count, precomputed per cohort.
3. **24-month rollups** per employee — mean, std, trend slope, max month-over-month jump for
   `base_pay`, each allowance, `overtime_pay`, `net`, `allowance_ratio`.
4. **Graph-derived** — IBAN cluster size, national-ID cluster size, manager depth, cycle flag,
   approver-is-self flag.
5. **Rule inputs** — site attributes, housing type, dependents, job band, calendar days joined and
   denormalised once, so Layer 1 is a scan over one wide table.

Write as Parquet, partitioned where it helps. Feature names are stable identifiers; the display
labels the UI shows come from a lookup, never from the column name.

### What phase 3 actually wrote

`data/features/scale=<n>/`, five tables. The grain is the contract; the column list is not, and
grows as later layers need more.

| Table | Grain | Partition | What it is |
|---|---|---|---|
| `features_period` | employee × period | `period` | The wide rule-input table. One row per employee per month from hire onward — not per payroll row, so a finding about a month somebody was *not* paid still has a row to be found on. ~165 columns. |
| `features_allowance` | employee × period × allowance code | `period` | Long format, with `expected_amount` recomputed from `allowance_rules.yaml` and `off_policy_amount`. The table an A07 alert quotes. |
| `features_employee` | employee | — | Statics, band position, the 24-month roll-ups and the graph-derived columns. The matrix layer 3 trains on and the grain layer 2 builds cohorts over. State is taken from the employee's **last period in the window**, not from `employee_master`, so statics and period features describe the same month. |
| `allowance_history` | employee × allowance code | — | Duration, level and largest step per allowance. |
| `cohort_stats` | ladder level × cohort key × metric | — | Long format: `n`, `median_value`, `mad`, `p01`, `p99` for all five `peer_cohort.fallback_order` levels. Long because the metric set will grow and a new one must not mean a schema migration. |

Two deviations from the block list above, both deliberate:

- **Per-allowance roll-ups carry duration, level and largest step, not mean/std/slope/max-jump.** A
  monthly entitlement is a flat line by construction; a standard deviation and a trend slope over it
  are noise. How long it has run, at what level, and the size of the one step that started it are
  the facts a reviewer asks about. The five *money* series (`base_pay`, `allowance_total`,
  `overtime_pay`, `net`, `standing_pay`, `allowance_ratio`) do carry all four.
- **Anything a rule would otherwise have to compute is a column.** The education ordinal, the
  certification expiry test, the GOSI class a nationality implies, an acting role's overrun against
  its policy maximum, the off-policy allowance count and its SAR total. A rule predicate is a
  statement of policy; arithmetic inside one is a feature that was not built.

### Ground truth is out of scope, structurally

`lake.connect()` — the connection every feature build, rule and layer uses — has no view named
`labels_anomaly`. A query that reaches for one fails with a binder error rather than silently
scoring 100%. Only `lake.connect_labels()`, which lives behind `detector.eval`, can see them, and
the phase-3 gate asserts both halves of that.

## Layer 1 — declarative rules (phase 3)

Rules are `policy/rules/*.yaml` in the shape defined by
`policy/rules/A01_remote_site_allowance_at_ineligible_site.yaml`. The engine:

1. Loads and validates every rule file (fail loudly on a malformed rule — a silently skipped rule is
   a silent 0% recall).
2. Compiles each `sql_predicate` into a DuckDB `SELECT` over the feature store, with `exclusions`
   applied as `AND NOT (…)`.
3. Emits one hit row per (employee, rule, period window) with the `evidence_fields` values attached
   and the `description_template` rendered.
4. Computes `financial_impact` from the rule's `monthly_expr` / `cumulative_expr`.

All rules over 1M rows must run in seconds. Layer 1 emits **100%-precision** hits — a rule that
produces a false positive is a bug in the rule, not a tuning opportunity.

Adding a policy = adding a YAML file. No code change. The `add-anomaly-rule` skill enforces the
full pattern (rule + injector + catalog entry + test).

**Layer 1 owns seventeen codes**, not twelve: `docs/ANOMALY_CATALOG.md` marks A01–A12 plus C04,
C07, C08, D03 and D04 as `L1`. The phase-3 gate asserts family A at 100/100 because family A is
what the phase table promises; the other five are gated to the same standard because they are
equally deterministic.

**Windows, not months.** Consecutive flagged periods are collapsed into one finding per
(employee, rule) by a gaps-and-islands pass on `period_index`. A rule that fires for fourteen months
is one case a reviewer works, not fourteen. The engine supplies `first_period_paid`,
`last_period_paid`, `months_paid` and their `..._label` forms (`March 2024`) to every template, and
evidence values are read from the last period in the window — the state as it stands now.

**Exclusions are null-safe.** They compile to `AND NOT coalesce((clause), FALSE)`, not
`AND NOT (clause)`. `NOT (a AND NULL)` is NULL, and a NULL in a `WHERE` drops the row, so an
unrelated missing field would silently eat a true positive. A row is excluded only when the
legitimate case is positively established.

**Two optional fields were added to the rule format** in phase 3, both additive:

- `severity_expr` — a SQL expression returning a severity, for codes whose severity depends on the
  row. A11 is CRITICAL in a safety-critical post and MEDIUM elsewhere, which a single static field
  cannot express. `severity` remains required and is the fallback.
- `financial_impact.confidence` — `exact` (default), `estimated` or `unknown`, per
  `docs/EVIDENCE_CONTRACT.md`. A rule declares `estimated` where the money it names is a
  reconstruction rather than a line in the payroll run; D04 is the only one that does today.

Output goes to `data/runs/scale=<n>/run_id=<id>/l1_hits.parquet` — employee, code, severity, window, rendered
description, recommended actions, evidence JSON and both financial-impact figures. That file is the
input phase 6 fuses and scores.

## Layer 2 — peer statistics (phase 4)

**Cohorts** are built by the fallback ladder in `policy/fusion.yaml`, walking from most specific
until `n ≥ 30`. The cohort key actually used and its size are recorded in the evidence, so the
reviewer sees *"compared against 412 peers at grade 12, Process Ops, plant sites"* rather than an
unexplained number. Log the distribution of fallback levels used — heavy reliance on level 5 means
the cohort design is wrong.

**Robust statistics only.** Median and MAD, never mean and σ: outliers are what we are looking for,
and they poison the mean. `robust_z = 0.6745 × (x − median) / MAD`, with a guard for `MAD = 0`.

**Expected-salary model.** `HistGradientBoostingRegressor` predicting `base_salary` from *legitimate*
drivers only — grade, job family, service years, education, performance, site class, nationality
class. The **residual** is the anomaly signal. TreeSHAP over this model gives per-feature attribution
which is then rendered **in SAR**: *"expected 18,400, actual 31,200; grade explains +2,100, site
+900, unexplained +9,800"*. That last sentence is the entire point of this layer — it is what a
reviewer can take to a manager.

Never train on `labels_anomaly`. This is an unsupervised residual, not a classifier.

**Temporal.** Rolling robust z plus CUSUM change-point detection over each employee's 24-month
series, for D06 and to date the anomaly window on other codes.

### What phase 4 actually wrote

**Layer 2 owns twelve codes** — B01–B07 plus D01, D02, D05, D06, D07, which is every code
`docs/ANOMALY_CATALOG.md` marks `L2`. They are configured in **`policy/peer_stats.yaml`**, a new
pack: thresholds, severities, the description a reviewer reads and the actions to take. Layer 1 has
one YAML file per rule because a rule *is* a single SQL predicate; a peer statistic needs a cohort,
a robust centre and a spread, or a model residual, or a change-point over 24 months, so the
computation is Python and only the dials are config. Numbers that already live in another pack —
`band_policy`, `allowance_load`, `payroll.overtime`, `payroll.bonus`, `peer_cohort` — are read from
there and never restated, because two copies of one threshold is how an injector and a detector
drift apart.

Layer 2's findings carry **the same shape as layer 1's**, written to
`data/runs/scale=<n>/run_id=<id>/l2_hits.parquet`, so phase 6 fuses one list rather than two. The
`evidence_json` of a layer-2 finding is richer than a rule's flat map: `fields`, plus a
`peer_context` block and a `feature_attributions` array in the shape
`docs/EVIDENCE_CONTRACT.md` defines.

**Cohort assignment** walks the ladder per (employee, metric) and takes the first rung reaching
`min_size`; where no rung does, the **widest** rung wins, because a comparison against too few
peers is worse than a broad one. Percentiles are computed over the true cohort at each rung, not
over the employees assigned to it. Every assignment records the key, the level, the size and a
`cohort_key_fallback_reason` in plain words, and the eval report prints the level distribution.

**A cohort below `min_size` is context, never a trigger.** At grade 19 there are nineteen people in
the company and the most senior of them is an outlier against the other eighteen by construction.

**Layer 2 is not held to layer 1's precision.** A rule quotes a broken clause, so a false positive
is a bug in the rule; a statistic says "this is unusual for somebody like them", which is a
judgement. The gate asks for ≥ 75% per code and ≥ 85% per family, and tuning past that against the
injected set would only mean fitting the detector to what we planted.

Four detectors diverge from the catalogue's first statement of them, each documented in
`docs/ANOMALY_CATALOG.md` in the same session:

- **B03 is triggered by the load ceiling, with the cohort comparison as context** — the reverse of
  "robust z within cohort". The catalogue's own note on the injection range says why: an offshore
  rotation worker legitimately stacks six site-driven allowances at a ratio around 0.60, which is
  what `legit_rotation_stack` is planted at, so a robust z alone flags the confounder as readily as
  the anomaly. The breach must also still be live in the employee's most recent paid month.
- **B01 has two routes in** — above the band ceiling (a fact about the approved band), or a robust-z
  outlier at the top of a cohort of at least `min_size` **whose salary the expected-salary model
  cannot account for**. The residual corroboration is what leaves `legit_high_earner` alone.
- **D06 is a step, then CUSUM** — a single month where standing pay rises past `step_ratio` with
  base pay flat and no assignment record either side, confirmed by CUSUM accumulating against the
  employee's own pre-step baseline. CUSUM alone over the whole series finds the drift but dates it
  badly, and a change-point a reviewer cannot line up against a payroll instruction is not evidence.
- **D07 requires the section to move together** — unit drift against its own baseline *and* against
  sibling units at the same level, *and* a majority of members who each moved. One manager with a
  large legitimate increase drags a unit average as far as a scheme does; only the member count
  tells them apart.

**CUSUM is two window functions, not a loop.** The reset-at-zero form
`S(i) = P(i) − min_{j≤i} P(j)`, with `P(i) = Σ (x − baseline − k·spread)`, is exactly the textbook
statistic written so DuckDB can compute it set-based over 24M rows.

**Runtime at 10k**: layer 2 in ~6.5s, of which ~4.7s is fitting the expected-salary model over
10,000 employees. Every detector is a single DuckDB query.

## Layer 3 — unsupervised ML and graph (phase 5)

Run in parallel; each produces a normalised score.

1. **Isolation Forest** (scikit-learn, `n_jobs=-1`) over the engineered matrix. The workhorse:
   ~1M × 150 features in a few minutes on 24 cores. `contamination` set from the expected anomaly
   rate, not left at `'auto'`.
2. **Tabular denoising autoencoder** (PyTorch, CUDA): categorical embeddings + a numeric branch,
   reconstruction error as the score, **per-feature reconstruction error as the attribution**.
   ~1M rows at batch 4096 fits comfortably in 8 GB VRAM. **The CPU path must work** — slower is
   fine, a hard CUDA dependency is not.
3. **Graph checks** — DuckDB self-joins to find candidate components (shared IBAN, duplicate ID),
   then `networkx` **only on the small candidate subgraphs**. Never build a 1M-node graph in memory.
   Manager-hierarchy cycles and self-approval are found the same way.
4. **Sequence autoencoder** — optional, only if CUSUM proves insufficient on the eval set. Do not
   build it speculatively.

Explicitly **not** doing: a transformer/foundation model over tabular HR data. It costs far more,
loses to gradient boosting plus isolation forest at this data shape, and cannot explain itself to an
auditor. This is a decision, not an omission.

### What phase 5 actually wrote

**Layer 3 owns five codes** — C01, C02, C03, C05 and C06, which is every code
`docs/ANOMALY_CATALOG.md` marks `L3` and every code that had no detector after phase 4. They are
configured in **`policy/graph_ml.yaml`**, a new pack on the same principle `peer_stats.yaml` set:
the computation is Python because a connected component, a Jaro-Winkler comparison and a
reconstruction gap are not SQL predicates, and only the dials, the severities and the wording live
in YAML. Findings carry **the same seventeen columns as layer 1's and layer 2's**, written to
`data/runs/scale=<n>/run_id=<id>/l3_hits.parquet`, so phase 6 fuses one list rather than three.

**The two models produce no anomaly code.** They score every employee, and the score is written
separately to `data/runs/scale=<n>/run_id=<id>/l3_scores.parquet` — `forest_score`,
`reconstruction_score`, `ml_score` and a per-feature attribution — because phase 6 weights layer 3
as one contributor over the whole population while the findings above are about a handful of
employees. `ml_score` is the **mean** of the two percentile ranks, not the max: two models agreeing
is the signal, and one model shouting alone is exactly what `corroboration_bonus` exists to price.

**The matrix is named by exclusion, not by an include list.** `features_employee` grows as later
layers need more columns, and a hand-maintained include list would silently stop feeding the models
the day somebody adds a feature. `graph_ml.yaml` names the identifiers and free text to drop and
the low-cardinality strings to embed; everything else numeric goes in — 66 numeric and 15 embedded
columns at 10k. Missing values are imputed to the **population median, never to zero**: zero is a
real salary and a real allowance count, and imputing to it invents an outlier where the record is
merely incomplete. Columns are centred on the median and scaled by the MAD for the same reason
layer 2 uses them — the mean and σ of a column are moved by exactly the records this layer exists
to find.

**The graph promise is measured, not asserted.** Candidates come from a DuckDB self-join;
`networkx` resolves components over those edges only. At 10k that graph has **30 nodes out of
10,000**, and the phase gate fails if the linked set ever exceeds 5% of the population. Manager
cycles work the same way: the feature build's `manager_cycle_flag` is computed set-based by a
recursive CTE, and only those employees and their chains — bounded by `max_cycle_length` — become
the directed subgraph `networkx` searches.

**One classifier decides three outcomes.** A shared-account component is `spousal` (no finding at
all), `near_duplicate` (C06) or `unrelated` (C01), and the decision is made once, over the whole
component, in `build_components`. This is the only place in the system where "not a finding" and
"a different finding" are different answers, and it is what leaves `spousal_shared_iban` alone
without a threshold. **A component is excluded only when every pair in it is explained** — a
three-person ring containing one married couple is still a ring.

**Jaro-Winkler is written out rather than depended on.** Thirty lines against a package, for a
function that runs over the handful of pairs that already share a date of birth *and* a bank
account. The blocking step has reduced the problem before the comparison starts, so it is never on
a hot path, and the arithmetic is fixed by the definition rather than by a library's version.

**CUDA is confirmed and the CPU path is proved.** `device: auto` in the pack resolves to what the
machine has; the phase-5 gate asserts the run used CUDA *and* refits the same network on CPU over a
slice of the matrix, because the machine that would find a hard CUDA dependency is the one nobody
runs the suite on.

Three detectors diverge from the catalogue's first statement of them, each documented in
`docs/ANOMALY_CATALOG.md` in the same session:

- **C03 does not require an empty assignment history**, and does require the silence to sit
  **before any termination date**. The injected ghosts carry a full career history like anybody
  else, so the first condition would have found none of them; and an employee still paid after
  their leaving date stops badging in for a reason C04 already owns, so without the second this
  detector reports every leaver twice.
- **C02 carries the record's own pay stream as its financial impact**, `estimated`, rather than the
  zero the injector records. One of the duplicated streams is going to stop, and an alert with no
  money on it sorts to the bottom of a queue ranked by exposure.
- **C05 is dated from the self-approved record to the last month paid**, and takes two description
  templates rather than one. A signature on a record and a reporting line that closes on itself are
  one code and nothing like one sentence.

**Runtime at 10k**: layer 3 in ~7.6s for 28 findings, of which ~6.3s is the autoencoder (60 epochs
over 10,000 rows on CUDA), 0.5s the isolation forest and 0.3s the whole graph pass. The five
detectors together cost under 0.1s: each is a single DuckDB query over tables the preparation step
already built.

## Layer 4 — fusion and evidence (phase 6)

1. Each layer's raw score → percentile rank within the scored population → 0–100.
2. Combine with `layer_weights` from `policy/fusion.yaml`. A Layer 1 hit floors the score at
   `rule_hit_floor` — a policy violation is a fact, not a probability.
3. Apply `corroboration_bonus` for each additional contributing layer.
4. Band by `severity_bands`, then **auto-tune thresholds to the alert budget** (≈500 CRITICAL,
   ≈5,000 HIGH at 1M; scaled linearly for smaller tiers). Budget is config, never a literal.
5. Build the evidence bundle per `docs/EVIDENCE_CONTRACT.md` and **validate it against
   `evidence_v1.json` before writing**. An invalid bundle fails the run.
6. Compute `financial_impact` — every alert carries one. Rule-derived is `exact`, model-derived is
   `estimated`.
7. Apply suppression: employee + code + evidence fingerprint matching a prior dismissal is
   suppressed and surfaced under a separate filter, not deleted.
8. Write `data/runs/scale=<n>/run_id=…/alerts.parquet`, the bundle travelling in the row's `evidence_json`
   rather than as one JSON file per alert, then upsert into Postgres and compute
   `agg_alerts_by_site_month` (phase 7).

### What phase 6 actually wrote

**An alert is one (employee, anomaly code)**, not one finding. The grain is fixed by
`docs/EVIDENCE_CONTRACT.md` rather than chosen here: `alert_id` is defined as stable for that pair
plus an evidence fingerprint, and `suppression.match_on` names the same three. It is also what
collapses B06's two flagged bonus months into the one case a reviewer works — 353 findings become
344 alerts at 10k. Findings are written to `alerts.parquet` with the bundle travelling in the row's
`evidence_json`, so phase 8 upserts the queue and its evidence in one pass.

**Each layer is ranked in its own population, because their outputs are not comparable.** Layer 1
emits a fact, layer 2 a distance from a peer group, layer 3 a percentile. Step 1's "percentile rank
within the scored population" is therefore applied per layer, over that layer's own findings, by
cumulative exposure; `cume_dist` is used rather than `percent_rank` so the weakest finding a layer
produced still scores above zero — a `layer_scores` entry of 0 means *this layer said nothing*, and
the contract requires `contributing_layers` to agree with the non-zero entries. Layer 3's
`ml_score` arrives already ranked over all 10,000 employees, and contributes only above
`layer_contribution.ml_unsupervised_min_score`: the models score everybody, so without a floor they
would corroborate every alert ever raised and the bonus would become a constant added to everything.

**The weighted mean is taken over the contributing layers, not over all four.** Dividing by the
full weight sum would put a graph finding on a shared bank account — SAR 1.3m of pay leaving to one
account — at 25 out of 100 because three layers had nothing to say about it. The weights decide the
blend when layers disagree; a lone layer's own rank stands, and `rule_hit_floor` then guarantees a
broken clause lands at least in the HIGH band whatever the models think.

**The corroboration bonus is spent on the distance still left to certainty**, `base + bonus ×
(100 − base) / 100`, rather than added and clamped. At the bottom of the scale that is the addition
`fusion.yaml` describes. At the top it is damped, because `min(100, base + bonus)` flattened every
corroborated alert above 94 onto the same 100 and left a nine-way tie at the head of the queue —
and a queue whose top is a tie cannot be banded to a budget at all.

**The bands are capacity, bounded by the configured floors.** `severity_bands` gives each band a
minimum score and `alert_budget` gives it a number of slots, scaled linearly from the 1m reference
(5 CRITICAL and 50 HIGH at 10k). The queue is ordered worst-first and the bands are filled from the
top until the slots run out. Threshold tuning alone cannot do this at 10k: the score is an integer
0–100 over a few hundred alerts, so its top is a run of ties and moving a threshold by one point
moves the CRITICAL count from three to eight. Capacity can only ever break such a tie — an alert it
keeps out of a full band is tied on score with the last one admitted, which the gate asserts — and
it can never promote: a band with only two records above its floor leaves three slots empty rather
than reaching into WATCHLIST to fill them. The score at each boundary is written into every
bundle's `provenance.severity_thresholds`, so a stored alert is checked against the bands it was
banded under rather than against today's pack.

**Every bundle is validated against `evidence_v1.json` before it is written**, and an invalid
bundle fails the run. The gate also asserts that the validator rejects a bundle with its `reasons`
removed: a schema nothing has ever seen fail is documentation rather than a gate.

**Suppression hides, it never deletes.** A dismissed finding comes back in `alerts.parquet` with
`suppressed = true` and a reason, so a "previously dismissed" filter is a query rather than a
re-run. Three things must match — employee, code and the evidence fingerprint — the dismissal
lapses after `expires_after_runs`, and a materially larger amount resurfaces it: the reviewer
accepted the figure they were shown, not a figure 25% higher.

Two divergences from `policy/fusion.yaml` as it was first written, both documented in the pack:

- **`min_monthly_impact_to_alert` became `min_cumulative_impact_to_alert`.** A SAR 60 allowance paid
  wrongly for 24 months is a SAR 1,440 recovery case; judging it on one month threw away five of
  B07's six findings and three of D01's seven. Exposure over the window is what a reviewer recovers.
  A finding with no financial dimension at all — a qualification gap, a grade outside its band — is
  never filtered on money.
- **`ranking.within_band_order` orders a band; it does not decide one.** Bands are handed out in
  score order (money breaking a tie) and displayed within a band in the configured order, which is
  impact first. Two different orders, and the pack's key says "within band".

**Runtime at 10k**: fusion in ~1.2s for 344 alerts — the scoring is arithmetic over a few hundred
rows, and the two DuckDB queries that denormalise employee display and the 24-month timeline are
the only work that grows with the population.

## Scale-up to 1M (phase 7)

Nothing in layers 1–4 changed its mind at a million employees; what changed is what the run is
allowed to spend. Phase 7 is therefore mostly about three questions — what may be held in memory,
what may be done per row in Python, and what has to be written once so the UI never computes it —
plus the pre-aggregated table the map is served from.

### `policy/runtime.yaml`, and why it is not a policy pack

The engineering dials live in a tenth YAML file that is deliberately **outside**
`policycore.packs.POLICY_FILES`, so it is not part of `policy_digest` and not recorded in a lake's
`manifest.json`. The rule that decides membership: a digested pack changes what the system *says*,
this file changes only what it *costs*. Lowering a memory limit must not mean regenerating
twenty-four million payroll rows. Anything that does move a figure — how many rows a model is
fitted on, how many records carry an attribution — stays in `graph_ml.yaml` and `peer_stats.yaml`
with the rest of the model configuration, where the stage digests already cover it.

It is read by `policycore.runtime.load()` because both services spend the same machine: the
generator's pass 2 and the detector's feature build are the two places a 1m run runs out of memory.

### What made the run affordable

**Every stage got a budget instead of the machine.** `lake.connect()` takes the policy pack and sets
DuckDB's `memory_limit` and a `temp_directory`, so a join over 24M rows spills and finishes rather
than being killed by the OS.

**The feature store is written out of the query pipeline, not out of memory.** The four
employee × period intermediates — the as-at spine, the long allowance table, the allowance pivot and
the wide rule-input table — used to be DuckDB temp tables. At 1m the wide one alone is 24 million
rows and 168 columns, and the build died with an out-of-memory error before writing anything, even
though every join in it streams. Each is now written straight to Parquet and read back as a view:
the peak is the pipeline rather than the result, later blocks read only the columns they ask for,
and two of the four *are* the feature tables, so there is no second copy. Two of them are internal
and go to a scratch directory the build removes when it finishes.

**The store is keyed per month, so a monthly run costs a month.** Each month's entry in the
features manifest is a signature over that month's raw partitions; the global entry covers the
policy pack, the feature queries and everything not partitioned by month (`employee_master` is
joined into all twenty-four). A month whose raw partitions changed — or whose output partition is
missing — is rebuilt, and the rest are left where they are. Everything employee-grained (the
roll-ups, the graph features, the statics, the cohorts) is rebuilt whatever happened, because each
of them reads the whole window. **This is what the phase-7 budget is measured against**: a
production batch runs monthly against a lake that gained one month, and the fifteen minutes covers
that. What a build from nothing costs is carried in the profile beside it as
`features_full_seconds` and reported in the gate, so the cheap number never stands alone.

Identity is by file size and modification time rather than by content: hashing the bytes of a 1m
lake is ten gigabytes of reading, and this errs the safe way — a rewritten file always looks
different. It does mean a full regeneration invalidates the whole store, which is honest, because
every file on disk really was rewritten.

**And it is written a few months at a time.** DuckDB's partitioned writer keeps a buffer per thread
per partition: twenty-four months of a 168-column table on a 32-thread machine is 768 open buffers,
which is its own out-of-memory error. `features.rows_per_write` in `policy/runtime.yaml` groups the
window into writes of about two million rows — one group at 10k and 100k, so those tiers run exactly
the plan phases 3–6 measured, and twelve groups of two months at 1m. The month filter is pushed into
every input rather than applied to the result, because a filter on the output of a left join prunes
nothing underneath it.

**`period_index` is numbered over the whole calendar, never over the group being written.** It is
the month's position in the 24-month window, and the gaps-and-islands pass that collapses
consecutive flagged months into one finding is built on it, as are the roll-ups and the
change-point detectors. Numbered inside a filtered pass it restarts at 1 for every group, and one
fourteen-month finding becomes fourteen — which is exactly what the first 1m run produced, 181,895
layer-1 findings against 15,610 planted ones. A test asserts that a store written in groups is
identical to the store written in one pass.

**A join in the feature build must be an equality.** `04_graph.sql` matched identity clusters with
`ON d.identifier = e.national_id OR d.identifier = e.iqama_no`. DuckDB cannot hash an `OR` and
degraded to a nested loop: under a second at 10k, ninety-six seconds at 100k, hours at 1m. Two
equality joins and a `UNION ALL` give the same answer in under a second.

**The models are fitted on a sample and score everybody.** `autoencoder.max_train_rows` caps the
rows the network is *trained* on; the sample is drawn from a seeded generator and sorted, so two
runs fit the same network on the same records. An autoencoder over HR records converges on the
shape of a normal record, and a hundred and fifty thousand of them describe that shape as well as a
million do — while a million is 245 optimiser steps an epoch instead of 37. Every employee is still
scored, in batches, and the matrix stays on the host with only the batch on the device.

**An explanation is built where there is something to explain.** TreeSHAP over a million records
costs minutes and is read for a few hundred, so `expected_salary.attribution_max_rows` explains the
widest residuals and `autoencoder.attribution_max_rows` the strangest records; below the cap the
column is empty. Both caps sit above the 10k population, so phases 3–6 score exactly what they
scored.

**The matrix is built in Arrow, not in Python.** `build_matrix` used to convert every numeric cell
through an object array — sixty-six million `float()` calls at 1m — and encode categoricals through
a dict lookup per row. Both are now one pass in C per column, and a categorical column stays as
codes plus levels (`LevelColumn`) rather than becoming one string per employee.

**Bundles are assembled a chunk of alerts at a time.** Layer 4 fetches the employee display rows and
the 24-month timelines for `fusion.bundle_chunk_alerts` alerts, builds their bundles, and moves on,
so the peak is one chunk rather than 35,000 bundles' worth of denormalised lake. The comparable
cases each alert points at are resolved once per code instead of once per alert — the same sort
inside the bundle loop is quadratic, which is invisible at three hundred alerts and is minutes at
thirty-five thousand.

**Pass 2 reads the lake through a database file.** The generator's injection step materialised every
table into an in-memory DuckDB; at 1m that is 140 million allowance rows and tens of gigabytes. It
is now a database file with a buffer budget, deleted when pass 2 ends. Injection also releases the
working copies of employees it did not edit between injectors — it loads about four candidates for
every victim it keeps — which is safe by construction: an untouched employee re-reads from the lake
identically, and the lake is not rewritten until every injector has finished. The 10k lake
regenerates byte-identical, all 108 files.

### `agg_alerts_by_site_month`

Written by a new `agg` stage to `data/runs/scale=<n>/run_id=<id>/agg_alerts_by_site_month.parquet`,
cached on the fusion stage's key. `docs/API_CONTRACT.md` serves `/analytics/geo` entirely from it.

- **The grain is (period, site, anomaly code)**, because the endpoint filters by `family` and
  `anomaly_code` and a site-month total cannot answer those. The total a default frame wants is
  stored as the row whose `anomaly_code` is `'*'`, carrying the severity mix and the three worst
  codes, so the default frame is a filter rather than a group-by.
- **An alert counts in every month of its window**, at the site the employee was at *that* month —
  an employee who moved takes their exposure with them. Cumulative exposure is spread evenly over
  the window, so a year of frames adds up to the recovery figure rather than a multiple of it;
  monthly exposure belongs to the last month of the window, which is the only month it is still
  going out in.
- **The denominator is the site's headcount that month**, and `alerts_per_1000` is stored rather
  than derived (CLAUDE.md: never raw counts).
- **Suppressed alerts are not on the map.** They stay in `alerts.parquet` — suppression hides, it
  never deletes — but a frame is a picture of what is open.

### Runs are partitioned by scale

`data/runs/scale=<n>/run_id=<id>/`. The default run id is the last period of the window, which is
the same string at every tier, so before this a 10k run and a 1m run of the same month wrote their
alerts over each other — and the second one looked like a successful run of the first.

### The runtime profile

`data/runs/runtime_profile.json` accumulates one entry per scale tier: stage timings, row counts,
the alert count, the peak resident set sampled while the batch ran, and the lake and policy digest
it was measured against. Only stages that actually ran are recorded — a cached stage's time is what
it cost the day it ran, and reporting it as this run's cost would make a tier comparison a lie.
`docs/EVAL_REPORT.md` section 5 renders it, which is the "runtime profile per stage at each scale
tier" this spec asks for, and `verify 7` holds the 1m entry to the phase budget rather than spending
fifteen minutes reproducing it.

## Evaluation harness (phase 3 onward)

Because ground truth is injected, evaluation is exact. Reads `labels_anomaly` and
`labels_confounder` — **and nothing else in the system may read them.**

Writes `docs/EVAL_REPORT.md` on every run:

- **Per-anomaly-code recall** — all 34 codes, one row each. **A code at 0% recall is a detector bug,
  and this table is the core development feedback loop.** It is the first thing anyone should read.
- **Precision@100 / @1000 / @5000**, and precision within each severity band.
- **False-positive analysis on the planted confounders** — legitimate high earners, legitimate
  salary jumps with proper records, spousal shared IBANs. None may be scored CRITICAL. This half of
  the report is as important as recall: a detector that flags everything scores 100% recall.
- **Alert budget adherence** and the score distribution.
- **Runtime profile** per stage at each scale tier.

## Batch runner

`python -m detector run --scale 1m --run-id 2026-08` executes features → L1 → L2 → L3 → fusion →
Parquet + Postgres. Target: **full 1M population under 15 minutes**, peak RAM under 12 GB. Progress
logged per stage with row counts and timings; every stage independently re-runnable and cached.

Also exposes `POST /score/employee/{id}` for on-demand re-check and what-if.

## LLM narrator (phase 12)

Everything about this is in `docs/LLM_PORTABILITY.md` and it must be followed exactly. The three
load-bearing points: **never on the batch path**, **numeral grounding enforced in code not prompt**,
and **the deterministic template fallback is a normal response, not an error state**.

## Non-negotiables

- `labels_anomaly` is never a detector input. A detector that reads it scores 100% and is worthless.
- Bulk work is DuckDB SQL and Polars lazy frames. No pandas on the 1M/24M tables.
- Every alert has at least one reason with human-readable text. An unexplained alert is a bug.
- No ML jargon reaches the evidence bundle's `text` fields — the bundle is user-facing.
- Thresholds, weights, budgets and the cohort ladder live in `policy/*.yaml`, never as literals.
