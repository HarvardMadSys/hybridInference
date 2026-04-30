# Per-User Concurrency Limit — Design

**Issue:** [#242](https://github.com/HarvardMadSys/hybridInference/issues/242) — Limit per-user concurrency
**Date:** 2026-04-30
**Status:** Draft for review
**Target branch:** `jason/claude/per-user-concurrency` → `dev`

## Goal

Cap the number of simultaneous in-flight inference requests per user, by role:

| Role     | Concurrent in-flight cap |
| -------- | ------------------------ |
| free     | 1                        |
| pro      | 3                        |
| internal | 10                       |
| admin    | 10                       |

A user attempting an additional concurrent request beyond their cap receives HTTP 429 immediately (no queueing). The cap applies to inference routes only: `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/anthropic/v1/messages`.

## Non-goals

- Multi-replica coordination (Redis/DB-backed limiter). Backend runs as a single uvicorn process today.
- Per-user DB-level overrides (no "VIP user with 50 slots").
- Queueing or wait-for-slot semantics. Fail-fast 429 only.
- Eviction of inactive user semaphores. Trivial memory footprint; restart clears.
- Role/tier merge. Tracked separately (precursor refactor).
- Grafana dashboard updates.
- Gating any JWT-authenticated inference paths (none exist today).

## Context

Investigation of the current code (file references below) established:

- **Auth on inference routes** uses the `verify_api_key` FastAPI dependency in [serving/servers/auth.py:78–259](serving/servers/auth.py#L78-L259). It returns `{user_id, user_name, tier, role, authenticated, is_admin}`.
- **`users.role`** values today: `free`, `internal`, `admin`. Defined in [serving/config/settings.py:161](serving/config/settings.py#L161) (`ROLE_RANK = {"free": 0, "internal": 1, "admin": 2}`).
- **`api_keys.tier`** is a separate column, defaults to `'free'`, with values like `free`/`pro`/`enterprise`. **Out of scope here** — see role/tier merge note below.
- **No existing per-user concurrency control.** The `PersistentRateLimiter` ([serving/servers/rate_limiter.py](serving/servers/rate_limiter.py)) is per-model token-bucket; the daily cost quota in `verify_api_key` is per-key cost, not in-flight count.
- **Single uvicorn process** — [infrastructure/docker/Dockerfile.backend:37–38](infrastructure/docker/Dockerfile.backend#L37-L38) runs `uvicorn ... --host 0.0.0.0 --port 8080` with no `--workers` flag. In-process state is sufficient.
- **No Redis** in the stack; only Postgres + a SQLite file for the rate limiter.

## Architecture

- **Bucket key:** `user_id` (the UUID returned by `verify_api_key`). Per-user, shared across all of that user's API keys.
- **Storage:** an in-process `dict[user_id, asyncio.Semaphore]` owned by a new `UserConcurrencyLimiter` service, registered on `AppServices` and initialized during the FastAPI lifespan.
- **Acquire:** at request entry via a new FastAPI dependency `enforce_user_concurrency`, which itself depends on `verify_api_key` (FastAPI caches the resolution — no double auth).
- **Release:** the dependency uses the `yield` + `finally` generator pattern. Starlette consumes the response body iterator (including streaming SSE) *after* the route handler returns and *after* dependency `yield` points, then runs the `finally` blocks. The slot is released:
  - after a unary response is sent,
  - after a streaming response is fully drained,
  - after the client disconnects mid-stream,
  - after an exception during the handler or stream.
- **Single-process assumption.** If the backend ever scales horizontally, the in-process semaphore is swapped for a Redis or DB backend behind the same `try_acquire` / `release` interface. Out of scope here.

## Components

### 1. `UserConcurrencyLimiter`

New module: `serving/servers/concurrency.py`.

Responsibilities:
- Hold the per-user semaphore map.
- Map `(role, is_admin)` → integer capacity.
- Lazy-create a semaphore on first acquire per user, with capacity captured at creation time from the user's role.
- Provide non-blocking `try_acquire` and synchronous `release`.
- Expose Prometheus metrics (see "Observability" below).

Interface (sketch):

```python
class UserConcurrencyLimiter:
    def __init__(self, limits: dict[str, int]):
        self._limits = limits  # {"free": 1, "pro": 3, "internal": 10, "admin": 10}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._create_lock = asyncio.Lock()  # guards lazy creation

    def limit_for(self, role: str, is_admin: bool) -> int:
        if is_admin:
            return self._limits["admin"]
        return self._limits.get(role, self._limits["free"])  # unknown -> free

    async def try_acquire(self, user_id: str, role: str, is_admin: bool) -> bool:
        """Non-blocking acquire. Returns True on success, False if at capacity."""

    def release(self, user_id: str) -> None:
        """Release a previously-acquired slot. Idempotent on missing user_id."""
```

Implementation notes:
- `asyncio.Semaphore` does not expose a public `acquire_nowait`. The non-blocking acquire is implemented as: under `_create_lock`, check `sem._value > 0`, and if so call `sem.acquire()` which completes synchronously when a slot is available. Alternatively use a small custom semaphore class — the plan will pin the exact mechanism.
- **Capacity at creation time.** If a user's role changes (free → pro) while the semaphore exists, the existing semaphore keeps the old capacity. Acceptable trade-off; restart corrects it. Documented behavior.
- **Defensive on unknown role.** Fall back to `free` (most restrictive) so an unexpected DB value never bypasses the limit.
- **`is_admin` precedence.** Admin users always get the admin cap (10), regardless of their `role`.
- **No eviction.** The dict grows with unique active users. ~50 bytes per semaphore × thousands of users → trivial. Process restart clears.

### 2. `enforce_user_concurrency` dependency

In `serving/servers/concurrency.py` (or `deps.py`):

```python
async def enforce_user_concurrency(
    user: dict = Depends(verify_api_key),
    services: AppServices = Depends(get_services),
):
    limiter = services.user_concurrency_limiter
    user_id = user["user_id"]
    role = user["role"]
    is_admin = user.get("is_admin", False)

    acquired = await limiter.try_acquire(user_id, role, is_admin)
    if not acquired:
        limit = limiter.limit_for(role, is_admin)
        raise HTTPException(
            status_code=429,
            detail={
                "error": {
                    "code": "concurrency_limit_exceeded",
                    "message": f"Too many concurrent requests (limit: {limit})",
                    "limit": limit,
                    "role": "admin" if is_admin else role,
                }
            },
            headers={"Retry-After": "1"},
        )
    try:
        yield
    finally:
        try:
            limiter.release(user_id)
        except Exception:
            # never let cleanup break the request lifecycle
            logger.exception("user_concurrency: release failed", extra={"user_id": user_id})
```

### 3. AppServices wiring

- Construct `UserConcurrencyLimiter(USER_CONCURRENCY_LIMITS)` in the FastAPI lifespan.
- Attach to `AppServices` as `user_concurrency_limiter`.
- Already-existing `get_services()` dependency surfaces it.

### 4. Route wiring

Add `Depends(enforce_user_concurrency)` to each of the four inference handlers. The handlers already use `Depends(verify_api_key)`; the new dependency depends on the same one, so FastAPI's dependency caching means no extra DB hit.

| Route                      | Handler file & line                                                      |
| -------------------------- | ------------------------------------------------------------------------ |
| `/v1/chat/completions`     | [serving/servers/routers/completions.py:107](serving/servers/routers/completions.py#L107) |
| `/v1/completions`          | [serving/servers/routers/compat.py:52](serving/servers/routers/compat.py#L52)             |
| `/v1/embeddings`           | [serving/servers/routers/embeddings.py:19](serving/servers/routers/embeddings.py#L19)     |
| `/anthropic/v1/messages`   | [serving/servers/routers/anthropic_proxy.py:309](serving/servers/routers/anthropic_proxy.py#L309) |

## Configuration

In [serving/config/settings.py](serving/config/settings.py):

```python
# Existing — extended to include "pro"
ROLE_RANK: dict[str, int] = {"free": 0, "pro": 1, "internal": 2, "admin": 3}
VALID_ROLES = frozenset(ROLE_RANK)

# New
USER_CONCURRENCY_LIMITS: dict[str, int] = {
    "free": 1,
    "pro": 3,
    "internal": 10,
    "admin": 10,
}
```

Two changes:

1. **Add `pro` to `ROLE_RANK`** at rank 1. Non-breaking — no existing user has `role='pro'`. The `users.role` column is `TEXT` with no enum constraint, so no DB migration is needed for the value addition itself.
2. **Add `USER_CONCURRENCY_LIMITS`** as a plain constant. Limits are product decisions, not deploy-time config. No env-var override (YAGNI; can be added in one line if ever needed).

**Audit fallout from adding `pro`:** any code that filters/branches on `users.role` membership needs to handle the new value. Quick grep before implementation; bring any required updates into this PR. The role/tier merge (which would also align `api_keys.tier` with the role vocabulary) is a separate prerequisite refactor and is out of scope here.

## API surface

429 response body when the cap is hit:

```json
{
  "detail": {
    "error": {
      "code": "concurrency_limit_exceeded",
      "message": "Too many concurrent requests (limit: 1)",
      "limit": 1,
      "role": "free"
    }
  }
}
```

Headers: `Retry-After: 1`. (FastAPI nests under `detail` by default for `HTTPException`; we keep that shape to match the existing 429 patterns from the rate limiter and quota.)

No changes to the success-path response.

## Streaming behavior

The dependency's `yield` + `finally` runs after Starlette finishes draining the `StreamingResponse` body iterator. This means a streaming response holds the slot for its full duration — exactly the desired semantics (a user with 1 slot streaming a long completion cannot fire a second request in parallel).

Validated by integration tests (case 5 in the test plan below).

## Observability

Three new Prometheus metrics defined in `serving/observability/metrics.py` (alongside all other domain metrics) and imported into `serving/servers/concurrency.py`:

```python
user_concurrency_in_flight = Gauge(
    "user_concurrency_in_flight",
    "Active concurrent inference requests, by role",
    labelnames=("role",),
)
user_concurrency_acquires_total = Counter(
    "user_concurrency_acquires_total",
    "Total slot acquire attempts",
    labelnames=("role", "outcome"),  # outcome ∈ {"granted", "rejected"}
)
user_concurrency_rejected_total = Counter(
    "user_concurrency_rejected_total",
    "Requests rejected due to per-user concurrency limit",
    labelnames=("role",),
)
```

- On grant: `acquires_total{outcome="granted"}` += 1; `in_flight{role}` += 1.
- On rejection: `acquires_total{outcome="rejected"}` += 1; `rejected_total{role}` += 1.
- On release: `in_flight{role}` -= 1. The role label is captured at acquire time and stored alongside the slot, so a role change mid-flight does not desync the gauge.

**Structured log on rejection only** (INFO): `"per-user concurrency limit hit"` with `user_id`, `role`, `limit`, `route`. Successful acquires are not logged (too noisy).

No Grafana dashboard updates in this PR.

## Test plan

Following the existing pytest-asyncio + `AsyncClient` pattern from [test/servers/conftest.py](test/servers/conftest.py).

### Unit tests (`test/servers/test_user_concurrency_limiter.py`)

1. `try_acquire` returns `True` up to capacity, `False` beyond.
2. `release` frees a slot for a subsequent acquire.
3. `limit_for(role, is_admin=True)` returns 10 regardless of role.
4. Unknown role falls back to `free` cap (1).
5. Two distinct `user_id`s have independent budgets.
6. Capacity at creation is sticky: change role between two acquires for the same `user_id`, second acquire still uses the original capacity.

### Integration tests (`test/servers/test_concurrency_endpoint.py`)

Use a controllable inference handler (a small test-only route mounted in the test app, or `respx` mocks against `/v1/chat/completions`) where the slot can be held for a deterministic duration via `asyncio.Event`.

1. **Free user, 2 concurrent requests** — first 200, second 429 with `code=concurrency_limit_exceeded`, `limit=1`.
2. **Pro user (DB-seeded `role='pro'`), 4 concurrent** — fourth gets 429.
3. **Admin user, 11 concurrent** — eleventh gets 429.
4. **Two free users, 1 concurrent each** — both succeed (per-user isolation).
5. **Streaming holds slot.** Open a streaming request; while it is mid-stream, send a second from the same user → 429. Drain the first; retry → succeeds.
6. **Client disconnect releases slot.** Open a streaming request, close client mid-stream → next request from same user succeeds.
7. **Handler exception releases slot.** Force the handler to raise → next request from same user succeeds.
8. **Embeddings count toward the cap.** Free user with embedding in flight → second embedding gets 429.

### Metrics tests

- `acquires_total{outcome="rejected"}` increments on a rejection.
- `in_flight{role="free"}` returns to 0 after a request completes (test reads the prometheus registry).

Cases 5, 6, 7 are the highest-value because they validate the `yield`/`finally` cleanup path that is the trickiest part of the design. If they ever regress, slots leak and users are permanently stuck.

## Rollout

- Single PR to `dev`, branch `jason/claude/per-user-concurrency`.
- No feature flag. Limits are conservative; failure mode (429) is recoverable client-side.
- No data migration — `pro` is a new role value but no existing user has it.
- Deploy to staging via `deploy_staging.sh`, smoke-test with a couple of API keys, then promote.

## Acceptance criteria

- The four inference routes return 429 with the documented body when the user's cap is exceeded.
- All test cases above pass.
- `user_concurrency_*` metrics appear at the existing `/metrics` endpoint.
- `pro` is in `ROLE_RANK`; no other role/tier logic touched.
- No Redis dependency; no DB migration.

## Risks & mitigations

- **Streaming cleanup correctness.** The whole design depends on Starlette running dependency `finally` blocks after the response body drains. Mitigation: integration tests 5, 6, 7 directly assert this; if any regress, slots leak.
- **Role mismatch between auth dict and DB.** `verify_api_key` reads `users.role` per request, but capacity is captured at semaphore creation. A user upgraded mid-session sees the old cap until process restart. Acceptable; documented.
- **Memory growth.** `dict[user_id, Semaphore]` grows unbounded. ~50 bytes × N users → trivial in practice. If it ever becomes a concern, swap to an LRU.
- **Adding `pro` to `ROLE_RANK`.** Any code that switches on role values needs to handle the new value. Caught by a grep audit during implementation; any required updates land in this PR.

## References

- Issue: https://github.com/HarvardMadSys/hybridInference/issues/242
- Auth dependency: [serving/servers/auth.py:78–259](serving/servers/auth.py#L78-L259)
- Existing role definitions: [serving/config/settings.py:161](serving/config/settings.py#L161)
- Existing rate limiter (per-model, for reference only): [serving/servers/rate_limiter.py](serving/servers/rate_limiter.py)
- Test fixtures: [test/servers/conftest.py](test/servers/conftest.py)
