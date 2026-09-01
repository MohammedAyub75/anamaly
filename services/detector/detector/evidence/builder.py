"""Assembles and validates the evidence bundle -- `docs/EVIDENCE_CONTRACT.md`.

The bundle is the whole product promise made concrete: a reviewer opening an
alert six months from now must understand it without querying anything else, so
site names, job titles, cohort sizes and the other records on a shared bank
account are copied in rather than referenced.

Validation is not advisory.  `schemas/evidence_v1.json` is that contract made
machine-checkable, and layer 4 refuses to write a bundle that fails it: an
invalid bundle reaching Postgres would be discovered by the UI, in front of a
reviewer, months later.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).parent / "schemas" / "evidence_v1.json"

# Which `reasons[].type` each fusion layer speaks with. The contract's five
# types are about what kind of statement the reviewer is reading, not about
# which module produced it.
REASON_TYPE = {
    "rules": "rule",
    "peer_stats": "peer",
    "ml_unsupervised": "ml",
    "graph": "graph",
}

MAX_ACTIONS = 5
MAX_SIMILAR = 5


class EvidenceError(RuntimeError):
    """A bundle that does not satisfy `evidence_v1.json`. Always a bug here."""


@lru_cache(maxsize=1)
def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _validator():
    """The compiled validator. Built once: schema compilation is not free and
    layer 4 checks every bundle it writes."""
    from jsonschema import Draft202012Validator

    return Draft202012Validator(load_schema())


def validate(bundle: dict) -> None:
    """Raise `EvidenceError` naming the first field that breaks the contract."""
    errors = sorted(_validator().iter_errors(bundle), key=lambda e: list(e.path))
    if not errors:
        return
    first = errors[0]
    where = "/".join(str(p) for p in first.path) or "(root)"
    raise EvidenceError(
        f"{bundle.get('alert_id', '?')}: evidence_v1.json rejected `{where}`: "
        f"{first.message}"
        + (f" (+{len(errors) - 1} more)" if len(errors) > 1 else "")
    )


def fingerprint(employee_id: str, anomaly_code: str, findings: list[dict]) -> str:
    """A stable hash of what this alert actually says.

    Suppression matches on it (`policy/fusion.yaml`), so it has to move when
    the substance moves and stay still when nothing has: a dismissed finding
    that comes back with a larger amount is a new finding, not a suppressed
    one. Built from the window, the money and the evidence fields -- never from
    the score, which changes with every weight a customer turns.
    """
    digest = hashlib.sha256()
    digest.update(f"{employee_id}|{anomaly_code}".encode())
    for finding in sorted(
        findings, key=lambda f: (f["period_from"], f["period_to"], f.get("allowance_code") or "")
    ):
        evidence = _parsed(finding)
        digest.update(
            json.dumps(
                {
                    "from": finding["period_from"],
                    "to": finding["period_to"],
                    "monthly": round(float(finding["financial_impact_monthly"]), 2),
                    "cumulative": round(
                        float(finding["financial_impact_cumulative"]), 2
                    ),
                    "fields": evidence.get("fields", {}),
                },
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        )
    return "sha256:" + digest.hexdigest()


class AlertIdRegistry:
    """`ALT-000173`, stable across runs for one (employee, code, fingerprint).

    A hash folded into six digits would collide -- at 1m scale certainly -- and
    two different findings sharing an alert id is a case a reviewer closes
    twice. So the mapping is kept: a small JSON file beside the runs, appended
    to in sorted key order so a rerun of the same data assigns nothing new and
    two machines given the same data agree.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.ids: dict[str, int] = {}
        if self.path.exists():
            self.ids = {
                str(k): int(v)
                for k, v in json.loads(
                    self.path.read_text(encoding="utf-8")
                ).items()
            }
        self._dirty = False

    @staticmethod
    def key(employee_id: str, anomaly_code: str, evidence_fingerprint: str) -> str:
        return f"{employee_id}|{anomaly_code}|{evidence_fingerprint}"

    def assign(self, keys: list[str]) -> dict[str, str]:
        """Ids for every key, minting only those that have never been seen."""
        nxt = max(self.ids.values(), default=0) + 1
        for key in sorted(set(keys) - set(self.ids)):
            self.ids[key] = nxt
            nxt += 1
            self._dirty = True
        return {key: f"ALT-{self.ids[key]:06d}" for key in keys}

    def save(self) -> Path:
        if self._dirty:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.ids, indent=0, sort_keys=True), encoding="utf-8"
            )
        return self.path


def _parsed(finding: dict) -> dict:
    payload = finding.get("evidence_json")
    if not payload:
        return {}
    if isinstance(payload, dict):
        return payload
    return json.loads(payload)


def _first(findings: list[dict], block: str) -> Any:
    for finding in findings:
        value = _parsed(finding).get(block)
        if value:
            return value
    return None


def _reasons(findings: list[dict], layer: str) -> list[dict]:
    """One reason per fused finding, in the words the layer already wrote.

    The layers render plain-language descriptions for exactly this purpose, so
    nothing is rephrased here: a second wording of one fact is a second thing
    to keep true.
    """
    out = []
    for finding in findings:
        evidence = _parsed(finding)
        out.append(
            {
                "type": REASON_TYPE.get(layer, "rule"),
                "rule_id": finding["anomaly_code"],
                "text": finding["description"],
                "regulatory_reference": finding.get("regulatory_reference"),
                "since": str(finding["period_from"]),
                "evidence_fields": evidence.get("fields") or {},
            }
        )
    return out


def _actions(findings: list[dict]) -> list[str]:
    """The fused findings' actions, deduplicated and capped at the contract's five."""
    seen: list[str] = []
    for finding in findings:
        for action in finding.get("recommended_actions") or []:
            if action and action not in seen:
                seen.append(action)
    return seen[:MAX_ACTIONS] or ["Review the record and confirm the entitlement"]


def build_bundle(
    *,
    alert_id: str,
    run_id: str,
    employee_id: str,
    anomaly_code: str,
    layer: str,
    severity: str,
    score: int,
    layer_scores: dict[str, float],
    contributing_layers: list[str],
    findings: list[dict],
    corroboration: list[dict],
    display: dict[str, Any],
    timeline: list[dict],
    impact: dict[str, Any],
    similar_cases: list[str],
    suppression: dict[str, Any],
    provenance: dict[str, Any],
) -> dict:
    """One alert as the object everything downstream reads. Validated by caller."""
    reasons = _reasons(findings, layer) + list(corroboration)
    bundle: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "alert_id": alert_id,
        "run_id": str(run_id),
        "employee_id": employee_id,
        "employee_display": {
            "name_en": display.get("name_en") or employee_id,
            "name_ar": display.get("name_ar"),
            "badge_no": str(display.get("badge_no") or ""),
            "grade": int(display.get("grade") or 0),
            "job_title_en": str(display.get("job_title_en") or ""),
            "org_unit_name_en": display.get("org_unit_name_en"),
            "site_name_en": display.get("site_name_en"),
            "site_id": display.get("site_id"),
            "region_code": display.get("region_code"),
            "employment_type": display.get("employment_type"),
            "status": display.get("status"),
        },
        "anomaly_codes": [anomaly_code],
        "families": [anomaly_code[0]],
        "severity": severity,
        "score": int(score),
        "layer_scores": {
            name: round(float(value), 1) for name, value in layer_scores.items()
        },
        "contributing_layers": list(contributing_layers),
        "reasons": reasons,
        "peer_context": _first(findings, "peer_context"),
        "graph_context": _first(findings, "graph_context"),
        "feature_attributions": _first(findings, "feature_attributions"),
        "timeline": timeline,
        "financial_impact": impact,
        "recommended_actions": _actions(findings),
        "similar_cases": similar_cases[:MAX_SIMILAR],
        "suppression": suppression,
        "provenance": provenance,
    }
    return bundle
