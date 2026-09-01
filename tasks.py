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
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# The generator lives in services/datagen and the shared policy core at the
# repo root; both are importable from here without an install step, which is
# what lets `python tasks.py` work on a fresh clone.
SERVICE_PATHS = [
    ROOT,
    ROOT / "services" / "datagen",
    ROOT / "services" / "detector",
]


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


def verify_3() -> int:
    """Phase-3 gate: the feature store, the layer-1 rule engine and the harness.

    The objective claim of this phase is narrow and checkable: family A is
    deterministic, so it must run at 100% recall AND 100% precision, and the
    feature build must stay inside sixty seconds at 10k.  Everything else here
    exists to stop that claim being true by accident -- that no rule was
    silently skipped, that no detector can see `labels_anomaly`, and that a
    second pass over the same lake produces the same findings.
    """
    _add_service_paths()
    from detector.config import DetectorConfig, LakeError
    from detector.eval import harness, report
    from detector.features.build import OUTPUTS, build, feature_columns
    from detector.lake import LABEL_TABLES, connect
    from detector.layers.l1_rules import RuleError, RuleSet, run_rules
    from detector.policy import DetectorPolicy
    from detector.run import rule_digest

    gate = Gate(3, PHASE_TITLES[3])
    policy = DetectorPolicy.load(ROOT / "policy")

    def _config():
        return DetectorConfig.build(
            "10k", run_id="verify-3",
            lake=ROOT / "data" / "raw",
            features=ROOT / "data" / "features",
            runs=ROOT / "data" / "runs",
        )

    try:
        cfg = _config()
    except LakeError:
        from datagen.config import ScaleConfig
        from datagen.pipeline import generate
        from datagen.policy import DatagenPolicy

        dg = DatagenPolicy.load(ROOT / "policy")
        print("generating 10k dataset with injection (seed 42) ...")
        generate(ScaleConfig.build("10k", 42, dg.population,
                                   out=ROOT / "data" / "raw"), dg)
        cfg = _config()

    # ---------------------------------------------------------------- rules
    try:
        ruleset = RuleSet.load(ROOT / "policy")
    except RuleError as exc:
        gate.check("rule pack loads", False, str(exc)[:90])
        return gate.report()

    family_a = {f"A{n:02d}" for n in range(1, 13)}
    missing_a = sorted(family_a - set(ruleset.codes))
    gate.check("family A rules present", not missing_a,
               f"{len(ruleset.rules)} rules loaded, A01-A12 complete"
               if not missing_a else f"missing={missing_a}")
    gate.check("every rule enabled", all(r.enabled for r in ruleset.rules),
               "a disabled rule is a silent 0% recall row")

    # ------------------------------------------------------------- features
    built = build(cfg, policy, force=True)
    gate.check("feature build under 60s", built.seconds < 60,
               f"{built.seconds:.1f}s for "
               f"{built.row_counts.get('features_period', 0):,} period rows")
    gate.check("feature tables written",
               all(built.row_counts.get(name) for name, _ in OUTPUTS),
               ", ".join(f"{n}={built.row_counts.get(n, 0):,}" for n, _ in OUTPUTS))

    columns = feature_columns(cfg)
    leaked = [c for c in columns if "label" in c.lower() or "anomaly" in c.lower()]
    gate.check("no label leaks into features", not leaked,
               f"{len(columns)} columns, none derived from ground truth"
               if not leaked else f"leaked={leaked[:5]}")

    try:
        ruleset.check_columns(columns)
        column_error = ""
    except RuleError as exc:
        column_error = str(exc)[:90]
    gate.check("evidence fields exist", not column_error,
               "every rule's evidence resolves to a feature column"
               if not column_error else column_error)

    # ------------------------------------------------------------- layer 1
    con = connect(cfg, features=True)
    try:
        blind = 0
        for table in LABEL_TABLES:
            try:
                con.execute(f"SELECT count(*) FROM {table}")
            except Exception:  # noqa: BLE001 - any binder error is the pass case
                blind += 1
        gate.check("detector cannot see labels", blind == len(LABEL_TABLES),
                   "labels_anomaly and labels_confounder are not in scope")
        try:
            ruleset.check_executable(con)
            bind_error = ""
        except RuleError as exc:
            bind_error = str(exc)[:90]
        gate.check("rules compile to SQL", not bind_error,
                   f"{len(ruleset.enabled)} predicates bind over the feature store"
                   if not bind_error else bind_error)
        l1 = run_rules(con, ruleset)
        again = run_rules(con, ruleset)
    finally:
        con.close()

    gate.check("layer 1 under 60s", l1.seconds < 60,
               f"{l1.total} findings in {l1.seconds:.2f}s")
    gate.check("layer 1 is deterministic",
               again.by_code == l1.by_code
               and [h["description"] for h in again.hits]
                   == [h["description"] for h in l1.hits],
               "a second pass finds the same cases with the same wording")

    # ---------------------------------------------------------------- eval
    scored = harness.evaluate(
        cfg, ruleset, l1,
        runtime={"features": built.seconds, "l1": l1.seconds},
        policy_digest=policy.digest,
        rule_digest=rule_digest(ruleset),
    )
    path = report.write(scored, ROOT / report.REPORT_PATH)

    for row in scored.implemented:
        gate.check(
            f"{row.code} {row.detector.lower()}",
            row.recall == 1.0 and row.precision == 1.0 and row.window_rate == 1.0,
            f"{row.injected} injected, {row.detected} found, {row.hits} raised, "
            f"{_rate(row.precision)} precision, {_rate(row.window_rate)} window",
        )

    gate.check("family A recall", scored.family_recall("A") == 1.0,
               f"{_rate(scored.family_recall('A'))} across 12 codes")
    gate.check("family A precision", scored.family_precision("A") == 1.0,
               f"{_rate(scored.family_precision('A'))} -- a family-A false "
               "positive is a bug in the rule, not a tuning opportunity")
    gate.check("no unaccounted findings", scored.unlabelled_hits == 0,
               f"{l1.total} findings, every one matched to ground truth"
               if not scored.unlabelled_hits
               else f"{scored.unlabelled_hits} unexplained")
    gate.check("no zero-recall detector", not scored.zero_recall,
               f"{len(scored.implemented)}/34 codes have a detector, "
               f"{len(scored.pending)} owned by phases 4-6")

    critical = [c for c in scored.confounders if c.flagged_critical]
    own_code = [c for c in scored.confounders if c.flagged_by_its_code]
    gate.check("confounders not flagged", not critical and not own_code,
               f"{len(scored.confounders)} types, "
               f"{sum(c.planted for c in scored.confounders)} employees, none flagged"
               if not critical and not own_code
               else f"critical={[c.confounder_type for c in critical]}")
    gate.check("precision@100", scored.precision_at.get(100) == 1.0,
               _rate(scored.precision_at.get(100)))
    gate.check("eval report written", path.exists(),
               f"{report.REPORT_PATH}, 34 code rows")

    return gate.report()


# Words a reviewer would have to be a data scientist to read. The evidence
# bundle and every description in it are user-facing (CLAUDE.md), so layer 2 is
# gated on the same standard as the UI: business terms and SAR amounts.
ML_JARGON = (
    "z-score", "z score", "robust z", "standard deviation", "sigma",
    "isolation forest", "autoencoder", "reconstruction", "residual", "shap",
    "percentile", "outlier", "regression", "cusum", "anomaly score", "quantile",
)


def _jargon(text: str) -> list[str]:
    lowered = text.lower()
    return [word for word in ML_JARGON if word in lowered]


def verify_4() -> int:
    """Phase-4 gate: layer 2 peer statistics, the expected-salary model, SHAP.

    The spec's claim for this phase is narrow: family B recall at or above 85%,
    and every cohort either reaches `min_size` or falls back for a reason the
    evidence records.  Everything else here exists to stop that claim being true
    by accident -- that the ladder is not quietly comparing people against four
    peers, that the SAR attribution adds up, that the planted confounders are
    still not flagged, that layer 1 has not regressed, and that nothing layer 2
    writes needs a data scientist to read.
    """
    _add_service_paths()
    from detector.config import DetectorConfig, LakeError
    from detector.eval import harness, report
    from detector.features.build import build
    from detector.lake import connect
    from detector.layers.l1_rules import RuleSet, run_rules
    from detector.layers.l2_peer import DETECTORS, L2Error, run_peer
    from detector.layers.l2_salary import additive_gap
    from detector.policy import DetectorPolicy
    from detector.run import rule_digest

    gate = Gate(4, PHASE_TITLES[4])
    policy = DetectorPolicy.load(ROOT / "policy")

    try:
        cfg = DetectorConfig.build(
            "10k", run_id="verify-4",
            lake=ROOT / "data" / "raw",
            features=ROOT / "data" / "features",
            runs=ROOT / "data" / "runs",
        )
    except LakeError as exc:
        gate.check("lake present", False, str(exc)[:90])
        return gate.report()

    # ----------------------------------------------------------- the pack
    expected_codes = {"B01", "B02", "B03", "B04", "B05", "B06", "B07",
                      "D01", "D02", "D05", "D06", "D07"}
    configured = set(policy.peer_codes)
    gate.check("peer detectors present",
               configured == expected_codes == set(DETECTORS),
               f"{len(configured)} codes, every code the catalogue marks L2"
               if configured == expected_codes
               else f"missing={sorted(expected_codes - configured)}")
    disabled = sorted(c for c, v in policy.peer_codes.items()
                      if not v.get("enabled", True))
    gate.check("every detector enabled", not disabled,
               "a disabled detector is a silent 0% recall row"
               if not disabled else f"disabled={disabled}")

    # ------------------------------------------------------------- layers
    build(cfg, policy)
    ruleset = RuleSet.load(ROOT / "policy")
    con = connect(cfg, features=True)
    try:
        l1 = run_rules(con, ruleset)
        try:
            l2 = run_peer(con, policy)
            again = run_peer(con, policy)
            l2_error = ""
        except L2Error as exc:
            l2, again, l2_error = None, None, str(exc)[:90]
    finally:
        con.close()
    if l2 is None or again is None:
        gate.check("layer 2 runs", False, l2_error)
        return gate.report()

    cohorts = l2.cohorts
    spread = ", ".join(f"L{lvl}={n:,}" for lvl, n in sorted(cohorts.by_level.items()))
    gate.check("cohort ladder resolves",
               sum(cohorts.by_level.values()) == cfg.employees, spread)
    # The gate the spec names: n >= 30, or a fallback the evidence records. An
    # employee still short of min_size on the LAST rung has nowhere further to
    # fall, and their cohort is context in the bundle rather than a trigger.
    last_rung = cohorts.by_level.get(len(cohorts.levels), 0)
    gate.check("cohorts reach n >= 30", cohorts.below_min <= last_rung,
               f"{cfg.employees - cohorts.below_min:,}/{cfg.employees:,} at "
               f"n>={cohorts.min_size}; {cohorts.below_min} short on the last "
               "rung, recorded in the evidence and never a trigger")
    gate.check("cohort design holds", cohorts.last_rung_share < 0.30,
               f"{cohorts.last_rung_share * 100:.0f}% fall all the way to "
               f"{cohorts.levels[-1]} alone, against a 30% ceiling -- above it "
               "the ladder is wrong, not the detector")

    salary = l2.salary
    gate.check("expected salary model fitted", salary is not None and salary.trained,
               f"{salary.rows:,} employees, {len(salary.drivers)} legitimate "
               f"drivers, median gap SAR {salary.median_abs_residual:,.0f}"
               if salary else "not fitted")
    gap = additive_gap(salary)
    gate.check("attributions add up", gap <= 1.0,
               f"{salary.method}: expected pay = baseline + every driver's "
               f"share, to within SAR {gap:.2f}")

    gate.check("layer 2 under 120s", l2.seconds < 120,
               f"{l2.total} findings in {l2.seconds:.2f}s")
    gate.check("layer 2 is deterministic",
               again.by_code == l2.by_code
               and [h["description"] for h in again.hits]
                   == [h["description"] for h in l2.hits],
               "a second pass finds the same cases with the same wording")

    # ---------------------------------------------------------------- eval
    scored = harness.evaluate(
        cfg, ruleset, l1, l2,
        runtime={"l1": l1.seconds, "l2": l2.seconds},
        policy_digest=policy.digest,
        rule_digest=rule_digest(ruleset),
    )
    path = report.write(scored, ROOT / report.REPORT_PATH)
    by_code = {row.code: row for row in scored.codes}

    for code in sorted(expected_codes):
        row = by_code.get(code)
        gate.check(
            f"{code} l2 peer",
            bool(row) and row.recall == 1.0 and (row.precision or 0) >= 0.75,
            f"{row.injected} injected, {row.detected} found, {row.hits} raised, "
            f"{_rate(row.precision)} precision, {_rate(row.window_rate)} window"
            if row else "no eval row",
        )

    recall_b = scored.family_recall("B")
    gate.check("family B recall >= 85%", (recall_b or 0) >= 0.85,
               f"{_rate(recall_b)} across 7 codes -- the phase-4 gate")
    gate.check("family B precision", (scored.family_precision("B") or 0) >= 0.85,
               f"{_rate(scored.family_precision('B'))} -- a statistic is not a "
               "fact, so this is a floor, not the 100% layer 1 owes")
    d_codes = [by_code[c] for c in ("D01", "D02", "D05", "D06", "D07") if c in by_code]
    d_recall = (sum(c.detected for c in d_codes) / sum(c.injected for c in d_codes)
                if d_codes else None)
    gate.check("layer-2 family D recall", (d_recall or 0) >= 0.85,
               f"{_rate(d_recall)} across the 5 family-D codes layer 2 owns")

    # -------------------------------------------------------- the evidence
    bundles = [json.loads(h["evidence_json"]) for h in l2.hits]
    peer_findings = [b for b in bundles if b.get("peer_context")]
    complete = [b for b in peer_findings
                if b["peer_context"].get("cohort_key")
                and b["peer_context"].get("cohort_n")]
    gate.check("peer evidence names its cohort",
               bool(peer_findings) and len(complete) == len(peer_findings),
               f"{len(complete)}/{len(peer_findings)} peer findings carry the "
               "cohort key and its size -- a comparison whose basis the reviewer "
               "cannot see is not evidence")
    attributed = [b for b in bundles if b.get("feature_attributions")]
    gate.check("salary findings carry SAR attribution", bool(attributed),
               f"{len(attributed)} findings split the gap driver by driver, in riyals")

    jargon = sorted({
        word
        for h in l2.hits
        for text in [h["description"], *h["recommended_actions"]]
        for word in _jargon(text)
    })
    gate.check("no ML jargon reaches the reviewer", not jargon,
               f"{len(l2.hits)} descriptions and their actions, against "
               f"{len(ML_JARGON)} banned terms"
               if not jargon else f"found={jargon}")

    critical = [c for c in scored.confounders if c.flagged_critical]
    own_code = [c for c in scored.confounders if c.flagged_by_its_code]
    gate.check("confounders not flagged", not critical and not own_code,
               f"{len(scored.confounders)} types, "
               f"{sum(c.planted for c in scored.confounders)} employees, none "
               "flagged by the code they exist to test"
               if not critical and not own_code
               else f"critical={[c.confounder_type for c in critical]}, "
                    f"own_code={[c.confounder_type for c in own_code]}")

    gate.check("layer 1 has not regressed",
               scored.family_recall("A") == 1.0
               and scored.family_precision("A") == 1.0,
               f"family A still {_rate(scored.family_recall('A'))} recall and "
               f"{_rate(scored.family_precision('A'))} precision over "
               f"{l1.total} findings")
    gate.check("no zero-recall detector", not scored.zero_recall,
               f"{len(scored.implemented)}/34 codes have a detector, "
               f"{len(scored.pending)} owned by phases 5-6")
    gate.check("eval report written", path.exists(),
               f"{report.REPORT_PATH}, 34 code rows")

    return gate.report()


def verify_5() -> int:
    """Phase-5 gate: layer 3 -- isolation forest, autoencoder, graph checks.

    The spec's claim for this phase is family C/D recall at or above 75% and a
    confirmed CUDA path.  Everything else here exists to stop that claim being
    true by accident: that the graph search really is walking a candidate
    subgraph rather than the workforce, that the spousal accounts and the
    quiet field roles are still left alone, that the CPU path a machine without
    a GPU depends on actually runs, that the models rank the injected set above
    the rest of the population rather than scoring everybody the same, and that
    nothing layer 3 writes needs a data scientist to read.
    """
    _add_service_paths()
    from detector.config import DetectorConfig, LakeError
    from detector.eval import harness, report
    from detector.features.build import build
    from detector.lake import connect
    from detector.layers.l1_rules import RuleSet, run_rules
    from detector.layers.l2_peer import run_peer
    from detector.layers.l3_graph import DETECTORS, L3Error, find_cycles, run_l3
    from detector.layers.l3_ml import MLError, build_matrix, train_autoencoder
    from detector.policy import DetectorPolicy
    from detector.run import rule_digest

    gate = Gate(5, PHASE_TITLES[5])
    policy = DetectorPolicy.load(ROOT / "policy")

    try:
        cfg = DetectorConfig.build(
            "10k", run_id="verify-5",
            lake=ROOT / "data" / "raw",
            features=ROOT / "data" / "features",
            runs=ROOT / "data" / "runs",
        )
    except LakeError as exc:
        gate.check("lake present", False, str(exc)[:90])
        return gate.report()

    # ----------------------------------------------------------- the pack
    expected_codes = {"C01", "C02", "C03", "C05", "C06"}
    configured = set(policy.graph_codes)
    gate.check("graph detectors present",
               configured == expected_codes == set(DETECTORS),
               f"{len(configured)} codes, every code the catalogue leaves to "
               "phase 5"
               if configured == expected_codes
               else f"missing={sorted(expected_codes - configured)}")
    disabled = sorted(c for c, v in policy.graph_codes.items()
                      if not v.get("enabled", True))
    gate.check("every detector enabled", not disabled,
               "a disabled detector is a silent 0% recall row"
               if not disabled else f"disabled={disabled}")
    gate.check("contamination is set, not 'auto'",
               isinstance(policy.isolation_forest["contamination"], (int, float)),
               f"{policy.isolation_forest['contamination'] * 100:.1f}% expected "
               "anomaly rate, from the catalogue's own estimate and never from "
               "the injected counts")

    # ------------------------------------------------------------- layers
    build(cfg, policy)
    ruleset = RuleSet.load(ROOT / "policy")
    con = connect(cfg, features=True)
    try:
        l1 = run_rules(con, ruleset)
        l2 = run_peer(con, policy)
        try:
            l3 = run_l3(con, policy)
            again = run_l3(con, policy)
            l3_error = ""
        except (L3Error, MLError) as exc:
            l3, again, l3_error = None, None, str(exc)[:90]
        matrix = build_matrix(con, policy) if l3 else None
    finally:
        con.close()
    if l3 is None or again is None:
        gate.check("layer 3 runs", False, l3_error)
        return gate.report()

    ml = l3.ml
    graph = l3.graph
    gate.check("model matrix built",
               matrix is not None and matrix.rows == cfg.employees,
               f"{matrix.rows:,} employees x {matrix.features} features "
               f"({len(matrix.numeric_names)} numeric, "
               f"{len(matrix.categorical_names)} embedded)"
               if matrix else "not built")
    gate.check("both models fitted", ml is not None and ml.trained,
               f"isolation forest {ml.forest_seconds:.2f}s, autoencoder "
               f"{ml.autoencoder_seconds:.2f}s over {ml.epochs} epochs"
               if ml else "not fitted")

    # The spec's own words: CUDA confirmed, and the CPU path must work. Both
    # halves are checked, because a machine without a GPU is a supported
    # deployment and a hard CUDA dependency would only be found there.
    gate.check("CUDA path confirmed", ml.cuda_available and ml.used_cuda,
               f"trained on `{ml.device}`"
               if ml.used_cuda
               else f"cuda available={ml.cuda_available}, trained on {ml.device}")
    cpu_started = time.perf_counter()
    try:
        small = _slice_matrix(matrix, 2000)
        gap, _above, cpu_device, _loss, _epochs = train_autoencoder(
            small, {**policy.autoencoder, "epochs": 2}, device="cpu"
        )
        cpu_ok = cpu_device == "cpu" and gap.shape == (small.rows, small.features)
    except Exception as exc:  # noqa: BLE001 - the gate reports, it does not raise
        cpu_ok, cpu_device = False, f"{type(exc).__name__}: {exc}"[:60]
    gate.check("CPU path works", cpu_ok,
               f"the same net fits on cpu in {time.perf_counter() - cpu_started:.1f}s "
               "-- slower is fine, a hard CUDA dependency is not"
               if cpu_ok else str(cpu_device))

    # ------------------------------------------------------------- graph
    gate.check("graph stays a candidate subgraph",
               graph.graph_nodes < cfg.employees * 0.05,
               f"{graph.graph_nodes:,} linked employees of {cfg.employees:,} "
               f"in {graph.components} components, largest "
               f"{graph.largest_component} -- networkx never sees the workforce")
    gate.check("components are classified",
               sum(graph.by_class.values()) == graph.components
               and set(graph.by_class) <= {"unrelated", "spousal", "near_duplicate"},
               ", ".join(f"{name} {count}"
                         for name, count in sorted(graph.by_class.items()))
               + " -- a declared joint account is not a finding and a shared "
                 "date of birth is C06, not a suppressed C01")
    cycles = find_cycles([("a", "b"), ("b", "c"), ("c", "a"), ("d", "a")], 6)
    gate.check("cycle detection works",
               cycles == [["a", "b", "c"]],
               f"{graph.cycles_found} cycles in this lake and "
               f"{graph.cycle_candidates} candidates, so the finder is checked "
               "against a known chain instead")

    gate.check("layer 3 under 120s", l3.seconds < 120,
               f"{l3.total} findings in {l3.seconds:.2f}s")
    gate.check("layer 3 is deterministic",
               again.by_code == l3.by_code
               and [h["description"] for h in again.hits]
                   == [h["description"] for h in l3.hits],
               "a second pass finds the same cases with the same wording")

    # ---------------------------------------------------------------- eval
    scored = harness.evaluate(
        cfg, ruleset, l1, l2, l3,
        runtime={"l1": l1.seconds, "l2": l2.seconds, "l3": l3.seconds},
        policy_digest=policy.digest,
        rule_digest=rule_digest(ruleset),
    )
    path = report.write(scored, ROOT / report.REPORT_PATH)
    by_code = {row.code: row for row in scored.codes}

    for code in sorted(expected_codes):
        row = by_code.get(code)
        gate.check(
            f"{code} l3",
            bool(row) and row.recall == 1.0 and (row.precision or 0) >= 0.75,
            f"{row.injected} injected, {row.detected} found, {row.hits} raised, "
            f"{_rate(row.precision)} precision, {_rate(row.window_rate)} window"
            if row else "no eval row",
        )

    recall_c = scored.family_recall("C")
    recall_d = scored.family_recall("D")
    gate.check("family C recall >= 75%", (recall_c or 0) >= 0.75,
               f"{_rate(recall_c)} across all 8 codes -- the phase-5 gate")
    gate.check("family D recall >= 75%", (recall_d or 0) >= 0.75,
               f"{_rate(recall_d)} across all 7 codes")
    gate.check("family C precision", (scored.family_precision("C") or 0) >= 0.90,
               f"{_rate(scored.family_precision('C'))} -- an identity finding "
               "names records, so a false one is a bug rather than a judgement")

    separation = scored.ml
    gate.check("the models rank the injected set high",
               separation is not None and separation.lift >= 2.0,
               f"{_rate(separation.top_decile_recall)} of injected employees "
               f"are in the top tenth ({separation.lift:.1f}x a random tenth); "
               f"median {separation.labelled_median:.0f} against "
               f"{separation.population_median:.0f} for the population"
               if separation else "not scored")

    # -------------------------------------------------------- the evidence
    bundles = [json.loads(h["evidence_json"]) for h in l3.hits]
    linked = [b for b in bundles if b.get("graph_context")]
    named = [b for b in linked
             if b["graph_context"].get("related_employees")
             and b["graph_context"].get("link_value_masked")]
    gate.check("graph evidence names the other records",
               bool(linked) and len(named) == len(linked),
               f"{len(named)}/{len(linked)} linked findings list every employee "
               "on the account or the identity number -- a link the reviewer "
               "cannot see the other end of is not evidence")
    unmasked = [
        b for b in linked
        if len(str(b["graph_context"].get("link_value_masked") or "")) > 6
    ]
    gate.check("identifiers are masked", not unmasked,
               f"{len(linked)} findings quote the last "
               f"{policy.graph['mask_visible_digits']} digits only",
               )
    attributed = [b for b in bundles if b.get("feature_attributions")]
    gate.check("ghost findings carry a model attribution", bool(attributed),
               f"{len(attributed)} findings name the columns the models could "
               "not account for")

    jargon = sorted({
        word
        for h in l3.hits
        for text in [h["description"], *h["recommended_actions"]]
        for word in _jargon(text)
    })
    gate.check("no ML jargon reaches the reviewer", not jargon,
               f"{len(l3.hits)} descriptions and their actions, against "
               f"{len(ML_JARGON)} banned terms"
               if not jargon else f"found={jargon}")

    critical = [c for c in scored.confounders if c.flagged_critical]
    own_code = [c for c in scored.confounders if c.flagged_by_its_code]
    gate.check("confounders not flagged", not critical and not own_code,
               f"{len(scored.confounders)} types, "
               f"{sum(c.planted for c in scored.confounders)} employees, none "
               "flagged by the code they exist to test -- including the "
               "spousal accounts and the quiet field roles this phase owns"
               if not critical and not own_code
               else f"critical={[c.confounder_type for c in critical]}, "
                    f"own_code={[c.confounder_type for c in own_code]}")

    gate.check("layers 1 and 2 have not regressed",
               scored.family_recall("A") == 1.0
               and scored.family_precision("A") == 1.0
               and (scored.family_recall("B") or 0) >= 0.85,
               f"family A still {_rate(scored.family_recall('A'))} recall and "
               f"{_rate(scored.family_precision('A'))} precision; family B "
               f"{_rate(scored.family_recall('B'))} recall over "
               f"{l1.total + l2.total} findings")
    gate.check("every code has a detector", not scored.pending,
               f"{len(scored.implemented)}/34 codes -- the eval report has no "
               "'not built' row left")
    gate.check("no zero-recall detector", not scored.zero_recall,
               f"{len(scored.implemented)} detectors, none finding nothing")
    gate.check("eval report written", path.exists(),
               f"{report.REPORT_PATH}, 34 code rows, sections 2b and 2c")

    return gate.report()


def _slice_matrix(matrix, rows: int):
    """The first `rows` of a feature matrix -- the CPU-path check runs small.

    Proving the CPU path means proving the same net builds and trains without
    CUDA, not paying for a second full fit inside a phase gate.
    """
    from dataclasses import replace

    take = min(rows, matrix.rows)
    return replace(
        matrix,
        employees=matrix.employees[:take],
        numeric=matrix.numeric[:take],
        categorical=matrix.categorical[:take],
        values={name: column[:take] for name, column in matrix.values.items()},
    )


def _rate(value: float | None) -> str:
    return "--" if value is None else f"{value * 100:.0f}%"


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
    if phase == 3:
        return verify_3()
    if phase == 4:
        return verify_4()
    if phase == 5:
        return verify_5()
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


def cmd_detect(args: argparse.Namespace) -> int:
    """Thin wrapper over `python -m detector run`, which is the real CLI."""
    _add_service_paths()
    from detector.__main__ import main as detector_main

    argv = ["run", "--scale", args.scale]
    if args.run_id:
        argv += ["--run-id", args.run_id]
    if args.stages:
        argv += ["--stages", args.stages]
    if args.force:
        argv.append("--force")
    return detector_main(argv)


def cmd_eval(args: argparse.Namespace) -> int:
    """Thin wrapper over `python -m detector eval`; writes docs/EVAL_REPORT.md."""
    _add_service_paths()
    from detector.__main__ import main as detector_main

    argv = ["eval", "--scale", args.scale]
    if args.run_id:
        argv += ["--run-id", args.run_id]
    if args.force:
        argv.append("--force")
    return detector_main(argv)


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
    p.add_argument("--stages", default=None,
                   help="comma-separated subset of features,l1,l2,l3")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("eval", help="run the evaluation harness (phase 3)")
    p.add_argument("--scale", choices=["10k", "100k", "1m"], default="10k")
    p.add_argument("--run-id", default=None)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("api", help="start the FastAPI backend (phase 8)")
    p.set_defaults(func=lambda a: _not_yet("api", 8))

    p = sub.add_parser("web", help="start the Vite dev server (phase 9)")
    p.set_defaults(func=lambda a: _not_yet("web", 9))

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
