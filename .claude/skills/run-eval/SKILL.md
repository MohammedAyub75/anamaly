---
name: run-eval
description: Run the evaluation harness and interpret the per-anomaly-code recall table, precision@k and confounder false-positive analysis. Use after any detector, rule, threshold or fusion change.
---

# Run and read the evaluation

```bash
python tasks.py eval --scale 10k
```

Writes `docs/EVAL_REPORT.md`. Because ground truth is injected, every figure here is exact — not an
estimate.

## Read it in this order

### 1. Per-anomaly-code recall — always first

One row per code, all 34. This is the core development feedback loop.

- **0% recall is a bug, not a tuning problem.** The injector and the detector disagree about what
  the code means. Both are defined side by side in `docs/ANOMALY_CATALOG.md` — reconcile them there
  first, then fix the code. Never lower a threshold to make a 0% row go away; that hides the defect
  and costs precision everywhere else.
- **Family A below 100%** is always a bug. Family A is deterministic: the clause was broken or it
  was not. Check the rule's `exclusions` first — an over-broad exclusion silently eats true
  positives.
- **A code with very few instances** (n < 5) gives a recall figure that is noise. Check the
  injection rate floor.

### 2. Confounder false positives — read this second, not last

Planted legitimate oddities: senior high earners, salary jumps *with* proper records, spousal shared
IBANs, low-activity field roles, legitimate final settlements.

**None may be scored CRITICAL.** A detector that flags everything scores 100% recall and is useless
to a reviewer. If recall went up and confounder false positives went up with it, the change was a
net loss — say so plainly rather than reporting the recall gain alone.

### 3. Precision@100 / @1000 / @5000

What a reviewer actually experiences. Precision@100 matters most: those are the alerts someone
works on Monday morning. High recall with poor precision@100 means the ranking is wrong, not the
detection.

### 4. Alert budget adherence

Roughly 500 CRITICAL and 5,000 HIGH at 1M scale, scaled linearly for smaller tiers, within ±20%.
Over budget means alert fatigue; well under means the thresholds are hiding findings. Budget lives
in `policy/fusion.yaml` — tune the config, never a literal in code.

### 5. Runtime profile

Per stage, per scale tier. Watch for a stage growing superlinearly — that is what breaks the 15
minute target at 1M, and it is always visible at 100k first.

## Interpreting a change

Compare against the previous report before drawing any conclusion. State three things together:

1. Which codes' recall moved, and by how much.
2. What happened to confounder false positives.
3. What happened to precision@100.

A change that improves one and silently degrades another is not an improvement. Report all three
even when only one was the goal.

## Common causes of a bad number

| Symptom | Usual cause |
|---|---|
| One code at 0% | Injector/detector definition mismatch |
| Family A below 100% | Over-broad `exclusions`, or a feature the predicate needs is null |
| Recall high, precision@100 low | Ranking problem — check fusion weights and financial-impact ordering |
| Confounder flagged CRITICAL | Missing exclusion, or the confounder pattern was not considered when the rule was written |
| Everything suspiciously perfect | A detector is reading `labels_anomaly`. It must never be an input. |
| Metrics moved with no code change | The data was regenerated, or the policy digest no longer matches |
