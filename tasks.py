#!/usr/bin/env python3
"""Cross-platform task runner for the anomaly platform.

Windows-first: no GNU Make dependency. Every workflow is reachable as

    python tasks.py <verb> [options]

Verbs
-----
    datagen   Generate the synthetic dataset          (phase 1+)
    detect    Run a detection batch                   (phase 3+)
    eval      Run the evaluation harness              (phase 3+)
    api       Start the FastAPI backend               (phase 8+)
    web       Start the Vite dev server               (phase 9+)
    verify    Run the objective gate for a phase      (phase 0+)

`verify` is the build's contract: each phase is gated by `python tasks.py
verify <n>`, which prints a compact table and a final PASS/FAIL line and
nothing else.  Cheap gates get run; expensive gates get skipped, so this
stays fast and quiet on purpose (see docs/PLAN.md section 9.3).

`verify 0` deliberately depends on the standard library plus PyYAML only,
so the scaffold gate runs before any environment has been provisioned.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# The generator lives in services/datagen and the shared policy core at the
# repo root; both are importable from here without an install step, which is
# what lets `python tasks.py` work on a fresh clone.
SERVICE_PATHS = [ROOT, ROOT / "services" / "datagen"]


def _add_service_paths() -> None:
    for path in SERVICE_PATHS:
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)

# --------------------------------------------------------------------------
# Gate reporting
# --------------------------------------------------------------------------


class Gate:
    """Collects check results and renders the compact verify table."""

    def __init__(self, phase: int, title: str) -> None:
        self.phase = phase
        self.title = title
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        return bool(ok)

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.rows)

    def report(self) -> int:
        width = max((len(n) for n, _, _ in self.rows), default=10)
        print(f"\nPhase {self.phase} gate — {self.title}")
        print("-" * (width + 34))
        for name, ok, detail in self.rows:
            flag = "ok  " if ok else "FAIL"
            print(f"  {flag}  {name.ljust(width)}  {detail}")
        print("-" * (width + 34))
        result = "PASS" if self.passed else "FAIL"
        print(f"{result} — phase {self.phase}\n")
        return 0 if self.passed else 1


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

SAUDI_BBOX = {"lat": (16.0, 32.5), "lon": (34.0, 56.0)}

# The 13 administrative regions, ISO 3166-2:SA (note: 13 is unassigned).
REGION_CODES = {
    "SA-01", "SA-02", "SA-03", "SA-04", "SA-05", "SA-06", "SA-07",
    "SA-08", "SA-09", "SA-10", "SA-11", "SA-12", "SA-14",
}

SITE_CLASSES = {
    "hq", "plant", "refinery", "offshore", "drilling_camp", "terminal",
    "office", "depot", "training", "medical", "pump_station",
}

CONTRACT_DOCS = [
    "docs/PROJECT_BRIEF.md",
    "docs/ARCHITECTURE.md",
    "docs/DATA_DICTIONARY.md",
    "docs/ANOMALY_CATALOG.md",
    "docs/EVIDENCE_CONTRACT.md",
    "docs/API_CONTRACT.md",
    "docs/RUNBOOK.md",
    "docs/DESIGN_SYSTEM.md",
    "docs/LLM_PORTABILITY.md",
    "docs/MIGRATION.md",
]

SPEC_DOCS = [
    "docs/specs/datagen.md",
    "docs/specs/detector.md",
    "docs/specs/api.md",
    "docs/specs/web.md",
]

SKILLS = [
    "add-anomaly-rule",
    "regenerate-dataset",
    "add-api-endpoint",
    "add-ui-view",
    "run-eval",
    "phase-handoff",
]

# Paths that must never reach the index, checked with `git check-ignore`.
LAKE_PATHS = [
    "data/raw/scale=10k/employee_master.parquet",
    "data/features/employee_features.parquet",
    "data/models/isolation_forest.joblib",
    "data/models/autoencoder.pt",
    "data/runs/run_id=2026-08/alerts.parquet",
    "services/detector/scratch.duckdb",
    ".venv/pyvenv.cfg",
    "web/node_modules/react/package.json",
    "web/dist/index.html",
    ".env",
]


def _load_yaml(path: Path):
    try:
        import yaml
    except ImportError:  # pragma: no cover - environment guard
        print(
            "error: PyYAML is required for the verify gates.\n"
            "       install it with:  python -m pip install pyyaml",
            file=sys.stderr,
        )
        raise SystemExit(2)
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _git(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _missing(paths: list[str]) -> list[str]:
    return [p for p in paths if not (ROOT / p).exists()]


# --------------------------------------------------------------------------
# Phase 0 gate
# --------------------------------------------------------------------------


def _check_sites(gate: Gate) -> None:
    """Validate policy/sites.yaml: schema, region coverage, coordinates."""
    path = ROOT / "policy" / "sites.yaml"
    if not path.exists():
        gate.check("sites.yaml present", False, "policy/sites.yaml missing")
        return

    doc = _load_yaml(path)
    regions = {r["code"]: r for r in doc.get("regions", [])}
    sites = doc.get("sites", []) or []
    defaults = doc.get("class_defaults", {}) or {}

    gate.check(
        "sites.yaml regions",
        set(regions) == REGION_CODES,
        f"{len(regions)}/13 regions"
        + (
            ""
            if set(regions) == REGION_CODES
            else f", unexpected={sorted(set(regions) ^ REGION_CODES)}"
        ),
    )

    ids = [s.get("id") for s in sites]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    gate.check(
        "sites.yaml unique ids",
        not dupes and all(ids),
        f"{len(sites)} sites" + (f", duplicates={dupes}" if dupes else ""),
    )

    required = {"id", "name_en", "name_ar", "city", "region", "lat", "lon",
                "class", "hardship_tier", "headcount_weight"}
    bad_schema = [s.get("id") for s in sites if not required <= set(s)]
    gate.check(
        "sites.yaml schema",
        not bad_schema,
        "all keys present" if not bad_schema else f"incomplete={bad_schema[:5]}",
    )

    bad_region = [s["id"] for s in sites if s.get("region") not in regions]
    bad_class = [s["id"] for s in sites if s.get("class") not in SITE_CLASSES]
    bad_tier = [
        s["id"] for s in sites
        if not isinstance(s.get("hardship_tier"), int)
        or not 0 <= s["hardship_tier"] <= 3
    ]
    bad_weight = [
        s["id"] for s in sites
        if not isinstance(s.get("headcount_weight"), (int, float))
        or s["headcount_weight"] <= 0
    ]
    gate.check("sites.yaml region refs", not bad_region,
               "resolved" if not bad_region else f"unknown={bad_region[:5]}")
    gate.check("sites.yaml class enum", not bad_class,
               f"{len({s['class'] for s in sites})} classes used"
               if not bad_class else f"unknown={bad_class[:5]}")
    gate.check("sites.yaml hardship 0-3", not bad_tier,
               "in range" if not bad_tier else f"out of range={bad_tier[:5]}")
    gate.check("sites.yaml headcount>0", not bad_weight,
               "positive" if not bad_weight else f"invalid={bad_weight[:5]}")

    off_map = [
        s["id"] for s in sites
        if not (SAUDI_BBOX["lat"][0] <= s.get("lat", -999) <= SAUDI_BBOX["lat"][1]
                and SAUDI_BBOX["lon"][0] <= s.get("lon", -999) <= SAUDI_BBOX["lon"][1])
    ]
    gate.check(
        "sites.yaml coords in KSA", not off_map,
        "within bbox" if not off_map else f"outside={off_map[:5]}",
    )

    used_classes = {s.get("class") for s in sites}
    uncovered = sorted(used_classes - set(defaults))
    gate.check(
        "sites.yaml class defaults", not uncovered,
        "every class has defaults" if not uncovered else f"missing={uncovered}",
    )

    # Every region must carry at least one site, or geography is not a real
    # analytical dimension (docs/PLAN.md section 2.2.1).
    empty = sorted(set(regions) - {s.get("region") for s in sites})
    gate.check(
        "every region populated", not empty,
        "13/13 regions have sites" if not empty else f"empty={empty}",
    )

    # A02 (hardship allowance at a tier-0 site) is only detectable if both
    # sides of the discrimination actually exist in the reference data.
    tiers = {s.get("hardship_tier") for s in sites}
    eligible = sum(
        1 for s in sites
        if s.get("remote_allowance_eligible",
                 defaults.get(s.get("class"), {}).get("remote_allowance_eligible"))
    )
    gate.check(
        "hardship/remote contrast",
        {0, 3} <= tiers and 0 < eligible < len(sites),
        f"tiers={sorted(t for t in tiers if t is not None)}, "
        f"remote-eligible={eligible}/{len(sites)}",
    )


def _check_geojson(gate: Gate) -> None:
    """Validate the bundled region boundary file used by the UI map."""
    path = ROOT / "policy" / "geo" / "sa_regions.geojson"
    if not path.exists():
        gate.check("region GeoJSON present", False, "policy/geo/sa_regions.geojson missing")
        return

    doc = json.loads(path.read_text(encoding="utf-8"))
    gate.check(
        "GeoJSON FeatureCollection",
        doc.get("type") == "FeatureCollection",
        f"type={doc.get('type')}",
    )

    feats = doc.get("features", [])
    codes = {f.get("properties", {}).get("region_code") for f in feats}
    gate.check(
        "GeoJSON 13 regions",
        codes == REGION_CODES,
        f"{len(feats)} features"
        + ("" if codes == REGION_CODES else f", mismatch={sorted(codes ^ REGION_CODES)}"),
    )

    def coords(geom):
        """Yield every (lon, lat) pair regardless of nesting depth."""
        stack = [geom.get("coordinates", [])]
        while stack:
            node = stack.pop()
            if (isinstance(node, list) and len(node) == 2
                    and all(isinstance(v, (int, float)) for v in node)):
                yield node
            elif isinstance(node, list):
                stack.extend(node)

    bad_geom, off_map = [], []
    for f in feats:
        code = f.get("properties", {}).get("region_code")
        geom = f.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            bad_geom.append(code)
            continue
        for lon, lat in coords(geom):
            if not (SAUDI_BBOX["lon"][0] <= lon <= SAUDI_BBOX["lon"][1]
                    and SAUDI_BBOX["lat"][0] <= lat <= SAUDI_BBOX["lat"][1]):
                off_map.append(code)
                break

    gate.check("GeoJSON geometry types", not bad_geom,
               "polygons" if not bad_geom else f"bad={bad_geom}")
    gate.check("GeoJSON coords in KSA", not off_map,
               "within bbox" if not off_map else f"outside={sorted(set(off_map))}")


def _check_lake_ignored(gate: Gate) -> None:
    """The data lake must be genuinely un-committable (docs/PLAN.md 9.5)."""
    code, _ = _git("rev-parse", "--git-dir")
    if code != 0:
        gate.check("git repository", False, "not a git repository")
        return

    not_ignored = []
    for rel in LAKE_PATHS:
        rc, _ = _git("check-ignore", "-q", "--no-index", rel)
        if rc != 0:
            not_ignored.append(rel)
    gate.check(
        "lake paths gitignored", not not_ignored,
        f"{len(LAKE_PATHS)} probe paths ignored"
        if not not_ignored else f"NOT ignored={not_ignored}",
    )

    # Nothing under data/ may ever show up as untracked or modified. This is
    # the same assertion the phase-1 gate re-runs after a real 10k generation.
    _, out = _git("status", "--porcelain")
    leaked = [
        ln[3:].strip().strip('"') for ln in out.splitlines()
        if ln[3:].strip().strip('"').startswith("data/")
    ]
    gate.check(
        "no data/ in git status", not leaked,
        "lake invisible to git" if not leaked else f"leaked={leaked[:5]}",
    )


def verify_0() -> int:
    gate = Gate(0, "contract docs, scaffold, policy, geo")

    missing = _missing(CONTRACT_DOCS)
    gate.check("contract docs", not missing,
               f"{len(CONTRACT_DOCS) - len(missing)}/{len(CONTRACT_DOCS)} present"
               + (f", missing={missing}" if missing else ""))

    missing = _missing(SPEC_DOCS)
    gate.check("service specs", not missing,
               f"{len(SPEC_DOCS) - len(missing)}/{len(SPEC_DOCS)} present"
               + (f", missing={missing}" if missing else ""))

    missing = _missing(["CLAUDE.md", "docs/PLAN.md", "tasks.py",
                        "docker-compose.yml", ".gitignore",
                        "docs/handoff/INDEX.md"])
    gate.check("repo scaffold", not missing,
               "root files present" + (f", missing={missing}" if missing else ""))

    missing = _missing([f".claude/skills/{s}/SKILL.md" for s in SKILLS])
    gate.check("claude skills", not missing,
               f"{len(SKILLS) - len(missing)}/{len(SKILLS)} skills"
               + (f", missing={missing}" if missing else ""))

    missing = _missing(["services/datagen", "services/detector", "services/api",
                        "web", "policy/rules"])
    gate.check("service tree", not missing,
               "directories present" + (f", missing={missing}" if missing else ""))

    missing = _missing(["policy/grade_bands.yaml", "policy/allowance_rules.yaml",
                        "policy/fusion.yaml"])
    gate.check("policy packs", not missing,
               "present" + (f", missing={missing}" if missing else ""))

    # CLAUDE.md is re-read at the start of every session; keeping it short is
    # the whole point (docs/PLAN.md section 9.2).
    claude = ROOT / "CLAUDE.md"
    lines = len(claude.read_text(encoding="utf-8").splitlines()) if claude.exists() else -1
    gate.check("CLAUDE.md <= 150 lines", 0 < lines <= 150, f"{lines} lines")

    _check_sites(gate)
    _check_geojson(gate)
    _check_lake_ignored(gate)

    return gate.report()


# --------------------------------------------------------------------------
# Later-phase gates — declared here so the runner's surface is stable and a
# premature call fails loudly instead of silently reporting success.
# --------------------------------------------------------------------------

PHASE_TITLES = {
    1: "datagen clean population (10k)",
    2: "anomaly injection + labels",
    3: "features + layer 1 rules + eval",
    4: "layer 2 peer stats + expected salary",
    5: "layer 3 isolation forest + autoencoder + graph",
    6: "fusion, severity, evidence bundle",
    7: "scale-up to 1m",
    8: "postgres + FastAPI backend",
    9: "frontend shell",
    10: "triage queue + alert detail",
    11: "geographic anomaly map",
    12: "ollama narrator",
    13: "feedback loop",
    14: "compose end-to-end",
}


def verify_1() -> int:
    """Phase-1 gate: the clean population, checked against the contract docs.

    Generates the 10k lake if it is missing, then runs the integrity suite --
    schema, row counts, referential integrity, domain validity, arithmetic and
    all 34 anomaly-code predicates, each reported separately so a leak is
    visible by code rather than as a total.
    """
    _add_service_paths()
    from datagen.config import ScaleConfig
    from datagen.integrity import run
    from datagen.policy import DatagenPolicy

    policy = DatagenPolicy.load(ROOT / "policy")
    cfg = ScaleConfig.build("10k", 42, policy.population, out=ROOT / "data" / "raw")

    if not cfg.manifest_path.exists():
        from datagen.pipeline import generate

        print(f"generating {cfg.scale} dataset (seed {cfg.seed}) ...")
        result = generate(cfg, policy)
        print(f"generated in {result.seconds:.1f}s")

    report = run(cfg, policy)
    gate = Gate(1, PHASE_TITLES[1])
    for check in report.checks:
        gate.check(check.name, check.ok, check.detail)
    return gate.report()


def verify_2() -> int:
    """Phase-2 gate: the injected anomalies and the ground truth that records them.

    The inverse of the phase-1 gate. Phase 1 asked whether the population was
    clean; this asks whether every code is present at its floor, whether the
    predicate that defines each code actually finds every employee the injector
    claims to have broken, and whether anything at all is broken that the labels
    do not account for. The phase-1 suite is re-run underneath, collapsed to a
    single row unless something in it fails.
    """
    _add_service_paths()
    from datagen.config import ScaleConfig
    from datagen.integrity import (
        anomaly_predicates,
        connect,
        found_count,
        labelled_employees,
        run,
        unlabelled_count,
    )
    from datagen.policy import DatagenPolicy

    policy = DatagenPolicy.load(ROOT / "policy")
    cfg = ScaleConfig.build("10k", 42, policy.population, out=ROOT / "data" / "raw")
    manifest = (json.loads(cfg.manifest_path.read_text(encoding="utf-8"))
                if cfg.manifest_path.exists() else {})
    if not manifest.get("injection", {}).get("by_code"):
        from datagen.pipeline import generate

        print(f"generating {cfg.scale} dataset with injection (seed {cfg.seed}) ...")
        result = generate(cfg, policy)
        manifest = result.manifest
        print(f"generated in {result.seconds:.1f}s")

    gate = Gate(2, PHASE_TITLES[2])
    spec = policy.pack.injection
    floor = int(spec["min_instances"])
    by_code = manifest["injection"]["by_code"]
    planted = manifest["injection"]["confounders"]
    con = connect(cfg)
    try:
        codes = sorted(spec["codes"])
        missing = [c for c in codes if by_code.get(c, 0) < floor]
        gate.check("every code injected at its floor", not missing,
                   f"{len(codes)} codes, at least {floor} each"
                   if not missing else f"short={missing}")

        employees = manifest["employee_count"]
        carrying = manifest["injection"]["employees_with_anomaly"]
        rate = carrying / employees if employees else 0
        target = float(spec["target_anomaly_rate"])
        # The floor lifts the realised rate above the catalogue's headline sum
        # at 10k -- eleven codes are rarer than five in ten thousand.
        gate.check("injection rate in range", target <= rate <= target * 1.5,
                   f"{rate * 100:.2f}% of employees carry an anomaly "
                   f"(catalogue {target * 100:.2f}%, floors lift it)")

        counts = _label_counts(con)
        gate.check("labels resolve to employees", counts["orphans"] == 0,
                   f"{counts['labels']:,} label rows, {counts['confounders']:,} "
                   "confounder rows, no orphans")
        gate.check("label windows inside the run", counts["outside"] == 0,
                   f"{cfg.period_from}..{cfg.period_to}"
                   if not counts["outside"] else f"outside={counts['outside']}")
        gate.check("severities in domain", counts["bad_severity"] == 0,
                   "CRITICAL, HIGH, MEDIUM" if not counts["bad_severity"]
                   else f"invalid={counts['bad_severity']}")
        gate.check("injection params reproducible", counts["bad_json"] == 0,
                   f"{counts['labels']:,} parameter sets parse")
        gate.check("label counts match manifest", counts["by_code"] == by_code,
                   f"{sum(by_code.values()):,} rows agree with manifest.injection")

        types = sorted(spec["confounders"])
        thin = [t for t in types if planted.get(t, 0) < floor]
        gate.check("confounders planted", not thin,
                   f"{len(types)} types, {sum(planted.values())} employees"
                   if not thin else f"short={thin}")
        gate.check("confounders are unlabelled", counts["confounded_and_labelled"] == 0,
                   "no confounder carries an anomaly label"
                   if not counts["confounded_and_labelled"]
                   else f"overlap={counts['confounded_and_labelled']}")

        for code, (label, sql) in anomaly_predicates(cfg, policy).items():
            injected = by_code.get(code, 0)
            detected = labelled_employees(con, code, sql)
            leaked = unlabelled_count(con, code, sql)
            total = found_count(con, sql)
            gate.check(
                f"{code} {label}",
                detected == injected and leaked == 0 and injected >= floor,
                f"{injected} injected, {detected} found, {leaked} unlabelled"
                + (f", {total} rows" if total else ""),
            )
    finally:
        con.close()

    report = run(cfg, policy)
    failed = [c for c in report.checks if not c.ok]
    if failed:
        for check in failed:
            gate.check(f"phase-1: {check.name}", False, check.detail)
    else:
        gate.check("phase-1 integrity suite", True,
                   f"{len(report.checks)}/{len(report.checks)} checks still pass")
    return gate.report()


def _label_counts(con) -> dict:
    """Everything the label tables have to satisfy, in one pass each."""
    scalar = con.execute(
        """
        SELECT
          (SELECT count(*) FROM labels_anomaly),
          (SELECT count(*) FROM labels_confounder),
          (SELECT count(*) FROM labels_anomaly l LEFT JOIN employee_master e
             USING (employee_id) WHERE e.employee_id IS NULL)
          + (SELECT count(*) FROM labels_confounder c LEFT JOIN employee_master e
             USING (employee_id) WHERE e.employee_id IS NULL),
          (SELECT count(*) FROM labels_anomaly l LEFT JOIN dim_calendar a
             ON a.period = l.period_from LEFT JOIN dim_calendar b
             ON b.period = l.period_to
             WHERE a.period IS NULL OR b.period IS NULL OR l.period_to < l.period_from),
          (SELECT count(*) FROM labels_anomaly WHERE injected_severity
             NOT IN ('CRITICAL','HIGH','MEDIUM')),
          (SELECT count(*) FROM labels_anomaly WHERE try_cast(
             injection_params_json AS JSON) IS NULL),
          (SELECT count(*) FROM labels_confounder c JOIN labels_anomaly l
             USING (employee_id))
        """
    ).fetchone()
    by_code = dict(con.execute(
        "SELECT anomaly_code, count(*) FROM labels_anomaly GROUP BY 1 ORDER BY 1"
    ).fetchall())
    return {
        "labels": scalar[0], "confounders": scalar[1], "orphans": scalar[2],
        "outside": scalar[3], "bad_severity": scalar[4], "bad_json": scalar[5],
        "confounded_and_labelled": scalar[6],
        "by_code": {k: int(v) for k, v in by_code.items()},
    }


def verify_pending(phase: int) -> int:
    title = PHASE_TITLES.get(phase, "unknown phase")
    print(f"\nPhase {phase} gate — {title}")
    print(f"FAIL — phase {phase} is not implemented yet "
          f"(build it, then this gate is written in that phase's session)\n")
    return 1


def cmd_verify(args: argparse.Namespace) -> int:
    phase = args.phase
    if phase == 0:
        return verify_0()
    if phase == 1:
        return verify_1()
    if phase == 2:
        return verify_2()
    if phase in PHASE_TITLES:
        return verify_pending(phase)
    print(f"error: no such phase: {phase} (valid: 0-14)", file=sys.stderr)
    return 2


# --------------------------------------------------------------------------
# Placeholder verbs — implemented in the phase that owns them
# --------------------------------------------------------------------------


def _not_yet(verb: str, phase: int) -> int:
    print(
        f"'{verb}' is delivered in phase {phase}; it does not exist yet.\n"
        f"See docs/PLAN.md section 9.1 for the build sequence.",
        file=sys.stderr,
    )
    return 2


def cmd_datagen(args: argparse.Namespace) -> int:
    """Thin wrapper over `python -m datagen`, which is the real CLI."""
    _add_service_paths()
    from datagen.__main__ import main as datagen_main

    argv = [args.command, "--scale", args.scale, "--out", args.out]
    if args.command == "generate":
        argv += ["--seed", str(args.seed), "--periods", str(args.periods),
                 "--reference-date", args.reference_date]
        if args.no_noise:
            argv.append("--no-noise")
    return datagen_main(argv)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python tasks.py",
        description="Task runner for the entitlement & payroll anomaly platform.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    p = sub.add_parser("verify", help="run a phase gate")
    p.add_argument("phase", type=int, help="phase number 0-14")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("datagen", help="generate the synthetic dataset (phase 1)")
    p.add_argument("--scale", choices=["10k", "100k", "1m"], default="10k")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default="data/raw")
    p.add_argument("--periods", type=int, default=24)
    p.add_argument("--reference-date", default="2026-08-31")
    p.add_argument("--no-noise", action="store_true")
    p.add_argument("--command", default="generate",
                   choices=["generate", "validate", "summary"])
    p.set_defaults(func=cmd_datagen)

    p = sub.add_parser("detect", help="run a detection batch (phase 3)")
    p.add_argument("--scale", choices=["10k", "100k", "1m"], default="10k")
    p.add_argument("--run-id", default=None)
    p.set_defaults(func=lambda a: _not_yet("detect", 3))

    p = sub.add_parser("eval", help="run the evaluation harness (phase 3)")
    p.add_argument("--scale", choices=["10k", "100k", "1m"], default="10k")
    p.set_defaults(func=lambda a: _not_yet("eval", 3))

    p = sub.add_parser("api", help="start the FastAPI backend (phase 8)")
    p.set_defaults(func=lambda a: _not_yet("api", 8))

    p = sub.add_parser("web", help="start the Vite dev server (phase 9)")
    p.set_defaults(func=lambda a: _not_yet("web", 9))

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
