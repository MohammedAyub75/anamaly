# PHASE 6 — layer 4: fusion, severity banding, the evidence bundle, financial impact

**Status**: PASSED   **Date**: 2026-09-01   **Tag**: `phase-06`

353 findings across three layers are now **344 alerts in one ranked queue**: 5 CRITICAL, 50 HIGH,
211 MEDIUM, 78 WATCHLIST — exactly the budget `policy/fusion.yaml` sets at this scale. Every alert
carries a validated evidence bundle, a figure in SAR and at least one sentence saying why. CRITICAL
and HIGH are 100% precise, and no planted confounder reaches either.

The headline decision: **a severity band is capacity, not a threshold.** Threshold tuning alone
cannot hit a budget of five at 10k — the score is an integer 0–100 over a few hundred alerts, so its
top is a run of ties and moving a threshold by one point moves the CRITICAL count from three to
eight. The bands are filled from the top of the queue until the slots run out, bounded below by the
configured band floors, and the gate asserts that capacity **only ever breaks a tie**: each of the
three alerts it kept out of a full band scores exactly what the last one admitted scored.

The second decision: **an alert is one (employee, anomaly code)**, which is the grain
`EVIDENCE_CONTRACT.md` already implied through `alert_id` stability and `suppression.match_on`. That
collapses B06's two flagged bonus months into one case — 9 alerts fuse 18 findings.

## What was built

**`services/detector/detector/layers/l4_fusion.py`** — the whole layer. `percentile_ranks`
(`cume_dist`, never zero), `split_layers` (every finding under the `layer_weights` contributor that
produced it), the weighted blend with `rule_hit_floor` and the damped corroboration bonus,
`tune_bands` (capacity filling), suppression matching, and the orchestration that denormalises
employee display and the 24-month timeline in two DuckDB queries and validates every bundle.

**`services/detector/detector/evidence/`** — new package. `builder.py`: `build_bundle`, `validate`
(a compiled Draft 2020-12 validator that names the failing field), `fingerprint` (window + money +
evidence fields, never the score), and `AlertIdRegistry`, a persisted key→number map rather than a
hash folded into six digits, because two cases filed under one id is worse than a file on disk.
`schemas/evidence_v1.json`: `EVIDENCE_CONTRACT.md` made machine-checkable, `additionalProperties:
false` at the root.

**`policy/fusion.yaml`** — three changes. New `layer_contribution.ml_unsupervised_min_score: 90`
(the models score everybody, so without a floor they corroborate every alert and the bonus becomes a
constant). New `corroboration_text.ml_unsupervised`, the sentence a model with no anomaly code says
to a reviewer. `ranking.min_monthly_impact_to_alert` → **`min_cumulative_impact_to_alert`**, see
decisions.

**`policy/graph_ml.yaml`** — each of the five codes declares `layer: graph | ml_unsupervised`.
Layer 3 is the only layer that splits across two fusion contributors, and which one a code feeds is
config rather than a dict in Python.

**`detector/policy.py`** — the layer-4 accessors, and `code_layer`, which maps every code to its
contributor by reading `peer_stats.yaml` and `graph_ml.yaml` (layer-1 codes come from the rule pack,
so they are not restated).

**`detector/run.py`** — the `fusion` stage, cached on `features key + the three layers' keys +
layer4_digest + run_id`, writing `data/runs/run_id=<id>/alerts.parquet`. `layer4_digest` hashes the
fusion module, the bundle builder **and the schema**: a bundle that validated yesterday and fails
today is a different answer, not a stale one. `BUILT` now covers all five stages; nothing is
pending. Plus `read_ml_scores`, `read_dismissals`, `write_alerts`, `read_alerts`.

**`detector/eval/harness.py`** — `AlertSummary`, and `evaluate(..., l4=None)`. Precision is measured
**per band**, because a CRITICAL band that is two-thirds right is a worse product than a MEDIUM band
that is two-thirds right and one overall figure hides which you have. **`report.py`** section 4 is
now the fused queue rather than a per-rule severity count.

**`tasks.py`** — `verify 6` (32 checks). **Tests** — `services/detector/tests/test_l4.py`, 49 new;
`test_eval.py`'s report assertions follow the renamed section and now assert the queue.

## Public interfaces added

```python
from detector.layers.l4_fusion import run_fusion, L4Result, L4Error, ScoredAlert
from detector.layers.l4_fusion import BandTuning, tune_bands, band_of
from detector.layers.l4_fusion import percentile_ranks, split_layers, LAYERS, BAND_ORDER

run_fusion(con, cfg, policy, *, l1_hits, l2_hits, l3_hits, ml_scores=None,
           dismissals=None, registry_path=None, correlation_id=None, log=None) -> L4Result
  # .alerts .live .total .tuning .thresholds .by_severity .by_code .by_layer
  # .findings_in .dropped_low_impact .suppressed .corroborated .validated .seconds
ScoredAlert  # .alert_id .employee_id .anomaly_code .family .layer .severity .score
             # .layer_scores .contributing_layers .period_from .period_to .months_flagged
             # .financial_impact_{monthly,cumulative,confidence} .evidence_fingerprint
             # .suppressed .suppression_reason .findings .rank_in_band .evidence_json
BandTuning   # .thresholds .configured .counts .budget .tolerance .remainder .bands
             # .within_tolerance(band) .ok
tune_bands(scores, *, configured, budget, tolerance, remainder, consumes=None) -> BandTuning
  # `scores` in queue order (worst first); `.bands` is one band per input
band_of(score, thresholds, remainder) -> str      # the strongest band a score is *eligible* for
percentile_ranks(values) -> list[float]           # cume_dist as 0-100, never 0

from detector.evidence import build_bundle, validate, fingerprint, load_schema
from detector.evidence import AlertIdRegistry, EvidenceError, SCHEMA_PATH, SCHEMA_VERSION
validate(bundle) -> None                          # raises EvidenceError naming the field
fingerprint(employee_id, anomaly_code, findings) -> "sha256:…"
AlertIdRegistry(path).assign(keys) -> {key: "ALT-000173"};  .save();  .key(emp, code, print)

from detector.policy import DetectorPolicy
pol.layer_weights / .fusion_layers / .rule_hit_floor / .corroboration_bonus
pol.severity_bands / .alert_budget / .budget_tolerance / .remainder_disposition
pol.ranking / .min_cumulative_impact / .ml_contribution_floor / .suppression
pol.code_layer            # {anomaly_code: "rules"|"peer_stats"|"ml_unsupervised"|"graph"}
pol.corroboration_text(layer) -> str

from detector.run import run, layer4_digest, write_alerts, read_alerts, alert_rows
from detector.run import read_ml_scores, read_dismissals, ALERTS_FILE, DISMISSALS_FILE
from detector.run import ALERT_SCHEMA           # the 24 columns of alerts.parquet
run(cfg, pol, rs, stages="features,l1,l2,l3,fusion") -> RunResult   # .l4 .alerts_path

from detector.eval import harness
harness.evaluate(cfg, ruleset, l1, l2=None, l3=None, l4=None, ...) -> EvalReport  # + .alerts
harness.AlertSummary  # .alerts .findings .collapse .per_1000 .by_severity .budget
                      # .thresholds .within_budget .budget_ok .precision_by_band
                      # .impact_by_band .confounders_by_band .critical_confounders
                      # .dropped_low_impact .suppressed .corroborated .validated
```

```
python tasks.py detect --scale 10k [--stages features,l1,l2,l3,fusion]
python tasks.py eval   --scale 10k
python tasks.py verify 6
```

**`alerts.parquet`** — 24 columns: `alert_id`, `employee_id`, `anomaly_code`, `family`, `layer`,
`severity`, `score`, `rank_in_band`, the four `layer_score_*`, `contributing_layers`,
`period_from`/`period_to`/`months_flagged`, the three `financial_impact_*`, `evidence_fingerprint`,
`suppressed`, `suppression_reason`, `findings`, `evidence_json`. **The bundle travels in the row**
rather than as one JSON file per alert: phase 8 upserts the queue and its evidence in one pass, and
35,000 small files at 1m would be a directory nobody can copy.

**`data/runs/alert_ids.json`** — the alert-id registry, shared across runs and scales. Not
gitignored-sensitive but not committed either; a fresh clone mints ids from 1.

**`data/runs/dismissals.parquet`** — read if present, ignored if not. Phase 13 writes it. Columns
used: `employee_id`, `anomaly_code`, `evidence_fingerprint`, `disposition_id`, `runs_since`,
`cumulative_impact`.

## Verify output

```
Phase 6 gate — fusion, severity, evidence bundle
------------------------------------------------------------------------
  ok    every layer is weighted                 rules 1, peer_stats 0.45, ml_unsupervised 0.35, graph 0.6 -- a rule is a fact, the rest are opinions
  ok    every code reaches a layer              34/34 codes map to one of the four contributors
  ok    severity bands are ordered              CRITICAL 88 > HIGH 72 > MEDIUM 55, and the budget fills them from the top
  ok    budget scales from the 1m reference     5 CRITICAL and 50 HIGH at 10,000 employees, plus or minus 20%
  ok    one alert per employee and code         353 findings became 344 alerts (1.03 each) -- repeated windows of one finding are one case, not several
  ok    repeated windows collapse               9 alert(s) fuse more than one window, covering 18 findings
  ok    every layer reaches the queue           graph 23, ml_unsupervised 5, peer_stats 150, rules 166
  ok    a broken clause is never averaged away  166 alerts carry a rule hit, none below the floor of 78 -- a policy violation is a fact
  ok    score is 0-100                          344 alerts, 88 distinct scores
  ok    contributing layers match the scores    every non-zero entry in `layer_scores` is named in `contributing_layers`, and nothing else is
  ok    corroboration is priced                 60 alert(s) have a second layer behind them; the bonus is spent on the distance left to certainty, so agreement cannot manufacture a 100
  ok    severity agrees with score              every alert scores at or above the boundary its band was cut at, and the boundary travels in the bundle
  ok    capacity only ever breaks a tie         3 alert(s) were kept out of a full band, every one of them tied on score with the last one admitted
  ok    CRITICAL within the budget              5 against a budget of 5 (plus or minus 1), cut at score 99
  ok    HIGH within the budget                  50 against a budget of 50 (plus or minus 10), cut at score 88
  ok    every bundle validates                  344/344 against evidence_v1.json before writing -- an invalid bundle fails the run rather than the UI
  ok    the validator actually rejects          a bundle with its reasons removed is refused -- an unexplained alert is the one bug this product cannot ship
  ok    every alert says why                    413 reasons across 344 alerts, none empty
  ok    every serious alert carries a figure    55 CRITICAL and HIGH alerts, each with a monthly exposure in SAR
  ok    the timeline is the whole window        24 months, ascending, no gaps -- a month with no pay row is padded rather than missing
  ok    identifiers stay masked                 18 linked alerts quote the last four digits only
  ok    no ML jargon reaches the reviewer       344 bundles, their reasons and their actions, against 16 banned terms
  ok    an alert keeps its identity             a second pass over the same data reassigns no id and moves no score -- an alert id is what a case is filed under
  ok    a dismissal hides, never deletes        1 suppressed, 344 alerts still written -- a suppressed finding is filtered, not lost
  ok    a larger amount resurfaces              the reviewer accepted the amount they were shown; 25% more is a new finding
  ok    the top of the queue is right           100% precision at CRITICAL and 100% at HIGH -- these are the alerts somebody opens on Monday
  ok    no confounder reaches CRITICAL          0 planted look-alikes are alerted on at all, none of them CRITICAL
  ok    layers 1-3 have not regressed           A 100%, B 100%, C 100%, D 100% recall over 353 findings
  ok    every code reaches the queue            34/34 codes have at least one alert; 0 finding(s) fell below the SAR 150 money floor
  ok    alerts written                          alerts.parquet, 344 rows x 24 columns, the bundle travelling in the row
  ok    layer 4 is quick                        344 alerts and 344 validated bundles in 1.40s
  ok    eval report written                     docs/EVAL_REPORT.md, section 4 now the fused queue
------------------------------------------------------------------------
PASS — phase 6
```

`verify 0` (24/24), `verify 1` (54/54), `verify 2` (44/44), `verify 3` (34/34), `verify 4` (31/31)
and `verify 5` (30/30) all still pass against the regenerated lake. Test suites: **411 passed, 2
skipped** repo-wide. `ruff check .` clean.

Fusion at 10k: **1.2–1.4s for 344 alerts and 344 validated bundles.** The scoring is arithmetic over
a few hundred rows; the two DuckDB queries that denormalise employee display and the 24-month
timeline are the only work that grows with the population. Cumulative impact across the queue is
unchanged from the layer totals — fusion re-ranks money, it does not re-count it.

## Decisions made

1. **The band is capacity; the threshold is the bound.** `alert_budget` says how many cases a
   reviewer can work and `severity_bands` says how bad a record must be to qualify. Filling from the
   top until the slots run out hits 5 and 50 exactly. Pure threshold tuning cannot: with 344 alerts
   on a 0–100 integer scale, the candidate thresholds around the budget give counts of 3, 8, 11 —
   there is no threshold that yields five. The gate therefore asserts the invariant that makes this
   safe rather than arbitrary: **capacity only ever breaks a tie**, and it never promotes — a band
   with two qualifying records leaves three slots empty rather than reaching into WATCHLIST.
2. **`provenance.severity_thresholds` is in the bundle.** The budget moves the boundary every run,
   so "severity agrees with `policy/fusion.yaml`" is not checkable six months later. The score at
   each boundary travels with the alert, and the contract check became `score >=
   provenance.severity_thresholds[severity]` — which holds for all 344, in both this run and the
   verification pass.
3. **An alert is one (employee, anomaly code).** Fixed by the contract, not chosen: `alert_id` is
   stable for (employee, code, fingerprint) and suppression matches on the same three.
   `anomaly_codes` and `families` stay arrays so a future that fuses codes into one case does not
   need a schema change; today each carries one entry.
4. **Each layer is ranked in its own finding population, not against the workforce.** Their raw
   outputs are not comparable — a fact, a distance, a percentile — and ranking each among its own
   is the only blend that does not privilege whichever layer produces the largest numbers. Ranking
   them against all 10,000 employees would put every finding between 96 and 100 and destroy the
   spread the budget needs.
5. **`cume_dist`, not `percent_rank`.** A `layer_scores` entry of 0 means *this layer said nothing*,
   and the contract requires `contributing_layers` to agree with the non-zero entries. The weakest
   finding a layer produced still said something, so the scale must not reach zero.
6. **The weighted mean is over the contributing layers, not all four.** Dividing by the full weight
   sum would score a shared bank account draining SAR 1.3m at 25 out of 100 because three layers had
   nothing to say about it. The weights decide the blend when layers disagree; a lone layer's rank
   stands.
7. **The corroboration bonus is spent on the distance left to certainty**, `base + bonus ×
   (100 − base)/100`. `min(100, base + bonus)` flattened every corroborated alert above 94 onto 100
   and left a nine-way tie at the head of the queue. At the bottom of the scale this is still the
   addition the pack describes; at the top it is damped, and two layers agreeing can no longer
   manufacture certainty out of a merely high score.
8. **The ML layer contributes only above a floor.** The models score all 10,000 employees. Without
   `layer_contribution.ml_unsupervised_min_score: 90` they corroborate every alert ever raised and
   `corroboration_bonus` becomes a constant added to everything. With it, 60 of 344 alerts have a
   second layer behind them.
9. **`min_monthly_impact_to_alert` became `min_cumulative_impact_to_alert`.** The monthly form threw
   away five of B07's six findings and three of D01's seven — a SAR 60 allowance paid wrongly for 24
   months is a SAR 1,440 recovery case, not noise. Exposure over the window is what a reviewer
   recovers. A finding with no financial dimension at all (a qualification gap, a grade outside its
   band) is never filtered on money. At 10k the floor now drops nothing, which is the honest answer:
   at this scale there is no noise to filter, only true positives to lose.
10. **`monthly` is live exposure, `cumulative` is everything spent.** When findings fuse, only the
    windows reaching the alert's last month count towards `monthly`. Two separate one-month
    overpayments last year are SAR 0 a month going out now, and a queue ranked by burn must not read
    them as ongoing.
11. **The evidence bundle travels in `alerts.parquet`.** The spec said "alerts.parquet plus evidence
    JSON"; 35,000 small files at 1m is a directory nobody can copy, and phase 8 wants one pass. Spec
    updated.
12. **Alert ids come from a persisted registry, not a hash.** A 6-digit hash collides at 1m scale
    with certainty and at 344 alerts with 6% probability, and two cases filed under one id is worse
    than a small JSON file on disk. New keys are minted in sorted order, so two machines given the
    same data agree.
13. **`correlation_id` is `uuid5(run_id, policy_digest)`.** It has to span UI → API → detector, so a
    random value would mean a re-run of the same data quoting an id the other side cannot
    reconstruct. `scored_at` is the only genuinely non-deterministic field in a bundle.
14. **The gate asserts the validator rejects.** A bundle with its `reasons` removed must be refused.
    A schema nothing has ever seen fail is documentation, not a gate.
15. **`fusion.yaml` and `graph_ml.yaml` changed, so the 10k lake was regenerated** (36.3s, all 343
    labels and 90 confounders identical) and `verify 1`–`5` re-run. Same reasoning as phases 4 and
    5: `datagen`'s integrity check compares the whole digest map, so an alert scored under a
    superseded policy must be visibly stale rather than silently wrong.

## Known gaps / deferred

1. **Nothing is upserted into Postgres and there is no `agg_alerts_by_site_month`.** Step 8 of the
   spec's layer-4 list is split: the Parquet half is done, the database half is phases 7 and 8.
2. **`similar_cases` is "same code, nearest score", not "comparable evidence shape".** The contract
   asks for a comparable shape; comparing evidence shapes needs a distance over `fields`, which is a
   phase-10 concern when the UI actually renders the list.
3. **No employee at 10k carries two anomaly codes**, so cross-layer corroboration only ever happens
   through the models — all 60 corroborated alerts are `<source layer> + ml_unsupervised`. The
   three- and four-layer bonuses are implemented and unit-tested but have never fired on real data.
   A lake that plants two codes on one employee would exercise them.
4. **Suppression has no producer.** `dismissals.parquet` is read if present and the matching,
   expiry and resurfacing rules are all tested against synthetic dismissals; phase 13 writes the
   real thing.
5. **The evidence bundle is validated but not versioned in flight.** `schema_version` is a constant
   1 and nothing yet reads a bundle written under an older schema. The migration path is
   `docs/MIGRATION.md`'s problem when there is a second version.
6. **`timeline[].event` is the assignment change reason with its underscores removed.** It is a fact
   from the record rather than a sentence, and the contract's example (`"REMOTE_SITE allowance
   starts"`) is richer than what is produced. Allowance starts and stops are visible in the series
   but are not labelled.
7. **`python -m detector score` is still layer 1 only**, unchanged since phase 4. A what-if against
   a fused score would have to re-rank the whole population to mean anything.
8. **Runtime is 10k-shaped.** Fusion itself is arithmetic, but the bundle assembly builds 344 dicts
   and 344 JSON strings in Python; at 1m that is ~35,000 of each plus a 24-row timeline apiece, and
   the two denormalising queries return 840,000 rows. Phase 7 owns it. The stage cache means a
   weight change re-runs only this stage, which is the point.
9. **One unaccounted finding remains repo-wide**, unchanged since phase 4: B01's grade-5 IT employee
   at 11,100 against a cohort median of 9,645. It is a MEDIUM alert.

## Start here (next session)

Read exactly these three files:

1. `CLAUDE.md`
2. `docs/specs/detector.md`
3. `docs/handoff/PHASE_06.md` (this file)

First command to run:

```
python tasks.py verify 6
```

then build phase 7 — scale-up to 1M plus `agg_alerts_by_site_month`. Gate: a full 1M run under 15
minutes with peak RAM under 12 GB. Four things phase 7 will want to know.

**The stage cache is the tool.** Every stage keys on `features key + its own digest + run_id`, and
`fusion` additionally on the three layers' keys, so a scale-up run can be built one stage at a time
without repeating the expensive ones.

**The autoencoder and the bundle assembly are the two stages to watch.** Phase 5 flagged the first
(60 epochs × rows on CUDA). The second is new: bundle assembly is per-alert Python, and at 1m it is
~35,000 bundles each carrying a 24-month timeline. Both are `O(n)` and neither is vectorised.

**The alert budget at 1m is 500 CRITICAL and 5,000 HIGH**, and `cfg.scaled()` already does the
arithmetic. Capacity filling scales without change, and the tie-breaking that 10k needs matters
less at 1m — 35,000 alerts over 101 score levels leaves the boundary in a much less crowded place.

**`agg_alerts_by_site_month` reads `alerts.parquet`.** Every column it needs is there — severity,
score, period window, financial impact — except the site, which is in the bundle's
`employee_display` rather than a column. Either join `features_employee` or promote `site_id` and
`region_code` to columns; the map defaults to **alerts per 1,000 employees**, never raw counts
(CLAUDE.md), so the aggregate needs a site headcount denominator either way.

## Contract doc changes

- **`docs/EVIDENCE_CONTRACT.md`** — the alert grain stated explicitly (one employee, one code, and
  why the plural fields stay plural); where the bundle lives between the detector and Postgres; the
  `severity` rule rewritten for capacity-bounded banding; three new `provenance` rows
  (`severity_thresholds`, `evidence_fingerprint`, `correlation_id`) with the example updated to
  match; the schema path corrected to
  `services/detector/detector/evidence/schemas/evidence_v1.json`; the validation list extended with
  the timeline rule and with the requirement that the validator itself be shown to reject.
- **`docs/specs/detector.md`** — a new "What phase 6 actually wrote" section covering the alert
  grain, per-layer ranking, the contributing-weight mean, the damped bonus, capacity banding,
  validation, suppression, the two policy divergences and the runtime. Layer-4 step 8 amended to say
  the bundle travels in the row.
- **`docs/ANOMALY_CATALOG.md`** — unchanged. No detector's definition moved.
- **`docs/DATA_DICTIONARY.md`** — unchanged. `policy_digest` still covers nine packs; no new pack
  was added, only keys inside two existing ones.
- **`docs/API_CONTRACT.md`** — unchanged. Layer 4 adds no endpoint.
- **`docs/EVAL_REPORT.md`** — regenerated; section 4 is now the fused queue with precision, planted
  confounders and cumulative impact per band.
