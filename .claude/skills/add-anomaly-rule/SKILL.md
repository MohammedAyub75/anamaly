---
name: add-anomaly-rule
description: Add or change an anomaly code end to end — policy rule YAML, datagen injector, catalog entry, detector wiring and tests — so the injector and detector can never drift apart. Use when adding a new anomaly code, changing an existing rule's predicate or severity, or fixing a code showing 0% recall in the eval report.
---

# Add an anomaly rule

An anomaly code is **five artifacts, not one**. Adding the rule without the injector produces a
detector with nothing to find; adding the injector without the rule produces a permanent 0% recall
row in the eval report. Both failures are silent, which is why this checklist exists.

## The five artifacts

1. **`docs/ANOMALY_CATALOG.md`** — the entry. Definition, injection logic, detection logic,
   severity, rate, evidence fields, recommended actions. **Write this first**: if you cannot state
   the injection and detection logic side by side in a paragraph each, the code is not ready.
2. **`policy/rules/<CODE>_<slug>.yaml`** — the rule. Copy
   `policy/rules/A01_remote_site_allowance_at_ineligible_site.yaml`; every field in it is
   load-bearing.
3. **`services/datagen/datagen/injection/<code>.py`** — the injector.
4. **The detector** — Layer 1 needs no code (the YAML is executed). Layers 2/3 need a signal in
   `l2_peer.py` / `l3_ml.py` / `l3_graph.py`.
5. **Tests** — one in `services/datagen/tests/` asserting the injector produces exactly what it
   claims, and one asserting the detector finds it.

## Steps

1. **Pick the family and code.** A = deterministic policy violation, B = peer-group statistical,
   C = identity/payroll fraud, D = behavioural/temporal. Next free number in that family.
2. **Write the catalog entry** in `docs/ANOMALY_CATALOG.md`, in family order. Include the injection
   rate (% of employees) — rare fraud 0.01–0.05%, common entitlement drift 0.15–0.45% — and
   remember the **floor of 5 instances at any scale**, or recall at 10k is measured on n=1.
3. **Write the rule YAML.** For a family-A code the `sql_predicate` is the whole detector.
   - `evidence_fields` must contain every column the reviewer needs to see the finding is real.
   - `description_template` is plain English for a non-technical reviewer. No jargon.
   - `recommended_actions` must be specific enough to act on without further analysis.
   - `regulatory_reference` is what makes it a finding rather than an opinion.
   - **`exclusions`** — think about the legitimate case that looks identical. There is almost always
     one (a posting lag, a final settlement, a declared spousal account). Missing it costs precision.
4. **Write the injector.** Draw from the seeded stream, never `random`. Write a `labels_anomaly` row
   with `injection_params_json` filled in so the case is reproducible. Set the anomaly window
   (`period_from`, `period_to`) and `expected_monthly_impact`.
5. **Consider a confounder.** If the injected pattern has a legitimate twin, plant it in
   `labels_confounder` too. Without it you are measuring recall against a straw man.
6. **Wire the detector** if it is not a family-A rule.
7. **Regenerate and evaluate:**
   ```bash
   python tasks.py datagen --scale 10k --seed 42
   python tasks.py eval --scale 10k
   ```
8. **Read the per-code recall row.** 0% means the injector and detector disagree — reconcile them in
   the catalog first, then in the code. Do not tune thresholds to paper over a logic mismatch.
9. **Check precision on the confounders.** A new rule that lifts recall while flagging legitimate
   employees is a net loss.

## Rules

- **Never** make `labels_anomaly` an input to the detector.
- The injector and the detector must be reconcilable by reading the catalog entry alone.
- If the rule needs a feature that does not exist, add it in `features/sql/` — do not compute it in
  the rule predicate.
- Changing an existing code's severity or predicate means updating `docs/ANOMALY_CATALOG.md` in the
  same commit. The catalog is authoritative.
- Rerun the full eval, not just the new code. Rules interact through the alert budget: a noisy new
  rule pushes real findings out of the CRITICAL band.
