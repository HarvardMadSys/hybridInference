# Stable control OpenAPI

`control-v1.policy.json` is the small, reviewable auth/permission rule set.
`control-v1.allowlist.json` is its generated, explicit operation-by-operation
public control-surface promise. It includes every registered operation routed
through the frontend's stable-control rewrites and intentionally excludes
inference protocols, compatibility aliases, distribution-private routes, and
operator-only routes.

Generate the explicit allowlist, canonical OpenAPI subset, and its recursive
schema reference closure:

```bash
uv run python contracts/openapi/generate_control_snapshot.py
```

Check it without writing:

```bash
uv run python contracts/openapi/generate_control_snapshot.py --check
```

The generated operation objects preserve request/response schemas and native
OpenAPI security metadata. `x-control-auth` and `x-control-permission` freeze
the browser/session and role boundary; generation also checks those claims
against markers on the actual nested FastAPI dependency graph. Every operation
declares the [stable error envelope](../control-errors-v1.md) as its default
error response and carries the current `x-control-error-codes` vocabulary.

The frontend rewrite inventory has a second generated layer:

```bash
uv run python contracts/openapi/generate_frontend_operation_inventory.py
uv run python contracts/openapi/generate_frontend_operation_inventory.py --check
```

`frontend-backend-routes.v1.json` classifies every Next rewrite.
`frontend-backend-operations.v1.json` expands those rules to every registered
FastAPI `path + method`, preventing wildcard prefixes from hiding unclassified
endpoints. `node contracts/verify_frontend_rewrites.mjs` executes the exported
Next config and compares its effective backend rewrites with the checked
inventory, so source formatting or helper functions cannot bypass the gate.
