# ARCHITECTURE.md

## Shape of the system

Four processes, two stores, one batch pipeline. Nothing is a microservice for its own sake — the
split follows where the data lives and where the latency budget changes.

```
                  policy/*.yaml  (sites, grades, allowances, rules, fusion)
                        │  read by generator AND detector — one definition, two consumers
        ┌───────────────┴────────────────┐
        ▼                                ▼
┌──────────────────┐            ┌──────────────────────────────────────┐
│ services/datagen │  Parquet   │ services/detector                    │
│  pass 1: clean   ├───────────▶│  build_features (DuckDB)             │
│  pass 2: inject  │  data/raw/ │  L1 rules → L2 peer stats            │
│  + labels        │            │  L3 ML/graph → L4 fusion + evidence  │
└──────────────────┘            └───────────────┬──────────────────────┘
                                                │ alerts.parquet + evidence JSON
                                                ▼
                                        ┌───────────────┐
                                        │   Postgres    │  alerts, evidence JSONB,
                                        │               │  cases, dispositions, audit
                                        └───────┬───────┘
                                                ▼
                                     ┌────────────────────┐      ┌──────────────┐
                                     │  services/api      │◀────▶│ Ollama (LLM) │
                                     │  FastAPI BFF       │      │  optional    │
                                     └─────────┬──────────┘      └──────────────┘
                                               ▼
                                          ┌─────────┐
                                          │  web/   │  React + Vite
                                          └─────────┘
```

## The two-store split — the most important structural decision

| Store | Holds | Accessed by |
|---|---|---|
| **DuckDB / Parquet** (`data/`) | The analytical lake: 1M employees × 24 months, features, model artifacts, run outputs | Batch only — `datagen` and `detector` |
| **Postgres** | Alerts, evidence bundles (JSONB), cases, dispositions, users, audit log, pre-aggregated map tables | The API, on user requests |

**The API never scans Parquet on a user request.** A triage queue page load must be a handful of
indexed Postgres reads, not a 24M-row scan. Everything the UI needs is computed during the batch and
written to Postgres — including `agg_alerts_by_site_month`, which is what makes the map's 24-frame
animation smooth instead of a series of aggregations.

The corollary: anything the UI needs must be *anticipated* in the batch. That is a real constraint,
and it is the right trade for a system where the data changes once per run and is read thousands of
times.

## Dataflow — a full run

1. **`datagen`** (phase 1–2, re-run only when the population changes)
   - Pass 1 writes a clean, policy-compliant population. Chunked in 100k row-groups; nothing holds
     1M × 24 months in memory.
   - Pass 2 injects the 34 anomaly codes plus confounders, writing `labels_anomaly` and
     `labels_confounder`.
   - Writes `manifest.json` with the seed, row counts, injection rates and a digest of every policy
     file used.

2. **`detector build_features`** — one DuckDB step from `data/raw/` to `data/features/`: employee
   statics, cohort keys and aggregates, 24-month rollups, graph-derived features, and the
   denormalised rule inputs. Target under 10 minutes for 1M × 24 on 24 cores.

3. **Layer 1 — rules.** `policy/rules/*.yaml` compiled to DuckDB SQL. All rules over 1M rows in
   seconds. Emits 100%-precision hits with a citable clause.

4. **Layer 2 — peer statistics.** Cohort built by the fallback ladder to n ≥ 30; robust z via
   median/MAD; an expected-salary `HistGradientBoostingRegressor` whose *residual* is the signal and
   whose TreeSHAP values become per-feature attributions in SAR; CUSUM change-points per employee.

5. **Layer 3 — unsupervised.** Isolation Forest on the engineered matrix (`n_jobs=-1`), a tabular
   denoising autoencoder on CUDA whose per-feature reconstruction error is the attribution, and
   graph checks (DuckDB self-joins, `networkx` only on the small candidate subgraphs).

6. **Layer 4 — fusion.** Each layer's raw score → percentile → 0–100; weights and the rule-hit floor
   from `policy/fusion.yaml`; corroboration across layers escalates severity; thresholds auto-tuned
   to the alert budget. Builds and validates the evidence bundle, computes financial impact, and
   upserts into Postgres.

Every stage is independently re-runnable and cached. A fusion-weight change must not require
regenerating features.

## Request path — a reviewer opens an alert

```
Browser  ──GET /alerts?severity=CRITICAL&page=1──▶  API  ──▶ Postgres (indexed, paginated)
Browser  ──GET /alerts/ALT-000173──────────────▶  API  ──▶ Postgres (evidence JSONB, one row)
Browser  ──POST /explain/ALT-000173────────────▶  API  ──▶ Ollama ──▶ grounding check ──▶ cache
                                                            │ unavailable or check fails
                                                            └──▶ deterministic template
Browser  ──POST /alerts/ALT-000173/disposition─▶  API  ──▶ Postgres (+ audit row, always)
```

The LLM is **off the batch path entirely** and behind a cache. Scoring one million employees never
calls it. It runs only when a human is looking at one alert, and if it is unavailable the
deterministic explanation renders instead — the product does not degrade, it just gets less
conversational.

## Deployment

`docker compose up` brings up four services: `postgres`, `detector` (batch + `/score` endpoint),
`api`, and `web`. Ollama runs on the host (it needs the GPU) and is reached over the compose
network; the API treats it as optional and starts healthy without it.

Both Python services expose `/health` (process alive) and `/ready` (dependencies reachable).
Structured JSON logs carry a `correlation_id` threaded UI → API → detector, so one reviewer action
can be traced end to end.

The laptop and the GPU server run the same images. The only difference is the CUDA device count and
the scale flag; nothing in the code branches on environment.

## Why this shape

| Decision | Reason | Rejected alternative |
|---|---|---|
| Rules before ML | A reviewer can act on a cited policy clause; they cannot act on an anomaly score. Family A is deterministic, so ML there would only add error. | ML-first, rules as a filter |
| Two stores | Analytical scans and interactive reads have irreconcilable access patterns. | Everything in Postgres; everything in Parquet |
| Batch + cached alerts | Data changes once per run. Re-scoring per request would be slow and non-reproducible. | Score on read |
| Pre-aggregated map tables | 24 frames × 180 sites must animate. Client-side aggregation of 1M rows cannot. | Aggregate in the browser |
| Long-format allowances | 26 codes today, more tomorrow. A new code must not be a schema migration. | Wide table, one column per code |
| Policy as YAML, read by both generator and detector | An entitlement cannot mean one thing when generating and another when detecting. | Duplicated constants in each service |
| Local LLM, optional, cached | Explanation quality is a nice-to-have; availability is not. Also keeps HR data on the machine. | Cloud LLM on the critical path |
| Bundled GeoJSON, no tiles | The app must work air-gapped. Region polygons plus site coordinates are enough. | Mapbox / OSM tiles |

## Scale characteristics

| Stage | 10k | 1M target |
|---|---|---|
| datagen | seconds | < 10 min |
| build_features | < 60 s (phase-3 gate) | < 10 min |
| L1 rules | < 1 s | seconds |
| L2 + L3 | seconds | a few minutes |
| Full batch | < 1 min | **< 15 min, < 12 GB peak RAM** |

The memory ceiling is the binding constraint, not the CPU. Polars lazy frames, DuckDB streaming,
100k row-groups and never materialising the full employee × period join are what keep it under
12 GB — see `docs/PLAN.md` §11.
