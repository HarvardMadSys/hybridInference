# Log Rejected Inference Requests

**Date:** 2026-05-04
**Status:** Approved (design)
**Author:** Juncheng Yang (with Claude)
**Issue:** #426

## Context

Inference requests can be rejected before they reach the upstream provider:

| Rejection | Where | Status | DB-logged today? |
|---|---|---|---|
| Per-user concurrency limit | `enforce_user_concurrency` (`servers/concurrency.py`) | 429 | **No** — only stdout `logger.info` |
| Daily cost quota | `verify_api_key` (`servers/auth.py:213-241`) | 429 | **No** — only stdout |
| Auth failures (invalid key, missing key, DB unavailable, suspended user, etc.) | `verify_api_key` (`servers/auth.py`, multiple raise sites) | 401/500 | **No** |
| OpenAI-shape model-not-found | `routers/completions.py` | 404 | **Yes** — unconditional `_schedule_db_log_task` |
| Anthropic-shape model-not-found | `routers/anthropic_messages.py:_resolve` | 404 | **No** |

An admin who wants to answer "which users are getting rate-limited" or "is anyone hitting the quota" has to grep stdout. Persisting these rejections in `api_logs` (the same table successful requests go into) makes them visible in existing admin queries and dashboards alongside successes.

## Goals

- Persist rejections from the four currently-unlogged sites to `api_logs` with a clear marker (`metadata.rejection = true`).
- Behind a runtime-toggleable admin setting `log_rejected_requests` so an operator can turn it on/off without a restart.
- Default the toggle to **`False`**: no behavior change at deploy time; admins opt in when they want the data.
- Helper failures (DB write timeout, connection error) MUST NOT alter the rejection HTTP response.

## Non-Goals

- Changing the existing OpenAI-shape model-not-found logging in `routers/completions.py`. It stays unconditional. Documented as a known asymmetry; can be unified in a follow-up if desired.
- Logging non-inference-path 4xx (e.g., `/admin/*`, `/health`, `/user/*`).
- A separate admin UI for filtering rejections; queries use `WHERE metadata->>'rejection' = 'true'`.
- Aggregated rejection counters / stats. The raw rows are sufficient for now.

## Design

### 1. Runtime setting

Add to `RUNTIME_SETTINGS_REGISTRY` in `serving/config/runtime_settings.py`:

```python
"log_rejected_requests": {
    "type": "bool",
    "default": False,
    "description": (
        "Persist rejected inference requests (rate-limit, quota, auth, "
        "model-not-found) to api_logs with metadata.rejection=true."
    ),
},
```

Toggled via the existing `PATCH /admin/settings/log_rejected_requests` endpoint. No new endpoint, no new schema.

### 2. Helper module

New file `serving/observability/rejection_log.py`:

```python
INFERENCE_PATH_PREFIXES = (
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/embeddings",
    "/completion",
    "/anthropic/v1/messages",
)

async def log_rejection(
    *,
    log_store: BaseLogStore | None,
    runtime_settings: RuntimeSettings | None,
    request: Request,
    status_code: int,
    error_code: str,            # machine-readable: "concurrency_limit_exceeded", ...
    reason: str,                # short human-readable detail
    user: dict[str, Any] | None,
    model_id: str = "",
) -> None:
    """Best-effort: write a rejection row to api_logs if the toggle is on.

    Never raises; logs to stdout on internal errors and returns.
    """
```

Behavior:

1. If `log_store` or `runtime_settings` is `None` → return immediately.
2. If `request.url.path` does not start with any prefix in `INFERENCE_PATH_PREFIXES` → return (rejections on `/admin/*`, `/health`, etc. are out of scope).
3. `enabled = await runtime_settings.get_bool("log_rejected_requests")` (cached, ~free on hot path).
4. If `enabled is False` → return.
5. Build the `log_request(...)` kwargs:

   | Field | Value |
   |---|---|
   | `request_id` | from `req_ctx.get()["request_id"]` (set by request-id middleware) |
   | `model_id` | provided arg or `""` (NOT NULL TEXT — empty string allowed) |
   | `provider` | `""` |
   | `prompt` | `None` (we don't capture rejected request bodies) |
   | `response` | `None` |
   | `usage` | `None` |
   | `latency_ms` | `0` |
   | `status_code` | provided arg |
   | `error` | `error_code` |
   | `params` | `None` |
   | `metadata` | `{"rejection": True, "reason": reason, "route": request.url.path, "role": user.get("role") if user else None, "remote_ip": get_client_ip(request), "user_id": user.get("user_id") if user else None}` |

6. `await log_store.log_request(**kwargs)` inside a `try` / `except Exception` block. On error: `logger.exception("rejection_log_failed", ...)` and return.

The helper is intentionally pure-function-ish — no module-level state, dependencies passed in.

All four call sites invoke the helper via `asyncio.create_task(log_rejection(...))` so the rejection's HTTP response is not delayed by a DB round-trip.

### 3. Wire into rejection sites

**3a. Concurrency rejection** — in `serving/servers/concurrency.py`'s `enforce_user_concurrency`, just before `raise HTTPException(429, ...)`:

```python
await log_rejection(
    log_store=request.app.state.services.log_store,
    runtime_settings=request.app.state.services.runtime_settings,
    request=request,
    status_code=429,
    error_code="concurrency_limit_exceeded",
    reason=f"limit={limit} role={role_label}",
    user=user,
)
```

**3b. Quota rejection** — in `serving/servers/auth.py`'s `verify_api_key`, just before the `raise HTTPException(429, ...)` block at lines 221-241. The user dict is available locally (auth has succeeded, only quota check is failing). Pass `user=user`, `model_id=""` (model is unknown at the auth layer).

**3c. Auth failures** — in `serving/servers/auth.py`'s `verify_api_key`, before each `raise HTTPException(401, ...)` (the auth-rejection 4xxs; skip the 500s for DB / config errors). The implementation plan enumerates the exact sites. For these:

- `user=None` (auth itself failed; we don't have a verified user dict)
- `error_code` distinguishes the cases: `"auth_missing"` (no key supplied), `"auth_invalid"` (key not found / wrong format), `"auth_user_suspended"` (key valid but user suspended). The plan maps each raise site to one of these codes.
- Path filtering happens **inside the helper** (step 2 above), so call sites stay simple. Non-inference auth failures (e.g., `/admin/*`) are ignored automatically.

**3d. Anthropic model-not-found** — in `serving/servers/routers/anthropic_messages.py`'s `_resolve` helper, before each `raise HTTPException(404, ...)`. The user dict is reachable via the caller (the route function); `_resolve` gains a `user_ctx` arg if it doesn't already, or the calls are added at the route function instead. Implementation plan picks the cleaner option.

### 4. Toggle reads

`RuntimeSettings.get_bool` is TTL-cached (30s default). The on-hot-path overhead is one dict lookup after warm-up. No change to the runtime-settings caching layer needed.

### 5. Filtering and querying

Admin queries identify rejection rows via:

```sql
SELECT * FROM api_logs WHERE (metadata->>'rejection')::boolean = true
```

A coarser filter `WHERE provider = ''` also works, but the metadata flag is explicit.

Existing dashboards that aggregate by `model_id` / `provider` will see rejection rows under `model_id=''` / `provider=''`. If those bin into "unknown" buckets, that's acceptable — admins doing breakdown analysis should add the `metadata.rejection` filter.

## Affected components

| Component | Change |
|---|---|
| `serving/config/runtime_settings.py` | +1 registry entry: `log_rejected_requests` |
| `serving/observability/rejection_log.py` | **New** — `log_rejection(...)` helper |
| `serving/servers/concurrency.py` | Call helper before raising 429 |
| `serving/servers/auth.py` | Call helper at 3 auth-failure sites + 1 quota site |
| `serving/servers/routers/anthropic_messages.py` | Call helper at `_resolve` 404 sites |
| `tests/observability/test_rejection_log.py` | **New** — toggle gate, error swallowing, payload shape |
| `tests/servers/test_admin_settings.py` | Coverage for the new key in `GET /admin/settings` |

## Testing

Unit:

- Toggle off → helper does nothing, `log_store.log_request` not called.
- Toggle on → helper calls `log_request` with expected kwargs (mock the store, assert payload shape including `metadata.rejection=True`).
- `log_store=None` or `runtime_settings=None` → no-op, no exception.
- `log_store.log_request` raises → helper swallows, logs internally, returns.
- Auth-failure path filter: helper called with a non-inference path → no row written.

Integration:

- POST a request as a free user already at concurrency cap, with toggle on → 429 returned and `api_logs` row appears with `error='concurrency_limit_exceeded'`, `metadata->>'rejection'='true'`.
- Same with toggle off → 429 returned, no row.

## Risks

- **PII in metadata**: `remote_ip` is captured. Acceptable per existing `request_log.py` middleware that already logs IPs to stdout. If a stricter policy is wanted later, drop the field.
- **Schema fit**: rejection rows have `model_id=''` and `provider=''`. Any aggregation query that joins/groups on these fields without filtering rejections will see anomalies. Mitigation: standard query pattern documented above.
- **Toggle drift across replicas**: a `PATCH` invalidates the local cache; other replicas pick up the change within the standard `RuntimeSettings` TTL (30s). Same behavior as every other runtime setting — not specific to this feature.
- **Rejection storm fan-out**: a misbehaving client sending 1000 rejections/sec generates 1000 DB writes. The fire-and-forget task model mitigates request-side latency but doesn't bound DB pressure. Acceptable for now (the rejections are already the slow path); add rate-limit on logging itself if it becomes a problem.
