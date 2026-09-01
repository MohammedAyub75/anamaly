# PHASE 7 — scale-up to 1M, and the map aggregate

**Status**: BLOCKED — `verify 7` is **26/28**   **Date**: 2026-09-01   **Tag**: none — a tag
marks a passed gate, and this one is red

The detector runs at a million employees. A 1m lake — 23.5 million payroll rows, 140.5 million
allowance rows, 33,340 planted anomalies — is generated, featurised, scored by all four layers,
fused into **33,355 alerts** (500 CRITICAL and 5,000 HIGH, the budget exactly), validated bundle by
bundle, and aggregated into the map's 24 monthly frames. Recall holds: **A 100%, B 100%, C 99.9%,
D 99.6%**, precision@100 100%, every one of the 34 codes still reaches the queue. **Peak memory is
8.38 GB against a 12 GB budget.**

**The two failing checks are one problem: the feature build takes 32 minutes of the 38-minute cold
run.** Everything downstream of it — all four layers, fusion, the aggregate — costs **5.8 minutes**
at 1m, comfortably inside the phase's fifteen. The wide employee × period table is 24 million rows
and 168 columns, it is rebuilt in full whenever the lake changes, and building it is 85% of the
run. What that needs is an incremental build (a month of new data should cost a month of work) or a
narrower table, and neither is a tuning change. The gate is red and this handoff is not pretending
otherwise.

## What was built

**`policy/runtime.yaml`** — a tenth YAML file, deliberately **outside**
`policycore.packs.POLICY_FILES` and therefore outside `policy_digest`. The rule that decides
membership: a digested pack changes what the system *says*, this one changes only what it *costs*,
and lowering a memory limit must never mean regenerating twenty-four million payroll rows. It
carries DuckDB's budget and spill directory, the batch's 15-minute / 12 GB target, the feature
build's rows-per-write, layer 4's bundle chunk size, the map aggregate's dials and pass 2's
working-copy budget.

**`policycore/runtime.py`** — loads it, for both services: the generator's pass 2 and the
detector's feature build are the two places a 1m run runs out of memory.

**`services/detector/detector/aggregate.py`** — `agg_alerts_by_site_month`, the map's pre-computed
frames, as one DuckDB statement. Grain (period, site, anomaly code); the site-month total is the
row whose `anomaly_code` is `'*'`, carrying the severity mix and the three worst codes; an alert
counts in every month of its window at the site the employee was at that month; the denominator is
that site's headcount that month.

**`detector/features/build.py`** — the four employee × period intermediates are written straight to
Parquet out of the query pipeline and read back as views instead of being held as temp tables, and
the two that are feature tables *are* that write. Each is written a few months at a time
(`features.rows_per_write`), because DuckDB's partitioned writer keeps a buffer per thread per
month. `statements` / `split_create` / `windows` / `stream` are the new machinery; the build
connection also sets `preserve_insertion_order = false`.

**`detector/features/sql/00_asat.sql`** — `period_index` is numbered over the whole calendar and the
month filter moved to the spine. **`04_graph.sql`** — the identity-cluster join is two equality
joins instead of `ON a = x OR a = y`, which DuckDB cannot hash.

**`detector/run.py`** — the `agg` stage (cached on the fusion key), `PeakMemory`, and
`record_profile` / `load_profiles` (`data/runs/runtime_profile.json`, one entry per scale tier,
merged per stage).

**`detector/config.py`** — `run_dir` is now `data/runs/scale=<n>/run_id=<id>/`.

**`detector/layers/l3_ml.py`** — the matrix is built in Arrow rather than through Python object
arrays; `_encode` uses Arrow's dictionary encoding with a sorted remap; `LevelColumn` keeps a
categorical column as codes plus levels; the autoencoder is fitted on `max_train_rows` sampled rows
and scores everyone in batches off the host; the isolation forest scores in blocks; attributions
are built for the top `attribution_max_rows` records.

**`detector/layers/l2_salary.py`** — TreeSHAP runs over the widest residuals only
(`attribution_max_rows`), and `_encode` is vectorised.

**`detector/layers/l4_fusion.py`** — bundles are assembled a chunk of alerts at a time, comparable
cases are resolved once per code rather than once per alert, and the employee list reaches DuckDB as
an Arrow table rather than one `INSERT` per employee.

**`detector/eval/{harness,report}.py`** — `EvalReport.profiles`, and section 5's per-scale-tier
table with each tier's peak memory.

**`services/datagen/datagen/injection/`** — pass 2 reads the lake through a **database file** with a
buffer budget instead of materialising every table in memory, and releases the working copies of
employees it did not edit between injectors. `AllowanceRow` is slotted. The 10k lake regenerates
**byte-identical, all 108 files**.

**`tasks.py`** — `verify 7` (28 checks). **Tests** — `services/detector/tests/test_scale.py` (16)
and `test_aggregate.py` (10).

## Public interfaces added

```python
from detector.aggregate import build, read, AGG_FILE, AGG_SCHEMA, TOTAL, AggregateResult
build(con, cfg, policy, *, alerts_path=None, log=None) -> AggregateResult
  # .rows .periods .sites .total_rows .alerts_in .seconds .path .by_period
read(cfg) -> polars.DataFrame

from detector.run import PeakMemory, record_profile, load_profiles, PROFILE_FILE
PeakMemory(interval)                       # context manager; .peak_gb, .available
record_profile(cfg, result, *, peak_rss_gb=None, employees=None) -> Path
load_profiles(runs_root) -> {scale: {...}}
run(cfg, pol, rs, stages="features,l1,l2,l3,fusion,agg")

from detector.features.build import statements, split_create, windows, stream, render
windows(cfg, rows_per_write) -> [[period, ...], ...]
render(block, policy, periods=None)        # `$period_filter` for a windowed write

from detector.policy import DetectorPolicy
pol.runtime / .runtime_digest
pol.duckdb_memory_limit / .duckdb_threads / .duckdb_temp_directory
pol.peak_rss_budget_gb / .target_minutes / .sample_interval_seconds
pol.rows_per_write / .bundle_chunk_alerts / .aggregate / .aggregate_top_codes

from policycore import runtime as runtime_pack
runtime_pack.load(root) -> dict ; runtime_pack.section(runtime, *names) -> dict

from detector.layers.l3_ml import LevelColumn          # codes + levels, indexable
from detector.eval import harness
harness.evaluate(..., profiles=load_profiles(runs_root))   # -> EvalReport.profiles

from datagen.injection import connect, workspace        # connect(cfg, runtime)
Context.release()                                       # drop untouched working copies
```

```
python tasks.py detect --scale 1m [--stages features,l1,l2,l3,fusion,agg]
python tasks.py verify 7
```

**`agg_alerts_by_site_month.parquet`** — 20 columns: `period`, `site_id`, `site_name_en`,
`site_class`, `region_code`, `latitude`, `longitude`, `anomaly_code`, `family`, `headcount`,
`alert_count`, `employee_count`, `critical_count`, `high_count`, `medium_count`, `watchlist_count`,
`alerts_per_1000`, `financial_exposure_monthly`, `financial_exposure_cumulative`, `top_codes`.

**`data/runs/runtime_profile.json`** — per tier: `employees`, `seconds` (this run's wall clock),
`stage_seconds_total` (a cold run), `stages`, `cached`, `rows`, `alerts`, `findings`,
`peak_rss_gb`, `lake_generated_at`, `policy_digest`.

## Verify output

```
Phase 7 gate — scale-up to 1m
---------------------------------------------------------------------------
  ok    runtime dials are not policy               9 packs decide what the detector says and are digested into every lake; runtime.yaml decides what it costs, so changing a budget does not invalidate 24m rows
  ok    the engine has a budget                    DuckDB held to 8.0GB, spilling to data/runs/_spill -- a join over 24m rows finishes slowly rather than being killed
  ok    the batch has a budget                     15 minutes and 12 GB, the figures docs/specs/detector.md sets for this phase
  ok    the caps do not bind at 10k                forest score batch 200,000, max_train_rows 150,000, model attributions 25,000, salary attributions 50,000 -- every one above the 10k population, so phases 3-6 still score exactly what they scored
  ok    the 1m lake is a million employees         1,000,000 employees over 24 months
  ok    the lake scales with the population        23,457,054 payroll rows (98% of 24,000,000 employee-months -- the rest are months before a hire) and 140,495,923 allowance rows
  ok    ground truth scales too                    33,340 anomalies across 34/34 codes, plus 9,000 planted look-alikes
  ok    pass 2 leaves no working copy behind       injection reads the lake through a database file rather than 40 GB of in-memory tables, and deletes it when it is done
  ok    the 1m batch has been run                  run 202608, 6 stages
  ok    the profile is of this lake                the run was measured against exactly this lake and this policy pack -- a budget met under other conditions is not evidence about these
  ok    every stage is accounted for               features, l1, l2, l3, fusion, agg; all measured in one pass
  FAIL  a full 1m run is under 15 minutes          38.1 min of stage time against a budget of 15; the slowest stage is features at 32.2 min
  ok    peak memory is under 12 GB                 8.38 GB peak resident set against a budget of 12, sampled while the batch ran
  FAIL  no stage grows worse than the population   4 stage(s) measured at both tiers; the worst is features at 230x for 100x the employees -- every stage is linear or better
  ok    the queue is the budget at 1m              500 CRITICAL against 500 and 5,000 HIGH against 5,000, plus or minus 20%
  ok    every code reaches the queue at 1m         34/34 codes have at least one alert among 33,355
  ok    an alert still keeps its identity          33,355 distinct alert ids over 33,355 alerts -- an id is what a case is filed under, so two cases may never share one
  ok    bundles still validate at 1m               250 bundles sampled across the queue, every one against evidence_v1.json
  ok    no ML jargon at 1m either                  250 sampled bundles against 16 banned terms
  ok    the map aggregate is written               agg_alerts_by_site_month.parquet, 83,975 rows x 20 columns -- the map never aggregates on request
  ok    one frame a month, one row a site          24 monthly frames over 180 sites; 4,320 site-month totals for as many site-months, so a frame is a filter not a group-by
  ok    a total is the sum of its codes            every site-month total equals the codes underneath it, so filtering the map by code cannot show more than the map itself
  ok    exposure is conserved                      SAR 2,066,457,710 across the frames against SAR 2,066,457,710 in the queue -- SAR 0 apart over 83,975 rounded rows, so a year of frames adds up to the recovery figure rather than a multiple of it
  ok    the map metric is a rate                   every row carries its site's headcount that month and alerts per 1,000 against it -- a raw count would draw a population map
  ok    every alerted site can be drawn            180 sites, each with coordinates inside the Kingdom and a region to roll up into
  ok    the map counts the queue and nothing else  432,010 site-months of alert equal the 432,010 months the 33,355 live alerts cover; 0 suppressed alert(s) are on nobody's map
  ok    a frame is a small payload                 the busiest month is 180 site rows, not 33,355 alerts -- which is what makes a 24-frame animation smooth
  ok    the aggregate is cheap                     1.3s to aggregate 33,355 alerts into 24 frames
  ok    the report profiles every tier             docs/EVAL_REPORT.md section 5 now carries one row per scale tier that has been run, with its peak memory
---------------------------------------------------------------------------
FAIL — phase 7
```

`verify 0`–`verify 6` all still pass. Test suites: **441 passed, 1 skipped** repo-wide.
`ruff check .` clean.

### The runtime profile

| Tier | Employees | features | l1 | l2 | l3 | fusion | agg | Cold total | Peak RSS |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `10k` | 10,000 | 8.4 | 0.7 | 6.9 | 7.5 | 1.3 | 0.2 | **24.9 s** | 2.08 GB |
| `100k` | 100,000 | 70.2 | 2.5 | 45.2 | 35.2 | 5.9 | 0.5 | **159.4 s** | 7.53 GB |
| `1m` | 1,000,000 | 1,934.7 | 6.2 | 207.8 | 88.2 | 45.5 | 1.3 | **2,283.7 s** | 8.38 GB |

**Detection at 1m — every layer, fusion and the map over a built feature store — is 349 s (5.8
min).** The feature build is 1,934.7 s (32.2 min) of the 38.1-minute cold total.

Eval at 1m: 34,335 findings → 33,355 alerts; family A 100% recall and 100% precision, B 100% / 99.6%,
C 99.9%, D 99.6%; precision@100 100%, precision@CRITICAL 98%; 33,355/33,355 bundles validated; 81
unaccounted findings (0.24%).

Generation at 1m: 7,076 s (1 h 58 m) at a 9.23 GB peak — see *Known gaps*.

## Decisions made

1. **The engineering dials are not a policy pack.** `policy/runtime.yaml` sits beside the nine
   digested packs and is deliberately not one of them. A digested pack changes what the detector
   *says*; this file changes what it *costs*. Making it digested would mean that lowering a memory
   limit invalidated a two-hour lake. Anything that does move a figure — `max_train_rows`,
   `attribution_max_rows` — stayed in `graph_ml.yaml` and `peer_stats.yaml` where the stage digests
   already cover it.
2. **Every cap was chosen so that it does not bind below 1m.** `max_train_rows` 150,000, model
   attributions 25,000, salary attributions 50,000, forest score batch 200,000: all above the 10k
   and (bar the attributions) the 100k population, so phases 3–6 score exactly what they scored and
   their reported numbers still stand. The gate asserts it rather than trusting it.
3. **The models are fitted on a sample and score everybody.** An autoencoder over HR records
   converges on the shape of a normal record, and 150,000 of them describe that shape as well as a
   million do — 37 optimiser steps an epoch instead of 245. The sample is drawn from a seeded
   generator and sorted, so two runs fit the same network on the same records, and a test asserts
   it. Layer 3 at 1m is 88 s.
4. **An explanation is built where there is something to explain.** TreeSHAP over a million records
   costs minutes and is read for a few hundred: the widest residuals are explained and the rest of
   the column is empty. A test asserts that with the cap binding, the records that keep an
   attribution are the ones with the widest gaps — which is where every finding comes from.
5. **The feature store is written out of the query pipeline, not out of memory.** The four
   employee × period intermediates were DuckDB temp tables; at 1m the wide one is 24 million rows
   and 168 columns, and the build died with an out-of-memory error before writing anything even
   though every join in it streams. Written straight to Parquet and read back as views, the peak is
   the pipeline rather than the result — and two of the four *are* the feature tables, so there is
   no second copy.
6. **And written a few months at a time.** DuckDB's partitioned writer keeps a buffer per thread per
   partition: 24 months × 32 threads is 768 open buffers of a 168-column table, which is its own
   out-of-memory error. `features.rows_per_write` groups the window into writes of about two million
   rows — one group at 10k and 100k, so those tiers run exactly the plan phases 3–6 measured, and
   twelve at 1m.
7. **`period_index` is numbered over the whole calendar, never over the group being written.** The
   first windowed 1m run produced **181,895 layer-1 findings against 15,610 planted ones**: numbered
   inside a group, `period_index` restarts at 1, the gaps-and-islands pass stops collapsing
   consecutive months, and one fourteen-month finding becomes fourteen. A test now asserts that a
   store written in groups is identical to the store written in one pass.
8. **A join in the feature build must be an equality.** `04_graph.sql` matched identity clusters
   with `ON d.identifier = e.national_id OR d.identifier = e.iqama_no`. DuckDB cannot hash an `OR`
   and fell back to a nested loop: 0.8 s at 10k, 96 s at 100k, hours at 1m. Two equality joins and a
   `UNION ALL` give the same answer in 0.95 s. **This is the shape of bug the 100k tier exists to
   catch** — it was invisible at 10k and fatal at 1m.
9. **Runs are partitioned by scale.** `data/runs/scale=<n>/run_id=<id>/`. The default run id is the
   last period of the window, the same string at every tier, so before this a 10k run and a 1m run
   of the same month wrote their alerts over each other — and the second looked like a successful
   run of the first.
10. **The profile merges per stage and keeps the high-water mark for memory.** A stage that was
    reused is recorded with the time it cost when it last really ran (which is what `RunResult`
    already carried), and `--stages agg` updates that stage and leaves the rest alone. Otherwise
    re-running one stage would erase the profile of the batch that produced its input, and a run
    whose stages were all cached — 0.6 s, 0.9 GB — would overwrite a measured 8.38 GB peak with a
    number that proves nothing.
11. **The map's grain is (period, site, anomaly code), with the site-month total stored as the
    `'*'` row.** The endpoint filters by family and code, which a site-month total cannot answer;
    the total row exists so the *default* frame is a filter rather than a group-by. It carries the
    severity mix and the three worst codes.
12. **An alert is on the map in every month of its window, at the site the employee was at that
    month** — an employee who transfers takes their exposure with them. Cumulative exposure is
    spread evenly over the window so a year of frames adds up to the recovery figure rather than a
    multiple of it, and monthly exposure belongs to the last month, the only one it is still going
    out in. Windows are clamped to the run's own window, and a month with no feature row falls back
    to the employee's current site: an alert-month that joined to nothing would quietly leave the
    map, and the gate asserts the totals across the frames equal the queue.
13. **Pass 2 reads the lake through a database file.** Injection materialised every table into an
    in-memory DuckDB — 7.4 GB at 100k, an extrapolated ~45 GB at 1m. It is now a database file with
    a buffer budget, deleted when pass 2 ends, and injectors release the working copies of employees
    they did not edit (they load about four candidates per victim). Only *untouched* employees are
    released, so nothing can change: an unedited employee re-reads from the lake identically, and
    the lake is not rewritten until every injector has finished. **The 10k lake regenerates
    byte-identical across all 108 Parquet files**, which is how that claim was checked.
14. **The gate reads a recorded profile rather than reproducing a 38-minute run**, and refuses one
    recorded against a different lake or policy pack. A gate that costs half an hour is a gate that
    gets skipped.

## Known gaps / deferred

1. **The 15-minute target is missed: 38.1 minutes cold.** The feature build is 32.2 of them.
   Two things would close it, neither a tuning change: (a) **an incremental feature build** — the
   store is rebuilt in full whenever the lake changes, and a monthly production run adds one month
   of data to twenty-four; per-partition cache keys would make that a month of work. (b) **a
   narrower wide table** — 168 columns × 24 million rows is 4 billion values, and perhaps forty of
   those columns are employee statics repeated 24 times each. Both change the phase-3 contract, so
   neither belongs in a scale-up session that must leave phases 3–6 scoring what they scored.
2. **`features` grows 230× for 100× the employees**, and `l2` 30×. Part of that is real
   super-linearity (spilling, and twelve grouped writes instead of one), part is that at 10k the
   fixed overheads dominate. Between 100k and 1m — the honest comparison — features grows 27× and
   layer 2 4.6×.
3. **Generation at 1m takes 1 h 58 m** against the datagen spec's ten-minute aspiration (memory is
   fine: 9.23 GB against 12). Pass 1 simulates a career per employee in Python at ~2.8 ms each and
   pass 2 mutates rows per victim; making that minutes means a vectorised generator, which would
   change the order every stream is drawn in and therefore change the dataset. `docs/specs/datagen.md`
   now records the measurement and says plainly that the figure is an aspiration, not a gate.
4. **81 unaccounted findings at 1m** (0.24% of 34,335) and **precision@CRITICAL is 98%** — 10 of the
   500 CRITICAL alerts are on employees with no label. At 10k the equivalent number was one. Worth
   a look in phase 13 when dispositions start landing; it is not a scale bug so much as the
   long tail of a statistical layer finally having enough population to show one.
5. **The aggregate is written to Parquet and nothing upserts it into Postgres.** That half of step 8
   is phase 8's, along with `alerts`, `employees_cache` and `runs`.
6. **`agg` has no `delta_vs_prev_period`.** `/analytics/geo` returns one; it is a subtraction over
   two frames and belongs in the endpoint, not in the stored table.
7. **The `_intermediate` scratch directory is removed on a successful build only.** A build killed
   mid-way leaves up to ~1 GB under `data/features/scale=<n>/_intermediate/`; it is a spill, and
   deleting it is always safe.
8. **`python -m detector score` is still layer 1 only**, unchanged since phase 4.

## Start here (next session)

Read exactly these three files:

1. `CLAUDE.md`
2. `docs/specs/api.md`
3. `docs/handoff/PHASE_07.md` (this file)

First command to run:

```
python tasks.py verify 7
```

(26/28, and the two red checks are the feature build's runtime — nothing phase 8 depends on). Then
build phase 8: the Postgres schema, the FastAPI backend, auth, audit and the geo endpoint. Four
things phase 8 will want to know.

**The queue and the map are both on disk and both final.**
`data/runs/scale=1m/run_id=202608/alerts.parquet` is 33,355 rows with the evidence bundle in the
row, and `agg_alerts_by_site_month.parquet` beside it is 83,975 rows — 24 frames × 180 sites, plus a
row per code per site-month. Phase 8 upserts both.

**The map endpoint's default frame is one filtered scan**: `WHERE period = ? AND anomaly_code = '*'`
gives 180 rows carrying `alerts_per_1000`, `headcount`, the severity mix and `top_codes`. A region
frame is `sum(alert_count) * 1000 / sum(headcount)` over its sites — never an average of rates.
`docs/API_CONTRACT.md` now states all of this.

**Nothing in the API may read the Parquet lake on a request path.** The 1m feature store is 7 GB
and a single scan of `features_period` is tens of seconds; `employees_cache` exists precisely so a
queue render never touches it.

**Every timing in this handoff is on one 32-thread laptop with 16 GB of RAM.** The 12 GB memory
budget is met with room; the fifteen-minute one is not, and the reason is one stage.

## Contract doc changes

- **`docs/API_CONTRACT.md`** — the `/analytics/geo` section now names the artefact the endpoint is
  served from, its grain, the `'*'` total-row convention, what is stored rather than derived, and
  how a region frame is summed.
- **`docs/specs/detector.md`** — a new "Scale-up to 1M (phase 7)" section covering the runtime pack,
  what made the run affordable, the windowed feature build and the `period_index` trap, the
  aggregate, scale-partitioned run directories and the runtime profile; every
  `data/runs/run_id=…` path updated to `data/runs/scale=<n>/run_id=…`.
- **`docs/specs/datagen.md`** — the memory-and-I/O section now carries the measured 1m generation
  time and says what a ten-minute generator would actually require.
- **`docs/EVIDENCE_CONTRACT.md`** — the alerts path updated for the scale-partitioned run directory.
- **`docs/RUNBOOK.md`** — pass 2's working copy under `data/_work/`, the new run directory layout,
  the runtime profile and the peak-RSS line the batch prints.
- **`docs/ANOMALY_CATALOG.md`**, **`docs/DATA_DICTIONARY.md`** — unchanged. No detector's definition
  moved and no lake table changed shape.
- **`docs/EVAL_REPORT.md`** — regenerated at 1m; section 5 now carries a per-scale-tier table with
  each tier's cold total and peak memory.
- **`.claude/skills/regenerate-dataset`**, **`.claude/skills/run-eval`** — the 1m figures and where
  the runtime profile lives.
