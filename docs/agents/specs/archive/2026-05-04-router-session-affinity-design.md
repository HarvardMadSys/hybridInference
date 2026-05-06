# Router session affinity (per-user provider stickiness)

## Problem

`FixedRouter._select_adapter` ([apps/backend/routing/routers.py:619](../../../apps/backend/routing/routers.py#L619)) picks a provider via weighted random selection on every request. Two consecutive requests from the same user can hit different providers, which:

- Produces inconsistent latency and behavior across turns of one conversation.
- Defeats provider-side prompt caches (each provider sees a cold prefix).
- Splits prompt history across providers in ways users don't expect.

Per-API-key affinity already exists at the *key* level inside one provider ([apps/backend/serving/adapters/key_pool.py:47](../../../apps/backend/serving/adapters/key_pool.py#L47), 5-minute TTL). No equivalent at the *provider* level.

## Goal

Same user → same provider for one model within a 5-minute window, unless the pinned provider errors. Anonymous traffic gets pinned by client IP. No new infrastructure (no Redis); reuse the in-process pattern that `key_pool.py` already uses.

## Non-goals

- Cross-process / cross-host affinity (each worker keeps its own table; same constraint as `key_pool`).
- Per-model TTL overrides.
- Affinity persistence across restarts.
- IP-affinity NAT-collision mitigation beyond using the raw client IP from `get_client_ip`.

## Requirements

| ID | Requirement |
|----|-------------|
| R1 | Affinity key = `auth_key_hash` for authenticated users, `f"ip:{client_ip}"` for anonymous. |
| R2 | Affinity scope = `(affinity_key, model_id)`. Different models use independent entries. |
| R3 | TTL = 300 s, sliding (refreshed on every successful selection that reuses the entry). |
| R4 | Any exception raised from the pinned provider drops the entry before fallback selection runs. |
| R5 | If the pinned endpoint is no longer in the allowed pool (weight=0 or circuit open), drop the entry and re-pick. |
| R6 | `pin_provider` (admin override at `X-Route-Pin`) overrides affinity. |
| R7 | Feature is on by default with kill switch `ROUTING_AFFINITY_ENABLED`. |
| R8 | No new YAML; module constants only. |

## Design

### Data model

In `BaseRouter`:

```python
@dataclass
class _Affinity:
    endpoint_id: str
    expires_at: float            # time.monotonic()

class BaseRouter:
    _affinity: dict[tuple[str, str], _Affinity]   # (affinity_key, model_id) -> _Affinity
```

State is in-process, guarded by the existing `self._lock` (same lock that protects `_circuits`).

Constants in `apps/backend/routing/routers.py`:

```python
AFFINITY_TTL_SECONDS: float = 300.0
AFFINITY_SWEEP_THRESHOLD: int = 1000
AFFINITY_ENABLED: bool = os.environ.get("ROUTING_AFFINITY_ENABLED", "1") != "0"
```

### Affinity key plumbing

`auth_key_hash` is already pushed into `req_ctx` at [completions.py:338](../../../apps/backend/serving/servers/routers/completions.py#L338). Extend that block:

```python
auth_key_hash = user_ctx.get("auth_key_hash")
if auth_key_hash:
    affinity_key = auth_key_hash
else:
    affinity_key = f"ip:{get_client_ip(request)}"
req_ctx.update({
    "auth_key_hash": auth_key_hash or "_anon",
    "affinity_key": affinity_key,
})
```

The router reads `affinity_key` from `req_ctx`, never sees the request object. If `affinity_key` is missing or empty, affinity is skipped (request behaves as today).

### Selection logic — `FixedRouter._select_adapter`

Pseudocode (replaces the body after the `pin_provider` early-return):

```
snapshot = build snapshot under self._lock     # existing
allowed = [(adapter, weight) for ... weight>0 and circuit allow_request()]
if not allowed: raise AllCircuitsOpenError    # existing

if AFFINITY_ENABLED:
    affinity_key = req_ctx.get().get("affinity_key")
    if affinity_key:
        with self._lock:
            entry = self._affinity.get((affinity_key, model_id))
            now = time.monotonic()
            if entry and entry.expires_at > now:
                # Entry is fresh. Honor only if endpoint still in allowed pool.
                for adapter, _w in allowed:
                    if _get_endpoint_id(adapter) == entry.endpoint_id:
                        entry.expires_at = now + AFFINITY_TTL_SECONDS  # sliding
                        ROUTING_AFFINITY.labels(event="hit", model=...).inc()
                        return adapter
                # Pinned endpoint not in allowed pool → drop and re-pick.
                del self._affinity[(affinity_key, model_id)]
                ROUTING_AFFINITY.labels(event="dropped_unavailable", model=...).inc()
            elif entry:
                del self._affinity[(affinity_key, model_id)]
                ROUTING_AFFINITY.labels(event="expired", model=...).inc()
            else:
                ROUTING_AFFINITY.labels(event="miss", model=...).inc()

# weighted-random pick over `allowed` (existing code)
adapter = ... weighted_random ...

if AFFINITY_ENABLED and affinity_key:
    with self._lock:
        self._affinity[(affinity_key, model_id)] = _Affinity(
            endpoint_id=_get_endpoint_id(adapter),
            expires_at=time.monotonic() + AFFINITY_TTL_SECONDS,
        )
        self._maybe_sweep_affinity_locked(time.monotonic())
        ROUTING_AFFINITY.labels(event="created", model=...).inc()

return adapter
```

Two lock acquisitions are tolerable: the random pick happens outside the lock, matching the existing pattern that splits the snapshot from `allow_request()` in the current code.

### Error → drop affinity

`chat_completion` and `stream_chat_completion` already record `_on_failure(endpoint_id)` on primary error at [routers.py:459](../../../apps/backend/routing/routers.py#L459) and [routers.py:523](../../../apps/backend/routing/routers.py#L523). Add one call right after each:

```python
except Exception as primary_error:
    self._on_failure(_get_endpoint_id(primary), reason=...)
    self._drop_affinity(model_id)
    fallback_adapters = ...
```

`_drop_affinity` is on `BaseRouter`:

```python
def _drop_affinity(self, model_id: str) -> None:
    affinity_key = req_ctx.get().get("affinity_key")
    if not affinity_key:
        return
    with self._lock:
        if self._affinity.pop((affinity_key, model_id), None):
            ROUTING_AFFINITY.labels(event="dropped_error", model=...).inc()
```

The fallback path runs through normal weighted-random selection over the remaining adapters. When that fallback succeeds, the next request from the same user creates a fresh entry via the post-selection write (sticky-write step). The current request itself is *not* re-pinned mid-call — that would require routing the failed request through `_select_adapter` a second time, which complicates the fallback loop.

Fallback adapter selection is not currently fed through `_select_adapter`, so no affinity write happens for fallbacks. This is intentional: a fallback is by definition the second-choice provider for this request, and pinning on it would lock the user onto a provider that was only chosen because their preferred one failed.

### Memory and sweep

- Bound: `O(active_users × distinct_models)`. 1k users × 10 models × ~150 bytes ≈ 1.5 MB. Fine.
- Sweep at acquire time when `len(self._affinity) > AFFINITY_SWEEP_THRESHOLD`: drop entries with `expires_at < now`. Identical trigger to `key_pool._maybe_sweep_locked`.
- Multi-worker: each Uvicorn worker has its own table. Acceptable — same caveat as `key_pool.py`. Documented in module docstring.

### Observability

New Prometheus counter in `apps/backend/serving/observability/metrics.py`:

```python
ROUTING_AFFINITY = Counter(
    "routing_affinity_events_total",
    "Per-user provider affinity decisions",
    ["event", "model"],
)
```

Events: `hit`, `miss`, `created`, `expired`, `dropped_error`, `dropped_unavailable`. Labels normalized via `normalize_model_label`.

No log changes on the hot path. One debug log in `_select_adapter` when an entry is dropped via `dropped_unavailable` — useful signal during a circuit storm.

### Config

Module-level constants only. Kill switch via `ROUTING_AFFINITY_ENABLED=0`. No `config/routing.yaml` change.

## Concurrency

- All reads and writes of `_affinity` happen under `self._lock`, the same lock that already serializes `_circuits` access. No new lock.
- The selection write occurs *after* the random pick, but the pick itself is over a snapshot taken before. A second concurrent request can race and create two entries; the second `dict[__setitem__]` simply overwrites the first. That's a benign race — the lost entry is identical in shape, just possibly pointing at a different endpoint. The next request reads whichever survived.
- Sweep occurs only on writes; readers never trigger a sweep.

## Failure modes

| Scenario | Behavior |
|----------|----------|
| `affinity_key` empty (req_ctx not populated) | Pure weighted-random, as today. |
| Pinned endpoint disabled in config | Entry dropped on next `_select_adapter`; re-pick. |
| Pinned endpoint circuit-open | Entry dropped on next `_select_adapter`; re-pick. |
| Pinned provider returns 5xx mid-call | `_drop_affinity` invoked; fallback path runs. |
| Sweep triggered with all entries fresh | No-op (filter finds nothing to drop). |
| `time.monotonic()` rollover | N/A on Linux. |
| Worker restart | All entries lost. Users re-pick on next request. Acceptable. |

## Testing

`tests/unit/routing/test_session_affinity.py`:

1. First request from `(user_a, model_x)` creates an entry; selection follows weighted random.
2. Second request from `(user_a, model_x)` within TTL returns the same endpoint as request 1; entry's `expires_at` advances.
3. Second request from `(user_a, model_x)` after TTL re-picks; possibly different endpoint.
4. `(user_a, model_x)` and `(user_a, model_y)` produce independent entries.
5. `(user_a, model_x)` and `(user_b, model_x)` produce independent entries.
6. Pinned endpoint receives weight=0 from a config change → next selection drops entry, re-picks, emits `dropped_unavailable`.
7. Pinned endpoint's circuit opens → next selection drops entry, re-picks.
8. Pinned provider raises in `chat_completion` → entry is gone before fallback runs (assert via `_affinity.get(...) is None`).
9. Pinned provider raises in `stream_chat_completion` → same.
10. Anonymous request with `affinity_key="ip:1.2.3.4"` → entry created and reused.
11. `pin_provider` parameter overrides affinity (admin override wins).
12. `AFFINITY_ENABLED=False` → no entries created; behavior identical to current.
13. Sweep: insert > 1000 entries with mixed expiries, trigger one more acquire, verify expired entries gone, fresh entries kept.
14. Concurrency: 50 threads acquire `(user_a, model_x)` in parallel; final state has one entry; all return endpoints from the allowed pool.

`tests/unit/serving/test_completions_affinity_key.py`:

15. Authenticated request → `req_ctx["affinity_key"] == auth_key_hash`.
16. Anonymous request → `req_ctx["affinity_key"] == f"ip:{client_ip}"`.

## Implementation outline

1. Add `_Affinity`, `_affinity` dict, constants, `_drop_affinity`, `_maybe_sweep_affinity_locked` to `BaseRouter`.
2. Add `ROUTING_AFFINITY` counter to `serving/observability/metrics.py`.
3. Modify `FixedRouter._select_adapter` to consult and write affinity table.
4. Add `_drop_affinity` calls in `BaseRouter.chat_completion` and `BaseRouter.stream_chat_completion` immediately after the `_on_failure` calls on primary error.
5. Update `apps/backend/serving/servers/routers/completions.py` to push `affinity_key` into `req_ctx`.
6. Tests as listed above.
7. Update `docs/developer/routing.md` with one section describing the behavior, kill switch, and multi-worker caveat.
