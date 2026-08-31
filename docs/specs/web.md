# SPEC — `web/`

**Self-contained build spec** for the React frontend. Phases 9–11 build this.

Visual tokens and language rules are in `docs/DESIGN_SYSTEM.md`; endpoint shapes are in
`docs/API_CONTRACT.md`. Both are authoritative — this file covers structure and behaviour.

## Who is using this

A non-technical HR or audit reviewer, working a queue for hours, accountable for every decision they
record. That single fact drives every choice below: plain language everywhere, evidence always
visible, no ML jargon anywhere on screen, and never a screen that says something is wrong without
saying why.

## Stack

React 18 + TypeScript + Vite + shadcn/ui + Tailwind, TanStack Query (server state) and TanStack
Table (virtualised tables), React Router, Recharts for charts, `d3-geo` / `react-simple-maps` for
the map. Playwright for smoke tests.

**No external CDN, tile server or font host anywhere.** Fonts self-hosted, GeoJSON bundled from
`policy/geo/sa_regions.geojson`. The app must work air-gapped.

## Layout

```
web/src/
  app/          router, providers, error boundaries
  styles/       theme.css  <- the ONLY place a colour is defined
  components/   ui/ (shadcn), severity/, charts/, layout/
  features/
    dashboard/ triage/ alert-detail/ employee/ cases/ policy/ map/ admin/
      # each: page.tsx, hooks.ts (queries), components/, types.ts
  lib/          api client, formatters (money, period, name), auth
  test/         playwright specs
```

Feature-first, not layer-first. Everything for the triage queue lives under `features/triage/`.

## Screens

1. **Overview dashboard** — alerts by severity, total financial exposure, top sites and departments,
   run-over-run trend, "what changed since last run".
2. **Triage queue** — *the primary screen*. Virtualised table, server-side pagination/sort/filter,
   saved filter presets, bulk assign, severity chips, financial-impact column, and the one-line
   plain-English reason inline so a reviewer can triage without opening anything.
3. **Alert detail** — the explainability screen, and the reason the product exists:
   - **What we found** — plain English (LLM-narrated, deterministic fallback rendered identically).
   - **Why we flagged it** — the rule citation with its regulatory reference; the peer comparison
     chart showing the employee's position in the cohort distribution; contribution bars for the ML
     attribution, labelled in business terms and SAR.
   - **Timeline** — 24-month pay and allowance chart with the anomaly window highlighted.
   - **Financial impact** — monthly and cumulative, prominent.
   - **What to do** — the recommended-action checklist.
   - **Disposition panel** — Confirm / Dismiss (reason required) / Escalate / Request info + notes.
4. **Employee 360** — profile, pay timeline, allowance history, **entitlement matrix vs policy**,
   all historical alerts.
5. **Case management** — grouped alerts, assignee, status, activity trail.
6. **Policy explorer** — read-only, plain-language view of every rule, so reviewers understand what
   the system checks. Rendered from `GET /policy/rules`.
7. **Geographic anomaly map** — §Map below.
8. **Admin** — run history, threshold and budget config, model metrics, data-quality panel.

## Map (phase 11)

Answers *"where in the Kingdom are anomalies concentrated, and how does that move month to month?"*

- **Base**: the bundled 13-region GeoJSON rendered with `d3-geo`. Vector, no raster tiles, no
  external anything.
- **Two layers**: a region choropleth shaded by the selected metric, and site bubbles (radius by
  alert count, colour by severity mix) that filter the triage queue to that site on click.
- **Metric selector**: raw alert count, **alerts per 1,000 employees (the default)**, financial
  exposure, critical-only count.
- **Month scrubber** across the 24 months with play/pause animation, so a reviewer watches hotspots
  emerge and fade.
- **Filters** shared with the triage queue: family, code, org unit, employment type.
- **Side panel**: ranked region/site table for the selected month, month-over-month delta arrows,
  and the top 3 anomaly codes driving each hotspot.

**Per-1,000 is the default for a reason**: Eastern Province headcount would otherwise make every
frame a population map. Raw count stays available, but it is not what the screen opens on.

Every frame is a small pre-computed payload from `GET /analytics/geo`. **The UI never aggregates
1M rows client-side** — that is what makes the animation smooth. Prefetch adjacent months so
scrubbing does not stutter.

## Stability requirements

These are build requirements, not polish (`docs/PLAN.md` §6.3):

- Error boundary around **every** route. A failed chart must not take down the queue.
- Skeleton / empty / error states on **every** data view. The skeleton matches the final layout —
  never a spinner over blank space.
- Request cancellation and retry with backoff in the query layer.
- Optimistic-but-reconciled disposition updates: apply immediately, reconcile against the server
  response, roll back visibly on a `409` and show the current state.
- Server-side pagination everywhere. No unbounded fetch exists in the codebase.
- Correlation id from the API surfaced in every error state, so support can trace it.
- Playwright smoke tests over the critical paths: queue → detail → disposition → map.

## Accessibility

WCAG AA contrast on every pair; keyboard navigable throughout including the table and the map's
month scrubber; visible focus rings; severity **always** carries a text label and a distinct shape,
never colour alone. Lighthouse accessibility ≥ 90 is the phase-9 gate.

Light and dark both first-class. RTL via logical CSS properties — an Arabic UI is a `dir` change,
not a rewrite.

## Formatting rules

- Money: thousands separators, `SAR` prefix on first appearance in a block, `tabular-nums` in every
  numeric column. Never a bare number where an amount is meant.
- Periods: `YYYYMM` from the API renders as `March 2024`, never as `202403`.
- Names: `name_en` with `name_ar` alongside; never truncate an Arabic name to fit a column.
- Nulls: "not recorded", never a blank cell or `null`.

## Language

No ML jargon reaches the screen. The mapping table in `docs/DESIGN_SYSTEM.md` is the reference. The
test: if a sentence cannot be said out loud to the employee's line manager, it does not belong.

## Non-goals

- No client-side aggregation of large result sets.
- No policy editing.
- No direct database or Parquet access — the API is the only data source.
- No feature that requires network access outside the deployment.
