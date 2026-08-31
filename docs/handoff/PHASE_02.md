# PHASE 2 — datagen pass 2: anomaly injection and ground truth

**Status**: PASSED   **Date**: 2026-08-31   **Tag**: `phase-02`

Pass 2 only. The 34 anomaly codes are injected into the lake pass 1 wrote, recorded in
`labels_anomaly`, and the seven legitimate look-alikes are planted in `labels_confounder`. The
headline result is that **for every one of the 34 codes, the employees the injector broke are
exactly the employees that code's own predicate finds, and nothing the predicate finds is
unlabelled** — the injector and the detector cannot have drifted apart.

## What was built

**`policy/injection.yaml`** (new, the seventh pack) — every dial pass 2 turns: per-code rate,
severity, window and magnitude; the seven confounder types with the code each one confounds; the
three collision guards; and `unowned_allowance_codes`, the allowance codes no family-A rule
polices. Digested into `manifest.json` with the rest.

**`services/datagen/datagen/injection/`** (was an empty package)
- `model.py` — the edit set (`Edits`) and the two label row types. Pass 2 hands `apply.py`
  *complete replacement rows*, so the rewrite is a substitution rather than a second generator.
- `context.py` — the working set. Loads the rows an injector needs (batched, one query per table),
  rebuilds the as-at feature row from the lake, recomputes `allowance_total`/GOSI/`gross`/`net`
  exactly as `facts/payroll.py` did, and owns the guards.
- `common.py` — `fill()`, the select-then-*attempt* loop every injector uses, plus the payment
  helpers.
- `family_a.py` (A01–A12), `family_b.py` (B01–B07), `family_c.py` (C01–C08), `family_d.py`
  (D01–D07) — one function per code.
- `confounders.py` — the seven planters.
- `apply.py` — rewrites only the Parquet parts holding an edited row, through the same
  `build_table` the generator writes with.
- `__init__.py` — `inject()`: run order, the `employee_master` allowance-column refresh, and the
  two label tables.

**`schemas.py`** — `labels_anomaly` and `labels_confounder` as Arrow schemas (16 tables now).
**`config.py`** — both label tables in `TABLE_NAMES`; `ScaleConfig.inject`; a slice keeps sections a
plausible size (`count // 12` units, was `count // 4`).
**`writer.py`** — `money_cents()` (the inverse of `money_array`), `write_arrow()` shared with pass
2, and a manifest that takes the injection payload and recounted rows.
**`integrity.py`** — the 34 predicates now return the `(employee_id, period)` rows they find instead
of a count, and `unlabelled_count()` / `labelled_employees()` / `label_filter()` are built on that.
Four predicates were corrected (see Decisions).
**`pipeline.py`** — pass 2 runs after the chunk loop; `--no-inject` writes empty label tables.
**`tasks.py`** — `verify 2`.
**Tests** — `tests/test_injection.py` (34 × 2 parametrised code tests plus label, confounder and
arithmetic checks); `test_integrity.py` and `test_distributions.py` adapted to an injected lake.
**166 passed, 1 skipped of 167 collected, ~2 min** at 1k scale.

**Contract docs** — `ANOMALY_CATALOG.md` (6 entries + §5), `DATA_DICTIONARY.md`,
`docs/specs/datagen.md`.

## Public interfaces added

```python
from datagen.injection import inject, connect, InjectionResult
inject(cfg, policy, streams) -> InjectionResult   # .by_code .confounders
                                                  # .employees_with_anomaly .row_counts .seconds
InjectionResult.manifest_payload(target_rate) -> dict          # manifest["injection"]

from datagen.injection.context import Context, sar, params_json
ctx.target(code) / ctx.confounder_target(name)    # max(min_instances, round(rate * employees))
ctx.candidates(sql) -> list[str]                  # pool, already-claimed removed
ctx.feature_row(employee, period) -> dict         # as-at row the clauses read
ctx.policy_amount(code, employee, period) -> int  # cents the policy table gives
ctx.guard_step(employee, window, delta) -> bool   # D06 step / B03 ceiling / D05 proximity
ctx.ratio_ok(employee, periods, codes) -> bool    # B03 ceiling, every period
ctx.set_master / set_history / set_bank / set_allowances / add_allowance
ctx.set_payroll / add_payroll_row / set_attendance / set_activity
ctx.label(employee, code, window, impact, description, **params)
ctx.confound(employee, name, window, impact, description, **params)

from datagen.injection.common import fill, pay, pay_resolved, already_paid, site_rate
from datagen.injection.apply import apply_edits           # (cfg, edits) -> {table: rows}

# integrity — the predicates are now row-returning
from datagen.integrity import (anomaly_predicates, found_count, unlabelled_count,
                               labelled_employees, label_filter)
anomaly_predicates(cfg, policy)          # {code: (label, sql)}; sql yields (employee_id, period)
unlabelled_count(con, code, sql) -> int  # hits no label accounts for. MUST be 0.
labelled_employees(con, code, sql) -> int  # injected employees the predicate finds
```

```
python -m datagen generate --scale 10k --seed 42 [--no-inject]
python tasks.py datagen --scale 10k --seed 42      # writes the clean lake, then injects
python tasks.py verify 2
```

New tables: `data/raw/scale=10k/labels_anomaly/part-0000.parquet`, `labels_confounder/`.
New policy keys: all of `policy/injection.yaml`.

## Verify output

```
Phase 2 gate — anomaly injection + labels
-----------------------------------------------------------------------------------
  ok    every code injected at its floor                   34 codes, at least 5 each
  ok    injection rate in range                            3.43% of employees carry an anomaly (catalogue 2.75%, floors lift it)
  ok    labels resolve to employees                        343 label rows, 90 confounder rows, no orphans
  ok    label windows inside the run                       202409..202608
  ok    severities in domain                               CRITICAL, HIGH, MEDIUM
  ok    injection params reproducible                      343 parameter sets parse
  ok    label counts match manifest                        343 rows agree with manifest.injection
  ok    confounders planted                                7 types, 90 employees
  ok    confounders are unlabelled                         no confounder carries an anomaly label
  ok    A01 remote-site allowance at an ineligible site    22 injected, 22 found, 0 unlabelled, 339 rows
  ok    A02 hardship allowance at a tier-0 site            18 injected, 18 found, 0 unlabelled, 301 rows
  ok    A03 offshore allowance onshore                     5 injected, 5 found, 0 unlabelled, 94 rows
  ok    A04 family assistance with no dependents           15 injected, 15 found, 0 unlabelled, 215 rows
  ok    A05 housing allowance while company-housed         14 injected, 14 found, 0 unlabelled, 217 rows
  ok    A06 transport allowance on a company bus           16 injected, 16 found, 0 unlabelled, 295 rows
  ok    A07 amount outside the policy table                11 injected, 11 found, 0 unlabelled, 123 rows
  ok    A08 grade outside the job band                     7 injected, 7 found, 0 unlabelled, 7 rows
  ok    A09 nationality-restricted benefit misapplied      6 injected, 6 found, 0 unlabelled, 67 rows
  ok    A10 rotation allowance without a rotation pattern  8 injected, 8 found, 0 unlabelled, 130 rows
  ok    A11 qualification below the job minimum            6 injected, 6 found, 0 unlabelled, 6 rows
  ok    A12 time-limited allowance beyond its maximum      5 injected, 5 found, 0 unlabelled, 120 rows
  ok    B01 salary above the band maximum                  14 injected, 14 found, 0 unlabelled, 14 rows
  ok    B02 salary below the band minimum                  16 injected, 16 found, 0 unlabelled, 16 rows
  ok    B03 allowance load above the hard ceiling          12 injected, 12 found, 0 unlabelled, 304 rows
  ok    B04 salary jump with no assignment record          9 injected, 9 found, 0 unlabelled, 9 rows
  ok    B05 overtime beyond base pay or the legal maximum  10 injected, 10 found, 0 unlabelled, 18 rows
  ok    B06 top-decile bonus on a bottom rating            8 injected, 8 found, 0 unlabelled, 9 rows
  ok    B07 increments above the policy frequency          6 injected, 6 found, 0 unlabelled, 6 rows
  ok    C01 IBAN shared across employees                   6 injected, 6 found, 0 unlabelled, 12 rows
  ok    C02 duplicate national id or iqama                 6 injected, 6 found, 0 unlabelled, 6 rows
  ok    C03 ghost employee                                 5 injected, 5 found, 0 unlabelled, 5 rows
  ok    C04 terminated employee still on payroll           5 injected, 5 found, 0 unlabelled, 25 rows
  ok    C05 self-approval or a manager cycle               5 injected, 5 found, 0 unlabelled, 5 rows
  ok    C06 near-duplicate identity                        6 injected, 6 found, 0 unlabelled, 6 rows
  ok    C07 active payroll with an expired iqama           5 injected, 5 found, 0 unlabelled, 37 rows
  ok    C08 payroll charged to a foreign cost centre       5 injected, 5 found, 0 unlabelled, 48 rows
  ok    D01 promotion velocity outlier                     7 injected, 7 found, 0 unlabelled, 7 rows
  ok    D02 repeated retroactive adjustments               9 injected, 9 found, 0 unlabelled, 9 rows
  ok    D03 leave and overtime in the same period          10 injected, 10 found, 0 unlabelled, 10 rows
  ok    D04 attendance beyond the physical maximum         8 injected, 8 found, 0 unlabelled, 8 rows
  ok    D05 allowance step after a manager change          6 injected, 6 found, 0 unlabelled, 6 rows
  ok    D06 unexplained personal change-point              5 injected, 5 found, 0 unlabelled, 5 rows
  ok    D07 section-wide allowance drift                   47 injected, 47 found, 0 unlabelled, 47 rows
  ok    phase-1 integrity suite                            54/54 checks still pass
-----------------------------------------------------------------------------------
PASS — phase 2
```

`python tasks.py verify 1` also still passes, 54/54, against the same injected lake.

10k generation: **36 s total, of which injection is 7.9 s**; 26 MB on disk. 343 employees carry an
anomaly (3.43%), 90 carry a confounder (0.90%).

| Table | Rows | | Table | Rows |
|---|---:|---|---|---:|
| `employee_master` | 10,000 | | `fact_payroll_allowance` | 1,407,830 |
| `fact_assignment_history` | 100,167 | | `fact_attendance_monthly` | 233,769 |
| `fact_bank_account` | 10,831 | | `fact_system_activity_monthly` | 233,769 |
| `fact_payroll_monthly` | 234,292 | | `labels_anomaly` / `labels_confounder` | 343 / 90 |

## Decisions made

1. **Injection runs inside `generate()`, over the lake pass 1 just wrote.** One command produces the
   dataset the detector is built against, which is what the `regenerate-dataset` skill documents.
   Pass 2 never regenerates: it loads the rows it means to break, mutates them and rewrites the
   affected Parquet parts, so every anomaly is an auditable delta from a certified-clean population.
2. **The phase-1 headline check changed meaning, and phase 1 still passes.** It was "every predicate
   returns zero rows"; it is now "every predicate returns only rows that `labels_anomaly` or
   `labels_confounder` accounts for". On an uninjected lake the label tables are empty and it
   reduces to the original assertion exactly. This is the one change that let both gates live on the
   same lake, and it is a *stronger* statement than the old one: every violation present is one the
   generator wrote down. `docs/specs/datagen.md` and `DATA_DICTIONARY.md` updated.
3. **The predicates return rows, not counts.** A count could answer neither "is this unlabelled?"
   nor "does the predicate find every employee the injector broke?", and the second question is the
   whole phase-2 gate. Three domain rules — `salary_in_band`, `grade_in_job_band`, `attendance_days`
   — police exactly what B01/B02, A08 and D04 break, so they now defer to the ground truth for
   labelled employees and stay absolute for everyone else.
4. **Five catalogue entries were corrected rather than coded around.** Each is in
   `ANOMALY_CATALOG.md` now:
   - **B03** injects *above* `hard_ceiling_ratio`, not into 0.7–0.9. Phase 1 clamps the clean
     population at 0.88, so a 0.7–0.9 injection would sit inside the clean distribution and be
     indistinguishable from an ordinary offshore rotation worker. The catalogue number predated
     phase 1's calibration.
   - **A07** fires only where the recomputed amount is above zero. An allowance the employee has no
     claim to recomputes to zero, and that is an eligibility breach with its own code; counting it
     as A07 too would put two codes on one row and split its recall.
   - **C01** excludes a declared couple and excludes a pair sharing a date of birth and a
     near-identical name. The second is one person on the payroll twice, which is C06 — a different
     finding with a different remedy. Without this, every C06 injection was also an unlabelled C01.
   - **D06** requires base pay to be unchanged month over month. A salary that moved with no
     paperwork behind it is B04's finding; D06 hunts for the step with no visible cause at all.
   - **D05** injects the manager change itself. See gap 1.
5. **A08 moves the job code, not the grade.** Moving the grade would drag the salary band (B01/B02)
   and the `grade_entitlements` gate with it, so the injection would be three findings. A different
   post in the same family, same `safety_critical`, no higher education minimum, whose band excludes
   the grade held, is the same mismatch and touches nothing else.
6. **Family C reuses existing employees rather than manufacturing records.** A cloned employee would
   need a career, a payroll series and an activity history invented for it in pass 2 — a second
   generator. Making two real employees share an account or an identity gives two genuine pay
   streams, which is what the evidence panel has to show.
7. **D05/D06/D07 pay allowance codes that no family-A rule polices** (`unowned_allowance_codes`).
   Money appearing with nothing in the record to account for it is precisely those findings; paid as
   `REMOTE_SITE` the same money would be A01 and the ground truth would name the wrong code.
8. **The guards are the expensive part of this phase, and every one of them was earned.** Each was
   added after the gate caught an unlabelled collision: a D05 step that overshot into D06 (the stack
   builder now takes a `maximum`, because fixed-size codes overshoot a target by a fifth); a B02
   salary cut that raised the allowance ratio over B03's ceiling; a D07 member whose base pay rose
   in the last month, so a ceiling checked only at the final period passed and every earlier period
   breached; a `legit_salary_jump` whose SEVERANCE line was left at the old salary and so read as
   A07. **`ratio_ok` checks every period, not the last one** — that specific bug appeared twice.
9. **Injection order is part of the output.** Confounders run first (a married couple both on the
   payroll is genuinely rare), then D07 (the only code needing a whole intact section of nine),
   then A, B, C, D. Each injector skips employees an earlier one claimed, so the scarcest cases have
   to go first or they find nobody left. Changing this order changes the dataset.
10. **Victim sets are disjoint.** The catalogue expects ~10% of flagged employees to carry more than
    one code; pass 2 does not reproduce that, because overlapping codes make per-code recall
    ambiguous and a per-code recall table is the point of the eval report. The realised rate is
    3.43% against the catalogue's 2.75% headline, the difference being the floor of five lifting
    eleven codes that are rarer than 5-in-10,000.
11. **`employee_master`'s derived allowance columns are re-derived from what is paid.** B03 is
    detected on `allowance_ratio`; a stale one would hide the finding it exists to show.
12. **A slice keeps sections a plausible size.** `ScaleConfig` gave a 1,000-employee slice 250 org
    units — four people each. D07 is a finding about a whole section and had nothing to happen to.
    Now `count // 12`, so a slice section is twelve people against the 10k tier's eight. This only
    affects runs with `--employees`; the tier shapes are untouched.
13. **Pass 2's DuckDB view is materialised with an index on `employee_id`.** Every injector filters
    the fact tables by employee a few hundred times over, and against a Parquet view each of those
    is a fresh scan of the file: 128 s became 8 s. At 1m this will need revisiting with phase 7.

## Known gaps / deferred

1. **D05's precision denominator is thinner than the catalogue claims.** Phase 1 reported that 62%
   of employees carry an in-window manager change, and the catalogue took that as D05's denominator.
   In fact **every one of them comes with a promotion**, which D05 excludes by design — transfers
   cluster early in a career, as phase 1's own gap 1 warned. There are zero in-window manager
   changes at an unchanged grade in the clean population, which is why D05 now injects the manager
   change as well. The exclusion is well exercised; the step test is not. **Phase 4 should decide
   whether to plant legitimate no-promotion manager changes**, or accept that D05's precision is
   measured against a thin denominator.
2. **The predicates in `integrity.py` are the gate's reading of the catalogue, not the phase-3
   detector.** They are deliberately independent SQL, and phase 3 must build its rules from
   `ANOMALY_CATALOG.md` rather than importing them — but where the two disagree, one of them is
   wrong, and the eval report is where that will show up.
3. **No code is injected on top of another.** See decision 10. If phase 6's fusion needs
   multi-code employees to exercise severity escalation, phase 2 will need a controlled overlap set.
4. **Two confounders sit inside their rule's reach and are suppressed by an exclusion rather than by
   magnitude**: `spousal_shared_iban` (C01 excludes a declared couple) and `legit_final_settlement`
   (C04 excludes a SEVERANCE-only month). The test that asserts a confounder does not trip its own
   rule skips those two by name.
5. **Confounder counts are population-bound at small scales.** At 1k there are only two mutual
   spouse pairs, so `spousal_shared_iban` cannot reach the floor of five there. The gate asserts the
   floor at 10k, where every type reaches 12–16; the 1k test asserts presence only.
6. **A12 backdates `acting_role_since` and repays the whole window**, so an employee acting for 26
   months shows the allowance across all 24 periods. Realistic, but it means A12 carries no step and
   is invisible to any change-point detector — it is a duration finding only.
7. **`fact_bank_account.is_known_benign_share` is now true only on the planted spousal shares.**
   Phase 1's gap 2 is closed; the flag remains metadata for the eval harness and must never become a
   detector feature.
8. **Injection is 10k-shaped.** 8 s at 10k, but the working set is loaded per employee into Python
   dicts and `apply.py` rewrites whole Parquet parts. At 1m the label count rises to ~27,500 and the
   parts are 100k rows each; phase 7 owns it.
9. **`expected_monthly_impact` is the injector's own arithmetic**, not a recomputation from the
   lake. For A-family it is the allowance amount, for B-family the delta, for C03/C06 the monthly
   net. Phase 6's financial-impact estimate should be scored against it, not derived from it.

## Start here (next session)

Read exactly these three files:

1. `CLAUDE.md`
2. `docs/specs/detector.md`
3. `docs/handoff/PHASE_02.md` (this file)

First command to run:

```
python tasks.py verify 2
```

(regenerates the 10k lake with injection if `data/` is empty, then confirms every code is present
and every violation is accounted for), then build phase 3 — the feature build, the layer-1 rule
engine and the eval harness. `docs/ANOMALY_CATALOG.md` is the contract for what each rule must
detect; `policy/rules/A01_*.yaml` is the shape every rule file copies.

Two things phase 3 will want to know. The gate's SQL in `datagen/integrity.py` is a working,
independently-written statement of all 34 detection predicates — read it as a reference, but build
the rule engine from the catalogue, because a rule engine that imports the gate's SQL proves only
that the gate agrees with itself. And `labels_anomaly` carries `human_description` and
`injection_params_json` per instance, which is what the eval report should quote when a code shows
0% recall.

## Contract doc changes

- **`docs/ANOMALY_CATALOG.md`** — A07, A08, B03, C01, C06, D05, D06 and D07 injection/detection
  entries corrected as described in Decisions 4 and 5; D05's confounder note qualified with gap 1;
  §5 gains the `confounds_code` contract and the note that two confounders are suppressed by an
  exclusion rather than by magnitude.
- **`docs/DATA_DICTIONARY.md`** — `labels_confounder` given its real column list
  (`confounder_type` + `confounds_code`, no family or severity, because a confounder is legitimate);
  `policy_digest` now covers seven packs; `manifest.injection.by_code` documented as counting
  employees; the derived allowance flags documented as re-derived from what is paid after pass 2;
  §4's zero-violations line restated as zero *unlabelled* violations.
- **`docs/specs/datagen.md`** — scope of phase 2; `--no-inject`; the `injection/` module layout;
  `policy/injection.yaml` in the pack table (seven packs); a "Pass 2 — how injection works" section
  covering the edit model, the as-at feature row, the guards, selection and application; the
  integrity headline check restated; `test_injection.py` in the test list.
- **`docs/EVIDENCE_CONTRACT.md`**, **`docs/API_CONTRACT.md`** — unchanged, not touched by this phase.
