# Adjustable Per-User Concurrency Limits

**Date:** 2026-05-04
**Status:** Implemented (PR #423)
**Author:** Juncheng Yang (with Claude)

> **Note:** This spec captures the design *as approved before implementation*.
> The "Context" snippet below describes the pre-change codebase. As of PR #423
> the static `USER_CONCURRENCY_LIMITS` dict has been **removed** from
> `serving/config/settings.py`; the authoritative defaults now live in
> `RUNTIME_SETTINGS_REGISTRY` (`user_concurrency_*` keys), and the limiter's
> error fallback derives from those registry defaults.

## Context

Per-user concurrency caps were configured in
[`apps/backend/serving/config/settings.py`](../../../apps/backend/serving/config/settings.py)
as a static module-level dict (this constant has been removed by PR #423):

```python
USER_CONCURRENCY_LIMITS: dict[str, int] = {
    "free": 1,
    "pro": 3,
    "internal": 10,
    "admin": 10,
}
```

The dict was consumed once at boot by
[`UserConcurrencyLimiter`](../../../apps/backend/serving/servers/concurrency.py)
in `bootstrap.py:388`. Capacity was captured per-user at first acquire and was
sticky for the lifetime of the process. Changing any cap therefore required
a code edit and a redeploy.

We want to:

1. Raise the `free` cap from 1 to 3.
2. Allow operators to adjust each role's cap at runtime via the existing
   admin settings API, without restarting the server.

## Goals

- Bump default `free` cap to 3.
- All four role caps (`free`, `pro`, `internal`, `admin`) adjustable at
  runtime via the admin API, persisted to the database via the existing
  `RuntimeSettings` infrastructure.
- A change to any cap takes effect for **existing** in-memory slots, not
  just newly-created ones (within the runtime-settings TTL window).
- Prevent the admin from accidentally locking themselves out (admin cap
  must be >= 1).

## Non-Goals

- Per-user overrides (every user of a given role gets the same cap).
- A separate UI; we reuse the generic `PATCH /admin/settings/{key}`.
- Cluster-wide instantaneous propagation. Other replicas pick up the new
  value within the standard `RuntimeSettings` TTL (30s).
- Migrating any other static dict in `settings.py` to runtime-adjustable.

## Design

### 1. Registry entries

Add four entries to `RUNTIME_SETTINGS_REGISTRY` in
[`apps/backend/serving/config/runtime_settings.py`](../../../apps/backend/serving/config/runtime_settings.py):

| Key | Type | Default | Min | Description |
|---|---|---|---|---|
| `user_concurrency_free` | int | 3 | 1 | Per-user concurrency cap for free-tier users |
| `user_concurrency_pro` | int | 3 | 1 | Per-user concurrency cap for pro-tier users |
| `user_concurrency_internal` | int | 10 | 1 | Per-user concurrency cap for internal users |
| `user_concurrency_admin` | int | 10 | 1 | Per-user concurrency cap for admin users |

The registry entry shape gains two optional fields applicable to numeric
types: `min` and `max` (both `int | float | None`). Other entries are
unaffected by these new fields.

### 2. Default change

The default `free` cap rises from `1` to `3` via the new
`user_concurrency_free` registry entry's `default: 3`. The static
`USER_CONCURRENCY_LIMITS` dict in
[`apps/backend/serving/config/settings.py`](../../../apps/backend/serving/config/settings.py)
is removed; the authoritative source is `RUNTIME_SETTINGS_REGISTRY`, and the
limiter's error-time fallback (`_FALLBACK_LIMITS` in
`serving/servers/concurrency.py`) is derived from those registry defaults at
import time so the two sources cannot drift.

### 3. Validation in PATCH endpoint

In
[`apps/backend/serving/servers/routers/admin/settings.py`](../../../apps/backend/serving/servers/routers/admin/settings.py),
extend the existing type-validation block in
`update_runtime_setting_endpoint` so that for `int` and `float` settings,
if the registry entry has a `min` and the value is below it, or has a
`max` and the value is above it, return HTTP 400 with a clear message.
Other entries (without `min`/`max`) are unchanged.

This makes self-lockout protection a generic feature (`min: 1` on the
admin cap), not a hardcoded role-name check.

### 4. Limiter refactor

In
[`apps/backend/serving/servers/concurrency.py`](../../../apps/backend/serving/servers/concurrency.py):

- Replace `UserConcurrencyLimiter.__init__(limits: dict[str, int])` with
  `__init__(limits_provider: LimitsProvider)`, where
  `LimitsProvider = Callable[[], Awaitable[dict[str, int]]]`.
- `limit_for(role, is_admin, *, limits)` and
  `role_label(role, is_admin, *, limits)` take the resolved dict as a
  parameter. They stay synchronous and pure.
- `try_acquire(user_id, role, is_admin)` calls
  `limits = await self._limits_provider()` once at the top, then uses
  that snapshot for the rest of the call.
- The fallback for unknown roles in `limit_for` is unchanged: unknown
  roles get the `free` cap.
- **Provider error fallback**: if the provider raises (e.g., DB hiccup),
  the limiter logs the exception and uses the registry defaults
  (`free=3, pro=3, internal=10, admin=10`) for that call. Concurrency
  is a soft control; failing open under a default is preferable to
  serving HTTP 500s when the limit lookup itself breaks.

Decoupling the limiter from `RuntimeSettings` directly (via the provider
callable) keeps tests trivial: pass a stub provider that returns a dict.

### 5. Lazy slot resize

In `try_acquire`, after resolving the current limits:

```
target_capacity = limits[label]
slot = self._slots.get(user_id)
if slot is None:
    create slot with capacity=target_capacity, role=label
elif slot.capacity != target_capacity:
    slot.capacity = target_capacity   # in_use untouched
```

`asyncio` is single-threaded, so the read-compare-update on
`slot.capacity` is atomic with respect to other tasks on the same loop.

If a downward resize leaves `slot.in_use > slot.capacity`, no in-flight
request is killed — the next `try_acquire` simply fails until the user
drains. This matches the principle that we never abort a request that is
already running.

The role label captured at slot creation remains sticky (admin -> "admin"
even if their `is_admin` flag flipped after creation). Only `capacity`
is dynamic.

### 6. Bootstrap wiring

In
[`apps/backend/serving/servers/bootstrap.py`](../../../apps/backend/serving/servers/bootstrap.py)
at the point where `user_concurrency_limiter` is constructed (currently
line 388), build a closure that reads the four keys from
`RuntimeSettings`:

```python
async def _read_concurrency_limits() -> dict[str, int]:
    return {
        "free":     await rt.get_int("user_concurrency_free"),
        "pro":      await rt.get_int("user_concurrency_pro"),
        "internal": await rt.get_int("user_concurrency_internal"),
        "admin":    await rt.get_int("user_concurrency_admin"),
    }

user_concurrency_limiter = UserConcurrencyLimiter(_read_concurrency_limits)
```

`RuntimeSettings` is initialized earlier in bootstrap; the implementation
plan must verify that ordering before relying on it.

The static `USER_CONCURRENCY_LIMITS` import in `bootstrap.py` is dropped.

### 7. Cache invalidation

The existing `PATCH /admin/settings/{key}` handler already calls
`rt.invalidate_key(key)` after a successful update. The next
`try_acquire` on the local replica reads fresh from the DB and resizes
matching slots. Other replicas pick up the change within the
`RuntimeSettings` TTL (30s by default).

No new invalidation logic is required.

## Affected components

| Component | Change |
|---|---|
| `serving/config/runtime_settings.py` | +4 registry entries, optional `min`/`max` fields |
| `serving/config/settings.py` | `free` default 1 -> 3 |
| `serving/servers/concurrency.py` | Provider-based constructor, lazy slot resize |
| `serving/servers/bootstrap.py` | Build provider closure, drop static dict |
| `serving/servers/routers/admin/settings.py` | Validate `min`/`max` for numeric settings |
| `tests/servers/test_concurrency_endpoint.py` | Stub provider, retain coverage |
| `tests/servers/test_enforce_user_concurrency.py` | Stub provider, retain coverage |
| `tests/servers/test_deps.py` | Stub provider |
| `tests/integration/...` (admin settings) | Add min-floor 400 case |

## Testing

Unit:

- `try_acquire` reads via provider on every call.
- Limit increase: existing slot at `capacity=1`, provider switches to
  `capacity=3`, next `try_acquire` resizes; user can now hold 3 in flight.
- Limit decrease: existing slot at `capacity=3`, `in_use=2`, provider
  drops to `capacity=1`. No in-flight request killed; next
  `try_acquire` returns `False`. After releases bring `in_use` to 0,
  acquire works again.
- Sticky role label: an admin's slot created at `capacity=10` retains
  the `"admin"` label after `user_concurrency_admin` is changed.
- Unknown role still falls back to `free` cap.

API:

- `PATCH /admin/settings/user_concurrency_free` with value `5` returns
  the updated entry; subsequent free-user `try_acquire` honors `5`.
- `PATCH /admin/settings/user_concurrency_admin` with value `0` returns
  HTTP 400 (below `min`).
- `PATCH /admin/settings/user_concurrency_admin` with value `1`
  succeeds (boundary).

## Risks / open questions

- **Multi-replica drift**: a PATCH only invalidates cache on the
  replica that handled it. Other replicas wait up to the TTL (30s).
  This is the existing behavior of every `RuntimeSettings`-backed knob;
  not new to this change.
- **TTL granularity**: 30s is enough for a knob like this. If we ever
  need instant cluster-wide propagation we can add a pub/sub later.
  Out of scope here.
- **Provider error path**: handled by the limiter falling back to
  registry defaults (see Limiter refactor section). Callers never see a
  500 due to a transient settings lookup failure.
