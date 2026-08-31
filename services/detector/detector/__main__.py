"""`python -m detector` -- the detector's command line.

    python -m detector build-features --scale 10k
    python -m detector run   --scale 10k --run-id 2026-08 [--stages features,l1]
    python -m detector score --employee-id E00042317 [--what-if key=value]
    python -m detector eval  --scale 10k

`tasks.py` is a thin wrapper over this; this is the real CLI.
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import SCALES, DetectorConfig, LakeError
from .eval import harness, report
from .features.build import build as build_features
from .lake import connect
from .layers.l1_rules import RuleError, RuleSet, run_rules
from .policy import DetectorPolicy, DigestMismatch
from .run import STAGES, RunResult, StageNotBuilt, rule_digest
from .run import run as run_batch


def _context(args: argparse.Namespace) -> tuple[DetectorConfig, DetectorPolicy, RuleSet]:
    cfg = DetectorConfig.build(
        args.scale, run_id=getattr(args, "run_id", None), lake=args.lake
    )
    return cfg, DetectorPolicy.load(args.policy), RuleSet.load(args.policy)


def cmd_build_features(args: argparse.Namespace) -> int:
    cfg, policy, _ = _context(args)
    built = build_features(cfg, policy, force=args.force, threads=args.threads,
                           log=print)
    state = "reused" if built.cached else "built"
    print(f"\nfeature store {state} in {built.seconds:.2f}s -> {cfg.features}")
    for name, rows in built.row_counts.items():
        print(f"  {name:<22} {rows:>10,} rows  {built.columns.get(name, 0):>4} cols")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg, policy, ruleset = _context(args)
    result = run_batch(
        cfg, policy, ruleset, stages=args.stages, force=args.force,
        threads=args.threads, log=print,
    )
    print(f"\nrun {result.run_id} complete in {result.seconds:.2f}s")
    if result.hits_path:
        print(f"  findings -> {result.hits_path}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    """Re-check one employee, optionally against a hypothetical.

    `--what-if housing_type=allowance` rewrites that column for this employee
    only and re-runs every rule, which is how a reviewer answers "would moving
    them out of the camp clear this?" without changing anything.
    """
    cfg, _policy, ruleset = _context(args)
    overrides: dict[str, str] = {}
    for pair in args.what_if or []:
        key, _, value = pair.partition("=")
        if not key or not _:
            print(f"error: --what-if expects key=value, got {pair!r}", file=sys.stderr)
            return 2
        overrides[key.strip()] = value.strip()

    con = connect(cfg, features=True)
    try:
        columns = [d[0] for d in (con.execute(
            "SELECT * FROM features_period LIMIT 0").description or [])]
        unknown = sorted(set(overrides) - set(columns))
        if unknown:
            print(f"error: no such feature column(s): {unknown}", file=sys.stderr)
            return 2
        projection = ", ".join(
            f"{overrides[c]} AS {c}" if c in overrides else c for c in columns
        )
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE one AS SELECT {projection} "
            "FROM features_period WHERE employee_id = ?", [args.employee_id]
        )
        rows = con.execute("SELECT count(*) FROM one").fetchone()
        if not rows or not rows[0]:
            print(f"no such employee in the feature store: {args.employee_id}",
                  file=sys.stderr)
            return 1
        ruleset.check_executable(con)
        result = run_rules(con, ruleset, table="one")
    finally:
        con.close()

    if overrides:
        print(f"what-if: {overrides}")
    if not result.hits:
        print(f"{args.employee_id}: no policy findings")
        return 0
    for hit in result.hits:
        print(f"\n{hit['anomaly_code']}  {hit['severity']}  "
              f"{hit['period_from']}..{hit['period_to']}  "
              f"SAR {hit['financial_impact_cumulative']:,.0f} cumulative")
        print(f"  {hit['description']}")
        for action in hit["recommended_actions"]:
            print(f"  - {action}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    cfg, policy, ruleset = _context(args)
    result: RunResult = run_batch(
        cfg, policy, ruleset, stages=args.stages, force=args.force,
        threads=args.threads, log=print if args.verbose else None,
    )
    if result.l1 is None:
        print("error: layer 1 did not run; nothing to evaluate", file=sys.stderr)
        return 2
    scored = harness.evaluate(
        cfg, ruleset, result.l1,
        planned=report.PLANNED,
        runtime=result.runtime,
        policy_digest=policy.digest,
        rule_digest=rule_digest(ruleset),
    )
    path = report.write(scored, args.out)
    print(f"\nwrote {path}")
    width = max(len(name) for name, _ in report.summary_rows(scored))
    for name, value in report.summary_rows(scored):
        print(f"  {name.ljust(width)}  {value}")
    if scored.zero_recall:
        print("\nzero recall with a detector: "
              + ", ".join(r.code for r in scored.zero_recall))
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    """List the rule pack. Useful when a code shows 0% and you want to see why."""
    ruleset = RuleSet.load(args.policy)
    print(json.dumps(
        {
            "digest": rule_digest(ruleset),
            "rules": [
                {
                    "id": r.id, "severity": r.severity, "enabled": r.enabled,
                    "name_en": r.name_en, "exclusions": len(r.exclusions),
                    "evidence_fields": len(r.evidence_fields),
                }
                for r in ruleset.rules
            ],
        },
        indent=2,
    ))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m detector",
        description="Entitlement and payroll anomaly detector.",
    )
    parser.add_argument("--policy", default="policy", help="policy pack root")
    parser.add_argument("--lake", default="data/raw", help="raw lake root")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--scale", choices=list(SCALES), default="10k")
        p.add_argument("--force", action="store_true",
                       help="ignore the stage cache and rebuild")
        p.add_argument("--threads", type=int, default=None)
        return p

    p = common(sub.add_parser("build-features", help="build the feature store"))
    p.set_defaults(func=cmd_build_features)

    p = common(sub.add_parser("run", help="run a detection batch"))
    p.add_argument("--run-id", default=None)
    p.add_argument("--stages", default=None,
                   help=f"comma-separated subset of {','.join(STAGES)}")
    p.set_defaults(func=cmd_run)

    p = common(sub.add_parser("score", help="re-check one employee"))
    p.add_argument("--employee-id", required=True)
    p.add_argument("--what-if", action="append", metavar="COLUMN=VALUE")
    p.set_defaults(func=cmd_score)

    p = common(sub.add_parser("eval", help="run the evaluation harness"))
    p.add_argument("--run-id", default=None)
    p.add_argument("--stages", default=None)
    p.add_argument("--out", default=report.REPORT_PATH)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("rules", help="list the loaded rule pack")
    p.set_defaults(func=cmd_rules)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (LakeError, RuleError, DigestMismatch, StageNotBuilt) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
