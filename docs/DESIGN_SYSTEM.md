# DESIGN_SYSTEM.md

Enterprise theme for a data-dense review tool. Tokens live in `web/src/styles/theme.css` and are the
only place a colour is defined — no hex literals in components, ever.

The visual language is **inspired by** the Saudi energy sector: deep petrol blue, an energy teal
accent, warm sand and stone neutrals. No trademarked logo, wordmark or proprietary asset is used; a
neutral placeholder mark sits in the header for whoever owns the real brand assets to swap out.

## Palette

Light theme on `:root`, dark under both `[data-theme="dark"]` and `prefers-color-scheme: dark`.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--brand-petrol-900` | `#062a3d` | `#04202f` | Header, left nav surface |
| `--brand-petrol-700` | `#0b4a68` | `#0a5b7f` | Primary actions, active nav |
| `--brand-petrol-500` | `#12719b` | `#2b93c0` | Links, focus ring base |
| `--accent-teal-500` | `#12a594` | `#1fc3ae` | Accent, positive trend, selection |
| `--accent-teal-100` | `#d7f2ee` | `#0e3f39` | Accent fills, chips |
| `--sand-050` | `#faf7f2` | `#15171a` | Page background |
| `--sand-100` | `#f2ede4` | `#1c1f23` | Card / table stripe |
| `--stone-300` | `#d9d4cb` | `#33383e` | Borders, dividers |
| `--stone-600` | `#6b665e` | `#9aa1a9` | Secondary text |
| `--ink-900` | `#14181c` | `#f2f4f6` | Primary text |

Contrast: every text/background pair meets **WCAG AA** (4.5:1 body, 3:1 large). The phase-9 gate
includes a Lighthouse accessibility score ≥ 90.

## Severity — the semantics that matter most

| Severity | Token | Light | Dark | Always paired with |
|---|---|---|---|---|
| CRITICAL | `--sev-critical` | `#9b1c1c` | `#f87171` | Filled circle icon + the word "Critical" |
| HIGH | `--sev-high` | `#b45309` | `#fbbf24` | Triangle icon + "High" |
| MEDIUM | `--sev-medium` | `#1d4ed8` | `#60a5fa` | Square icon + "Medium" |
| WATCHLIST | `--sev-watchlist` | `#475569` | `#94a3b8` | Outline icon + "Watchlist" |

**Colour is never the only signal.** Every severity indicator carries a text label and a distinct
shape. The palette is colour-blind-safe (deuteranopia and protanopia checked: the red/amber pair is
separated by lightness, not hue alone). This is an accessibility requirement and an audit one — a
printed or photocopied alert must still be readable.

Use severity colours **only** for severity. A red button that means "delete" and a red chip that
means "critical" in the same view teaches the reviewer nothing.

## Typography

| Role | Family | Notes |
|---|---|---|
| Latin UI | Inter | Self-hosted in `web/src/assets/fonts/`. No Google Fonts, no CDN. |
| Arabic UI | IBM Plex Sans Arabic | Names are stored bilingually; Arabic is never a fallback glyph. |
| Numerals / tables | Inter, `font-variant-numeric: tabular-nums` | Non-negotiable in any column of money. |

Scale: 12 / 13 / 14 / 16 / 20 / 24 / 32 px. Body is 14. Table rows are 14 with 40px row height —
dense, but not cramped: reviewers read these for hours.

Money is always rendered with thousands separators and an explicit `SAR` prefix on first appearance
in a block. Never a bare number where an amount is meant.

## Layout

- Persistent left navigation (collapsible, 240 / 64 px), sticky filter bar, content region.
- Max content width 1600px; tables go full-bleed within it.
- 4px spacing base; 8/12/16/24/32 the common steps.
- **Restrained borders over heavy shadows.** One elevation level for overlays, none for cards.
- Card radius 8px, buttons 6px, chips 999px.
- Data-first, decoration-last: if a pixel is not carrying information, it should not be there.

## RTL

Logical CSS properties throughout — `margin-inline-start`, not `margin-left`; `inset-inline-end`,
not `right`. Icons that encode direction (chevrons, arrows) flip with `[dir="rtl"]`. Charts and the
map do **not** mirror — a map of Saudi Arabia is not a directional element.

An Arabic UI must be a `dir="rtl"` attribute change, not a rewrite. Any component that breaks under
RTL is a bug, not a future enhancement.

## Charts

Charts consume the same tokens so the map, timeline and peer-distribution charts read as one system.

- **Categorical**: petrol → teal → sand ramp, max 6 series before switching to "top 5 + other".
- **Sequential** (map choropleth): single-hue teal ramp, 5 steps, lightest = lowest.
- **Peer distribution**: grey histogram with the employee's position marked in `--sev-*` for their
  severity — the one place a severity colour appears in a chart.
- Every chart has a text alternative: the underlying figures are available as a table.
- No 3D, no gradients-as-decoration, no dual axes.

## States — required, not optional

Every data view ships with all four: **loading** (skeleton matching the final layout, never a
spinner over blank space), **empty** (what it means and what to do next), **error** (what failed,
with a retry, and the correlation id for support), **populated**.

Every route is wrapped in an error boundary. A failed chart must not take down the queue.

## Language

The whole UI is written for someone who has never heard of an isolation forest.

| Never write | Write instead |
|---|---|
| "Anomaly score 94, z=4.3" | "Very unusual — higher than 99% of similar employees" |
| "Isolation Forest flagged this record" | "This pay pattern does not match anyone doing the same job" |
| "Reconstruction error 0.41" | "Remote-site allowance is the biggest reason this was flagged" |
| "Cohort n=412" | "Compared against 412 people at grade 12 in Process Ops at office sites" |
| "Null value in dependents_count" | "Number of dependents is not recorded" |

If a sentence cannot be said out loud to an employee's line manager, it does not belong on screen.
