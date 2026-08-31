# PHASE 1 — datagen pass 1: the clean, policy-compliant population

**Status**: PASSED   **Date**: 2026-08-31   **Tag**: `phase-01`

Pass 1 only. No anomaly injection, no `labels_anomaly` — phase 2 owns those. The headline result is
that all **34 anomaly-code predicates return zero rows** against the generated 10k dataset, so the
ground truth phase 2 writes will be exact.

## What was built

**`policycore/`** (new, repo root — shared with the phase-3 rule engine)
- `clauses.py` — the eligibility grammar (`<field> <op> <literal>`, dotted fields, `in`/`not in`)
  parsed once into `Predicate`/`ClauseSet`. A NULL field never satisfies a clause.
- `packs.py` — loads and validates the six YAML packs, resolves `class_defaults` inheritance and the
  grade-band multipliers, SHA-256 digests every file. Fails loudly on an inconsistent pack.
- `entitlement.py` — `resolve()`: the one answer to "what is this person entitled to", enforcing the
  clause, the grade gate and `mutual_exclusions`.

**`services/datagen/datagen/`**
- `config.py` — `ScaleConfig`, lake paths, period arithmetic, `TABLE_NAMES`, `CHUNK_ROWS = 100_000`.
- `rng.py` — one `SeedSequence` stream per table, one child per field per chunk keyed by a stable
  `blake2b` hash of the field name. `weighted_index`/`weighted_choice` are prefix-stable.
- `schemas.py` — `docs/DATA_DICTIONARY.md` as 14 Arrow schemas; the writer casts to these and the
  gate compares against them.
- `writer.py` — Parquet writer (ZSTD-3, 100k row-groups, pyarrow). Money crosses into
  DECIMAL(12,2) here via a hand-built decimal128 buffer; everything upstream is int64 halalas.
- `policy.py` — datagen's view of a pack: sampling weights, band bounds, education ordinals.
- `entitlement.py` — feature-row assembly, the memoised resolver (24 periods collapse to a handful
  of distinct cache keys), and the allowance-load repair ladder.
- `identifiers.py` — check-digit-valid national ID / iqama (Luhn), IBAN (MOD-97), badge, employee id.
- `names.py` — bilingual Saudi/GCC/expat name pools plus the transliteration table.
- `noise.py` — missingness, name casing, transliteration variants, date typos, late postings. Every
  effect recorded in `dq_flags`; nothing that an anomaly predicate reads is ever touched.
- `dimensions/` — `region`, `site`, `calendar` (tabular Hijri, Saudi holidays, Ramadan windows),
  `grade`, `allowance`, `job`, `org_unit`.
- `facts/` — `employee` (population pass + wide rows), `assignment` (career synthesis), `banking`,
  `attendance`, `payroll`, `activity`.
- `pipeline.py` — generation order and the chunk loop. `__main__.py` — the CLI.
- `integrity.py` — the validation suite: schema, row counts, referential integrity, domain rules,
  arithmetic, the 34 predicates, determinism, git hygiene.
- `injection/` — empty package with a docstring saying phase 2 owns it.

**Policy** — `payroll.yaml` and `population.yaml` created (see Decisions); `grade_bands.yaml` and
`allowance_rules.yaml` corrected.

**Tests** — `services/datagen/tests/`: determinism, entitlement (one case per allowance code),
identifiers, integrity, distributions. **90 passed, 1 skipped in ~25s** at 1k scale.

**Root** — `tasks.py` gains `verify 1` and a real `datagen` verb; `conftest.py` puts both services
on the path for a bare checkout.

## Public interfaces added

```python
# policycore — the phase-3 rule engine imports these
from policycore.packs import PolicyPack, Site, GradeBand, Allowance, money
pack = PolicyPack.load("policy")
pack.sites, pack.sites_by_id, pack.grade_bands[(grade, klass)], pack.allowances[code]
pack.grade_entitlements[grade], pack.exclusions, pack.allowance_load, pack.band_policy
pack.digest              # {"sites.yaml": "sha256:...", ...} -> manifest.policy_digest

from policycore.entitlement import resolve, eligible_codes, apply_exclusions, total
resolve(pack, row, include_one_off=False) -> list[Entitlement]   # .code .amount .amount_basis .snapshot
from policycore.clauses import Predicate, ClauseSet              # .evaluate(row), .failing(row)

# datagen
from datagen.entitlement import feature_row, EntitlementResolver, to_sar, snapshot_json
feature_row(employee_mapping, site, job_safety_critical) -> dict   # flat: `site.x`, `job.x`
                                                                   # base_salary crosses cents -> SAR here
from datagen.config import ScaleConfig, CHUNK_ROWS, TABLE_NAMES, period_add, period_diff
ScaleConfig.build(scale, seed, population, out=, periods=, reference_date=, employees=, noise=)
from datagen.pipeline import generate;      generate(cfg, policy) -> RunResult(manifest, seconds)
from datagen.integrity import run, connect, anomaly_predicates, summarise
run(cfg, policy, include_determinism=True, include_git=True) -> Report(checks=[Check(name, ok, detail)])
connect(cfg)                       # DuckDB with one view per table + an `asat` as-at view
anomaly_predicates(cfg, policy)    # {code: (label, sql)} — all 34, each must return 0
```

```
python -m datagen generate --scale {10k|100k|1m} --seed INT [--out DIR] [--periods INT]
                           [--reference-date YYYY-MM-DD] [--no-noise] [--employees INT]
python -m datagen validate --scale 10k [--skip-determinism]
python -m datagen summary  --scale 10k
python tasks.py datagen --scale 10k --seed 42 [--command generate|validate|summary]
python tasks.py verify 1                      # generates the 10k lake if absent, then gates it
```

Lake layout: `data/raw/scale=<n>/<table>/part-NNNN.parquet`, facts under `period=<YYYYMM>/`.
New policy keys: `allowance_load.clean_population_ratio_max` (0.88), all of `payroll.yaml` and
`population.yaml`.

## Verify output

```
Phase 1 gate — datagen clean population (10k)
-----------------------------------------------------------------------------------
  ok    schema matches dictionary                          14 tables
  ok    row counts match manifest                          2,228,124 rows
  ok    employee count matches manifest                    10,000 employees
  ok    payroll one row per active period                  no duplicates, no gaps
  ok    no orphan foreign keys                             14 relationships
  ok    assignment intervals contiguous                    no gaps or overlaps
  ok    org chains reach level 1                           every unit rooted
  ok    enum values in domain                              19 columns
  ok    IBAN check digits (MOD-97)                         10,831 accounts
  ok    national id / iqama check digits                   10,000 identifiers
  ok    site coordinates inside region                     180 sites
  ok    domain rules hold                                  10 rules
  ok    gross and net reconcile                            to the cent
  ok    allowance_total matches child rows                 every employee-period
  ok    no zero or negative allowance rows                 all positive
  ok    policy digest current                              6 packs
  ok    A01 remote-site allowance at an ineligible site    0
  ok    A02 hardship allowance at a tier-0 site            0
  ok    A03 offshore allowance onshore                     0
  ok    A04 family assistance with no dependents           0
  ok    A05 housing allowance while company-housed         0
  ok    A06 transport allowance on a company bus           0
  ok    A07 amount outside the policy table                0
  ok    A08 grade outside the job band                     0
  ok    A09 nationality-restricted benefit misapplied      0
  ok    A10 rotation allowance without a rotation pattern  0
  ok    A11 qualification below the job minimum            0
  ok    A12 time-limited allowance beyond its maximum      0
  ok    B01 salary above the band maximum                  0
  ok    B02 salary below the band minimum                  0
  ok    B03 allowance load above the hard ceiling          0
  ok    B04 salary jump with no assignment record          0
  ok    B05 overtime beyond base pay or the legal maximum  0
  ok    B06 top-decile bonus on a bottom rating            0
  ok    B07 increments above the policy frequency          0
  ok    C01 IBAN shared across employees                   0
  ok    C02 duplicate national id or iqama                 0
  ok    C03 ghost employee                                 0
  ok    C04 terminated employee still on payroll           0
  ok    C05 self-approval or a manager cycle               0
  ok    C06 near-duplicate identity                        0
  ok    C07 active payroll with an expired iqama           0
  ok    C08 payroll charged to a foreign cost centre       0
  ok    D01 promotion velocity outlier                     0
  ok    D02 repeated retroactive adjustments               0
  ok    D03 leave and overtime in the same period          0
  ok    D04 attendance beyond the physical maximum         0
  ok    D05 allowance step after a manager change          0
  ok    D06 unexplained personal change-point              0
  ok    D07 section-wide allowance drift                   0
  ok    same seed, identical bytes                         106 files
  ok    different seed, different data                     seed changes output
  ok    no data/ in git status                             lake invisible to git
-----------------------------------------------------------------------------------
PASS — phase 1
```

10k generation: **27.4 s, 26 MB on disk**, periods 202409–202608.

| Table | Rows | | Table | Rows |
|---|---:|---|---|---:|
| `dim_region` | 13 | | `employee_master` | 10,000 |
| `dim_site` | 180 | | `fact_assignment_history` | 100,146 |
| `dim_calendar` | 24 | | `fact_bank_account` | 10,831 |
| `dim_grade` | 60 | | `fact_payroll_monthly` | 234,267 |
| `dim_allowance` | 26 | | `fact_payroll_allowance` | 1,402,616 |
| `dim_job` | 1,143 | | `fact_attendance_monthly` | 233,769 |
| `dim_org_unit` | 1,280 | | `fact_system_activity_monthly` | 233,769 |

Distributions (`python tasks.py datagen --command summary --scale 10k`):

```
nationality mix        saudi 62.7, expat 32.4, gcc 4.9
top regions            SA-04 4600, SA-02 1103, SA-01 1100, SA-03 907, SA-09 649
grade p50/p90          8.0 / 13.0
median salary          15970 SAR
allowance ratio p50/max 0.401 / 0.88
status mix             active 92.9, terminated 5.2, suspended 1.1, on_leave 0.8
```

## Decisions made

1. **Clause evaluation is a shared library at the repo root, not a datagen module.** The spec said
   "prefer the latter"; `policycore/` is that library and `datagen/entitlement.py` is the adapter
   (feature rows, memoisation, repair). Spec module layout updated.
2. **Two policy packs created.** `policy/payroll.yaml` holds GOSI rates and the contributory
   ceiling, the overtime multiplier and legal monthly maximum, the bonus month and its
   rate-by-rating table, retro/loan rates and the settlement window — the detector reads the same
   numbers back for B05, B06, C04 and D02. `policy/population.yaml` holds every distribution
   parameter. Both were literals-in-Python otherwise, which CLAUDE.md forbids. Spec updated with a
   pack table; both are in `policy_digest`.
3. **Fixed a real policy bug**: `grade_entitlements` listed neither `SAUDI_DEV_SCHEME` nor
   `SEVERANCE` in *any* band, making both unpayable at every grade — A09 needs the first (a
   nationality restriction in both directions) and the legitimate final-settlement month needs the
   second. Both added to all five bands. Also `grade_distribution_weights` was invalid YAML (a
   comma-separated block mapping); phase 0 never parsed that file, so it had never been caught.
4. **`one_off` allowances are excluded from monthly entitlement.** `SEVERANCE`'s clause
   (`status == 'terminated'`) stays true for every month after somebody leaves, so resolving it
   monthly pays a full salary every period — which is exactly C04. Payroll asks for it explicitly,
   once, in the settlement month. `resolve(..., include_one_off=True)` is the escape hatch, and
   `employee_master.has_SEVERANCE` is therefore always false (noted in the dictionary).
5. **An allowance-load repair ladder, driven by the worst period of the career.** The flat tier-3
   site allowances (REMOTE_SITE 3,200 + HARDSHIP 3,000 + OFFSHORE 4,200 + ROTATION 1,800 …) are
   enormous against a junior band, and `SCHOOL_ASSIST` alone is 7,500 for three resident children.
   Without intervention ~13% of employee-periods breached the hard ceiling, and the clean set would
   have sat inside B03's own injection range. The ladder changes *who the employee is* — company
   housing instead of a housing allowance, family not resident, own transport, one language, not
   acting, a post that is not safety-critical, then a higher position inside their own salary band —
   and never withholds an entitlement, because withholding would put a policy breach in the clean
   set pointing the other way. New key `allowance_load.clean_population_ratio_max: 0.88`.
   **This was the expensive part of the phase**: the repair has to be evaluated against the worst
   *historical* period, not the current month, because service years, a recent relocation and the
   grade held at the time all move entitlement, and every one of those periods is a row in the lake.
6. **Careers are floored by the site.** A career cannot start below the grade its posting implies
   (`grade.hardship_min_grade`, `grade.site_class_min_grade`) — nobody works an offshore platform at
   grade 2, and a start grade under the floor put flat site allowances against a junior band.
7. **The job code follows the grade.** A promotion is a new post; a fixed job code across a career
   left 41,640 assignment rows outside their job's permitted band (A08). Jobs are picked within the
   same family *and the same `safety_critical` class*, so a promotion never silently changes
   entitlement via ON_CALL / SAFETY / CERT_PREMIUM.
8. **`months_since_site_change` counts from the last transfer that moved the site**, 999 when the
   employee has never been moved. Being hired is not a relocation; reading it as one would pay
   RELOCATION (3,500 SAR flat) to every new joiner. Site-changing transfers are restricted to
   grade ≥ 14 and to a destination of the same site class and hardship tier, so a transfer does not
   manufacture a D05/D06 change-point. Dictionary updated.
9. **Attendance is generated before payroll**, not after as the spec's numbered order had it —
   payroll deducts the unauthorised absence and pays the overtime recorded there. Spec updated.
10. **Determinism claim corrected in the spec.** Chunk size is a *constant* (100k), which is what
    makes chunk boundaries scale-invariant; field streams are keyed by a stable hash of the field
    name rather than by draw order; draws are prefix-stable. But fields that are a function of the
    whole population — `manager_id`, `spouse_employee_id`, `dim_org_unit.head_employee_id` — do
    legitimately differ between population sizes, so the spec's "a 10k run and a 1m run must produce
    identical rows for the first 10k employees" was more than is achievable and now says so.
11. **The gate's predicates are SQL, generated from the policy pack, not the Python that wrote the
    rows.** A07 recomputes every expected amount in DuckDB from `allowance_rules.yaml` via a
    generated `CASE`, joined to the as-at assignment state with an `ASOF JOIN`. A check that called
    the same resolver would only prove the resolver agrees with itself.
12. **Two statistical predicates were given honest clean-set readings.** D06 is measured on the
    *standing* part of net pay (base + allowances − GOSI − loan): overtime, the bonus month, a retro
    correction and an absence deduction are explained variation a reviewer can already account for.
    D07 compares a section against **its own earlier baseline over the employees present in both
    windows**, on allowance load as a share of base — a raw monthly total drifts whenever a section
    hires or loses somebody, which is turnover, not a scheme.
13. **Money is int64 halalas everywhere inside the generator** and only widens to DECIMAL(12,2) in
    the writer. Integer arithmetic is why `gross` and `net` reconcile on *every* row rather than
    almost every row. `feature_row()` is the one place the unit boundary is crossed.
14. **`dim_org_unit` scales with the tier** (~1,280 at 10k, ~4,200 at 100k, ~12,000 at 1m). The
    dictionary's flat "~12,000 rows" would have meant more units than employees at 10k. Dictionary
    updated.

No other deviations from `docs/specs/datagen.md`; every one above is reflected in the spec.

## Known gaps / deferred

1. **`manager_id` is constant across an employee's assignment history.** Pass 1 contains no
   manager-change events at all, so D05 ("allowance mix changing abruptly after a manager change")
   has nothing to sit against in the clean set. **Phase 2's D05 injector must create the manager
   change itself**, and should also consider planting benign manager changes so D05 has a
   precision denominator.
2. **`fact_bank_account.is_known_benign_share` is False on every row** and no IBAN is shared. Phase
   2 plants the spousal shares that make C01's precision measurable.
3. **Performance is 10k-shaped.** 10k takes 27 s; the per-period entitlement resolution is a Python
   loop and would be roughly 45 minutes at 1m. The memoisation cache and the chunked writer are in
   place; **phase 7 owns the scale-up** and will likely need the per-period resolution vectorised or
   pushed into DuckDB. The spec explicitly deferred this.
4. **No proration.** A hire month and a termination month are paid in full. Prorating would create a
   base-pay change with no assignment row, which is B04.
5. **`paid_flag` is `not payroll_hold_flag`** (~0.3% false). No other unpaid-but-posted rows exist.
6. **Arabic job titles and org-unit names are transliterated by hand** and would benefit from a
   native-speaker review, as with the site names from phase 0.
7. **Region GeoJSON is still the simplified phase-0 draft.** The gate re-verifies all 180 sites fall
   inside their own polygon, but it is not survey-accurate — replace before phase 11 ships the map.
8. **The schema gate compares written Parquet against `datagen/schemas.py`**, which is the
   dictionary transcribed by hand. It catches writer bugs, cast failures and column reordering; it
   cannot catch a transcription error in `schemas.py` itself. Re-read the dictionary alongside it if
   a column ever looks wrong.

## Start here (next session)

Read exactly these three files:

1. `CLAUDE.md`
2. `docs/specs/datagen.md`
3. `docs/handoff/PHASE_01.md` (this file)

First command to run:

```
python tasks.py verify 1
```

(regenerates the 10k lake if `data/` is empty, then confirms the clean population is still clean),
then build phase 2 — **pass 2**: the 34 injectors, `labels_anomaly`, and the seven confounder types
in `labels_confounder`. `docs/ANOMALY_CATALOG.md` is the contract; the `add-anomaly-rule` skill is
the per-code pattern.

The phase-2 gate does not exist yet — **writing it is part of phase 2**. It should assert the
inverse of this phase's headline check: every code injected at or above its floor of 5 instances,
every injected row labelled, confounders present and *unlabelled*, and the per-code predicate counts
now matching `manifest.injection.by_code` rather than zero.

## Contract doc changes

- **`docs/DATA_DICTIONARY.md`** — `dim_org_unit` row count qualified as per-tier with the employee
  placement rule; `months_since_site_change` semantics (999 sentinel, hire is not a site change);
  `has_SEVERANCE` always false because SEVERANCE is `one_off`; `manifest.json` gains
  `reference_date` and `noise`, and the note that `generated_at` is the only wall-clock value in a
  run and `policy_digest` covers all six packs.
- **`docs/specs/datagen.md`** — `--employees` flag; corrected determinism model; module layout with
  `policycore`, `pipeline.py` and `schemas.py`; `policycore` as the shared entitlement core plus the
  `one_off` rule; attendance before payroll in the generation order; the allowance-load repair
  ladder and `months_since_site_change` semantics under `employee_master`; a new "Policy packs this
  service reads" section.
- **`docs/ANOMALY_CATALOG.md`** — unchanged. All 34 codes are as specified.
- **`docs/EVIDENCE_CONTRACT.md`**, **`docs/API_CONTRACT.md`** — unchanged, not touched by this phase.
