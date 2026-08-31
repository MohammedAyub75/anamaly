# Employee Entitlement & Payroll Anomaly Detection Platform

## Context

A large energy-sector organisation (~1,000,000 employees, Aramco-like) needs to find employees who are
receiving pay or benefits they are not entitled to — e.g. an employee based at Dhahran HQ drawing a
remote-site allowance, a salary far outside the peer band, a ghost employee, a terminated worker still
on payroll. Real HR/payroll data is not available, so the project starts by generating a realistic
synthetic dataset with deliberately injected, labelled anomalies, then builds a detection platform on
top of it.

The end users are **non-technical HR/audit reviewers**. An alert is worthless to them unless it says
*why* it fired, *what the evidence is*, and *what to do next*. Explainability is a first-class
requirement, not a nice-to-have.

Everything must run on one laptop (i9-14900HX, 24 cores, 16 GB RAM, RTX 5060 8 GB) as a batch process,
and lift unchanged to a GPU server later.

### Decisions locked during planning

| Area | Decision |
|---|---|
| Anomaly families | All four: entitlement/policy, compensation outliers, identity/payroll fraud, behavioural/temporal |
| Dataset shape | Relational: master + 24-month monthly ledger + reference/dimension tables |
| Domain realism | Deep Saudi/energy-sector (iqama, GOSI, Saudization, real site taxonomy, rotation, camps) |
| Scale | Tiered generator: `--scale 10k \| 100k \| 1m`, 24 months history |
| Detection | Layered hybrid: rules → peer-group stats → unsupervised ML → fusion + attribution |
| LLM | **Local only (Ollama)** on the RTX 5060, behind a swappable provider interface. Off the hot path |
| Execution | Batch scoring run writes cached alerts; API serves them; plus single-employee re-score endpoint |
| Stack | FastAPI (Python) + React/TypeScript/Vite/shadcn + Postgres + DuckDB/Parquet, Docker Compose |
| Workflow | Full triage queue with reviewer dispositions feeding back into scoring |
| Alert budget | ~500 critical / ~5,000 high per full run; remainder as filterable watchlist |
| Geography | Sites across **all 13 regions of the Kingdom**, with coordinates; UI includes a month-by-month anomaly map |
| Design | Enterprise Aramco-inspired theme (deep petrol blue / energy teal / sand neutrals), no trademarked assets |
| Docs | Architecture + data contracts, per-service build specs, CLAUDE.md + skills, LLM-portability pack |
| Build process | **One phase per Claude Code session**, session cleared between phases, each phase gated by an objective verify command and a handoff artifact (see §9) |

---

## 1. Repository layout

```
anomaly/
├─ CLAUDE.md                     # conventions every future session reads first
├─ docker-compose.yml
├─ tasks.py                      # cross-platform task runner: python tasks.py <verb>
                                 # (Windows-first: no GNU Make dependency. Verbs: datagen,
                                 #  detect, api, web, eval, verify <phase>)
├─ docs/                         # see §8 — the token-saving contract layer
├─ policy/                       # YAML policy packs (grade bands, allowance eligibility, sites)
│  ├─ sites.yaml
│  ├─ grade_bands.yaml
│  ├─ allowance_rules.yaml
│  └─ rules/*.yaml               # declarative detection rules (Layer 1)
├─ services/
│  ├─ datagen/                   # synthetic data generator (CLI)
│  ├─ detector/                  # AI service: features, 4 layers, fusion, eval, batch CLI + /score API
│  └─ api/                       # backend BFF: alerts, cases, employees, auth, exports
├─ web/                          # React + TS + Vite + shadcn/ui + TanStack Query/Table
├─ data/                         # Parquet lake + model artifacts (gitignored)
│  ├─ raw/scale=1m/…
│  ├─ features/
│  ├─ models/
│  └─ runs/run_id=…/alerts.parquet
└─ .claude/skills/               # project skills (see §8.3)
```

**Language/runtime**: Python 3.12 (uv or poetry), Polars + DuckDB for all bulk data work (never
`pandas.read_parquet` on the 1M/24M-row tables), scikit-learn + PyTorch (CUDA) for models,
Node 20 + Vite for the web app.

---

## 2. Synthetic data generation (`services/datagen`)

### 2.1 Design rules

- **Deterministic**: `--seed` controls everything; the same seed reproduces byte-identical output.
- **Chunked**: generate and write in row-groups of 100k; never hold 1M×24 months in memory.
- **Two-pass**: pass 1 writes the *clean, policy-compliant* population; pass 2 injects anomalies and
  writes `labels_anomaly`. This guarantees ground truth is exact and the "normal" baseline is genuinely normal.
- **Output**: partitioned Parquet under `data/raw/scale=<n>/`, plus a `manifest.json` (seed, scale,
  row counts, generator version, injection rates) used for reproducibility and for the eval harness.

### 2.2 Reference / dimension tables

| Table | Content |
|---|---|
| `dim_site` | **~180 sites covering all 13 administrative regions of Saudi Arabia** — see §2.2.1. Attributes: `site_id`, `site_name_en/ar`, `city`, `region_code`, `region_name`, `latitude`, `longitude`, `site_class` (hq/plant/refinery/offshore/drilling_camp/terminal/office/depot/training/medical), `hardship_tier` 0–3, `remote_allowance_eligible`, `offshore_eligible`, `camp_available`, `family_housing_available`, `rotation_supported`, `headcount_weight` |
| `dim_org_unit` | 4-level hierarchy: Business Line → Admin Area → Division → Department → Section. ~12k units, each with `cost_center`, `head_employee_id`, `primary_site_id` |
| `dim_job` | ~1,200 job codes across job families (Drilling, Reservoir, Process Ops, Maintenance, HSE, IT, Finance, HR, Procurement, Medical, Security). Each with `min_grade`, `max_grade`, `min_education`, `required_certifications`, `safety_critical` flag |
| `dim_grade` | Grades 1–20 with `salary_min/mid/max` **per nationality class** (Saudi national / GCC / expat), plus allowance entitlement matrix |
| `dim_allowance` | ~25 codes: HOUSING, TRANSPORT, REMOTE_SITE, HARDSHIP, OFFSHORE, ROTATION, SCHOOL_ASSIST, FAMILY, SHIFT, ON_CALL, CAR, FUEL, MOBILE, SAFETY, ACTING_ROLE, EXPAT_PREMIUM, RELOCATION, SEVERANCE… each with `eligibility_rule_id`, `amount_basis` (fixed / %-of-base / grade-table / site-table) |
| `dim_region` | The 13 administrative regions with centroid coordinates and a bundled GeoJSON boundary file — the join key for the UI map |
| `dim_calendar` | Gregorian + Hijri month mapping, Saudi public holidays, Ramadan windows (drives working-hour rules) |

#### 2.2.1 Kingdom-wide site coverage

Sites are distributed across all 13 regions so that geography is a genuine analytical dimension, not a
constant. Indicative spread (exact list generated into `policy/sites.yaml`):

| Region | Representative sites | Typical classes |
|---|---|---|
| Eastern Province | Dhahran HQ, Abqaiq, Ras Tanura, Jubail, Khobar, Dammam, Hofuf, Khurais, Shaybah, Manifa, Safaniya offshore, Berri, Qatif | hq, plant, refinery, offshore, terminal |
| Riyadh | Riyadh corporate office, Riyadh refinery, Kharj depot, Dawadmi field ops | office, refinery, depot |
| Makkah | Jeddah regional office, Jeddah terminal, Rabigh complex, Taif depot | office, terminal, plant |
| Madinah | Yanbu refinery, Yanbu NGL, Madinah office, Badr field | refinery, plant, office |
| Jazan | Jazan refinery & terminal, Jazan Economic City, Sabya depot | refinery, terminal, remote |
| Asir | Abha office, Khamis Mushait depot | office, depot |
| Najran, Jouf, Northern Borders, Tabuk, Hail, Qassim, Al-Bahah | Regional offices, exploration camps, pipeline pump stations, depots | office, drilling_camp, remote, depot |

Design consequences worth stating up front:
- `hardship_tier` and `remote_allowance_eligible` vary by site, so the same allowance is legitimate in
  Shaybah and a violation in Dhahran — this is exactly the discrimination the detector must learn.
- Headcount is deliberately skewed (Eastern Province dominant) so the map must normalise by headcount,
  not just show raw counts, or every heat map degenerates into a population map.
- Every anomaly label carries the employee's `work_site_id` and `region_code`, enabling
  region × month aggregation directly.

### 2.3 Fact tables

**`employee_master`** — 1 row per employee, ~95 columns. Key groups:

- *Identity*: `employee_id`, `badge_no`, `name_en`, `name_ar`, `gender`, `dob`, `nationality`,
  `nationality_class`, `national_id` (Saudi) / `iqama_no` (expat), `iqama_expiry`, `passport_expiry`
- *Personal*: `marital_status`, `dependents_count`, `dependents_in_kingdom`, `spouse_employed_internally`
- *Qualification*: `education_level`, `degree_field`, `institution`, `graduation_year`,
  `certifications[]` (with expiry), `languages[]`, `training_hours_ytd`
- *Employment*: `hire_date`, `service_years`, `employment_type` (direct/contractor/secondee/trainee),
  `contract_type`, `status` (active/terminated/suspended/on-leave), `termination_date`, `probation_end`
- *Position*: `grade`, `job_code`, `org_unit_id`, `manager_id`, `position_id`, `acting_role_flag`
- *Location*: `work_site_id`, `residence_city`, `work_pattern` (regular / rotation_28_28 / rotation_14_14 / shift / remote / hybrid), `housing_type` (company_camp_bachelor / company_family_housing / allowance / own), `transport_mode` (company_bus / allowance / own), `remote_work_approved_flag`
- *Compensation*: `base_salary`, `currency`, `pay_grade_step`, `last_increment_date`,
  `last_promotion_date`, `performance_rating[3y]`, `bonus_eligible`, `gosi_class`
- *Banking*: `bank_code`, `iban`, `iban_effective_from`, `payment_method`
- *Derived flags*: one boolean per active allowance code (`has_REMOTE_SITE`, `has_HARDSHIP`, …)

**`fact_payroll_monthly`** — employee × 24 months (~24M rows at 1m scale):
`employee_id`, `period` (YYYYMM), `base_pay`, `allowance_<CODE>` columns (or a long-format
`fact_payroll_allowance` child table — prefer **long format** for flexibility, wide view built in DuckDB),
`overtime_hours`, `overtime_pay`, `bonus`, `retro_adjustment`, `gosi_employee`, `gosi_employer`,
`loan_deduction`, `absence_deduction`, `gross`, `net`, `cost_center`, `payroll_run_id`, `paid_flag`.

**`fact_assignment_history`** — every grade/job/org/site change with effective dates and `change_reason`.
**`fact_attendance_monthly`** — `days_worked`, `days_leave`, `leave_type_breakdown`, `overtime_hours`, `absence_days`, `rotation_cycle_id`.
**`fact_bank_account`** — IBAN history with effective dating (enables shared-account detection over time).
**`fact_system_activity_monthly`** — light proxy signals: `badge_swipes`, `email_count`, `erp_logins` (fuels ghost-employee detection).
**`labels_anomaly`** — ground truth: `employee_id`, `anomaly_code`, `family`, `period_from`, `period_to`, `injected_severity`, `injection_params_json`, `human_description`.

### 2.4 Anomaly catalogue to inject

Target **~2.5% of employees carry ≥1 anomaly** (~25,000 at 1m scale), unevenly distributed across
codes (rare fraud codes at 0.01–0.05%, common entitlement drift at 0.3–0.8%). Every code gets its own
generator function and its own detector, documented side-by-side in `docs/ANOMALY_CATALOG.md`.

**Family A — Entitlement / policy violations** (deterministic ground truth)
- `A01` Remote-site allowance while posted to an HQ/office site *(the Dhahran example)*
- `A02` Hardship allowance at a `hardship_tier = 0` site
- `A03` Offshore allowance for an onshore assignment
- `A04` School/family assistance with `dependents_count = 0` or dependents not in kingdom
- `A05` Housing allowance **and** company-provided camp/family housing simultaneously
- `A06` Transport allowance **and** assigned company bus route
- `A07` Allowance amount outside the policy table for that grade/site combination
- `A08` Grade outside the permitted band for the assigned job code
- `A09` Nationality-restricted benefit paid to an ineligible class (expat on a national-only scheme, wrong GOSI class)
- `A10` Rotation allowance without a rotation work pattern
- `A11` Qualification/certification below the job's mandatory minimum, or expired certification in a safety-critical role
- `A12` Acting-role allowance running beyond the maximum permitted duration

**Family B — Compensation outliers vs peer group** (statistical)
- `B01` Base salary above P99 of the peer cohort
- `B02` Base salary below band minimum (under-payment — a real finding, not just fraud)
- `B03` Total allowances as a share of base far above cohort norm
- `B04` Mid-year salary jump beyond threshold with **no** corresponding `fact_assignment_history` record
- `B05` Overtime pay exceeding base pay, or overtime hours beyond legal maximum
- `B06` Bonus inconsistent with performance rating history
- `B07` Increment frequency far above policy (multiple increments within 12 months)

**Family C — Identity & payroll fraud**
- `C01` IBAN shared across unrelated employees
- `C02` Duplicate national ID / iqama number
- `C03` Ghost employee: paid every month, zero leave variance, zero badge/system activity, no assignment history
- `C04` Terminated employee still receiving payroll after `termination_date`
- `C05` Employee is their own approver, or a cycle exists in the manager hierarchy
- `C06` Near-duplicate identity (fuzzy name + same DOB + same IBAN/address)
- `C07` Active payroll with expired iqama / invalid work permit
- `C08` Payroll paid to a cost centre with no corresponding org assignment

**Family D — Behavioural / temporal drift**
- `D01` Promotion velocity outlier (e.g. 3 grades within 24 months)
- `D02` Repeated retroactive adjustments to the same employee
- `D03` Leave recorded and overtime claimed in the same period
- `D04` Attendance days exceeding calendar days, or hours beyond physical maximum
- `D05` Allowance mix changing abruptly right after a manager change
- `D06` Personal change-point: step change vs the employee's own 24-month baseline
- `D07` Cluster drift: an entire department/section deviating from the org norm (collusion signal)

### 2.5 Realism safeguards

Anomalies must not be trivially separable, or the evaluation is meaningless:
- Inject **confounders** — legitimate high earners (senior specialists), legitimate mid-year jumps
  *with* proper assignment records, legitimate shared IBANs (rare, e.g. spousal — flagged in metadata as
  known-benign to test false-positive suppression).
- Add realistic noise: missing values, inconsistent casing/spacing in names, late payroll postings,
  data-entry typos in dates.
- Skew distributions realistically: nationality mix, grade pyramid, tenure curve, site headcount.

---

## 3. Detection engine (`services/detector`) — the AI service

### 3.1 Feature build (DuckDB → Parquet feature store)

One `build_features` step produces `data/features/` from `data/raw/`:
- Employee-level static features (encoded categoricals, band positions, tenure, ratios)
- Peer-cohort keys and cohort aggregates (median, MAD, P01/P99, count)
- 24-month rollups per employee: mean/std/trend/max-jump for base, each allowance, overtime, net
- Graph-derived features: IBAN cluster size, national-ID cluster size, manager depth, cycle flag
- Rule-input columns (site attributes, housing type, dependents) joined and denormalised once

All of this stays in DuckDB SQL + Polars — target under 10 minutes for 1M×24 on 24 cores.

### 3.2 Layer 1 — Declarative policy rule engine

- Rules live in `policy/rules/*.yaml`: `id`, `family`, `severity`, `description_template`,
  `sql_predicate`, `evidence_fields`, `recommended_actions`, `regulatory_reference`.
- Executed as DuckDB SQL over the feature store — all rules over 1M rows in seconds.
- Emits **100%-precision** hits with a citable reason. This is what non-technical reviewers trust.
- Adding a new policy = adding a YAML file, no code change (backed by an `add-anomaly-rule` skill).

### 3.3 Layer 2 — Peer-group statistical baselines

- Cohort definition with **graceful fallback**: `grade × job_family × site_class × nationality_class ×
  service_band` → drop service_band → drop nationality_class → … until `n ≥ 30`. Cohort key used is
  recorded in the evidence so the reviewer sees "compared against 412 peers at grade 12, Process Ops, plant sites".
- Robust z-score via median/MAD (not mean/σ — outliers poison the mean), plus quantile position.
- Expected-salary model: `HistGradientBoostingRegressor` predicting base salary from legitimate
  drivers; the **residual** is the anomaly signal, and TreeSHAP on this model gives per-feature
  attribution in currency terms ("expected 18,400 SAR, actual 31,200 SAR; grade explains +2,100, site +900, unexplained +9,800").
- Temporal: rolling robust z + CUSUM change-point over each employee's 24-month series.

### 3.4 Layer 3 — Unsupervised ML (unknown-unknowns)

Run in parallel, each producing a normalised score:
1. **Isolation Forest** (scikit-learn, `n_jobs=-1`) on the engineered feature matrix — the workhorse; ~1M×150 features in a few minutes on 24 cores.
2. **Tabular denoising autoencoder** (PyTorch, CUDA on the RTX 5060): categorical embeddings +
   numeric branch, reconstruction error as the score, **per-feature reconstruction error as the
   attribution**. ~1M rows trains in minutes at batch 4096, fits comfortably in 8 GB VRAM.
3. **Graph checks** (DuckDB self-joins + `networkx` only on the small candidate subgraphs): shared
   IBAN/ID components, manager-hierarchy cycles, self-approval.
4. **Sequence autoencoder (optional, phase 2)**: GRU autoencoder over the 24-month payroll vector for
   temporal shape anomalies. Only build this if CUSUM proves insufficient on the eval set.

Explicitly **not** doing: a giant transformer/foundation model over tabular HR data. It costs far more
and loses to gradient boosting + IF on this data shape, and it cannot explain itself to an auditor.

### 3.5 Layer 4 — Fusion, severity banding, evidence

- Each layer's raw score → percentile rank within the population → 0–100.
- Fusion: rule hits dominate (a policy violation is a fact, not a probability); statistical and ML
  scores combine with configurable weights in `policy/fusion.yaml`. Multiple corroborating layers
  escalate severity.
- **Calibrated to the alert budget**: thresholds auto-tuned each run to yield ≈500 CRITICAL, ≈5,000
  HIGH, remainder MEDIUM/WATCHLIST. Budget is config, not hard-coded.
- **Evidence bundle** (JSON, persisted per alert) — the contract the UI and the LLM both consume:
  ```json
  { "alert_id", "employee_id", "anomaly_codes": ["A01"], "severity": "CRITICAL",
    "score": 94, "layer_scores": {...},
    "reasons": [{ "type":"rule", "rule_id":"A01", "text":"Remote-site allowance SAR 3,200/mo paid while
      posted to Dhahran HQ (site_class=hq, remote_allowance_eligible=false)", "since":"2024-03" }],
    "peer_context": { "cohort_key":"...", "cohort_n":412, "employee_value":31200,
      "cohort_median":18400, "percentile":99.4 },
    "feature_attributions": [{"feature":"allowance_REMOTE_SITE","contribution":0.41}, ...],
    "timeline": [...], "financial_impact": {"monthly":3200,"cumulative":38400,"currency":"SAR"},
    "recommended_actions": ["Suspend REMOTE_SITE allowance pending review",
                            "Request site-posting confirmation from Division HR",
                            "Raise recovery case for 12 months overpayment"],
    "similar_cases": ["ALT-0091","ALT-0233"] }
  ```
- **Financial impact estimate** on every alert — this is what makes the tool sellable and lets
  reviewers prioritise.
- Suppression: alerts matching a previously-dismissed disposition (same employee + code + unchanged
  evidence) are auto-suppressed and shown in a separate "previously dismissed" filter.

### 3.6 Batch runner

`python -m detector.run --scale 1m --run-id 2026-08` executes: features → L1 → L2 → L3 → fusion →
`data/runs/run_id=…/alerts.parquet` + evidence JSONB, then upserts into Postgres. Target: **full 1M
population under 15 minutes**. Progress logged; every stage independently re-runnable and cached.

Also exposes `POST /score/employee/{id}` for on-demand re-check and what-if.

---

## 4. Backend API (`services/api`, FastAPI)

- `GET /alerts` — paginated, filterable (severity, family, anomaly_code, org_unit, site, status,
  assignee, score range, financial impact), sortable, server-side. TanStack Table-friendly contract.
- `GET /alerts/{id}` — full evidence bundle
- `GET /employees/{id}/360` — profile, pay timeline, allowance history, peer comparison, all alerts
- `POST /alerts/{id}/disposition` — confirm / dismiss / escalate / request-info + note + reason code
- `GET /cases`, `POST /cases` — group alerts into an investigation case, assign, track
- `GET /runs`, `GET /runs/{id}/summary` — run comparison, new/resolved/worsened alerts
- `GET /analytics/*` — dashboard aggregates (by site, department, family, financial exposure trend)
- `POST /explain/{alert_id}` — proxies to the local LLM narrator (§5)
- `POST /exports` — CSV/XLSX/PDF for the current filter set
- Auth: JWT with roles `reviewer` / `investigator` / `admin`; row-level scoping by org unit.
- Full audit log on every disposition (who, when, what, previous value) — non-negotiable for HR/audit use.

**Postgres** holds alerts, evidence JSONB, cases, dispositions, users, audit. **DuckDB/Parquet** holds
the analytical lake. API never scans Parquet on a user request.

---

## 5. LLM narrator (local Ollama)

- Provider interface `LLMProvider.complete(prompt, system) -> str` with an `OllamaProvider`
  implementation (default model: `qwen2.5:7b-instruct` or `llama3.1:8b`, ~5 GB VRAM alongside the
  detector's models). Other providers (Anthropic/OpenAI/vLLM) implementable without touching callers.
- **Never on the batch path.** Called only when a reviewer opens an alert or asks a question. Results cached per alert.
- Three prompt templates, versioned files under `services/detector/prompts/`:
  1. `explain_alert` — turn the evidence bundle into a plain-English paragraph for a non-technical reviewer
  2. `suggest_actions` — expand the recommended-actions list with context
  3. `ask_about_alert` — grounded Q&A, evidence bundle only, explicit "I don't know" instruction
- **Grounding rule**: the LLM only rephrases the evidence bundle; it never computes, never invents
  numbers, and every figure it states must appear in the bundle. A post-check validates that numerals
  in the output exist in the input. Graceful degradation to the deterministic template if Ollama is
  unavailable — the product must be fully usable with the LLM off.

---

## 6. Frontend (`web/`, React + TS + Vite + shadcn/ui)

Designed for non-technical reviewers — plain language, no ML jargon anywhere in the UI.

1. **Overview dashboard** — alerts by severity, total financial exposure, top sites/departments,
   run-over-run trend, "what changed since last run".
2. **Triage queue** — the primary screen. Virtualised table, saved filter presets, bulk assign,
   severity chips, financial impact column, one-line plain-English reason inline.
3. **Alert detail** — the explainability screen:
   - "What we found" (plain English, LLM-narrated with deterministic fallback)
   - "Why we flagged it" — rule citation, peer comparison chart (employee vs cohort distribution),
     contribution bars for ML attribution
   - "Timeline" — 24-month pay/allowance chart with the anomaly window highlighted
   - "Financial impact" — monthly and cumulative
   - "What to do" — recommended action checklist
   - Disposition panel: Confirm / Dismiss (with reason) / Escalate / Request info + notes
4. **Employee 360** — full profile, entitlement matrix vs policy, all historical alerts.
5. **Case management** — grouped alerts, assignee, status, activity trail.
6. **Policy explorer** — read-only, non-technical view of every rule in `policy/` in plain language,
   so reviewers understand what the system checks.
7. **Geographic anomaly map** — see §6.1.
8. **Admin** — run history, threshold/budget config, model metrics, data-quality panel.

Accessibility, light/dark, exportable views, and an onboarding tour on first login.

### 6.1 Geographic anomaly map (month-wise)

A dedicated screen answering *"where in the Kingdom are anomalies concentrated, and how does that move
month to month?"*

- **Base map**: a Saudi Arabia GeoJSON (13 region polygons) **bundled in the repo** and rendered with
  `react-simple-maps` / `d3-geo`. No external tile server, no CDN — the app must work air-gapped, which
  rules out Mapbox/Google/OSM tiles. Region polygons + plotted site coordinates are sufficient and far
  lighter than raster tiles.
- **Two layers**:
  - *Region choropleth* — shaded by the selected metric.
  - *Site bubbles* — one bubble per site, radius by alert count, colour by severity mix; click to filter
    the triage queue to that site.
- **Metric selector** — raw alert count, **alerts per 1,000 employees** (the honest default, since
  Eastern Province headcount would otherwise dominate every view), financial exposure (SAR), or
  critical-only count.
- **Month control** — a timeline scrubber across the 24 months with a play/pause animation, so a
  reviewer literally watches hotspots emerge and fade. Selected month drives both layers.
- **Filters** — anomaly family, anomaly code, org unit, employment type; all shared with the triage queue.
- **Side panel** — ranked region/site table for the selected month, month-over-month delta arrows, and
  "top 3 anomaly codes driving this hotspot".
- **Backend**: `GET /analytics/geo?period=YYYYMM&metric=…&filters=…` served from a pre-aggregated
  `agg_alerts_by_site_month` table written during the batch run. The UI never aggregates 1M rows client-side;
  every map frame is a small pre-computed payload, which is what makes the animation smooth.

### 6.2 Design system — enterprise, Aramco-inspired

- **Palette** (tokens in `web/src/styles/theme.css`, light + dark): deep petrol blue as the primary
  surface/brand colour, an energy teal/green accent, warm sand and stone neutrals for backgrounds and
  cards, and a strict semantic set for severity — critical (deep red), high (amber), medium (blue),
  watchlist (slate). Severity colours are colour-blind-safe and always paired with a text label or icon,
  never colour alone.
- **Typography**: one clean corporate sans for Latin (Inter or IBM Plex Sans) with a proper Arabic
  companion (IBM Plex Sans Arabic / Noto Sans Arabic) since names are stored bilingually. Fonts
  self-hosted in the repo — no external font CDN.
- **Layout language**: dense but calm — persistent left navigation, sticky filter bar, generous table
  row height, card-based detail panels, restrained borders over heavy shadows. Data-first, decoration-last.
- **RTL-ready**: logical CSS properties throughout so an Arabic UI is a configuration change, not a rewrite.
- **Charts** follow the same token set, so the map, timeline, and peer-distribution charts read as one system.
- **Trademark note**: this is a *brand-inspired* enterprise theme. No Aramco logo, wordmark, or
  proprietary asset is used — a neutral placeholder mark sits in the header, swappable later by whoever
  owns the real brand assets.

### 6.3 Stability requirements

The application is meant to be enterprise-grade, so these are build requirements, not polish:
error boundaries around every route, skeleton/empty/error states for every data view, request
cancellation and retry with backoff in the query layer, optimistic-but-reconciled disposition updates,
server-side pagination everywhere (no unbounded fetches), `/health` and `/ready` endpoints on both
Python services, structured JSON logging with a correlation id spanning UI → API → detector, graceful
degradation when Ollama is down, and Playwright smoke tests over the critical paths (queue → detail →
disposition → map).

---

## 7. Evaluation harness

Because ground truth is injected, evaluation is exact:
- **Per-anomaly-code recall** — did we detect each of the 30 injected types? A code with 0% recall is a
  detector bug, and this table is the core development feedback loop.
- **Precision@100 / @1000 / @5000**, and precision within each severity band
- **False-positive analysis** on the planted confounders (legit high earners must *not* be critical)
- **Alert budget adherence** and score-distribution calibration
- **Runtime profile** per stage at each scale tier
- Report written to `docs/EVAL_REPORT.md` on every run, so quality is tracked over time.

---

## 8. Documentation, skills, and portability (built in Phase 0, before code)

### 8.1 Contract documents (`docs/`)
- `PROJECT_BRIEF.md` — vendor-neutral problem statement, goals, glossary, non-goals
- `ARCHITECTURE.md` — services, dataflow, sequence diagrams, deployment topology
- `DATA_DICTIONARY.md` — every table, column, type, domain, nullability, business meaning
- `ANOMALY_CATALOG.md` — the 30 codes: definition, injection logic, detection logic, expected signal,
  severity, evidence fields, recommended actions. **The single most important file in the repo.**
- `EVIDENCE_CONTRACT.md` — the alert/evidence JSON schema shared by detector, API, UI, and LLM
- `API_CONTRACT.md` — OpenAPI-derived endpoint reference
- `RUNBOOK.md` — how to generate data, run a batch, start services, troubleshoot
- `DESIGN_SYSTEM.md` — theme tokens, severity colour semantics, typography, layout and RTL rules (§6.2)
- `handoff/INDEX.md` + `handoff/PHASE_<n>.md` — the per-phase build artifacts (§9.2)

### 8.2 Per-service build specs (`docs/specs/`)
`datagen.md`, `detector.md`, `api.md`, `web.md` — each **self-contained**, so a fresh session (or a
different LLM) can build that one service reading only its spec + the contract docs. This is the main
token-saving mechanism: no session ever has to read the whole repo.

### 8.3 Claude Code assets
- `CLAUDE.md` — stack, conventions, directory map, "read `docs/specs/<service>.md` before touching a
  service", memory-efficiency rules (Polars/DuckDB not pandas), test commands
- `.claude/skills/`:
  - `add-anomaly-rule` — add a policy YAML rule + its injector + catalog entry + test, consistently
  - `regenerate-dataset` — correct flags, scale tiers, manifest/reproducibility checks
  - `add-api-endpoint` — router/schema/test/contract-doc pattern
  - `add-ui-view` — page/route/query-hook/shadcn component pattern
  - `run-eval` — execute the harness and interpret the per-code recall table
  - `phase-handoff` — write `docs/handoff/PHASE_<n>.md` to the fixed template in §9.2 and update `docs/handoff/INDEX.md`

### 8.4 LLM-portability pack
- `docs/LLM_PORTABILITY.md` — the provider interface, how to swap Ollama → any other backend, prompt
  file locations, and what must **not** depend on any specific model
- Prompts as versioned files, never inline strings
- `docs/MIGRATION.md` — handing the project to GPT/Gemini/local models: reading order, invariants,
  the contract files that must not drift

---

## 9. Build sequence — one phase per session, cleared between phases

### 9.1 The phases

Each row is **one Claude Code session**. Nothing carries over in context; everything carries over in files.

| # | Deliverable | Verify command | Objective gate |
|---|---|---|---|
| 0 | `docs/` contract set, `CLAUDE.md`, `.claude/skills/`, repo scaffold, Compose skeleton, `policy/sites.yaml` (all 13 regions) | `python tasks.py verify 0` | All contract docs exist; site YAML validates; region GeoJSON present |
| 1 | `datagen` clean population at 10k, all dimensions + facts | `python tasks.py verify 1` | Row counts match manifest; **0 policy violations** in the clean set; schema matches `DATA_DICTIONARY.md` |
| 2 | Anomaly injection — all 30 codes + `labels_anomaly` + confounders | `python tasks.py verify 2` | Every code present; injection rates within ±10% of target; confounders present and unlabelled |
| 3 | Feature build + Layer 1 rule engine + eval harness | `python tasks.py verify 3` | **100% recall, 100% precision on Family A** at 10k; features build < 60s |
| 4 | Layer 2 peer stats + expected-salary model + SHAP attributions | `python tasks.py verify 4` | Family B recall ≥ 85%; every cohort has n ≥ 30 or documented fallback |
| 5 | Layer 3 Isolation Forest + tabular autoencoder + graph checks | `python tasks.py verify 5` | Family C/D recall ≥ 75%; CUDA path confirmed on the RTX 5060 |
| 6 | Fusion, severity banding, evidence bundle, financial impact | `python tasks.py verify 6` | Evidence validates against `EVIDENCE_CONTRACT.md`; alert budget within ±20% |
| 7 | Scale-up 100k → 1M, batch tuning, `agg_alerts_by_site_month` | `python tasks.py verify 7` | Full 1M run **< 15 min**, peak RAM **< 12 GB** |
| 8 | Postgres schema + FastAPI backend + auth + audit + geo endpoint | `python tasks.py verify 8` | Contract tests pass; `/analytics/geo` returns 24 months of data |
| 9 | Frontend shell: theme tokens, layout, nav, dashboard | `python tasks.py verify 9` | Theme tokens applied; light/dark pass; Lighthouse a11y ≥ 90 |
| 10 | Triage queue + alert detail + evidence panel + employee 360 | `python tasks.py verify 10` | Playwright: queue → detail → disposition passes |
| 11 | Geographic anomaly map with month scrubber and animation | `python tasks.py verify 11` | 24 months render; per-1,000 normalisation correct; site click filters queue |
| 12 | Ollama narrator + numeral grounding check + caching + fallback | `python tasks.py verify 12` | Grounding check rejects invented figures; UI usable with Ollama stopped |
| 13 | Feedback loop: dispositions → suppression → threshold tuning | `python tasks.py verify 13` | Re-run confirms dismissed alerts suppressed |
| 14 | Compose end-to-end, exports, runbook, demo script, smoke tests | `python tasks.py verify 14` | `docker compose up` → full walkthrough green |

### 9.2 The session protocol (this is the token-saving mechanism)

Every phase runs the same loop:

1. **Start clean.** New session, then `/clear`. Never continue a phase in a session that built the previous one.
2. **Feed exactly three files as the input artifact**, nothing else:
   - `CLAUDE.md` (kept under ~150 lines — conventions only)
   - `docs/specs/<service>.md` for the service this phase touches
   - `docs/handoff/PHASE_<n-1>.md` — the previous phase's output artifact
3. **Build the phase.** The spec is the source of truth; if the spec is wrong, fix the spec first, then the code.
4. **Validate before anything else.** Run `python tasks.py verify <n>`. The gate is objective — a number or a
   pass/fail, not an opinion. **Do not write the handoff or move on until it passes.**
5. **Write the handoff artifact** `docs/handoff/PHASE_<n>.md` using the `phase-handoff` skill. Fixed template:
   - What was built (files created/modified, one line each)
   - Public interfaces added (function signatures, endpoints, table schemas, CLI flags)
   - Verify output pasted (the actual numbers)
   - Decisions made and any deviation from the spec, with reason
   - Known gaps / deferred items
   - **"Start here" block for the next session** — the exact three files to read and the exact first command to run
   - Anything that invalidates a contract doc, plus confirmation that doc was updated
6. **Clear the session** and start the next phase.

**Why an artifact and not `/compact`:** compaction keeps a lossy summary of a huge context and still
re-sends it every turn. A handoff file is ~2 pages, lossless about the things that matter (interfaces,
schemas, numbers), reviewable by you, diffable in git, and readable by any other LLM. It is also the
audit trail of the build.

### 9.3 How to drive Claude Code for this, concretely

- **Prompt shape at each phase start** — one message, no preamble:
  > Read `CLAUDE.md`, `docs/specs/detector.md`, and `docs/handoff/PHASE_04.md`. Implement Phase 5 exactly
  > as specified. Do not read other files unless a specific function is needed. When done, run
  > `python tasks.py verify 5` and show me the output before writing anything else.
- **`/clear`, not `/compact`,** between phases. Compaction is for surviving a long single task; clearing
  is for starting the next one.
- **Keep `data/` out of context permanently.** Add it to `.gitignore` and state in `CLAUDE.md`:
  *"Never read files under `data/`. Inspect data only by running a script or a DuckDB query and reading
  the printed summary."* Reading one Parquet preview can cost more than an entire phase.
- **Verification output must be small.** Every `python tasks.py verify <n>` prints a compact table and a final
  `PASS`/`FAIL` line — not thousands of log lines. Cheap gates get run; expensive gates get skipped.
- **Use skills for anything you'll do more than twice** (`add-anomaly-rule`, `add-api-endpoint`,
  `add-ui-view`, `phase-handoff`, `run-eval`). A skill replaces re-explaining a pattern every session.
- **Don't spawn subagents here.** Each one starts cold and re-derives context you already paid for; the
  phase specs already scope the work.
- **Split a phase if a session gets long.** Phases 10 and 14 are the likeliest to need splitting
  (10a queue, 10b detail; 14a compose, 14b tests) — write an interim handoff and clear, rather than
  pushing through a bloated context.
- **Use plan mode only when the design is genuinely open.** Phases 1–14 are already specified; going in
  with plan mode each time re-plans work that's already planned.
- **Fix the spec, not just the code.** If reality diverges from `docs/specs/*`, update the spec in the
  same session. Otherwise the next session builds against a lie and you pay for the correction twice.
- **Keep a `docs/handoff/INDEX.md`** — one line per phase with status and gate result, so you can see
  the whole build at a glance without opening 15 files.

### 9.4 Prerequisites (install before Phase 1; Phase 0 needs only Python)

| Tool | Needed from | Notes |
|---|---|---|
| Python 3.12 + `uv` | Phase 0 | `uv` for fast, reproducible envs |
| Node 20 + npm | Phase 9 | frontend only |
| Docker Desktop (WSL2 backend) | Phase 8 | Postgres; keep WSL memory capped ~6 GB in `.wslconfig` so the 1M batch still has room |
| NVIDIA driver + CUDA-enabled PyTorch | Phase 5 | verify with `torch.cuda.is_available()`; CPU fallback must work too |
| Ollama + a 7–8B instruct model | Phase 12 | `ollama pull qwen2.5:7b-instruct` |
| Git | Phase 0 | installed (2.54). Repo not yet initialised — Phase 0 runs `git init` and wires the remote; see §9.5 |

### 9.5 Git & GitHub

**Remote**: `https://github.com/MohammedAyub75/anamaly.git`
Identity is already configured globally (`Mohammed Ayub` / `mohammedayub.y@gmail.com`).

**Auth — do this once, before Phase 0.** Git Credential Manager is installed but not registered as the
credential helper, so a push will currently fail. In an interactive terminal, run:

```bash
git config --global credential.helper manager
```

The first `git push` then opens a browser sign-in. (`gh` CLI is not installed; it isn't required, but
`winget install GitHub.cli` followed by `gh auth login` is a fine alternative.) **I cannot complete this
sign-in for you** — the first authenticated push must be run by you.

**`.gitignore` is a Phase 0 deliverable and is load-bearing.** The Parquet lake reaches 3–6 GB at 1M
scale and model artifacts are large binaries; committing them would wreck the repo. Must exclude at
minimum: `data/`, `**/*.parquet`, `**/*.duckdb`, `**/*.pt`, `**/*.joblib`, `.venv/`, `node_modules/`,
`web/dist/`, `.env`, `__pycache__/`. Phase 0's verify gate asserts `git status --porcelain` is clean
after a full 10k data generation — proving the lake is genuinely ignored.

**Commit rhythm** — one commit per phase gate, plus intermediate commits within a phase as work
completes:

```
phase(0): repo scaffold, contract docs, task runner
phase(3): layer-1 rule engine + eval harness — Family A recall 100%
```

Each phase's handoff commit includes the pasted verify output in the commit body, so `git log` alone
tells you which gates passed. Work directly on `main` (single developer, linear history); tag each
passed gate as `phase-00`, `phase-01`, … giving you a clean rollback point per phase.

**When to push**: at the end of every phase, after the gate passes and the handoff is written. If a
push fails on auth, that is not a reason to skip the commit — commit locally and push once auth is sorted.

### 9.6 Bootstrapping Phase 0

Phase 0 has no predecessor handoff, so it bootstraps from this plan. Its **first action** is to copy this
plan file into the repo as `docs/PLAN.md` — that makes the plan survive every session clear and become
the root document all the specs are derived from. Phase 0 then produces the contract docs and specs from
it, plus `docs/handoff/PHASE_00.md`. From Phase 1 onward, no session ever needs to read `docs/PLAN.md`
again — the per-service spec plus the previous handoff is enough, which is precisely what keeps each
session cheap.

Opening prompt for the cleared session:

```
Read C:/Users/Mohammed Ayub/.claude/plans/hi-i-want-to-serene-nebula.md. Execute Phase 0 ONLY.
First actions: git init, write .gitignore per §9.5, copy the plan to docs/PLAN.md, and add the
remote https://github.com/MohammedAyub75/anamaly.git. Then produce the Phase 0 deliverables in §9.1.
Stop when `python tasks.py verify 0` passes and docs/handoff/PHASE_00.md is written and committed.
Do not start Phase 1.
```

---

## 10. Verification (end-to-end)

```bash
python tasks.py datagen --scale 10k --seed 42          # generate + verify manifest row counts
python tasks.py eval --scale 10k                     # per-anomaly-code recall table must show no 0% rows
python tasks.py datagen --scale 1m --seed 42           # full generation, watch RAM stays under 12 GB
python tasks.py detect --scale 1m --run-id 2026-08     # full batch; assert wall clock < 15 min
docker compose up                       # postgres + detector-api + backend + web
```

Then, in the browser:
- Triage queue shows ≈500 critical alerts.
- A known-injected `A01` employee's alert cites the Dhahran/remote-allowance rule with correct financial impact.
- Dismiss one alert, re-run the batch, confirm it is suppressed.
- Stop Ollama; confirm the deterministic explanation still renders and nothing errors.
- **Map**: all 24 months render; switching metric from raw count to per-1,000 employees visibly changes
  the ranking (proving headcount normalisation works); play the animation end-to-end; click a Shaybah
  bubble and confirm the triage queue filters to that site.
- Cross-check the UI's flagged employee IDs against `labels_anomaly` to prove true positives are being
  surfaced, not just plausible-looking ones.

---

## 11. Key risks and mitigations

| Risk | Mitigation |
|---|---|
| 16 GB RAM insufficient at 1M×24 months | Chunked generation, Polars lazy + DuckDB streaming, Parquet row-groups, never materialise the full join |
| Synthetic anomalies too easy → inflated metrics | Planted confounders, realistic noise, per-code recall reporting, deliberate hard cases |
| Alert fatigue for reviewers | Hard alert budget, severity banding, financial-impact ranking, dismissal suppression |
| ML explanations too abstract for HR users | Rules-first architecture; ML attributions always rendered in business terms (SAR amounts, peer counts) |
| Local LLM hallucinating figures | LLM never computes; numeral-grounding post-check; deterministic fallback always available |
| Doc/code drift across sessions | Contract files (`EVIDENCE_CONTRACT`, `DATA_DICTIONARY`, `ANOMALY_CATALOG`) are authoritative; skills enforce the update pattern |
| Map becomes a population map (Eastern Province always "worst") | Default metric is alerts per 1,000 employees, not raw count; raw count available but not default |
| Map performance over 24 months × 180 sites | Pre-aggregated `agg_alerts_by_site_month` computed in the batch; each frame is a small payload, no client-side aggregation |
| Air-gapped deployment breaks the map | Bundled GeoJSON + `d3-geo` vector rendering and self-hosted fonts; **zero external CDN/tile dependencies** anywhere in the app |
| A phase session grows too long and gets compacted | Split the phase, write an interim handoff, clear (§9.3); handoff artifacts make splitting cheap |
