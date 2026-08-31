# services/detector

The AI service: features, four detection layers, fusion, evidence, eval harness.
Build spec: `docs/specs/detector.md`. Phases 3-7 and 12.

## Built so far (phase 3)

The DuckDB feature build, the layer-1 declarative rule engine, and the evaluation harness.
Layers 2-4 arrive in phases 4-6; asking for those stages fails loudly rather than silently
skipping.

```
python tasks.py detect --scale 10k --run-id 2026-08     # features -> L1
python tasks.py eval   --scale 10k                      # writes docs/EVAL_REPORT.md
python tasks.py verify 3                                # the phase gate
```

`tasks.py` is a thin wrapper over `python -m detector`, which is the real CLI
(`build-features`, `run`, `score`, `eval`, `rules`). Like the generator, this package is not
pip-installed during a phase build, so run it through `tasks.py` or put `services/detector` on
`PYTHONPATH` first.

`python -m detector score --employee-id E00042317 --what-if "housing_type='allowance'"` re-checks
one employee against a hypothetical -- the question a reviewer actually asks, answered without
changing anything.

## The one rule that shapes this package

`lake.connect()` -- the connection every feature build, rule and layer uses -- has no view named
`labels_anomaly`. A query that reaches for ground truth fails with a binder error instead of
silently scoring 100%. Only `lake.connect_labels()`, behind `detector/eval/`, can see it.
