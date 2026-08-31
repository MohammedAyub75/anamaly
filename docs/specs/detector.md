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
    __main__.py         # CLI: build-features, run, score, eval
    config.py
    features/
      build.py          # DuckDB SQL -> data/features/
      sql/*.sql         # the feature queries, one file per feature block
    layers/
      l1_rules.py       # compiles policy/rules/*.yaml to DuckDB SQL
      l2_peer.py        # cohorts, robust z, expected-salary model, CUSUM
      l3_ml.py          # isolation forest + tabular autoencoder
      l3_graph.py       # shared IBAN / duplicate ID / manager cycles
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
8. Write `data/runs/run_id=…/alerts.parquet` plus evidence JSON, then upsert into Postgres and
   compute `agg_alerts_by_site_month` (phase 7).

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
