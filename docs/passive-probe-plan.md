# Plan: Reduce Probe Quota by Using Real User Traffic as Health Signal

## Problem

The prober sends ~420 synthetic requests/hour (direct + E2E + health) at fixed 5-minute intervals. Every probe to a paid API (ZAI, MiniMax, Ollama Cloud) costs quota that could serve real users. The gateway already logs every real user request to `api_logs` with full outcome data (model_id, provider, status_code, latency_ms, ttft_ms, tokens, error, stream).

## Solution

Use real user traffic data to skip unnecessary synthetic probes. When a model+provider+mode combination has sufficient recent successful user requests, the prober records a "passive" health signal derived from that traffic instead of sending a synthetic probe.

## Architecture

```
┌──────────┐  GET /health/model-activity       ┌──────────┐
│  Prober  │ ────────────────────────────────► │  Gateway  │
│          │  Authorization: Bearer <api_key>  │           │
│          │ ◄──────────────────────────────── │           │
│ skip if  │  { routes: { key: ... } }        │  queries  │
│ active   │                                   │ api_logs  │
└──────────┘                                   └──────────┘
```

## Design Decisions from Review

### Round 1 fixes (all addressed)

1. **Provider-level granularity (High)**: Aggregate by `(model_id, provider)` tuple, not just `model_id`.

2. **Endpoint authentication (High)**: Gate the endpoint (see Round 2/3 refinements below).

3. **Mode-specific coverage (Medium)**: Skip decision checks `stream_count` or `non_stream_count` per probe mode.

4. **Freshness threshold (Medium)**: Passive eligibility requires `last_request_at` within `MAX_PASSIVE_AGE`.

### Round 2 fixes (all addressed)

5. **Route key granularity for direct probes (High)**: `api_logs.provider` stores the provider kind (e.g. `"openai_compat"`, `"zhipu"`), not a unique route identifier. Two routes with the same provider kind under one model (e.g. two `openai_compat` endpoints with different `base_url`) share the same `(model_id, provider)` key in `api_logs`.

   **Accepted limitation**: `api_logs` has no `base_url` column, so we cannot distinguish these routes without a schema migration. For direct probes where the prober has multiple routes sharing the same `(model_id, provider_kind)` pair, passive skip is **disabled** — those routes always run active probes.

6. **X-Probe is not a real secret (High)**: The current `probe_header` defaults to `"synthetic"` — a fixed well-known marker, not a secret. It is used for request tagging only.

   **Fix**: Use the prober's existing `gateway.api_key` for authentication instead.

### Round 3 fixes

7. **Authentication ≠ Authorization (High)**: Simply using `verify_api_key` (or `optional_verify_api_key`) would let **any valid API key** access global traffic stats, not just the prober. The `/health/model-activity` endpoint returns per-model request counts, success rates, and latency — this is business-sensitive operational data.

   **Fix**: Use `optional_verify_api_key` (lightweight, no quota burn, no `last_used_at` write) followed by an explicit `is_admin` check. The prober's `gateway.api_key` must belong to an admin account. This matches the existing pattern: `optional_verify_api_key` already returns `is_admin` (derived from `_check_admin(email)` → `is_admin_email()`), and the endpoint simply rejects non-admin callers with 403.

   Auth chain: `Authorization: Bearer <key>` → `optional_verify_api_key` → identity resolved → `is_admin == True` required → 403 if not admin.

8. **Aggregation key contract (Medium)**: The passive aggregation key `model_id::provider_kind` on the prober side must match the values actually written to `api_logs.(model_id, provider)`.

   **Verified**: Both originate from the same model registry. Gateway writes `adapter.config.provider = kind` (set in `serving/servers/registry.py:239`). Prober reads `provider_kind = route_kind` (set in `llm-prober/src/llm_prober/config.py:337`). Both `kind` values come from `route["kind"]` in the shared registry YAML. The contract holds as long as both services read the same registry file, which they do.

### Round 4 fixes (code review)

9. **Mode-specific success count missing (High)**: The SQL returned `stream_count` / `non_stream_count` (raw request counts) but only overall `success_count`. This meant 2 failed streaming + 8 successful non-streaming requests would wrongly skip the streaming probe (`stream_count=2`, `success_rate=0.8`).

   **Fix**: Added `stream_success_count` and `non_stream_success_count` to the SQL query. `_should_skip_probe()` now checks per-mode success rate (`mode_success / mode_count >= 0.8`), not overall success rate.

10. **Auth-disabled endpoint exposure (Medium)**: `optional_verify_api_key()` returns `is_admin=True` for anonymous callers when `USER_AUTH_ENABLED != 1`, making the endpoint public in dev/staging.

    **Fix**: The endpoint now explicitly checks `USER_AUTH_ENABLED=1` before proceeding. Returns 403 in auth-disabled deployments regardless of the anonymous admin shortcut.

11. **Prometheus label breakage (Medium)**: Passive probes were emitted as `probe_type="passive"`, creating new time series. Existing Grafana panels keyed on `probe_type="direct"` / `"e2e"` would stop updating for models under passive skip.

    **Fix**: Passive probes now use `probe_type="direct"` / `"e2e"` (matching the active series) so Prometheus consumers stay current. The `source="passive"` distinction is carried only in the in-memory state (for the dashboard badge) and structured logs (`"probe_type": "passive"` in log extra).

12. **E2E latency aggregation bug (Medium)**: Code comment said "take max for conservative check" but implementation only kept the first provider's value due to missing `else` branch.

    **Fix**: Corrected to actually take max across providers.

## Implementation Steps

### Step 1: Gateway — `DatabaseLogger.get_model_activity()`

**File**: `serving/storage/database.py`

Add a method that queries `api_logs` for per-model, per-provider request stats over a configurable window.

```python
async def get_model_activity(self, window_minutes: int = 10) -> dict[str, Any]:
    """Aggregate recent real-user traffic per (model_id, provider).

    Returns dict keyed by "model_id::provider" with stats.
    """
```

SQL query groups by `(model_id, provider)`:

```sql
SELECT model_id, provider,
       COUNT(*)                                    AS request_count,
       COUNT(*) FILTER (WHERE status_code < 400)   AS success_count,
       MAX(timestamp)                              AS last_request_at,
       AVG(latency_ms) FILTER (WHERE status_code < 400) AS avg_latency_ms,
       COUNT(*) FILTER (WHERE stream = TRUE)       AS stream_count,
       COUNT(*) FILTER (WHERE stream IS NOT TRUE)  AS non_stream_count,
       COUNT(*) FILTER (WHERE stream = TRUE AND status_code < 400)
           AS stream_success_count,
       COUNT(*) FILTER (WHERE stream IS NOT TRUE AND status_code < 400)
           AS non_stream_success_count
FROM api_logs
WHERE timestamp >= NOW() - ($1 || ' minutes')::interval
  AND user_id IS NOT NULL          -- exclude synthetic probes
GROUP BY model_id, provider
```

- Uses existing `idx_api_logs_model` index (leading column is `model_id`)
- `user_id IS NOT NULL` filters out synthetic probes (prober requests have no user_id)
- Groups by `(model_id, provider)` — the finest granularity `api_logs` supports without schema changes

**Key contract**: the `provider` column in `api_logs` stores the registry route `kind` value (e.g. `"zhipu"`, `"openai_compat"`, `"sglang"`), set via `adapter.config.provider = kind` in `serving/servers/registry.py:239`.

### Step 2: Gateway — `GET /health/model-activity` endpoint

**File**: `serving/servers/routers/health.py`

New endpoint with admin-only authorization.

```
GET /health/model-activity?window=10
Authorization: Bearer <admin_api_key>
```

**Authentication + Authorization**:

```python
from serving.servers.auth import optional_verify_api_key

@router.get("/health/model-activity")
async def model_activity(
    request: Request,
    window: int = Query(default=10, ge=1, le=60),
    user_ctx: dict[str, Any] | None = Depends(optional_verify_api_key),
    db_logger=Depends(get_db_logger),
) -> dict[str, Any]:
    # Authorization: admin only
    if not user_ctx or not user_ctx.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    ...
```

Why `optional_verify_api_key` instead of `verify_api_key`:
- **No quota burn**: This is an internal monitoring call, not a user request. `verify_api_key` would deduct quota cost and update `last_used_at`, which is wrong for a prober polling every 60s.
- **No side effects**: `optional_verify_api_key` only resolves identity + `is_admin`, no writes.
- **Same security**: Both validate against the same `api_keys` table. The admin check (`is_admin_email()`) is identical.

Response shape:
```json
{
  "window_minutes": 10,
  "routes": {
    "glm-5::zhipu": {
      "request_count": 12,
      "success_count": 11,
      "last_request_at": "2026-03-24T03:40:00Z",
      "avg_latency_ms": 2400,
      "stream_count": 8,
      "non_stream_count": 4,
      "stream_success_count": 7,
      "non_stream_success_count": 4
    },
    "glm-5::sglang": {
      "request_count": 3,
      "success_count": 3,
      "last_request_at": "2026-03-24T03:38:00Z",
      "avg_latency_ms": 1800,
      "stream_count": 0,
      "non_stream_count": 3,
      "stream_success_count": 0,
      "non_stream_success_count": 3
    }
  }
}
```

### Step 3: Prober — `fetch_model_activity()` in `probes.py`

**File**: `llm-prober/src/llm_prober/probes.py`

```python
async def fetch_model_activity(
    session: ClientSession,
    gateway: GatewayConfig,
    *,
    window_minutes: int = 10,
) -> dict[str, Any]:
    """Fetch per-(model, provider) traffic stats from gateway.

    Returns empty dict on any failure (fail-open: probes run as usual).
    """
```

- Sends `Authorization: Bearer <gateway.api_key>` via `_gateway_headers()`
- **Prerequisite**: `gateway.api_key` must be an admin account's API key (not just any user key). This is a deployment configuration requirement, documented in the config.
- 5s timeout (fast, non-blocking)
- Returns `{}` on any error — fail-open design

### Step 4: Prober — passive outcome support in `state.py`

**File**: `llm-prober/src/llm_prober/state.py`

Add a `source` field to `ProbeResult`:

```python
@dataclass(slots=True)
class ProbeResult:
    timestamp: datetime
    latency_ms: float | None
    ttft_ms: float | None
    output_tokens: int | None
    throughput_tps: float | None
    status: str
    source: str = "active"  # "active" | "passive"
```

Propagation:
- `record_probe()` and `record_e2e()` accept optional `source` parameter
- `summary()` returns `last_source` field
- `history()` returns `source` field per entry

### Step 5: Prober — skip logic in `main.py`

**File**: `llm-prober/src/llm_prober/main.py`

#### Ambiguous route detection

At startup, build a set of `(logical_model_id, provider_kind)` pairs that appear in more than one direct probe config:

```python
def _find_ambiguous_route_keys(providers: list[ProviderConfig]) -> set[str]:
    """Find route keys where multiple direct probes share the same
    (model_id, provider_kind) — passive skip is unsafe for these."""
    from collections import Counter
    counts = Counter(
        f"{p.logical_model_id}::{p.provider_kind or p.name}"
        for p in providers if p.type != "embedding"
    )
    return {key for key, count in counts.items() if count > 1}
```

Direct probes whose route key is in this set **always run active probes**, avoiding the false-merge issue.

#### Skip decision function

```python
MIN_PASSIVE_REQUESTS = 2
MIN_PASSIVE_SUCCESS_RATE = 0.8
MAX_PASSIVE_AGE_SECONDS = 600  # 2× default probe interval

def _should_skip_probe(
    activity: dict[str, Any],
    route_key: str,
    mode: str,
    *,
    ambiguous_keys: set[str] | None = None,
) -> dict[str, Any] | None:
    """Return route stats if probe can be skipped, None otherwise."""
```

Decision logic:
1. `embedding` mode → always return None (never skip)
2. If `route_key` is in `ambiguous_keys` → return None (multiple routes share this key)
3. Look up `route_key` in activity dict
   - For E2E: aggregate across any key starting with `f"{model_id}::"`
   - For direct: exact match on `f"{logical_model_id}::{provider_kind}"`
4. Check **mode-specific** request count and success rate:
   - `streaming` → require `stream_count >= MIN_PASSIVE_REQUESTS` AND `stream_success_count / stream_count >= MIN_PASSIVE_SUCCESS_RATE`
   - `non_streaming` → require `non_stream_count >= MIN_PASSIVE_REQUESTS` AND `non_stream_success_count / non_stream_count >= MIN_PASSIVE_SUCCESS_RATE`
6. Check **freshness**: `last_request_at` within `MAX_PASSIVE_AGE_SECONDS` of now
7. If all pass → return stats dict; otherwise → return None

#### Activity cache

```python
class ProberService:
    _activity_cache: dict[str, Any]
    _activity_cache_ts: float
    _ambiguous_route_keys: set[str]   # computed once at startup
    ACTIVITY_CACHE_TTL = 60.0
```

#### Changes in probe loops

`_run_provider_loop()`:
```python
await self._refresh_activity_cache()
route_key = f"{provider.logical_model_id}::{provider.provider_kind or provider.name}"
stats = _should_skip_probe(
    self._activity_cache, route_key, mode,
    ambiguous_keys=self._ambiguous_route_keys,
)
if stats is not None:
    self._record_passive_provider_outcome(provider, mode, stats)
    await self._sleep_or_shutdown(self._next_interval(provider.interval))
    continue
```

`_run_e2e_loop()`:
```python
await self._refresh_activity_cache()
stats = _should_skip_probe(self._activity_cache, target.model_id, mode)
if stats is not None:
    self._record_passive_e2e_outcome(target, mode, stats)
    await self._sleep_or_shutdown(...)
    continue
```

### Step 6: Dashboard — passive badge

**File**: `llm-prober/src/llm_prober/api.py`

CSS addition:
```css
.passive-tag{font-size:10px;color:var(--accent);margin-left:4px;font-weight:400}
```

In `badgeH()`:
```javascript
function badgeH(up, err, source) {
  const c = dotCls(up, err);
  const label = up ? "Up" : err ? err : "No data";
  const passive = source === "passive"
    ? ' <span class="passive-tag">via traffic</span>' : '';
  return `<span class="badge"><span class="dot ${c}"></span>${label}${passive}</span>`;
}
```

## Files Modified

| File | Change | Lines (est.) |
|------|--------|-------------|
| `serving/storage/database.py` | `get_model_activity()` method | +35 |
| `serving/servers/routers/health.py` | `GET /health/model-activity` with admin auth | +30 |
| `llm-prober/src/llm_prober/probes.py` | `fetch_model_activity()` function | +25 |
| `llm-prober/src/llm_prober/state.py` | `source` field on ProbeResult, propagate | +15 |
| `llm-prober/src/llm_prober/main.py` | Activity cache + mode-aware skip + ambiguous key detection | +90 |
| `llm-prober/src/llm_prober/api.py` | Passive badge CSS + JS | +10 |

## Deployment Configuration

**Prerequisites for production**:

1. **`USER_AUTH_ENABLED=1`** must be set on the gateway. When auth is disabled, `optional_verify_api_key` treats all callers as anonymous admin, making the `/health/model-activity` endpoint publicly accessible. This is acceptable in local development but **not in production**. The passive probe feature's security model assumes user authentication is enabled.

2. The prober's `gateway.api_key` must be set to an API key belonging to an **admin account** (an account whose email matches `is_admin_email()`). This is required for the `/health/model-activity` endpoint's admin authorization check.

If the prober currently uses a non-admin API key (or no key), this must be updated in the prober's config YAML before deploying. Example:

```yaml
gateway:
  api_key: "hyi-<admin-users-api-key>"
```

## What Stays the Same

- Probe intervals (300s) — we just skip when unnecessary
- Health check loop — always runs (no user traffic equivalent)
- Embedding probes — always actively probed (no user traffic overlap)
- Direct probes for cold models/providers — still actively probed
- No new infra dependencies — prober already talks to gateway via HTTP
- `X-Probe` header — still used for synthetic request tagging (unchanged)

## Known Limitations

1. **`api_logs` lacks `base_url` column**: When multiple routes share the same `(model_id, provider_kind)` — e.g. two `openai_compat` endpoints for the same model — their traffic merges in `api_logs`. Passive skip is disabled for these routes via the ambiguous key detection. A future schema migration adding `endpoint_id` or `route_key` to `api_logs` would eliminate this limitation.

2. **Passive latency is an average**: The `avg_latency_ms` from traffic stats doesn't reflect TTFT, output tokens, or throughput. Dashboard cards sourced from passive data show latency only — other metrics display "—". The `"via traffic"` badge makes this visible.

## Skip Decision Matrix

| Condition | Action |
|-----------|--------|
| Embedding mode | **Active probe** (never skip) |
| Activity fetch fails | **Active probe** (fail-open) |
| Ambiguous route key (multi-route same provider) | **Active probe** |
| Mode-specific count < 2 in window | **Active probe** |
| Success rate < 80% | **Active probe** (model may be degraded) |
| `last_request_at` older than 600s | **Active probe** (stale data) |
| Route key not found | **Active probe** (no matching traffic) |
| All checks pass | **Passive** — record from traffic, skip synthetic |

## E2E vs Direct: Lookup Key Comparison

| Probe type | Lookup key | Ambiguous check | Rationale |
|-----------|------------|-----------------|-----------|
| E2E | `model_id` (any provider) | No | Tests full gateway pipeline; any provider serving traffic validates the path |
| Direct | `model_id::provider_kind` | Yes | Tests specific upstream route; must have traffic on that exact route, and route must be unambiguous |

## Verification Plan

1. **Unit tests**: Mock activity response → assert probe skipped / not skipped for each matrix row
2. **Mode coverage test**: activity with `stream_count=0, non_stream_count=5` → streaming probe must NOT be skipped
3. **Freshness test**: activity with `last_request_at` 15min ago → probe must NOT be skipped
4. **Ambiguous route test**: two `openai_compat` routes for same model → both always actively probed
5. **Auth test**: request to `/health/model-activity` without valid admin key → 403
6. **Auth test**: request with valid non-admin key → 403
7. **Deploy**: Watch logs for `probe_type: "passive"` entries
8. **Monitor**: Probe count drops for busy models, dashboard stays green
9. **Rollback**: Remove cache check → all probes resume as before (no config change needed)
