"""CLI for the generator.

    python -m datagen generate --scale 10k --seed 42
    python -m datagen validate --scale 10k
    python -m datagen summary  --scale 10k

`python tasks.py datagen --scale 10k --seed 42` is the documented path and calls
straight into `main()`.

`--reference-date` exists so "today" is an argument rather than a wall-clock
read: `datetime.now()` anywhere in generation would mean the same seed produced
different data tomorrow, and every downstream comparison would quietly rot.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from .config import (
    DEFAULT_OUT,
    DEFAULT_PERIODS,
    DEFAULT_REFERENCE_DATE,
    SCALES,
    ScaleConfig,
)
from .policy import DatagenPolicy


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scale", choices=list(SCALES), default="10k")
    parser.add_argument("--out", default=DEFAULT_OUT, help="root of the lake")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m datagen",
        description="Deterministic synthetic HR and payroll generator (pass 1).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="write the lake")
    _add_common(generate)
    generate.add_argument("--seed", type=int, default=42,
                          help="controls everything; same seed, identical output")
    generate.add_argument("--periods", type=int, default=DEFAULT_PERIODS)
    generate.add_argument("--reference-date", type=date.fromisoformat,
                          default=DEFAULT_REFERENCE_DATE,
                          help="'today' for the run; never the wall clock")
    generate.add_argument("--no-noise", action="store_true",
                          help="skip realism noise; debugging only, never for a gate run")
    generate.add_argument("--employees", type=int, default=None,
                          help=argparse.SUPPRESS)  # slice override, used by the gate

    validate = sub.add_parser("validate", help="re-run the integrity suite")
    _add_common(validate)
    validate.add_argument("--skip-determinism", action="store_true")

    summary = sub.add_parser("summary", help="row counts and distributions")
    _add_common(summary)
    return parser


def _config(args: argparse.Namespace, policy: DatagenPolicy) -> ScaleConfig:
    return ScaleConfig.build(
        scale=args.scale,
        seed=getattr(args, "seed", 42),
        population=policy.population,
        out=args.out,
        periods=getattr(args, "periods", DEFAULT_PERIODS),
        reference_date=getattr(args, "reference_date", DEFAULT_REFERENCE_DATE),
        employees=getattr(args, "employees", None),
        noise=not getattr(args, "no_noise", False),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    policy = DatagenPolicy.load()
    cfg = _config(args, policy)

    if args.command == "generate":
        from .pipeline import generate

        result = generate(cfg, policy)
        counts = result.manifest["row_counts"]
        print(f"scale={cfg.scale} seed={cfg.seed} employees={cfg.employees:,} "
              f"periods={cfg.period_from}..{cfg.period_to}")
        for table, rows in counts.items():
            print(f"  {table:<32} {rows:>12,}")
        print(f"written to {cfg.lake} in {result.seconds:.1f}s")
        return 0

    if args.command == "validate":
        from .integrity import run

        report = run(cfg, policy, include_determinism=not args.skip_determinism)
        failed = [c for c in report.checks if not c.ok]
        for check in failed:
            print(f"  FAIL  {check.name}  {check.detail}", file=sys.stderr)
        print(f"{len(report.checks) - len(failed)}/{len(report.checks)} checks passed")
        return 0 if report.passed else 1

    from .integrity import summarise

    for label, value in summarise(cfg):
        print(f"  {label:<32} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
