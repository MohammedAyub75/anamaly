---
name: add-ui-view
description: Add a React page, route, query hook and components following the project's feature-first pattern, including the required loading/empty/error states, theme tokens and plain-language rules. Use when adding or changing any screen in web/.
---

# Add a UI view

## Structure

Feature-first. Everything for one screen lives together:

```
web/src/features/<feature>/
  page.tsx          the route component
  hooks.ts          TanStack Query hooks — all server state
  types.ts          response types mirroring docs/API_CONTRACT.md
  components/       pieces used only by this feature
```

Shared pieces go in `web/src/components/`. Do not promote a component to shared until a second
feature actually needs it.

## Steps

1. **Route** in `web/src/app/router.tsx`, wrapped in an error boundary. Every route, no exceptions —
   a failed chart must not take down the queue.
2. **Query hook** in `hooks.ts` using TanStack Query. Server-side pagination, sort and filter
   parameters go to the API; never fetch everything and filter in the browser.
3. **Types** in `types.ts`, mirroring `docs/API_CONTRACT.md` exactly. If the API shape is not
   documented there, document it first.
4. **Page** with **all four states**:
   - **loading** — a skeleton matching the final layout, never a spinner over blank space;
   - **empty** — what it means and what to do next, not just "No data";
   - **error** — what failed, a retry, and the correlation id for support;
   - **populated**.
5. **Styling** from `web/src/styles/theme.css` tokens only. **No hex literals in components, ever.**

## Non-negotiables

- **Plain language.** No ML jargon anywhere on screen — the mapping table in
  `docs/DESIGN_SYSTEM.md` is the reference. Test: if you cannot say the sentence out loud to the
  employee's line manager, it does not belong.
- **Severity is never colour alone.** Always a text label and a distinct shape alongside the colour.
- **Money formatting** — thousands separators, `SAR` prefix on first appearance in a block,
  `tabular-nums` in numeric columns. Never a bare number where an amount is meant.
- **Periods** render as `March 2024`, never `202403`.
- **Nulls** render as "not recorded", never a blank cell.
- **Bilingual names** — `name_en` with `name_ar` alongside; never truncate an Arabic name.
- **Logical CSS properties** — `margin-inline-start`, not `margin-left`. RTL must be a `dir` change.
- **Keyboard navigable**, visible focus rings, WCAG AA contrast.
- **No external CDN, font host or tile server.** Anything fetched must be bundled.
- **Virtualise** any table that can exceed ~100 rows.

## Tests

Add a Playwright spec for any critical path the view is part of. The required paths are
queue → detail → disposition → map; a new view on one of those extends the existing spec rather
than adding a parallel one.

## Before you finish

- Check it in **both light and dark**.
- Check it at a narrow viewport — reviewers use laptops with side panels open.
- Check the empty and error states actually render, by pointing the hook at a failing endpoint.
  These are the states that ship broken because nobody looked at them.
