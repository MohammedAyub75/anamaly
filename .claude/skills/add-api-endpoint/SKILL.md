---
name: add-api-endpoint
description: Add a FastAPI endpoint following the project's router/schema/service/test/contract-doc pattern, including org scoping, auditing and pagination. Use when adding or changing any route in services/api.
---

# Add an API endpoint

An endpoint is **five artifacts**: the contract doc entry, the pydantic schemas, the router, the
query service, and the tests. Skipping the contract doc is how the UI and API drift.

## Steps

1. **Document it first** in `docs/API_CONTRACT.md`: method, path, purpose, query parameters,
   response shape with a realistic example, and the status codes it can return. If you cannot write
   the response example, the endpoint is not designed yet.
2. **Schemas** in `services/api/api/schemas/` — pydantic v2, request and response. Reuse the shared
   `Page[T]` wrapper for any list. Never return a bare ORM model.
3. **Query service** in `services/api/api/services/` — all query logic here, not in the router. A
   router with a query in it cannot be tested without HTTP.
4. **Router** in `services/api/api/routers/` — parse, authorise, delegate, serialise. Thin.
5. **Tests** in `services/api/tests/`.

## Non-negotiables for every new endpoint

- **Org scoping.** If it returns employee or alert data, apply the scoping dependency. It returns a
  constrained query, not a boolean, so a reviewer scoped to org unit X cannot read Y — and so the
  scoping cannot be forgotten by omitting a filter.
- **Pagination.** Any list is paginated through the shared dependency, `page_size ≤ 200`. There must
  be no endpoint in the codebase capable of returning an unbounded list.
- **Audit.** Any mutation writes an audit row **in the same transaction** — who, when, what,
  previous value, correlation id. No exceptions; this data can end up in an employment dispute.
- **Correlation id** flows through automatically via middleware. Do not re-implement it.
- **No Parquet reads.** The API serves Postgres. If the data is not there, it belongs in the batch,
  not in a request handler.
- **Errors** as RFC 7807 problem+json with the correlation id.
- **Optimistic concurrency** on any update to a reviewer-editable record, via `expected_version`,
  returning `409` with current state.

## Tests to write

```python
def test_returns_expected_shape()          # matches the documented example
def test_pagination_caps_page_size()       # 201 -> 200 or 422, never unbounded
def test_scoping_blocks_other_org_unit()   # reviewer in X cannot read Y
def test_mutation_writes_audit_row()       # exactly one, rolled back with the transaction
def test_stale_version_returns_409()       # for updates
```

The contract test that asserts the generated OpenAPI matches `docs/API_CONTRACT.md` runs
automatically — a documented-but-unimplemented (or implemented-but-undocumented) route fails the
phase-8 gate.

## Then

Update the UI's query hook in `web/src/features/<area>/hooks.ts` and its TypeScript types. An
endpoint with no consumer is speculative; if nothing calls it, do not build it yet.
