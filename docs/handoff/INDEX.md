# Build index

The whole build at a glance. One line per phase — status, gate result, date, tag. Read this before
opening any handoff file.

**Protocol**: one phase per session, cleared between phases. Each phase reads exactly three files
(`CLAUDE.md`, its service spec, the previous handoff), builds against its spec, passes an objective
gate, writes a handoff, commits and tags. See `docs/PLAN.md` §9 and the `phase-handoff` skill.

| # | Deliverable | Status | Gate | Date | Tag |
|---|---|---|---|---|---|
| 0 | Contract docs, CLAUDE.md, skills, repo scaffold, Compose skeleton, `policy/sites.yaml` (13 regions) | ✅ PASSED | `verify 0` — 24/24 checks | 2026-08-31 | `phase-00` |
| 1 | `datagen` pass 1 — clean population at 10k, 7 dimensions + 6 facts, 0/34 policy violations | ✅ PASSED | `verify 1` — 53/53 checks | 2026-08-31 | `phase-01` |
| 2 | Anomaly injection — 34 codes + `labels_anomaly` + confounders | ⬜ not started | `verify 2` | — | — |
| 3 | Feature build + Layer 1 rule engine + eval harness | ⬜ not started | `verify 3` | — | — |
| 4 | Layer 2 peer stats + expected-salary model + SHAP | ⬜ not started | `verify 4` | — | — |
| 5 | Layer 3 Isolation Forest + autoencoder + graph checks | ⬜ not started | `verify 5` | — | — |
| 6 | Fusion, severity banding, evidence bundle, financial impact | ⬜ not started | `verify 6` | — | — |
| 7 | Scale-up 100k → 1M, batch tuning, `agg_alerts_by_site_month` | ⬜ not started | `verify 7` | — | — |
| 8 | Postgres schema + FastAPI backend + auth + audit + geo endpoint | ⬜ not started | `verify 8` | — | — |
| 9 | Frontend shell: theme tokens, layout, nav, dashboard | ⬜ not started | `verify 9` | — | — |
| 10 | Triage queue + alert detail + evidence panel + employee 360 | ⬜ not started | `verify 10` | — | — |
| 11 | Geographic anomaly map with month scrubber and animation | ⬜ not started | `verify 11` | — | — |
| 12 | Ollama narrator + numeral grounding + caching + fallback | ⬜ not started | `verify 12` | — | — |
| 13 | Feedback loop: dispositions → suppression → threshold tuning | ⬜ not started | `verify 13` | — | — |
| 14 | Compose end-to-end, exports, runbook, demo script, smoke tests | ⬜ not started | `verify 14` | — | — |

## Next session

Read `CLAUDE.md`, `docs/specs/datagen.md`, `docs/handoff/PHASE_01.md`. Implement phase 2 — anomaly
injection: the 34 codes, `labels_anomaly`, and the seven confounder types in `labels_confounder`.
First command:

```
python tasks.py verify 1
```

(confirms the clean population is still clean before breaking it on purpose).
