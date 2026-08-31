# SPEC — `services/datagen`

**Self-contained build spec.** A session building this service should need only this file,
`CLAUDE.md`, and the previous phase's handoff. Reach for `docs/DATA_DICTIONARY.md` when you need an
exact column list and `docs/ANOMALY_CATALOG.md` when you need injection detail — those two are the
contracts this service must satisfy.

## What this service is

A deterministic CLI that generates a synthetic Saudi/energy-sector HR and payroll dataset with
labelled anomalies, written as partitioned Parquet under `data/raw/scale=<n>/`.

It runs in **two passes**, split across two build phases:

| Pass | Phase | Produces |
|---|---|---|
| 1 — clean population | **1** | All dimensions and facts, **policy-compliant, zero violations** |
| 2 — injection | **2** | The 34 anomaly codes + confounders, `labels_anomaly`, `labels_confounder` |

The two-pass split is the reason ground truth is exact. Pass 1 must produce a population where every
paid allowance satisfies its eligibility clause; pass 2 then breaks specific clauses on purpose and
records precisely what it broke. If pass 1 leaks a violation, that violation is an unlabelled
anomaly and every recall figure downstream is wrong. **This is the phase-1 gate.**

## Scope of phase 1 (this build)

Everything in pass 1. Do not inject anomalies. Do not write `labels_anomaly`. The injection
framework may be stubbed with a module docstring saying phase 2 owns it, but no injector logic.

## CLI

```
python -m datagen generate --scale {10k|100k|1m} --seed INT [--out DIR] [--periods INT]
                           [--reference-date YYYY-MM-DD] [--no-noise]
python -m datagen validate --scale {10k|100k|1m}    # re-run integrity checks on existing output
python -m datagen summary  --scale {10k|100k|1m}    # print row counts + distributions, no data dump
```

Also reachable as `python tasks.py datagen --scale 10k --seed 42`, which is the documented path.

| Flag | Default | Meaning |
|---|---|---|
| `--scale` | `10k` | 10,000 / 100,000 / 1,000,000 employees |
| `--seed` | `42` | Controls **everything**. Same seed ⇒ byte-identical output. |
| `--out` | `data/raw` | Root of the lake |
| `--periods` | `24` | Months of history |
| `--reference-date` | `2026-08-31` | "Today" for the run. Never `datetime.now()` — that would break determinism. |
| `--no-noise` | off | Skip realism noise. Debugging only; never used for a gate run. |

Exit non-zero on any integrity failure. `summary` must print a compact table, never row data.

## Determinism model

Non-negotiable, and easy to get subtly wrong.

```python
root = np.random.SeedSequence(seed)
streams = {name: np.random.default_rng(s)
           for name, s in zip(TABLE_NAMES, root.spawn(len(TABLE_NAMES)))}
```

- **One independent stream per table**, spawned from a single root `SeedSequence`. Generating
  `dim_job` must not shift the numbers `employee_master` draws — otherwise adding a column anywhere
  silently changes the whole dataset.
- Within a table, chunk `k` draws from `streams[table].spawn(k)`, so **chunk size does not affect
  output**. A 10k run and a 1m run must produce identical rows for the first 10k employees.
- No `random`, no unseeded `numpy.random.*`, no `datetime.now()`, no `uuid4()`, no dict/set
  iteration order dependence, no unstable sort. Sort keys must be total.
- Faker (if used) gets its own seeded instance per stream. Do not use a module-level Faker.

A test asserts byte-identical Parquet across two runs with the same seed, and *different* output
with a different seed.

## Memory and I/O rules

- **Chunked at 100,000 rows.** Never hold 1M × 24 months in memory. At 1m scale
  `fact_payroll_monthly` is ~24M rows and must be written incrementally, period by period.
- Polars **lazy** frames; `.collect(streaming=True)` where a collect is unavoidable. DuckDB for
  joins and aggregates over the written Parquet.
- Never `pandas.read_parquet` on a bulk table.
- Parquet: ZSTD level 3, 100k row-groups, `use_pyarrow=True`. Facts partition by `period`.
- Target: 1m scale in **under 10 minutes, peak RAM under 12 GB**.

## Module layout

```
services/datagen/
  pyproject.toml
  datagen/
    __init__.py
    __main__.py        # CLI (argparse), wires everything
    config.py          # scale tiers, paths, ScaleConfig dataclass
    rng.py             # SeedSequence stream management — the determinism contract
    policy.py          # loads + validates policy/*.yaml, resolves class_defaults
    writer.py          # chunked Parquet writer, row-group control, manifest accumulation
    dimensions/
      site.py org_unit.py job.py grade.py allowance.py region.py calendar.py
    facts/
      employee.py      # employee_master — the widest and most consequential module
      payroll.py       # fact_payroll_monthly + fact_payroll_allowance
      assignment.py    # fact_assignment_history
      attendance.py    # fact_attendance_monthly
      banking.py       # fact_bank_account
      activity.py      # fact_system_activity_monthly
    entitlement.py     # evaluates policy/allowance_rules.yaml eligibility — THE shared core
    noise.py           # realism noise (missingness, casing, typos, late postings)
    names.py           # bilingual Saudi/Arab/expat name pools
    identifiers.py     # national_id, iqama, IBAN (MOD-97), badge — all check-digit valid
    integrity.py       # the validation suite behind `validate` and the phase-1 gate
    injection/         # PHASE 2 — empty package with a docstring in phase 1
  tests/
```

### `entitlement.py` is the most important module

It evaluates the eligibility clauses in `policy/allowance_rules.yaml` against a denormalised
employee row and returns the set of allowances that employee is entitled to, with amounts.

Pass 1 pays **exactly** this set. Phase 3's rule engine will evaluate the same clauses to detect
violations. Two implementations of the same policy is how injector/detector drift starts — so this
module must be written to be reusable by the detector, or the clause evaluation must be a shared
library from the start. Prefer the latter.

Amount resolution by `amount_basis`:

| Basis | Computation |
|---|---|
| `fixed` | `amount`, times `dependents` if `per_dependent` (capped at `max_dependents`) |
| `pct_of_base` | `base_salary × rate_pct / 100`, then `min(cap)` if a cap is set |
| `grade_table` | `grade_table[grade]` |
| `site_table` | `site_table["tier_" + str(site.hardship_tier)]`; a `0` means not payable |

Also enforce `mutual_exclusions` and the `grade_entitlements` gate from `policy/grade_bands.yaml`.
An allowance must pass **all three** — its own clause, the grade gate, and the exclusion set.

## Generation order

Dimensions first (they are small and everything joins to them), then employees, then the per-period
facts. Later steps read earlier output from Parquet rather than holding it in memory.

1. `dim_region`, `dim_site` ← `policy/sites.yaml` + `policy/geo/sa_regions.geojson`. Resolve each
   site's booleans from `class_defaults`, overridden by per-site keys.
2. `dim_calendar` — 24 periods ending at `--reference-date`'s month. Hijri mapping, Saudi public
   holidays, Ramadan windows.
3. `dim_grade` — materialise 20 × 3 from `policy/grade_bands.yaml` multipliers.
4. `dim_allowance` — 26 rows from `policy/allowance_rules.yaml`.
5. `dim_job` — ~1,200 codes across the 11 job families, with grade bands, education minimums,
   required certifications, `safety_critical`.
6. `dim_org_unit` — ~12,000 units, 5 levels, acyclic by construction (assign parents only from the
   level above). Each unit gets a unique `cost_center` and a `primary_site_id` weighted by the
   site's `headcount_weight`.
7. `employee_master` — see below.
8. `fact_assignment_history` — built from each employee's synthesised career, ending in their
   current state. Contiguous, non-overlapping intervals starting at `hire_date`.
9. `fact_bank_account` — IBAN history; most employees have one row, some have a change.
10. `fact_payroll_monthly` + `fact_payroll_allowance` — per period, chunked.
11. `fact_attendance_monthly` — per period, consistent with work pattern and the calendar.
12. `fact_system_activity_monthly` — per period, correlated with attendance.
13. `manifest.json`, then `integrity.py` runs.

`dim_org_unit.head_employee_id` is backfilled after step 7, since it references employees.

## `employee_master` — how to make it realistic

The exact column list is in `docs/DATA_DICTIONARY.md`. What matters here is that the distributions
are skewed the way a real workforce is, because a uniform population makes every anomaly trivially
separable and the evaluation meaningless.

- **Nationality mix** ≈ 62% `saudi`, 5% `gcc`, 33% `expat`, varying by site class — head office is
  more Saudi, drilling camps more expat. Saudization is a real constraint, and it makes
  `nationality_class` a genuine analytical dimension rather than noise.
- **Grade pyramid** from `grade_distribution_weights` in `policy/grade_bands.yaml`. Grade correlates
  with age, service years and education — a grade-18 22-year-old is an artefact, not a feature.
- **Tenure curve**: right-skewed, mode around 6–9 years, a long tail to 35.
- **Site assignment** weighted by `headcount_weight`, so Eastern Province dominates. This is what
  forces the map to normalise per 1,000 employees.
- **`base_salary`**: draw a position within the grade × nationality_class band using a beta
  distribution skewed toward the middle, then nudge by performance rating and service years. Every
  clean employee's salary lands strictly inside `[salary_min, salary_max]`.
- **Housing / transport**: correlate with site attributes. A drilling-camp worker is usually
  `company_camp_bachelor`, an HQ worker usually `allowance` or `own`. This correlation is what makes
  A05 and A06 non-trivial — the violation must be rare *and* plausible.
- **Work pattern**: rotation only where `site.rotation_supported`; shift mostly at plants and
  refineries; `remote`/`hybrid` mostly at offices.
- **`status`**: ~93% active, ~5% terminated during the window, ~1% on leave, ~1% suspended.
  Terminated employees stop being paid the month after `termination_date`, plus one legitimate
  `SEVERANCE` settlement month.
- **`manager_id`**: derived from the org hierarchy — a manager is in the same or a parent unit and
  is at least two grades higher. Must be acyclic; assert it.
- **Certifications**: safety-critical jobs get their required certifications, all valid in pass 1.

Then apply `entitlement.py` and set the 26 `has_<CODE>` flags plus `allowance_total_monthly` and
`allowance_ratio` from what it returns.

## `fact_payroll_monthly` — how the money is built

For each employee × period where the employee is active:

- `base_pay` = their `base_salary` at that period from `fact_assignment_history` (it changes when
  they are promoted or incremented — payroll must follow the history, not the current row).
- Allowances = `entitlement.py` evaluated **as at that period**, written as long rows to
  `fact_payroll_allowance` with the `eligibility_snapshot_json` frozen.
- `overtime_hours` — 0 for most; shift and field staff draw from a gamma, capped at the legal
  monthly maximum. `overtime_pay = hourly_rate × 1.5 × hours`.
- `bonus` — annual, paid in one period per year, scaled by performance rating.
- `retro_adjustment` — rare (≈1.5% of rows), small, signed.
- `gosi_employee` / `gosi_employer` — rates by `gosi_class`.
- `gross` and `net` computed per `docs/DATA_DICTIONARY.md` and **stored**, not derived on read.
- `cost_center` copied from the employee's org unit at that period.

Month-to-month must be *stable*: the same employee's net should not jitter randomly, because D06
detects change-points against the employee's own baseline and random jitter would drown it. Add only
small, structured variation (overtime, bonus month, occasional absence).

## Realism noise (`noise.py`)

Applied last, controlled by `--no-noise`. Record what was applied in `dq_flags` — these are data
quality issues, **not anomalies**, and must never appear in `labels_anomaly`.

| Noise | Rate | Detail |
|---|---|---|
| Missing optional field | 2–5% | Higher for `source_system = 'manual'` |
| Name casing / spacing | 3% | `name_en` only — `name_en_normalised` stays clean |
| Transliteration variant | 1.5% | e.g. Mohammed / Muhammad / Mohamed |
| Date typo | 0.3% | Transposed digits in non-key dates only |
| Late payroll posting | 1% | Row lands in the following period's `payroll_run_id` |

## `manifest.json`

Shape is fixed in `docs/DATA_DICTIONARY.md` §3. `policy_digest` must be a SHA-256 of each policy
file's bytes — if a policy changed but the data did not regenerate, downstream evaluation is against
stale ground truth and the eval harness needs to be able to say so.

## Integrity checks (`integrity.py`) — the phase-1 gate

`python tasks.py verify 1` runs these against the generated 10k dataset and prints a compact
pass/fail table. Every check is a hard failure.

**Schema**
- Every table's columns and types match `docs/DATA_DICTIONARY.md` exactly — names, order, dtypes.
- No unexpected columns, no missing columns.

**Row counts**
- Match `manifest.json` exactly, for every table.
- `fact_payroll_monthly` = one row per active employee per period they were active. No gaps, no
  duplicates.

**Referential integrity**
- Every FK resolves; zero orphans in any fact table.
- `fact_assignment_history` intervals are contiguous and non-overlapping per employee, starting at
  `hire_date`.
- `manager_id` graph is acyclic. Every `org_unit_id` chain terminates at a level-1 unit.

**Domain validity**
- Every IBAN passes MOD-97; every `national_id` / `iqama_no` passes its check digit.
- Every enum value is in its declared domain.
- Every site coordinate lies inside its region polygon.
- `dependents_in_kingdom ≤ dependents_count`; `days_worked + days_leave + absence_days ≤
  calendar_days`; `grade` within `dim_job` band; `base_salary` within the grade × class band.

**Arithmetic**
- `gross` and `net` reconcile with their components to the cent, on every row.
- `allowance_total` equals the sum of that employee-period's `fact_payroll_allowance` rows.

**Zero policy violations — the headline check**
- Evaluate every one of the 34 anomaly-code predicates against the clean dataset. **Every one must
  return zero rows.** Report the per-code count in the gate table so a leak is visible by code, not
  just as a total.

**Determinism**
- Regenerate a 1,000-employee slice with the same seed and assert byte-identical Parquet.

**Git hygiene**
- `git status --porcelain` shows nothing under `data/` after a full 10k generation.

## Tests

`services/datagen/tests/` — pytest, must run in under 60 seconds at 1k scale:

- `test_determinism.py` — same seed identical, different seed different, chunk size irrelevant.
- `test_entitlement.py` — one case per allowance code: entitled pays, ineligible does not, amounts
  match each `amount_basis`, exclusions hold.
- `test_identifiers.py` — IBAN MOD-97 and national-id check digits, both directions.
- `test_integrity.py` — the full suite at 1k scale.
- `test_distributions.py` — nationality mix, grade pyramid and site skew within tolerance of target.

## Deliberate non-goals for phase 1

- No anomaly injection, no `labels_anomaly` (phase 2).
- No features, no detection, no scoring (phase 3+).
- No Postgres. datagen writes Parquet and nothing else.
- No 1m-scale tuning — correctness first at 10k; phase 7 owns scale-up. Do not sacrifice a clear
  implementation for speed the gate does not yet ask for.
