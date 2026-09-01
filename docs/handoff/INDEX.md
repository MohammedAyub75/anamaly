# Build index

The whole build at a glance. One line per phase — status, gate result, date, tag. Read this before
opening any handoff file.

**Protocol**: one phase per session, cleared between phases. Each phase reads exactly three files
(`CLAUDE.md`, its service spec, the previous handoff), builds against its spec, passes an objective
gate, writes a handoff, commits and tags. See `docs/PLAN.md` §9 and the `phase-handoff` skill.

| # | Deliverable | Status | Gate | Date | Tag |
|---|---|---|---|---|---|
| 0 | Contract docs, CLAUDE.md, skills, repo scaffold, Compose skeleton, `policy/sites.yaml` (13 regions) | ✅ PASSED | `verify 0` — 24/24 checks | 2026-08-31 | `phase-00` |
| 1 | `datagen` pass 1 — clean population at 10k, 7 dimensions + 6 facts, 0/34 policy violations | ✅ PASSED | `verify 1` — 54/54 checks | 2026-08-31 | `phase-01` |
| 2 | `datagen` pass 2 — 34 anomaly codes injected + `labels_anomaly` + 7 confounder types | ✅ PASSED | `verify 2` — 44/44 checks, 34/34 codes at 100% agreement | 2026-08-31 | `phase-02` |
| 3 | Feature build + Layer 1 rule engine + eval harness | ✅ PASSED | `verify 3` — 34/34 checks, 17/17 layer-1 codes at 100% recall and precision | 2026-09-01 | `phase-03` |
| 4 | Layer 2 peer stats + expected-salary model + SHAP | ✅ PASSED | `verify 4` — 31/31 checks, 12/12 layer-2 codes at 100% recall, family B precision 99% | 2026-09-01 | `phase-04` |
| 5 | Layer 3 Isolation Forest + autoencoder + graph checks | ✅ PASSED | `verify 5` — 30/30 checks, 5/5 layer-3 codes at 100% recall and precision, 34/34 codes now have a detector | 2026-09-01 | `phase-05` |
| 6 | Fusion, severity banding, evidence bundle, financial impact | ✅ PASSED | `verify 6` — 32/32 checks, 353 findings fused into 344 alerts, budget hit exactly (5 CRITICAL / 50 HIGH), 344/344 bundles validated | 2026-09-01 | `phase-06` |
| 7 | Scale-up 100k → 1M, batch tuning, `agg_alerts_by_site_month` | ⬜ not started | `verify 7` | — | — |
| 8 | Postgres schema + FastAPI backend + auth + audit + geo endpoint | ⬜ not started | `verify 8` | — | — |
| 9 | Frontend shell: theme tokens, layout, nav, dashboard | ⬜ not started | `verify 9` | — | — |
| 10 | Triage queue + alert detail + evidence panel + employee 360 | ⬜ not started | `verify 10` | — | — |
| 11 | Geographic anomaly map with month scrubber and animation | ⬜ not started | `verify 11` | — | — |
| 12 | Ollama narrator + numeral grounding + caching + fallback | ⬜ not started | `verify 12` | — | — |
| 13 | Feedback loop: dispositions → suppression → threshold tuning | ⬜ not started | `verify 13` | — | — |
| 14 | Compose end-to-end, exports, runbook, demo script, smoke tests | ⬜ not started | `verify 14` | — | — |

## Next session

Read `CLAUDE.md`, `docs/specs/detector.md`, `docs/handoff/PHASE_06.md`. Implement phase 7 — scale-up
to 1M plus `agg_alerts_by_site_month`. First command:

```
python tasks.py verify 6
```

(confirms the feature store, all four layers, the evidence schema and the harness are intact before
anything is asked to run a hundred times bigger). The detector is now complete end to end at 10k:
34 codes, four layers, one ranked and validated queue. What is untested is size — the autoencoder is
`O(rows x epochs)` and bundle assembly is per-alert Python, and both are about to meet a million
employees and 24 million payroll rows against a 15-minute, 12 GB budget.
