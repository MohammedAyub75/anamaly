# PHASE 0 — contract docs, repo scaffold, policy packs, task runner

**Status**: PASSED   **Date**: 2026-08-31   **Tag**: `phase-00`

## Context

The repo's first commit (`7d90204`, "bootstrap repo with approved build plan") contained only
`.gitignore` and `docs/PLAN.md` — **phase 0 had never actually been executed**. This session
discovered that while attempting to start phase 1, and built phase 0 instead.

## What was built

**Root**
- `tasks.py` — cross-platform task runner. `verify <n>` gates plus placeholder verbs for
  `datagen`/`detect`/`eval`/`api`/`web` that exit with the phase that delivers them.
- `CLAUDE.md` — 115 lines. Conventions, hard rules, stack, commands, skills, phase discipline.
- `docker-compose.yml` — postgres + detector + api + web. Only `postgres` runs today; the rest sit
  behind the `full` profile until their phases land.
- `requirements.txt`, `.env.example`.

**Contract docs (`docs/`)**
- `PROJECT_BRIEF.md` — problem, users, goals, non-goals, success criteria, glossary.
- `ARCHITECTURE.md` — services, dataflow, the two-store split, request path, why-this-shape table.
- `DATA_DICTIONARY.md` — every table and column across 7 dimensions and 8 facts, plus `manifest.json`
  and the referential-integrity rules the phase-1 gate asserts.
- `ANOMALY_CATALOG.md` — **34 codes** with injection and detection logic side by side, injection
  rates, evidence fields, actions; plus the 7 planted confounder types.
- `EVIDENCE_CONTRACT.md` — the alert/evidence JSON schema v1 shared by detector, API, UI and LLM.
- `API_CONTRACT.md` — every endpoint, conventions, filters, the geo payload.
- `RUNBOOK.md`, `DESIGN_SYSTEM.md`, `LLM_PORTABILITY.md`, `MIGRATION.md`.
- `specs/datagen.md`, `specs/detector.md`, `specs/api.md`, `specs/web.md` — self-contained build specs.
- `handoff/INDEX.md` — the 15-phase status table.

**Policy (`policy/`)**
- `sites.yaml` — **180 sites across all 13 regions**, bilingual names, real coordinates, site class,
  hardship tier, headcount weight. Booleans resolve from `class_defaults` with per-site overrides.
- `geo/sa_regions.geojson` — 13 region polygons, bundled (no tile server, air-gapped).
- `grade_bands.yaml` — 20 grades × 3 nationality classes, grade pyramid weights, entitlement gates.
- `allowance_rules.yaml` — 26 allowance codes with eligibility clauses, amount bases, mutual
  exclusions.
- `fusion.yaml` — layer weights, severity bands, alert budget, suppression, cohort fallback ladder.
- `rules/A01_remote_site_allowance_at_ineligible_site.yaml` — the reference rule defining the
  Layer 1 file format.

**Skills (`.claude/skills/`)** — `add-anomaly-rule`, `regenerate-dataset`, `add-api-endpoint`,
`add-ui-view`, `run-eval`, `phase-handoff`.

**Scaffold** — `services/{datagen,detector,api}/` packages with READMEs, `web/`, `data/` subtree.

## Public interfaces added

```
python tasks.py verify <0..14>        # 0 implemented; 1-14 fail loudly as unimplemented
python tasks.py datagen --scale {10k|100k|1m} --seed INT
python tasks.py detect  --scale {10k|100k|1m} --run-id STR
python tasks.py eval    --scale {10k|100k|1m}
python tasks.py api | web
```

`tasks.py` internals reusable by later gates: `Gate(phase, title)` with `.check(name, ok, detail)`
and `.report() -> int`; `SAUDI_BBOX`, `REGION_CODES`, `SITE_CLASSES`; `_load_yaml`, `_git`,
`_missing`.

`policy/sites.yaml` resolution rule: a site inherits every boolean from `class_defaults[<class>]`;
a key written on the site overrides it. `hardship_tier` and `headcount_weight` are always explicit.

## Verify output

```
Phase 0 gate — contract docs, scaffold, policy, geo
-----------------------------------------------------------
  ok    contract docs              10/10 present
  ok    service specs              4/4 present
  ok    repo scaffold              root files present
  ok    claude skills              6/6 skills
  ok    service tree               directories present
  ok    policy packs               present
  ok    CLAUDE.md <= 150 lines     115 lines
  ok    sites.yaml regions         13/13 regions
  ok    sites.yaml unique ids      180 sites
  ok    sites.yaml schema          all keys present
  ok    sites.yaml region refs     resolved
  ok    sites.yaml class enum      11 classes used
  ok    sites.yaml hardship 0-3    in range
  ok    sites.yaml headcount>0     positive
  ok    sites.yaml coords in KSA   within bbox
  ok    sites.yaml class defaults  every class has defaults
  ok    every region populated     13/13 regions have sites
  ok    hardship/remote contrast   tiers=[0, 1, 2, 3], remote-eligible=90/180
  ok    GeoJSON FeatureCollection  type=FeatureCollection
  ok    GeoJSON 13 regions         13 features
  ok    GeoJSON geometry types     polygons
  ok    GeoJSON coords in KSA      within bbox
  ok    lake paths gitignored      10 probe paths ignored
  ok    no data/ in git status     lake invisible to git
-----------------------------------------------------------
PASS — phase 0
```

Site distribution (Eastern Province dominant by design, which is why every map metric defaults to
per-1,000 employees): EP 46, Riyadh 24, Makkah 20, Madinah 16, Jazan 12, Asir 10, Qassim 9,
Tabuk 9, Northern Borders 8, Al Jawf 8, Hail 7, Najran 6, Al Bahah 5.

Additionally verified out-of-gate: **all 180 sites fall inside their own region polygon**
(ray-casting point-in-polygon). The first polygon draft clipped 10 sites — Al Ula, Qurayyat, Arar
and Turaif are real region extremities — and the boundaries were corrected.

## Decisions made

1. **34 anomaly codes, not "~30".** `docs/PLAN.md` §2.4 says "~30"; the worked catalogue is
   A01–A12, B01–B07, C01–C08, D01–D07 = 34. The catalog is now the contract and says so.
2. **Injection-rate floor of 5 instances at any scale.** The plan's 0.01% rates would inject one
   employee at 10k, and recall measured on n=1 is noise. Every gate except phase 7 runs at 10k, so
   the floor is what makes those gates meaningful.
3. **`class_defaults` in `sites.yaml` rather than 180 × 16 explicit fields.** DRY and reviewable —
   the Policy Explorer screen renders resolved values, so reviewers never see the inheritance.
   Same approach for `grade_bands.yaml` (base band + per-class multiplier → 60 materialised rows).
4. **26 allowance codes, not 25.** The plan's list needed `SAUDI_DEV_SCHEME` as the counterpart to
   `EXPAT_PREMIUM` — A09 requires nationality restriction in *both* directions to be a real finding.
5. **`verify 0` is stdlib + PyYAML only**, so the gate runs before any environment is provisioned.
6. **The `git status` assertion is scoped to `data/`**, not the whole tree. The plan (§9.5) asserts a
   clean tree after a 10k generation; there is no generator in phase 0, so the gate asserts the real
   intent — that lake paths are ignored and nothing under `data/` is visible to git. The phase-1
   gate re-runs it after a genuine generation.
7. **Existing `.gitignore` kept as-is.** It already satisfied §9.5.
8. **A01 written as a rule file in phase 0** although the engine lands in phase 3 — the rule file
   format is a contract shared by the injector, the engine, the evidence bundle and the Policy
   Explorer, so it belongs with the other contracts.

No deviations from `docs/specs/*` — those specs were authored in this phase.

## Known gaps / deferred

1. **The region GeoJSON is simplified and hand-authored.** Low vertex counts, approximate borders,
   and a few neighbours overlap slightly (Al Bahah/Makkah, Qassim/Hail). It is a valid, self-contained
   choropleth base with correct region codes and all sites inside their polygons — but it is **not
   survey-accurate**. Replace with an accurate public-domain source (GADM level 1 or Natural Earth)
   **before phase 11 ships the map**. The file's own `note` property says this.
2. **`uv` is not installed** despite `docs/PLAN.md` §9.4 expecting it from phase 0. `requirements.txt`
   + venv works today; install `uv` before phase 1 if the faster resolver is wanted.
3. **Arabic site names are transliterated by hand** and would benefit from a native-speaker review
   before anything is shown to a real user.
4. **Phase-1..14 gates are stubs** that fail loudly. Each is written in the phase that owns it.
5. **No Dockerfiles yet** — the `detector`, `api` and `web` Compose services are behind the `full`
   profile and will not build until their phases land. `docker compose up postgres` works today.
6. **`policy/llm.yaml`** is referenced by `docs/LLM_PORTABILITY.md` but not created — phase 12 owns it.
7. **The `.claude/skills/` gate checks presence, not correctness.** Skills are prose checklists;
   nothing validates that they still match the code.

## Start here (next session)

Read exactly these three files:

1. `CLAUDE.md`
2. `docs/specs/datagen.md`
3. `docs/handoff/PHASE_00.md` (this file)

First command to run:

```
python tasks.py verify 0
```

(confirms the scaffold is intact), then build phase 1 — **pass 1 only**: the clean,
policy-compliant population. No anomaly injection, no `labels_anomaly`; phase 2 owns those.

The phase-1 gate is `python tasks.py verify 1`, and it does not exist yet — **writing it is part of
phase 1**. `docs/specs/datagen.md` §"Integrity checks" specifies exactly what it must assert; the
headline check is **zero policy violations in the clean set**, reported per anomaly code so a leak
is visible by code rather than as a total.

## Contract doc changes

All contract docs were created in this phase. Two now differ deliberately from `docs/PLAN.md`:

- `docs/ANOMALY_CATALOG.md` — 34 codes, where the plan said "~30".
- `docs/DATA_DICTIONARY.md` — 26 allowance codes, where the plan listed ~25.

The plan is the historical brief; the contract docs are authoritative from here.
