# EVIDENCE_CONTRACT.md

**Authoritative.** The evidence bundle is the one object the detector, the API, the UI and the LLM
all share. Everything a reviewer sees about an alert comes from here. If a number is not in the
bundle, it must not appear on screen and the LLM must not say it.

The bundle is produced by layer 4 (`services/detector`, phase 6), persisted as JSONB in Postgres
alongside the alert row, and returned verbatim by `GET /alerts/{id}`. Between the detector and
Postgres it travels in the `evidence_json` column of `data/runs/scale=<n>/run_id=<id>/alerts.parquet`, one row
per alert: phase 8 upserts the queue and its bundles in one pass, and thirty-five thousand small
JSON files at 1m scale would be a directory nobody can copy.

**One alert is one employee and one anomaly code.** That grain is fixed by two rules below --
`alert_id` is stable for (employee, code, evidence fingerprint) and suppression matches on the same
three -- and it is what collapses a detector that flagged two separate months into the one case a
reviewer works. `anomaly_codes` and `families` are arrays because the shape must survive a future
that fuses codes into one case; today each carries exactly one entry.

## Design rules

1. **Self-contained.** A reviewer opening an alert six months later must be able to understand it
   without querying anything else. Denormalise freely — site names, job titles and cohort sizes are
   copied in, not referenced.
2. **Plain language.** `reasons[].text` and `recommended_actions[]` are read by non-technical HR and
   audit staff. No "z-score", no "isolation forest", no "reconstruction error". Say
   *"higher than 99% of the 412 people doing the same job at similar sites"*.
3. **Every figure is grounded.** The LLM narrator rephrases this object and nothing else. A
   post-check asserts that every numeral in the generated text appears in the bundle (§5).
4. **Additive evolution only.** Adding a field is safe. Renaming or removing one is a breaking
   change: bump `schema_version`, update this file, the API contract and the UI in the same commit.

## Schema — version 1

```json
{
  "schema_version": 1,
  "alert_id": "ALT-000173",
  "run_id": "2026-08",
  "employee_id": "E00042317",
  "employee_display": {
    "name_en": "Abdullah Al-Otaibi", "name_ar": "عبدالله العتيبي",
    "badge_no": "B0421739", "grade": 12, "job_title_en": "Senior Process Engineer",
    "org_unit_name_en": "Gas Operations / Hawiyah Section",
    "site_name_en": "Dhahran Headquarters", "site_id": "EP-HQ-DHA",
    "region_code": "SA-04", "employment_type": "direct", "status": "active"
  },

  "anomaly_codes": ["A01"],
  "families": ["A"],
  "severity": "CRITICAL",
  "score": 94,
  "layer_scores": { "rules": 100, "peer_stats": 71, "ml_unsupervised": 46, "graph": 0 },
  "contributing_layers": ["rules", "peer_stats"],

  "reasons": [
    { "type": "rule",
      "rule_id": "A01",
      "text": "Remote-site allowance of SAR 3,200 per month has been paid since March 2024 while the employee is posted to Dhahran Headquarters, which is a head-office site and is not approved for remote-site payments.",
      "regulatory_reference": "HR-COMP-011 Remote Assignment s.1",
      "since": "202403",
      "evidence_fields": {
        "work_site_id": "EP-HQ-DHA", "site_class": "hq",
        "site_remote_allowance_eligible": false, "site_hardship_tier": 0,
        "allowance_REMOTE_SITE_amount": 3200, "months_paid": 12
      } },
    { "type": "peer",
      "text": "Total allowances are 47% of base pay, against a typical 21% for the 412 peers at grade 12 in Process Ops at office sites.",
      "since": "202403" }
  ],

  "peer_context": {
    "cohort_key": "grade=12|job_family=Process Ops|site_class=office|nationality_class=saudi",
    "cohort_key_level": 3,
    "cohort_key_fallback_reason": "service_band and nationality_class dropped to reach n>=30",
    "cohort_n": 412,
    "metric": "allowance_ratio",
    "employee_value": 0.47, "cohort_median": 0.21, "cohort_mad": 0.06,
    "percentile": 99.4, "robust_z": 4.3
  },

  "graph_context": {
    "link_type": "shared_iban",
    "link_value_masked": "4281",
    "component_size": 3,
    "component_class": "unrelated",
    "total_monthly_disbursement": 75637.0,
    "related_employees": [
      { "employee_id": "E00003043", "name_en": "Kamran Badr Khan",
        "org_unit_name_en": "Downstream Manufacturing Section 650",
        "site_name_en": "Khurais Field Camp", "monthly_net": 18388.42 }
    ]
  },

  "feature_attributions": [
    { "feature": "allowance_REMOTE_SITE", "label_en": "Remote-site allowance", "contribution": 0.41, "direction": "increases", "value_sar": 3200 },
    { "feature": "allowance_ratio",       "label_en": "Allowances as share of pay", "contribution": 0.22, "direction": "increases", "value_sar": null }
  ],

  "timeline": [
    { "period": 202403, "base_pay": 29500, "allowance_total": 13900, "net": 39120, "flagged": true, "event": "REMOTE_SITE allowance starts" },
    { "period": 202404, "base_pay": 29500, "allowance_total": 13900, "net": 39120, "flagged": true, "event": null }
  ],

  "financial_impact": {
    "monthly": 3200, "cumulative": 38400, "currency": "SAR",
    "basis": "allowance_REMOTE_SITE_amount * months_paid",
    "confidence": "exact",
    "periods_affected": { "from": 202403, "to": 202502 }
  },

  "recommended_actions": [
    "Suspend the remote-site allowance pending review",
    "Request site-posting confirmation from Division HR",
    "Raise a recovery case for the 12 months already paid (SAR 38,400)"
  ],

  "similar_cases": ["ALT-000091", "ALT-000233"],

  "suppression": { "suppressed": false, "reason": null, "prior_disposition_id": null },

  "provenance": {
    "detector_version": "1.0.0",
    "policy_digest": "sha256:9f2c…",
    "scored_at": "2026-08-31T04:12:07Z",
    "data_scale": "1m",
    "correlation_id": "c1f0a5e2-…",
    "evidence_fingerprint": "sha256:41ba…",
    "severity_thresholds": { "CRITICAL": 99, "HIGH": 88, "MEDIUM": 55 }
  }
}
```

## Field rules

| Field | Rule |
|---|---|
| `alert_id` | `ALT-` + zero-padded 6 digits. Stable across runs for the same (employee, code, evidence fingerprint). |
| `severity` | One of `CRITICAL`, `HIGH`, `MEDIUM`, `WATCHLIST`. Derived from `score`, never set by hand. The band floors in `policy/fusion.yaml` are the bound and `alert_budget` is the capacity: bands are filled from the top of the queue until the slots run out, so an alert tied on score with the last one admitted to a full band falls to the next one down. `score >= provenance.severity_thresholds[severity]` therefore always holds, and the reverse does not. |
| `score` | Integer 0–100. |
| `layer_scores` | Every layer present, `0` where the layer did not contribute. Never null. |
| `reasons` | **At least one, always.** An alert with no reason is a bug — that is the whole product promise. `type` ∈ `rule`, `peer`, `ml`, `graph`, `temporal`. |
| `reasons[].text` | Plain English, complete sentence, business terms, figures formatted with thousands separators. |
| `peer_context` | Required when `peer_stats` contributed; null otherwise. `cohort_key` and `cohort_n` are mandatory when present — a comparison the reviewer cannot see the basis of is not evidence. |
| `graph_context` | Required when `graph` contributed; null otherwise. `link_value_masked` and `related_employees` are mandatory when present — a link the reviewer cannot see the other end of is not evidence. **`link_value_masked` carries the last four digits only**; the whole account or identity number never reaches the bundle, a description or an action. `component_class` ∈ `unrelated`, `spousal`, `near_duplicate` and says *why* the component is a finding: a declared joint account is no finding at all and a shared date of birth is C06, so the class is the record of a decision rather than a suppressed alert. |
| `feature_attributions` | Sorted by `|contribution|` descending, max 10. `label_en` is what the UI renders; `feature` is the internal name and is never displayed. `direction` ∈ `increases`, `reduces`, `unexpected` — the third is for a categorical, where the model expected a different value rather than a larger or smaller one. `contribution` is in SAR where the layer that produced it works in SAR (the expected-salary model) and a share of the record's total gap where it does not (layer 3). |
| `timeline` | Exactly the periods in the run window (24), ascending, no gaps. `flagged` marks the anomaly window. |
| `financial_impact` | Always present. `confidence` ∈ `exact` (rule-derived), `estimated` (model-derived), `unknown`. `monthly` may be 0 for non-financial findings (e.g. A11), never null. |
| `recommended_actions` | 1–5 imperative sentences. Specific enough to act on without further analysis. |
| `similar_cases` | Up to 5 `alert_id`s sharing the anomaly code and a comparable evidence shape. |
| `provenance.policy_digest` | Hash of the policy pack the alert was scored under. An alert scored under a superseded policy must be visibly stale, not silently wrong. |
| `provenance.severity_thresholds` | The score at each band boundary **this run actually produced**, because the budget moves them every run. A stored alert is checked against the bands it was banded under rather than against today's pack, which is the only comparison that means anything six months later. |
| `provenance.evidence_fingerprint` | The hash suppression matches on: the window, the money and the evidence fields, never the score. A dismissed finding that comes back with a materially larger amount has a different fingerprint and is a new alert. |
| `provenance.correlation_id` | Spans UI → API → detector. Derived from the run id and the policy digest, so a re-run of the same data quotes the same id rather than inventing one both sides then disagree about. |

## Validation

`services/detector` validates every bundle against
`services/detector/detector/evidence/schemas/evidence_v1.json` before writing. An invalid bundle
fails the run: it would otherwise be discovered by the UI, in front of a reviewer, months later.
The phase-6 gate fails if any bundle does not validate, and additionally asserts:

- every alert has ≥ 1 reason with non-empty `text`;
- every `CRITICAL`/`HIGH` alert has a non-null `financial_impact.monthly`;
- `contributing_layers` is consistent with the non-zero entries in `layer_scores`;
- `severity` agrees with `provenance.severity_thresholds` — every alert scores at or above the
  boundary its band was cut at, and any alert the budget kept out of a full band is tied on score
  with the last one admitted;
- `timeline` covers exactly the run window, ascending, with no gaps;
- the validator itself rejects a bundle with its `reasons` removed, because a schema nothing has
  ever seen fail is documentation rather than a gate.

## 5. LLM grounding rule

The narrator (phase 12) receives the bundle and returns prose. It **never computes and never
invents**. The post-check extracts every numeric token from the generated text and asserts each one
appears in the bundle (allowing for formatting: `3200`, `3,200` and `SAR 3,200` are the same token;
percentages and dates are checked against their source fields).

A failed check discards the generated text and falls back to the deterministic template built from
`reasons[].text`. **The product must be fully usable with the LLM switched off** — the deterministic
path is the baseline, and the LLM is an enhancement to it.
