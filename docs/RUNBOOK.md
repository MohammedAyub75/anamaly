# RUNBOOK.md

Operational guide: generate data, run a batch, start the services, and diagnose what broke.

> Phases 1–14 are still being built. Commands below are marked with the phase that delivers them;
> before that phase, `python tasks.py <verb>` exits with a message saying so rather than failing
> obscurely. `docs/handoff/INDEX.md` shows what has actually landed.

## Prerequisites

| Tool | Needed from | Check |
|---|---|---|
| Python 3.12 | phase 0 | `python --version` |
| `uv` | phase 1 | `uv --version` — **not currently installed**; `pip install uv` or use plain venv + pip |
| Node 20 + npm | phase 9 | `node --version` |
| Docker Desktop (WSL2) | phase 8 | `docker --version` |
| NVIDIA driver + CUDA PyTorch | phase 5 | `python -c "import torch; print(torch.cuda.is_available())"` |
| Ollama + a 7–8B instruct model | phase 12 | `ollama list` |

Cap WSL memory at ~6 GB in `%USERPROFILE%\.wslconfig` so the 1M batch still has room on a 16 GB
machine.

## Environment

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Generate data — phase 1

```bash
python tasks.py datagen --scale 10k --seed 42
```

Scales: `10k` (development, seconds), `100k` (integration), `1m` (full, target < 10 min).
The same `--seed` reproduces byte-identical output; that is asserted by the phase-1 gate.

Output lands in `data/raw/scale=<n>/` with a `manifest.json`. **Never open these files directly** —
inspect with a query and read the printed summary (`CLAUDE.md`). Reading one Parquet preview into a
session costs more than an entire phase.

## Run a detection batch — phase 3+

```bash
python tasks.py detect --scale 10k --run-id 2026-08
```

Stages run in order and each is independently cached and re-runnable: features → L1 rules → L2 peer
stats → L3 ML/graph → L4 fusion → Postgres upsert. A fusion-weight change re-runs L4 only.

## Evaluate — phase 3+

```bash
python tasks.py eval --scale 10k
```

Writes `docs/EVAL_REPORT.md`. **The per-anomaly-code recall table is the core feedback loop**: a code
showing 0% recall is a detector bug, not a tuning problem, and it is the first thing to read.

## Start the services

```bash
docker compose up            # postgres + detector + api + web
python tasks.py api          # API alone, phase 8
python tasks.py web          # Vite dev server, phase 9
```

Ollama runs on the host (it needs the GPU), not in Compose. The API starts healthy without it.

## Phase gates

```bash
python tasks.py verify 0
```

Prints a compact table and a final `PASS`/`FAIL`. Gates are cheap on purpose — an expensive gate is
a skipped gate, and a skipped gate is a broken gate. Never write a handoff before the gate passes.

## Troubleshooting

**`verify 0` fails on "lake paths gitignored"** — `.gitignore` no longer covers the Parquet lake.
Do not commit until it does; a 3–6 GB lake in git history is not practically reversible.

**`verify 0` fails on "no data/ in git status"** — something under `data/` became visible to git.
Check for a nested `.gitignore` or a force-added file (`git ls-files data/`).

**PyYAML missing** — the gates need it: `pip install pyyaml`.

**Out of memory during a 1M generation or batch** — something materialised the full employee ×
period join. Check for a `.collect()` on a lazy frame that should have stayed lazy, or a pandas call
on a bulk table. Row-groups must stay at 100k.

**Batch slower than 15 minutes at 1M** — read the per-stage timing in the run summary before tuning
anything. Feature build and the autoencoder are the usual suspects; rules never are.

**`torch.cuda.is_available()` is False** — the CPU path must still work and is a supported
configuration; the batch is slower but correct. Do not add a hard CUDA dependency.

**Ollama is down** — expected and handled. Explanations return `source: "template"`. If the UI shows
an error instead of the deterministic text, that is a bug in the fallback, not in Ollama.

**A previously-dismissed alert reappears** — check `policy/fusion.yaml` → `suppression`. It is
matched on employee + code + evidence fingerprint; a changed amount is intentionally a new finding.

**Eval shows 0% recall for one code** — the injector and the detector have drifted apart. Both are
defined side by side in `docs/ANOMALY_CATALOG.md`; reconcile them there first, then fix the code.

**Eval metrics look suspiciously perfect** — check that no detector reads `labels_anomaly`. It is
ground truth for the eval harness only.

## Reproducing a reported alert

1. Note `run_id`, `employee_id` and `provenance.policy_digest` from the evidence bundle.
2. Confirm the policy digest matches the current `policy/` — if not, the alert was scored under a
   superseded policy and must be re-scored, not re-interpreted.
3. `POST /score/employee/{id}` for a live re-score, or re-run the batch stage with the same seed.
