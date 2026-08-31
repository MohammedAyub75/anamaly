---
name: regenerate-dataset
description: Regenerate the synthetic dataset at any scale tier with correct flags, manifest and reproducibility checks. Use when policy files changed, the schema changed, an injector was added, or the eval harness reports a policy digest mismatch.
---

# Regenerate the dataset

## When you must regenerate

- Any file in `policy/` changed — the data was built against the old policy and the eval harness
  will report a **policy digest mismatch**, which means ground truth is stale.
- A column was added, removed or retyped in `docs/DATA_DICTIONARY.md`.
- An injector was added or changed (`add-anomaly-rule`).
- The generator version changed.

You do **not** need to regenerate for a detector, fusion-weight, API or UI change. Regenerating
takes minutes at 1m scale and invalidates every cached feature build downstream — do not do it
reflexively.

## Commands

```bash
python tasks.py datagen --scale 10k  --seed 42     # development — seconds
python tasks.py datagen --scale 100k --seed 42     # integration
python tasks.py datagen --scale 1m   --seed 42     # full — target < 10 min, < 12 GB RAM
```

**Always the same seed** unless you are deliberately testing seed independence. `42` is the project
default and every documented figure assumes it.

## Checks after generating

1. **Read the summary, not the data.**
   ```bash
   python tasks.py datagen --scale 10k --summary
   ```
   Never open a Parquet file. Reading one preview into a session can cost more than an entire phase.
2. **Run the gate.**
   ```bash
   python tasks.py verify 1     # clean population: 0 policy violations
   python tasks.py verify 2     # injection: every code present, rates within +/-10%
   ```
3. **Check `manifest.json`** — row counts, injection rates by code, and `policy_digest` matching the
   current `policy/` files.
4. **Confirm determinism** — regenerate and confirm byte-identical output. If it is not identical,
   something unseeded crept in (`random`, `datetime.now()`, `uuid4()`, dict ordering). Find it now;
   it gets much harder to find later.
5. **Confirm the lake is invisible to git**:
   ```bash
   git status --porcelain
   ```
   Nothing under `data/` may appear. A 3–6 GB lake in git history is not practically reversible.

## Then re-run downstream

Regenerated data invalidates every cached stage:

```bash
python tasks.py detect --scale 10k --run-id <new-run-id>
python tasks.py eval   --scale 10k
```

Use a **new** `run_id`. Reusing one across different underlying data makes run-over-run comparison
meaningless and quietly corrupts the "what changed since last run" view.

## Scale tiers

| Tier | Employees | Payroll rows | Use |
|---|---|---|---|
| `10k` | 10,000 | 240,000 | Development and every phase gate |
| `100k` | 100,000 | 2,400,000 | Integration, performance smoke |
| `1m` | 1,000,000 | 24,000,000 | Full run, phase-7 target |

Develop at 10k. Every gate except phase 7 runs at 10k on purpose — a cheap gate gets run, and an
expensive gate gets skipped.

## If it runs out of memory

Something materialised the full employee × period join. Look for a `.collect()` on a lazy frame
that should have stayed lazy, a pandas call on a bulk table, or a row-group larger than 100k. The
fix is always to stream, never to add RAM.
