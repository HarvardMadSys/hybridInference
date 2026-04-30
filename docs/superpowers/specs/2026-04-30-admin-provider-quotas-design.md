# Admin Dashboard — Provider Quotas Tab

**Status:** Design
**Date:** 2026-04-30
**Author:** brainstorming session

## Summary

Add a new "Providers" tab to the admin dashboard that shows, for each upstream LLM provider we route through, the configured API key (masked) and the live remaining quota fetched from that provider. Read-only, admin-only, on-tab-open + manual refresh (no auto-polling).

Target providers: **Chutes**, **ZAI**, **MiniMax**, **Ollama Cloud**.

## Goals

- Give admins a single place to verify which provider keys are configured and how much quota remains, without logging into each provider's web dashboard.
- Surface auth/health problems with provider quota endpoints (e.g., expired session cookie) so they can be repaired before quota runs out.

## Non-goals

- Managing/editing provider keys from the UI (still env-var driven).
- Auto-refresh polling.
- Historical quota tracking or time-series of consumption.
- Aggregating cost from our own request logs (a separate concern; covered by existing `provider` field on requests).
- User-facing display — admin-only.

## Context

The codebase already exposes an admin dashboard at [frontend/src/app/dashboard/admin/page.tsx](frontend/src/app/dashboard/admin/page.tsx) with three tabs: Users, Recent Requests, Audit Log. Backend admin routes live under `/admin/*` in [serving/servers/routers/admin.py](serving/servers/routers/admin.py); schemas in [serving/schemas_admin.py](serving/schemas_admin.py); frontend API client in [frontend/src/lib/api/admin.ts](frontend/src/lib/api/admin.ts).

Provider keys today are read from environment variables in [serving/config.py](serving/config.py) and [serving/config/settings.py](serving/config/settings.py).

## Architecture

A new tab "Providers" is added to the admin dashboard (tab key `providers`). On tab open and on Refresh click, the frontend calls `GET /admin/provider-quotas`. The backend handler:

1. Loads each provider's credentials from env vars.
2. Dispatches 4 async HTTP fetchers in parallel via `asyncio.gather(..., return_exceptions=True)` with an 8-second timeout each.
3. Masks each API key (server-side; the full key never leaves the backend).
4. Returns one combined response containing per-provider results.

No DB writes, no caching. The endpoint is admin-only (reuses existing role check).

```
Browser ── GET /admin/provider-quotas ──► FastAPI handler
                                           ├─► chutes_fetcher(CHUTES_API_KEY)
                                           ├─► zai_fetcher(ZAI_API_KEY)
                                           ├─► minimax_fetcher(MINIMAX_SESSION_COOKIE)
                                           └─► ollama_fetcher(OLLAMA_SESSION_COOKIE)
                                          gather results
                                          → AdminProviderQuotasResponse
```

## Per-provider quota fetcher

Each fetcher is a small async function in a new module `serving/admin/provider_quotas.py`. They all return a common `ProviderQuotaResult` shape so the handler can aggregate uniformly. Errors are converted to structured results — fetchers never raise out of the gather.

| Provider | Auth source (env var) | Endpoint | Notes |
|---|---|---|---|
| **Chutes** | `CHUTES_API_KEY` | `GET https://api.chutes.ai/users/me/subscription_usage` with `Authorization: Bearer <key>` | Returns monthly + 4-hour window usage vs limits. Officially documented. |
| **ZAI** | `ZAI_API_KEY` | `GET https://api.z.ai/api/monitor/usage/quota/limit` with `Authorization: Bearer <key>` | Endpoint discovered from ZAI's official `glm-plan-usage` plugin. The plugin uses the user's Anthropic auth token; we attempt with `ZAI_API_KEY` directly. If the API key auth fails, fetcher reports `auth_failed`. |
| **MiniMax** | `MINIMAX_SESSION_COOKIE` | `GET https://api.minimaxi.com/v1/api/openplatform/coding_plan/remains` with `Cookie: <cookie>` | Endpoint requires browser session cookies (API key auth returns `1004: cookie missing`). Admin pastes the cookie from browser DevTools; expires periodically and admin re-pastes. |
| **Ollama Cloud** | `OLLAMA_SESSION_COOKIE` | `GET https://ollama.com/settings` with `Cookie: <cookie>`, parse HTML for usage figures | No quota API exists. Scrape the settings page. Same cookie-renewal pattern as MiniMax. |

Two new env vars are added to [serving/config/settings.py](serving/config/settings.py): `minimax_session_cookie: str = ""` and `ollama_session_cookie: str = ""`. The existing `CHUTES_API_KEY` and `ZAI_API_KEY` (already in `.env`) are read directly via `os.getenv` to match how other provider keys are loaded in [serving/config.py](serving/config.py).

### Common result shape

```python
class ProviderQuotaUsage(BaseModel):
    label: str           # e.g. "Monthly", "4-hour window", "Tokens"
    used: float | None
    limit: float | None
    unit: str            # "USD", "tokens", "requests"
    reset_at: datetime | None

class ProviderQuotaResult(BaseModel):
    name: str            # "chutes" | "zai" | "minimax" | "ollama"
    display_name: str    # "Chutes" | "ZAI" | "MiniMax" | "Ollama Cloud"
    key_configured: bool
    key_masked: str | None     # e.g. "cpk_ab12...xyz9"; None if not configured
    fetched_at: datetime | None
    ok: bool
    error: str | None    # short reason if !ok: "auth_failed" | "timeout" | "not_configured" | "parse_error" | "unexpected"
    usages: list[ProviderQuotaUsage]   # empty if !ok or no usage data

class AdminProviderQuotasResponse(BaseModel):
    generated_at: datetime
    providers: list[ProviderQuotaResult]
```

### Key-masking helper

`_mask_key(key: str) -> str`:
- If `len(key) >= 16`: return `key[:8] + "..." + key[-4:]` (e.g. `cpk_ab12...xyz9`)
- Else (short or weird key): return `***configured***`
- If key is empty/None: caller sets `key_configured=False` and `key_masked=None` instead of calling this

Cookies are also masked using the same helper for the UI representation (we store full cookie server-side; UI only sees the masked form).

## Backend endpoint

New route in [serving/servers/routers/admin.py](serving/servers/routers/admin.py):

```
GET /admin/provider-quotas
Auth: existing admin role check (same dependency as other /admin/* routes)
Response: AdminProviderQuotasResponse
```

Pseudocode:

```python
@router.get("/provider-quotas", response_model=AdminProviderQuotasResponse)
async def get_provider_quotas(_: AdminUser = Depends(require_admin)) -> AdminProviderQuotasResponse:
    fetchers = [fetch_chutes(), fetch_zai(), fetch_minimax(), fetch_ollama()]
    results = await asyncio.gather(*fetchers, return_exceptions=True)
    return AdminProviderQuotasResponse(
        generated_at=datetime.utcnow(),
        providers=[_normalize(r) for r in results],
    )
```

`_normalize` turns any `Exception` that leaked out into a `ProviderQuotaResult(ok=False, error="unexpected", ...)` so the response is always well-formed.

## Frontend

### API client

New code in [frontend/src/lib/api/admin.ts](frontend/src/lib/api/admin.ts):

```typescript
export interface ProviderQuotaUsage {
  label: string;
  used: number | null;
  limit: number | null;
  unit: string;
  reset_at: string | null;
}

export interface ProviderQuotaResult {
  name: string;
  display_name: string;
  key_configured: boolean;
  key_masked: string | null;
  fetched_at: string | null;
  ok: boolean;
  error: string | null;
  usages: ProviderQuotaUsage[];
}

export interface AdminProviderQuotasResponse {
  generated_at: string;
  providers: ProviderQuotaResult[];
}

export async function getProviderQuotas(): Promise<AdminProviderQuotasResponse>;
```

### UI

Adds a fourth tab to [frontend/src/app/dashboard/admin/page.tsx](frontend/src/app/dashboard/admin/page.tsx). Tab order: `Users → Recent Requests → Providers → Audit Log`.

Layout: a responsive 2-column grid of provider cards (matches the existing `RequestMetricsCard` style — `rounded-xl border-gray-200 bg-white p-4 shadow-sm`).

Per-card content:
- **Header:** display name (e.g. "Chutes"), masked key (e.g. `cpk_ab12...xyz9`) or "Not configured" muted.
- **Usage rows:** for each `ProviderQuotaUsage`, render: `<label>  <used> / <limit> <unit>  <pct>%` and a horizontal bar showing percentage consumed. If `limit` is `None`, just render `<used> <unit>`.
- **Error state (`ok: false`):** show one muted line `Quota unavailable — <error>`. No usage rows, no bars.
- **Reset hint:** if any usage has `reset_at`, show a small footer line: `Resets <relative time>`.

Loading & refresh:
- On tab change to `providers`, call `getProviderQuotas()` once.
- The existing top-right Refresh button re-fetches when this tab is active (extend `refreshActiveTab()`).
- Single spinner over the whole grid while loading; no per-card skeletons.

State additions in `AdminPage`:
- `providerQuotas: ProviderQuotaResult[]`
- `quotasLoading: boolean`
- `loadProviderQuotas` callback
- `useEffect` triggers when `activeTab === 'providers'`

## Error handling

Each fetcher catches `httpx.TimeoutException`, `httpx.HTTPStatusError`, `json.JSONDecodeError`, and a generic `Exception` fallback — converting each into a `ProviderQuotaResult` with `ok=False` and a short reason code:

| Reason | Trigger |
|---|---|
| `not_configured` | env var missing/empty |
| `timeout` | request exceeded 8s |
| `auth_failed` | HTTP 401 / 403 / cookie-rejected JSON body |
| `parse_error` | response body doesn't match expected shape (e.g. Ollama HTML scrape can't find usage rows) |
| `unexpected` | anything else (caught and logged at backend with traceback) |

The admin endpoint never returns HTTP 500 from a provider failure; one bad provider does not block the response. The frontend treats `ok: false` as a presentation state, not an error toast.

## Security

- Provider keys and cookies stay server-side. Only `key_masked` ever crosses the wire.
- Endpoint reuses the existing admin role check (`is_admin`); no new auth surface.
- Cookie env vars (`MINIMAX_SESSION_COOKIE`, `OLLAMA_SESSION_COOKIE`) are never logged in plaintext — masked when included in any error log.
- All outbound calls use an explicit 8-second timeout and follow at most one redirect, to avoid resource exhaustion if a provider hangs or redirects to an auth wall.

## Testing

New test file: [test/servers/test_admin_provider_quotas.py](test/servers/test_admin_provider_quotas.py).

- **Route auth:** request without admin role returns 403; with admin role returns the response shape.
- **Per-fetcher unit tests** using `respx`/`httpx` mock for each provider:
  - Success path returns parsed usages.
  - 401/403 → `auth_failed`.
  - Timeout → `timeout`.
  - Missing env var → `not_configured`.
  - Malformed body → `parse_error`.
- **Key-masking helper:** unit tests for short, normal-length, and empty keys.
- **Endpoint integration:** patch fetchers, verify the route aggregates correctly and never raises when one fetcher returns an exception.

No frontend unit tests (matches existing convention — admin tab has no `*.test.tsx` files).

## Configuration changes

- New env vars added to `.env.example` and [serving/config/settings.py](serving/config/settings.py):
  - `MINIMAX_SESSION_COOKIE` (default `""`)
  - `OLLAMA_SESSION_COOKIE` (default `""`)
- `CHUTES_API_KEY` and `ZAI_API_KEY` already exist in `.env` — no settings change needed.

## File map

**New files:**
- `serving/admin/provider_quotas.py` — fetcher functions + key-masking helper
- `test/servers/test_admin_provider_quotas.py` — backend tests

**Modified files:**
- `serving/schemas_admin.py` — add `ProviderQuotaUsage`, `ProviderQuotaResult`, `AdminProviderQuotasResponse`
- `serving/servers/routers/admin.py` — add `GET /admin/provider-quotas` route
- `serving/config/settings.py` — add `minimax_session_cookie`, `ollama_session_cookie`
- `frontend/src/lib/api/admin.ts` — add types + `getProviderQuotas()`
- `frontend/src/app/dashboard/admin/page.tsx` — add `providers` tab and UI
- `.env.example` — document the two new cookie env vars
