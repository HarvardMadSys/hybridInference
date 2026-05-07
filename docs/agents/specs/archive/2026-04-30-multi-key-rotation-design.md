# Multi-Key Rotation with 5-Minute Session Affinity

**Date:** 2026-04-30
**Status:** Design approved, implementation plan added
**Branch:** `jason/claude/multi-key-rotation`

## Problem

Some upstream providers (Zhipu, Chutes, Featherless, etc.) enforce **per-key** rate limits and daily quotas. Today, each route in `config/models.yaml` configures exactly one `api_key`, so a single hot key bottlenecks the whole pool of users sharing that route. We want to attach **multiple keys per route** and rotate across them so the aggregate throughput scales with the number of keys, while keeping a given user "sticky" to one key for short windows so behavior is predictable.

## Goals

- Allow a route to declare a list of API keys instead of a single one.
- Bind a given end-user (their `hyi-xxx` API key) to one upstream key for **5 minutes** (TTL from initial assignment).
- Pick the **least-loaded** upstream key (lowest lifetime request count in this process) when assigning new affinity.
- Detect per-key rate-limit / quota errors (HTTP 429) and **cool down** the offending key, honoring `Retry-After`.
- When all keys in a pool are cooled-down, surface the failure so the existing route-level fallback chain takes over.
- No changes to the router, no new Redis dependency, no DB schema changes.
- Backwards-compatible: existing single-`api_key` routes keep working unchanged.

## Non-Goals

- Cross-worker / cross-replica state sharing. Each gunicorn worker keeps its own pool. A user may bounce across keys across workers within their 5-min window — acceptable.
- Persistence across restarts. Counters and affinity are in-process and reset on restart — acceptable.
- Mid-stream re-routing. If a 429 arrives mid-stream, the in-flight request fails to the client (existing behavior). Only the next request gets re-routed.
- Sliding-window affinity. The 5-min window is a fixed TTL from initial assignment, not reset by activity.
- Runtime key management (admin API/UI to add/remove keys). Keys remain env-var-driven, edited in `models.yaml`.

## Known Limitations

- **Multi-key routes intentionally skip transient-error retries.** The single-key path uses `json_post_with_retry(retries=2)`, which retries on transient `ClientError`/`TimeoutError`. The multi-key path uses `json_post` with no retries — only HTTP 429 triggers rotation; other errors propagate immediately to the router fallback chain. This avoids burning retry attempts on the same key when other keys are available, and trades single-route resilience for cross-provider fallback (which the router already handles).

- **Anonymous traffic collapses to a single affinity bucket.** When `auth_key_hash` is absent (auth disabled, internal calls, or model probes), every request uses the sentinel `"_anon"` as the affinity key, pinning all anonymous traffic to one upstream key. This is by design — the affinity contract is "per end-user API key", and traffic without an authenticated identity has no per-user dimension to spread over. Production deployments should keep auth enabled; staging/dev under load may want to disable affinity for anonymous traffic if they observe key hot-spotting.

## Architecture

A new component `KeyPool` sits inside the adapter. Adapters that opt into multi-key construct a `KeyPool` from the configured key list; single-key routes are unchanged.

```
chat_completions request
  → router picks adapter (existing FixedRouter, unchanged)
  → adapter.chat_completion()
    ┌─ loop (until success, KeyPoolExhausted, or non-429 error):
    │    key_pool.acquire(affinity_key)            ← NEW
    │    POST upstream with that key
    │    if 429: key_pool.release(lease, retry_after=...) ← NEW; cool down THIS key, continue loop
    │    if 2xx: key_pool.release(lease, success);  return response
    │    if other error: key_pool.release(lease, no_cooldown); raise
    └─ on KeyPoolExhausted (no acquirable key): raise as adapter failure
  → existing failover hits next provider in route
```

**Within-pool retries vs cross-provider fallback:** the adapter loops over keys in *its own* pool on 429s. Only when the pool is fully exhausted (or a non-429 hard error occurs) does the failure bubble to the router for cross-provider fallback. This is what makes multi-key meaningful — a single 429 against key1 shouldn't waste the chutes/featherless fallback budget when key2/key3 would have worked.

**Bound on the loop:** at most `len(_keys)` iterations per request (each iteration either succeeds, marks the current key cooled-down, or raises). The lease from a 429'd attempt is released with cooldown info; the next iteration acquires a fresh lease for a different key. If the loop runs through every key without success, the last `acquire()` raises `KeyPoolExhausted`.

`KeyPool` exposes two operations:

- `acquire(user_id) -> (key_str, lease)` — returns the upstream key the user should use, creating or reusing affinity.
- `release(lease, outcome)` — reports the result so the pool can update cooldowns.

State lives in-process per worker, behind a single `threading.Lock`.

### Internal State

```python
@dataclass
class _KeyState:
    key: str                     # the actual API key
    request_count: int = 0       # lifetime-since-process-start; "least-loaded" signal
    cooldown_until: float = 0.0  # monotonic ts; <= now means available

@dataclass
class _Affinity:
    key_index: int
    expires_at: float            # monotonic ts; assigned_at + 300

class KeyPool:
    _keys: list[_KeyState]
    _affinity: dict[str, _Affinity]   # affinity_key -> affinity
    _lock: threading.Lock
    _provider_label: str         # used for metrics
```

**Affinity key:** the stable identifier of the caller's hyi-xxx API key — specifically the `key_hash` (HMAC-SHA256) already produced by `verify_api_key()` in `serving/servers/auth.py`. Using `key_hash` (not `user_id`) means users with multiple hyi-xxx keys get independent affinities, which matches the "per end-user API key" semantics. Anonymous / internal calls (no auth) use the sentinel `"_anon"`.

### `acquire(affinity_key)` Algorithm

1. Take `_lock`.
2. Opportunistic affinity sweep: if `len(_affinity) > 1000`, drop entries where `expires_at < now`.
3. Look up `_affinity[affinity_key]`:
   - If present, `now < expires_at`, AND `_keys[idx].cooldown_until <= now` → reuse.
   - If present but the bound key is cooled-down → drop affinity (fall through to re-pick).
   - If present but expired → drop affinity (fall through).
4. If no affinity: among keys where `cooldown_until <= now`, pick the one with the lowest `request_count` (ties broken by index — deterministic).
   - If no key is available → raise `KeyPoolExhausted`.
5. Set `_affinity[affinity_key] = _Affinity(idx, now + 300)`.
6. Increment `_keys[idx].request_count` (under lock — prevents concurrent acquires from piling onto the same "least-loaded" key).
7. Return `(_keys[idx].key, lease=_Lease(idx, affinity_key))`.

### `release(lease, outcome)` Algorithm

`outcome` carries either an HTTP status, a `Retry-After` value, or a flag for non-HTTP failures.

| Outcome | Action |
|---|---|
| 2xx | no cooldown change |
| 429 with `Retry-After` | `cooldown_until = now + parsed_seconds`, capped at 1 hour |
| 429 without `Retry-After` | `cooldown_until = now + 120` (2-min default) |
| Other 4xx (400, 401, 403, …) | no cooldown — these are not key-exhaustion. 401 still bubbles up to the caller as a hard failure (mis-configured key). |
| 5xx, network error, timeout | no cooldown — provider issue, not key issue. Existing circuit breaker handles it. |

**`Retry-After` parsing:** RFC 7231 allows either an integer number of seconds OR an HTTP-date. We support both: try integer first, fall back to `email.utils.parsedate_to_datetime`, and if neither parses cleanly, fall through to the 2-min default. Negative or malformed values → 2-min default. Final value is clamped to `[0, 3600]` so a misbehaving provider can't park a key for hours.

**Cooldown decisions are based on HTTP status only**, not response-body inspection. If a provider returns a 200 with an error payload describing a quota issue, the key is not cooled down — that's a known limitation and matches today's behavior.

`request_count` is **never decremented** — it is a load signal, not a semaphore.

### Pool Exhaustion

If `acquire` finds every key in cooldown, it raises `KeyPoolExhausted`. The adapter converts this into a normal upstream failure (raised the same way an HTTP 5xx would be), and the existing `FixedRouter` fallback chain in `routing/routers.py` engages — the request is retried on the next provider in the route (e.g., chutes/featherless).

## Configuration

### `config/models.yaml` Schema Extension

A route may use **either** `api_key` (string, existing) **or** `api_keys` (list, new) — not both.

```yaml
- id: glm-4.7
  routes:
    - provider: zai
      base_url: https://api.z.ai/api/paas/v4
      api_keys:
        - ${ZAI_API_KEY_1}
        - ${ZAI_API_KEY_2}
        - ${ZAI_API_KEY_3}
      provider_model_id: glm-4.6
      weight: 1.0
    - provider: ollama
      base_url: ...
      api_key: ${OLLAMA_API_KEY}    # unchanged single-key form
      weight: 1.0
```

### Loader Rules (`serving/servers/registry.py`)

- Both `api_key` and `api_keys` set on the same route → fail loading with a clear validation error.
- Empty / unset env vars resolve to `""` and are silently dropped from the list, with a `logger.warning` naming the route and dropped slot.
- After dropping blanks, an empty `api_keys` list → fail loading (clearly mis-configured).
- A single-entry `api_keys` list is allowed; the pool is just size 1.

### `ModelConfig` Change (`serving/adapters/base.py`)

```python
@dataclass
class ModelConfig:
    ...
    api_key: str | None = None
    api_keys: list[str] | None = None    # NEW
```

`OpenAICompatAdapter.__init__` decides:

- `api_keys` set → instantiate `self._key_pool = KeyPool(api_keys, provider_label=...)`.
- Else → leave `self._key_pool = None`; behave exactly as today.

## Adapter Integration

### Touchpoints in `serving/adapters/openai_compat.py`

1. **`_build_headers()`** (currently lines 164–188): if `self._key_pool` is set, call `self._key_pool.acquire(user_id)` and use the returned key; otherwise use `self.config.api_key` as today. Store the lease on a request-scoped variable accessible to the response path.
2. **HTTP response handling**: wrap the `httpx` call in a `try/finally` (or equivalent) that always calls `self._key_pool.release(lease, outcome)` — both on success and on exception.

### Where the Affinity Key Comes From

`serving/utils/context.py` already plumbs request context (`req_ctx.push(model=..., provider=...)`). We extend the context with `auth_key_hash` (set in `verify_api_key()` at `serving/servers/auth.py`, taken from the `key_hash` column the auth flow already computes) and read it inside the adapter. If absent (internal calls without auth), use sentinel `"_anon"` — all anonymous calls share affinity to one key, which matches today's behavior.

### Streaming

- 429 detected on the **first chunk** before any tokens stream → mark cooldown, raise the same error path as today (router can fail over).
- 429 mid-stream after tokens have been emitted → mark cooldown, but the in-flight stream still fails to the client (existing behavior). No automatic re-stream.

## Telemetry

Add the following Prometheus metrics in `serving/observability/metrics.py`:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `KEY_POOL_REQUESTS` | counter | `provider`, `key_index` | Bumped on every `acquire`, including affinity reuse |
| `KEY_POOL_COOLDOWNS` | counter | `provider`, `key_index`, `reason` (`retry_after` \| `default_2min`) | Bumped on every cooldown trigger |
| `KEY_POOL_EXHAUSTED` | counter | `provider` | Bumped when `KeyPoolExhausted` is raised |
| `KEY_POOL_ACTIVE_AFFINITIES` | gauge | `provider` | `len(_affinity)`, refreshed after sweep |

Raw key strings are **never** emitted as label values. `key_index` is the index into the pool list (0-based).

## Testing Strategy

### Unit Tests — `tests/unit/test_key_pool.py` (new)

- Initial pick chooses the lowest-`request_count` key (deterministic with seeded counters).
- Same `user_id` returns the same key index for 5 minutes; different users may get different keys.
- Affinity expiration: after `now > expires_at`, a fresh pick happens and may land on a different key.
- Cooldown: `release` with 429 + `Retry-After: 30` sets cooldown 30s out; key is skipped during selection until then.
- Cooldown without header defaults to 120s.
- Mid-affinity rotation: bound key entered cooldown externally → next `acquire` for that user picks a different key; old affinity is dropped.
- All-exhausted: every key's `cooldown_until > now` → `KeyPoolExhausted` is raised.
- Concurrency: 100 threads call `acquire` concurrently across N new users; counters consistent, no race; no key is over-picked. Real `threading` + actual lock, not mocks.
- Affinity sweep: insert >1000 expired entries, call `acquire`, confirm dict shrinks.
- Loader: `api_key` and `api_keys` both set → ValidationError; `api_keys` empty after env expansion → loader error; mix of set/unset env vars drops blanks with a warning.

### Integration Tests — `tests/integration/test_openai_compat_adapter.py` (extend)

- Route with `api_keys: [k1, k2]` and an `httpx` mock returning 429 + `Retry-After: 1` for k1 → first request lands on k1, gets 429, k1 cools down; second request lands on k2; after 1.1s, k1 is back in the pool.
- Pool exhausted (all keys 429'd) → `KeyPoolExhausted` propagates → router falls back to next provider in route (existing fallback assertion patterns).
- Single-key legacy route (`api_key: ${X}`) — unchanged behavior; no pool created (back-compat).

## Files Touched (estimated)

| File | Change |
|---|---|
| `serving/adapters/key_pool.py` | NEW — `KeyPool`, `_KeyState`, `_Affinity`, `KeyPoolExhausted` |
| `serving/adapters/base.py` | Add `api_keys: list[str] \| None` to `ModelConfig` |
| `serving/adapters/openai_compat.py` | Wire `KeyPool` into `_build_headers()` and request lifecycle |
| `serving/servers/registry.py` | Loader: parse `api_keys`, validate, env-expand, drop blanks |
| `serving/utils/context.py` | Plumb `user_id` through request context |
| `serving/servers/auth.py` | Set `user_id` on the context after key verification |
| `serving/observability/metrics.py` | Add 4 new Prometheus metrics |
| `tests/unit/test_key_pool.py` | NEW — unit tests above |
| `tests/integration/test_openai_compat_adapter.py` | Extend with integration tests above |
| `config/models.yaml` | (Documentation/example only — actual key roll-out is a separate ops task) |

## Open Questions

None at design time. Implementation will surface concrete env-var naming (`ZAI_API_KEY_1` vs `ZAI_API_KEY_A`, etc.) — that's a config detail decided when the keys are actually provisioned.
