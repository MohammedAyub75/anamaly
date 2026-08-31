# SPEC — `services/api`

**Self-contained build spec** for the FastAPI backend-for-frontend. Phase 8 builds this.

The endpoint-by-endpoint contract is `docs/API_CONTRACT.md` and it is authoritative — this file
covers how to build the service, not what each route returns.

## Responsibilities

Serve the UI from Postgres. Nothing more. The API does not detect, does not score in-process, and
**never scans Parquet on a user request** — a triage queue page load is a handful of indexed
Postgres reads. On-demand re-scoring is proxied to the detector service.

## Layout

```
services/api/
  pyproject.toml
  api/
    main.py            # app factory, middleware, exception handlers
    config.py          # pydantic-settings, env-driven
    db/
      session.py       # async SQLAlchemy engine + session dependency
      models.py        # ORM models
      migrations/      # alembic
    routers/
      alerts.py cases.py employees.py runs.py analytics.py
      explain.py policy.py exports.py auth.py health.py
    schemas/           # pydantic v2 request/response models
    services/          # query logic, kept out of the routers
    security/
      jwt.py scoping.py    # role checks + org-unit row-level scoping
    audit.py           # the audit-write helper every mutation goes through
  tests/
```

Routers stay thin: parse, authorise, delegate to `services/`, serialise. Query logic in a router is
how a service becomes untestable.

## Postgres schema

| Table | Notes |
|---|---|
| `alerts` | One row per alert per run. `evidence JSONB`. Indexed on `(run_id, severity, score DESC)`, `(employee_id)`, `(site_id, period)`, GIN on `evidence`. |
| `alert_status` | Current workflow state, separate from the immutable scored row. |
| `dispositions` | Append-only. Every reviewer decision. |
| `cases` / `case_alerts` | Investigation grouping. |
| `employees_cache` | The subset of `employee_master` the UI needs — name, badge, grade, org, site. Refreshed per run. Avoids a Parquet read on every alert render. |
| `runs` | Run metadata, counts, timings, `policy_digest`. |
| `agg_alerts_by_site_month` | Pre-aggregated map payloads. Written by the batch, never computed on request. |
| `users` / `user_org_scope` | Auth and row-level scoping. |
| `audit_log` | Append-only: who, when, what, previous value, correlation id. |

`alerts` is written by the detector's upsert and read by the API. The API never writes to it —
keeping the scored facts immutable is what makes run-over-run diffing meaningful.

## Auth and scoping

JWT bearer tokens, roles `reviewer` / `investigator` / `admin`. Row-level scoping by org unit is
applied **in the query**, as a mandatory `WHERE` clause injected by a dependency — never as a
post-filter and never in the UI. A scoping bug must be impossible to introduce by forgetting a
filter in one router, so the scoping dependency returns the query, not a boolean.

`admin` bypasses org scoping; that bypass is itself audited.

## Audit

**Every mutation writes an audit row in the same transaction as the change.** There is no code path
that dispositions, assigns, or changes a case without auditing. This is non-negotiable for HR and
audit use — the tool's output can end up in an employment dispute.

Audit rows are append-only: no update, no delete, at the database-permission level.

## Cross-cutting

- **Correlation id** — `X-Correlation-Id` middleware, generated when absent, attached to every log
  line and passed on to the detector. One reviewer action must be traceable UI → API → detector.
- **Structured JSON logging.** No print, no unstructured formatting.
- **Pagination is mandatory.** There is no endpoint capable of returning an unbounded list. Enforce
  `page_size ≤ 200` in a shared dependency, not per-router.
- **Optimistic concurrency** on dispositions via `expected_version`; a stale write returns `409`
  with current state so the UI can reconcile rather than silently overwrite a colleague.
- **`/health`** (process alive) and **`/ready`** (Postgres reachable). Ollama being down must
  **never** make `/ready` fail — the LLM is optional by design.
- **Errors** are RFC 7807 problem+json including the correlation id.

## Exports

Asynchronous: `POST /exports` returns a job id, `GET /exports/{id}` polls then downloads. A filter
set can be large and a reviewer should not hold a request open for it. CSV, XLSX and PDF; the export
respects the caller's org scope exactly as the queue does.

## Explanation proxy

`POST /explain/{alert_id}` calls the detector's narrator. Cache per alert. If the provider is
unavailable or the grounding check fails, return the deterministic template with
`source: "template"` and HTTP `200` — this is a healthy response, not a degraded one. **Never
return `503` because Ollama is down.**

## Testing

- **Contract tests** — the generated OpenAPI schema matches `docs/API_CONTRACT.md`; the phase-8 gate
  asserts this. Response shapes are asserted against the documented examples.
- **Scoping tests** — a reviewer scoped to org unit X cannot read an alert in Y, on every endpoint
  that returns employee data. Parametrise over the routers so a new router cannot skip it.
- **Audit tests** — every mutating endpoint writes exactly one audit row, and rolls it back with the
  transaction on failure.
- **Geo test** — `/analytics/geo` returns all 24 months with correct per-1,000 normalisation.

## Non-goals

- No detection logic. No scoring in-process.
- No Parquet reads on a request path.
- No writes to `alerts` — those come from the batch.
- No policy editing from the UI. `GET /policy/*` is read-only; editing policy is a different product
  with a different approval workflow.
