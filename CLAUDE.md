# CLAUDE.md — conventions for every session

Employee entitlement & payroll anomaly detection platform. Synthetic Saudi/energy-sector HR data
with injected, labelled anomalies; a layered detector; an explainable review UI for **non-technical
HR/audit reviewers**.

## Read this before you touch anything

This project is built **one phase per session** (`docs/PLAN.md` §9). At the start of a phase, read
exactly three files and nothing else:

1. `CLAUDE.md` (this file)
2. `docs/specs/<service>.md` for the service the phase touches
3. `docs/handoff/PHASE_<n-1>.md` — the previous phase's output artifact

`docs/handoff/INDEX.md` shows which phases have passed. Do not read `docs/PLAN.md` during phases
1–14; the spec plus the previous handoff is sufficient by design, and that is what keeps a session
cheap. Do not read another service's source to learn its interface — the handoff and the contract
docs carry it.

## Hard rules

- **Never read files under `data/`.** Inspect data only by running a script or a DuckDB query and
  reading the printed summary. Reading one Parquet preview can cost more than an entire phase.
- **Never `pandas.read_parquet`** the 1M / 24M-row tables. Bulk data work is **Polars + DuckDB**,
  lazy/streaming, chunked in 100k row-groups. pandas is acceptable only for small result sets
  (< 100k rows) already reduced by a query.
- **The spec is the source of truth.** If reality diverges from `docs/specs/*`, fix the spec in the
  same session, then the code. Otherwise the next session builds against a lie.
- **Contract docs are authoritative and must not drift**: `docs/DATA_DICTIONARY.md`,
  `docs/ANOMALY_CATALOG.md`, `docs/EVIDENCE_CONTRACT.md`, `docs/API_CONTRACT.md`. Changing a schema,
  an anomaly code, or the evidence shape means updating its contract doc in the same commit.
- **Determinism.** `--seed` controls all generation; the same seed reproduces byte-identical output.
  No unseeded `random`, no `datetime.now()` in generated data.
- **No jargon in the UI.** Reviewers are non-technical. No "z-score", "isolation forest",
  "reconstruction error" anywhere user-facing — say it in business terms and SAR amounts.
- **No external CDN, tile server, or font host** anywhere in the app. It must run air-gapped.

## Stack

| Layer | Choice |
|---|---|
| Language | Python 3.12, Node 20 |
| Bulk data | Polars (lazy) + DuckDB over partitioned Parquet |
| ML | scikit-learn (Isolation Forest, HistGradientBoostingRegressor), PyTorch CUDA (tabular autoencoder), SHAP, networkx |
| Backend | FastAPI + Pydantic v2, Postgres (alerts/cases/audit), JWT auth |
| Frontend | React + TypeScript + Vite + shadcn/ui + TanStack Query/Table, d3-geo / react-simple-maps |
| LLM | Local Ollama behind `LLMProvider`; off the batch path, always with a deterministic fallback |
| Orchestration | Docker Compose |

## Layout

```
policy/          YAML policy packs: sites, grade bands, allowance rules, rules/*.yaml, fusion.yaml
services/datagen synthetic generator CLI      services/detector features, 4 layers, fusion, eval
services/api     FastAPI BFF                  web/  React app
data/            Parquet lake + models (gitignored — never read, never commit)
docs/            contract layer; docs/specs/ per-service build specs; docs/handoff/ phase artifacts
.claude/skills/  repeatable patterns — use these instead of re-deriving
```

## Commands

```
python tasks.py verify <n>              # the objective phase gate — must PASS before a handoff
python tasks.py datagen --scale 10k --seed 42
python tasks.py detect  --scale 10k --run-id <id>
python tasks.py eval    --scale 10k
python tasks.py api | web
```

Verify output must stay **small**: a compact table and a final `PASS`/`FAIL`. Never let a gate print
thousands of log lines — expensive gates get skipped, and a skipped gate is a broken gate.

## Skills — use them, don't re-derive the pattern

| Skill | Use when |
|---|---|
| `add-anomaly-rule` | adding a policy rule + injector + catalog entry + test |
| `regenerate-dataset` | regenerating data at any scale, with manifest checks |
| `add-api-endpoint` | new router/schema/test/contract-doc |
| `add-ui-view` | new page/route/query-hook/component |
| `run-eval` | running the harness and reading the per-code recall table |
| `phase-handoff` | writing `docs/handoff/PHASE_<n>.md` and updating the index |

## Code conventions

- Python: type hints on public functions, `from __future__ import annotations`, `ruff` defaults,
  4-space indent, module docstrings that say *why*. No bare `except`.
- Config over code: thresholds, budgets, cohort fallback order and rules live in `policy/*.yaml`,
  never as literals in Python.
- Structured JSON logging with a `correlation_id` spanning UI → API → detector.
- Tests live beside the service in `tests/`; every anomaly code gets a test asserting its injector
  and its detector agree.
- SQL for set-based work, Python for orchestration. A 40-line DuckDB query beats a Python loop.

## Phase discipline

1. Build the phase against its spec.
2. Run `python tasks.py verify <n>`. **Do not write the handoff or move on until it passes.**
3. Write `docs/handoff/PHASE_<n>.md` with the `phase-handoff` skill; update `docs/handoff/INDEX.md`.
4. Commit as `phase(<n>): <summary>` with the verify output in the commit body; tag `phase-<nn>`.
5. Clear the session.

Work directly on `main`. Remote: `https://github.com/MohammedAyub75/anamaly.git`.

## Domain quick reference

- **Currency is SAR.** Salaries are monthly, not annual. 24 months of history, `period` is `YYYYMM`.
- `nationality_class` ∈ {`saudi`, `gcc`, `expat`} drives salary bands, GOSI class, and eligibility
  for nationality-restricted benefits.
- The same allowance can be **legitimate at one site and a violation at another** — `hardship_tier`
  and `remote_allowance_eligible` in `policy/sites.yaml` are what make the discrimination real.
- Map metrics default to **alerts per 1,000 employees**, never raw counts: Eastern Province
  headcount dominance would otherwise turn every heat map into a population map.
