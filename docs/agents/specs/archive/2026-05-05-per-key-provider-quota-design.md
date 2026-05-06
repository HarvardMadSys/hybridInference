# Per-Key Provider Quota Display

**Date:** 2026-05-05
**Status:** Approved

## Problem

The admin dashboard Providers > Quota tab shows one card per upstream provider,
each with a single `key_masked` field. Providers that have multiple API keys
configured (e.g. `ZAI_API_KEY` + `ZAI_API_KEY2`) only show quota for the first
key. The admin cannot see per-key quota usage.

## Solution

Extend the quota fetchers to discover and query all configured keys per provider,
returning one `ProviderQuotaResult` per key. The frontend groups results by
provider name and renders a single card with sub-sections per key.

## Env Var Discovery

Numbered suffix convention:

| Provider | Base env var | Additional keys |
|----------|-------------|-----------------|
| Chutes | `CHUTES_API_KEY` | `CHUTES_API_KEY2`, `CHUTES_API_KEY3`, ... |
| ZAI | `ZAI_API_KEY` | `ZAI_API_KEY2`, `ZAI_API_KEY3`, ... |
| MiniMax | `MINIMAX_SESSION_COOKIE` | `MINIMAX_SESSION_COOKIE2`, ... |
| Ollama | `OLLAMA_SESSION_COOKIE` | `OLLAMA_SESSION_COOKIE2`, ... |

Discovery: iterate `BASE + str(i)` for `i` in `[2, 3, ...]` until the env var
is missing. The base var (`i=1` or no suffix) always counts as the first key.

## Schema Changes

### `ProviderQuotaResult` (Python + TypeScript)

Add field:

```python
key_index: int | None = None  # None when single-key (backward compat)
```

When a provider has multiple keys:
- `key_index` is `1, 2, 3, ...`
- `display_name` becomes `"Chutes #1"`, `"Chutes #2"`, etc.
- `key_masked` shows the specific key for that index

When a provider has a single key, `key_index=None` and behavior is unchanged.

## Backend Changes

### `provider_quotas.py`

Each fetcher is refactored into two layers:

1. **Key discovery** — `_discover_keys(base_env, suffix_env)` returns a list of
   `(index, key_value)` tuples. For cookie-based providers, the base env var
   name differs (`*_SESSION_COOKIE`).

2. **Per-key fetch** — the existing fetcher logic (HTTP call + parse) becomes
   an inner function `fetch_<provider>_for_key(key: str) -> ProviderQuotaResult`
   that does not read env vars itself.

3. **Multi-key wrapper** — the public `fetch_<provider>()` discovers all keys
   and calls the inner function for each in parallel via `asyncio.gather()`.
   Sets `key_index` and `display_name` accordingly.

4. **`gather_all()`** — unchanged structure; returns a flat `list[ProviderQuotaResult]`.
   The frontend groups by `name`.

### Helper: `_discover_env_keys()`

```python
def _discover_env_keys(base_var: str, numbered_prefix: str) -> list[tuple[int, str]]:
    """Return (index, value) for all non-empty env vars.
    
    index=1 for base_var, index=N for numbered_prefix+N.
    """
    keys: list[tuple[int, str]] = []
    val = os.getenv(base_var, "")
    if val:
        keys.append((1, val))
    for i in range(2, 20):
        val = os.getenv(f"{numbered_prefix}{i}", "")
        if not val:
            break
        keys.append((i, val))
    return keys
```

## Frontend Changes

### TypeScript type update (`admin.ts`)

```typescript
export interface ProviderQuotaResult {
  // ...existing fields...
  key_index: number | null;
}
```

### `ProviderCard` component (`page.tsx`)

Current: receives a single `ProviderQuotaResult`.

New: receives a group of `ProviderQuotaResult[]` sharing the same `name`.

- Card header shows the provider name (from the first result's `display_name`
  with the `#N` suffix stripped).
- If the group has one result with `key_index === null`, render exactly as
  today.
- If the group has multiple results (or a single result with a non-null
  `key_index`), render sub-sections inside the card, each with:
  - Key label badge: `#1`, `#2`, etc.
  - Masked key display
  - Usage bars for that key
  - Error state if that specific key failed

### Quota tab rendering

Before rendering the grid, group `providerQuotas` by `name`:

```typescript
const grouped = Map.groupBy(providerQuotas, p => p.name);
```

Then render one `ProviderCard` per group.

## Files to Modify

| File | Change |
|------|--------|
| `apps/backend/serving/schemas_admin.py` | Add `key_index` to `ProviderQuotaResult` |
| `apps/backend/serving/admin/provider_quotas.py` | Multi-key discovery + per-key fetch for all 4 providers |
| `apps/frontend/src/lib/api/admin.ts` | Add `key_index` to TS type |
| `apps/frontend/src/app/dashboard/admin/page.tsx` | Group results, update `ProviderCard` |

## Backward Compatibility

- Single-key providers return `key_index=None` — existing frontend code
  (before the TS update) simply ignores the new field.
- The API response shape (`AdminProviderQuotasResponse.providers: list`) is
  unchanged; the list may now contain multiple entries with the same `name`.
- No migration needed; no database changes.
