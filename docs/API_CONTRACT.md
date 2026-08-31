# API_CONTRACT.md

FastAPI backend (`services/api`), delivered in phase 8. Base path `/api/v1`. This document is the
contract; the OpenAPI schema generated at `/api/v1/openapi.json` must match it, and the phase-8 gate
asserts that.

## Conventions

- **JSON only.** `Content-Type: application/json`, UTF-8. Arabic strings pass through unescaped.
- **Server-side everything.** Pagination, filtering and sorting are always server-side. There is no
  endpoint that can return an unbounded list — the UI must never be able to ask for 1M rows.
- **Pagination**: `?page=1&page_size=50` (max 200). Every list response is
  `{ "items": [...], "page": 1, "page_size": 50, "total": 4127, "total_pages": 83 }`.
- **Sorting**: `?sort=-score,employee_id` — `-` prefix for descending. Only whitelisted columns.
- **Filtering**: repeated params are OR within a field, AND across fields
  (`?severity=CRITICAL&severity=HIGH&family=A` → (CRITICAL or HIGH) and family A).
- **Errors**: RFC 7807 problem+json — `{ "type", "title", "status", "detail", "correlation_id" }`.
- **Correlation**: every request carries `X-Correlation-Id` (generated if absent) and it appears in
  every log line the request touches, UI → API → detector.
- **Auth**: `Authorization: Bearer <JWT>`. Roles `reviewer`, `investigator`, `admin`. Row-level
  scoping by org unit is applied to every alert and employee query — a reviewer sees only their
  scope, and this is enforced in the query, never in the UI.
- **Timestamps**: ISO 8601 UTC. **Periods**: integer `YYYYMM`.

## Endpoints

### Alerts

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/alerts` | Paginated, filterable, sortable alert list. The triage queue. |
| `GET` | `/alerts/{alert_id}` | Full evidence bundle (`docs/EVIDENCE_CONTRACT.md`) verbatim. |
| `POST` | `/alerts/{alert_id}/disposition` | Record a reviewer decision. |
| `GET` | `/alerts/{alert_id}/history` | Every disposition and change on this alert. |

`GET /alerts` filters: `severity`, `family`, `anomaly_code`, `org_unit_id`, `site_id`, `region_code`,
`status` (`open`, `confirmed`, `dismissed`, `escalated`, `info_requested`), `assignee_id`,
`score_min`, `score_max`, `impact_min`, `impact_max`, `run_id`, `suppressed` (default `false`),
`q` (free text over employee name/id/badge).

List item shape — deliberately small, because this endpoint serves a virtualised table:

```json
{ "alert_id": "ALT-000173", "employee_id": "E00042317",
  "employee_name_en": "Abdullah Al-Otaibi", "badge_no": "B0421739",
  "anomaly_codes": ["A01"], "severity": "CRITICAL", "score": 94,
  "headline_reason": "Remote-site allowance paid while posted to Dhahran Headquarters",
  "financial_impact_monthly": 3200, "financial_impact_cumulative": 38400,
  "site_id": "EP-HQ-DHA", "site_name_en": "Dhahran Headquarters", "region_code": "SA-04",
  "org_unit_name_en": "Gas Operations / Hawiyah Section",
  "status": "open", "assignee_id": null, "first_seen_run": "2026-07", "run_id": "2026-08" }
```

`headline_reason` is `reasons[0].text` truncated to 120 characters — the one-line plain-English
reason shown inline in the queue, so a reviewer can triage without opening anything.

`POST /alerts/{id}/disposition` body:

```json
{ "decision": "dismiss", "reason_code": "approved_exception",
  "note": "Site posting confirmed by Division HR, exception on file ref HR-2026-0412.",
  "expected_version": 3 }
```

`decision` ∈ `confirm`, `dismiss`, `escalate`, `request_info`. `reason_code` is required for
`dismiss` and `escalate`. `expected_version` gives optimistic concurrency: a stale write returns
`409` with the current state, which is what lets the UI reconcile an optimistic update instead of
silently overwriting a colleague.

**Every disposition writes an audit row** — who, when, what, previous value. Non-negotiable for HR
and audit use; there is no code path that dispositions without auditing.

### Employees

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/employees/{employee_id}/360` | Profile, pay timeline, allowance history, entitlement matrix vs policy, peer comparison, all alerts. |
| `GET` | `/employees/{employee_id}/timeline` | 24-month pay and allowance series for charting. |

### Cases

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/cases` | Paginated case list. |
| `POST` | `/cases` | Create a case from a set of alert ids. |
| `GET` | `/cases/{case_id}` | Case with member alerts and activity trail. |
| `PATCH` | `/cases/{case_id}` | Assign, change status, add note. |

### Runs

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/runs` | Run history with counts and timings. |
| `GET` | `/runs/{run_id}/summary` | Totals by severity and family, budget adherence, runtime profile. |
| `GET` | `/runs/{run_id}/diff?against={run_id}` | New / resolved / worsened alerts between two runs. |

### Analytics

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/analytics/overview` | Dashboard headline figures. |
| `GET` | `/analytics/by-dimension?dim=site\|org_unit\|family\|anomaly_code\|region` | Aggregates. |
| `GET` | `/analytics/exposure-trend` | Financial exposure over runs. |
| `GET` | `/analytics/geo` | **The map endpoint.** |

`GET /analytics/geo?period=YYYYMM&metric=…&family=…&anomaly_code=…&org_unit_id=…`

`metric` ∈ `alert_count`, `alerts_per_1000` (**default**), `financial_exposure`, `critical_count`.

`alerts_per_1000` is the default deliberately: Eastern Province headcount dominance would otherwise
turn every heat map into a population map (`docs/PLAN.md` §11).

```json
{ "period": 202408, "metric": "alerts_per_1000",
  "regions": [ { "region_code": "SA-04", "value": 3.1, "alert_count": 412, "headcount": 132000,
                 "delta_vs_prev_period": -0.4, "top_codes": ["A01","B04","A05"] } ],
  "sites":   [ { "site_id": "EP-HQ-DHA", "lat": 26.2794, "lon": 50.1583, "value": 5.2,
                 "alert_count": 94, "headcount": 18000,
                 "severity_mix": { "CRITICAL": 6, "HIGH": 31, "MEDIUM": 57 } } ] }
```

Served entirely from `agg_alerts_by_site_month`, written during the batch. Each frame is a small
pre-computed payload — that is what makes the 24-month animation smooth.

### Explanation

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/explain/{alert_id}` | Plain-English narration of the evidence bundle. |
| `POST` | `/explain/{alert_id}/ask` | Grounded Q&A about one alert. |

Response always carries `{ "text": "...", "source": "llm" | "template", "cached": true|false }`.
`source: "template"` is a normal, healthy response — not an error — and the UI renders it without
any degraded-mode styling. See `docs/LLM_PORTABILITY.md`.

### Policy

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/policy/rules` | Every rule in plain language, for the Policy Explorer screen. |
| `GET` | `/policy/allowances` | The 26 allowance codes with resolved eligibility text. |

Read-only. Reviewers need to understand what the system checks; letting them edit it from the UI is
a different product with a different approval workflow.

### Scoring, exports, health

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/score/employee/{employee_id}` | On-demand re-score / what-if. Proxies to the detector. |
| `POST` | `/exports` | Start a CSV / XLSX / PDF export of the current filter set. Returns a job id. |
| `GET` | `/exports/{job_id}` | Poll status, then download. |
| `GET` | `/health`, `/ready` | Liveness and dependency readiness. Unauthenticated. |

Exports are asynchronous because a filter set can be large and a reviewer should not hold a request
open for it.

## Status codes

`200` ok · `201` created · `202` accepted (exports) · `400` validation · `401` unauthenticated ·
`403` out of org scope · `404` not found · `409` version conflict · `422` semantic validation ·
`429` rate limited · `503` dependency unavailable (never returned merely because Ollama is down).
