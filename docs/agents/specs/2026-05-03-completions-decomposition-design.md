# `chat_completions` Decomposition — Design

**Date:** 2026-05-03
**Status:** Draft → ready for plan
**Author:** Architecture review follow-up (issue #2 of 6)

## Problem

[`apps/backend/serving/servers/routers/completions.py`](../../../apps/backend/serving/servers/routers/completions.py) is the gateway's hottest path — every paid request flows through it — and it has accreted into a 1042-line god-object:

- One handler (`chat_completions`, lines 168-1042) carries ~874 lines of body and four nested helper functions captured every request.
- The `stream_generator` co-routine alone is 396 lines (lines 392-787) mixing chunk consumption, tool-call merging, TTFT timing, sanitization, content accumulation, error/fallback decisions, cost finalization, and request logging.
- An untyped `routing_info: dict[str, Any]` floats through every layer carrying magic keys (`endpoint_id`, `base_url`, `provider`, `pricing`, `routewise`, `upstream_cost_usd`).
- Pricing lookup duplicates 4× across streaming success, streaming error, non-streaming success, and non-streaming cost-increment paths.
- `_status_code_from_exception` heuristic (6-attribute fallback chain) is duplicated at lines 768 and 987-1007.

This is the file every reviewer reads on every paid-path change, and the surface area for tail bugs is large. It's also the bottleneck for adding new request-shape behavior (e.g., per-tier cost caps) without further inflating it.

## Goals

1. Decompose the handler into a thin orchestrator (~200-250 lines) that delegates to three focused modules.
2. Replace the magic `routing_info` dict with a frozen `RoutingInfo` dataclass.
3. Centralize the duplicated pricing-lookup and exception-status-code heuristics.
4. Land in three independent PRs that each leave the file working end-to-end and pass the existing FastAPI integration tests byte-for-byte.
5. Preserve all observable behavior — wire format, log payload, cost math, alert payloads.

## Non-goals

- Routing-layer changes (covered by brainstorm #6).
- Observability changes beyond moving the logger callsites (covered by #1).
- Wire-protocol changes (SSE chunk format, request/response Pydantic shapes locked).
- `log_data` / `api_logs` column shape changes (downstream consumers depend on these).
- Adapter changes.
- Embeddings router decomposition.
- Anthropic Messages router decomposition (smaller; not flagged as a god-object).

## Architecture

### Approach: extract by concern into siblings of `completions.py`

Three new modules under `apps/backend/serving/servers/routers/`:

| Module | Responsibility |
|---|---|
| `routing_info.py` | Frozen dataclasses: `RoutingInfo`, `Pricing`, `RouteWiseDecision`. Factory: `build_initial_routing_info(...)`. |
| `completions_logging.py` | `CompletionsLogger` — schedule_request_log, record_routing_observation, internal `_status_code_from_exception`, `_build_db_params`. |
| `completions_cost.py` | `PricingLookup` (cached), `CostTracker` (async fire-and-forget increment). |
| `completions_stream.py` | `StreamSession` — one-shot streaming orchestrator + internal `_ToolCallAccumulator`, `_TTFTTracker`. |

**Why siblings (vs. subpackage `completions/` or top-level `apps/backend/serving/servers/`):**
- Smallest reorganization; matches the codebase's existing flat-file pattern under `routers/`.
- Module names self-document scope (`completions_*` prefix).
- If the embeddings router or anthropic_messages router later needs the same shape, the modules can be promoted to `apps/backend/serving/servers/` without disrupting consumers.

### Typed shape: `RoutingInfo`

Frozen dataclass; immutable after construction. Stages enrich by returning a new instance via `dataclasses.replace`:

```python
@dataclass(frozen=True, slots=True)
class Pricing:
    input_per_1k: float
    output_per_1k: float
    cache_read_per_1k: float = 0.0
    cache_write_per_1k: float = 0.0


@dataclass(frozen=True, slots=True)
class RouteWiseDecision:
    """Opaque to handler; only CompletionsLogger.record_routing_observation reads its fields."""
    tier: str
    hedge_used: bool
    lp_weights: dict[str, float] | None
    # ... whatever current routing_info["routewise"] carries


@dataclass(frozen=True, slots=True)
class RoutingInfo:
    request_id: str
    model: str                      # logical model name from request
    provider: str | None            # resolved upstream provider, e.g. "openai"
    endpoint_id: str | None         # circuit-breaker key, from adapter config
    base_url: str | None            # concrete upstream URL chosen
    pricing: Pricing | None         # input/output/cache prices, or None if unknown
    routewise: RouteWiseDecision | None
    upstream_cost_usd: float | None # populated post-call from upstream usage
```

`build_initial_routing_info(chat_req, *, request_id, pin_provider) -> RoutingInfo` constructs the pre-routing state; the routing layer returns an enriched copy.

### Module 1 — `completions_logging.py`

```python
class CompletionsLogger:
    def __init__(self, log_store: LogStore, op_store: OperationalStore,
                 model_router_registry, settings):
        ...

    def schedule_request_log(self, *,
                             request: ChatCompletionRequest,
                             response: ChatCompletionResponse | None,
                             error: Exception | None,
                             status_code: int,
                             latency_ms: int,
                             routing: RoutingInfo,
                             user_ctx: UserContext) -> None:
        """Fire-and-forget: build payload, schedule async write to log_store."""

    def record_routing_observation(self, routing: RoutingInfo, *,
                                   success: bool,
                                   latency_ms: int,
                                   ttft_ms: int | None,
                                   upstream_cost_usd: float | None) -> None:
        """Forward telemetry to RouteWise; no-op if non-RouteWise route."""
```

Internal `_status_code_from_exception(exc) -> int` consolidates the duplicated 6-attribute fallback chain (lines 768 and 987-1007).

### Module 2 — `completions_cost.py`

```python
class PricingLookup:
    """Caches Pricing per endpoint_id (fallback: (provider, base_url))."""

    def __init__(self, model_router_registry):
        self._cache: dict[str, Pricing | None] = {}
        ...

    def for_routing(self, routing: RoutingInfo) -> Pricing | None:
        """Resolve pricing from adapter config; cache by endpoint_id."""


class CostTracker:
    def __init__(self, op_store, pricing: PricingLookup):
        ...

    async def schedule_increment(self, *,
                                 user_id: str,
                                 routing: RoutingInfo,
                                 prompt_tokens: int,
                                 completion_tokens: int,
                                 cache_read_tokens: int = 0,
                                 cache_write_tokens: int = 0) -> RoutingInfo:
        """Compute cost, fire-and-forget op_store increment, return RoutingInfo
        with upstream_cost_usd populated."""
```

**Cache key:** `endpoint_id` if available, fallback to `(provider, base_url)`. Adapters are immutable after `bootstrap.initialize()`; no invalidation needed in v1. (`PricingLookup.invalidate()` is a 3-line addition if hot-reload is ever introduced.)

**Cost math** is centralized: `prompt_tokens × input_per_1k / 1000 + completion_tokens × output_per_1k / 1000 + cache_read_tokens × cache_read_per_1k / 1000 + cache_write_tokens × cache_write_per_1k / 1000`. Identical to today's behavior.

### Module 3 — `completions_stream.py`

```python
class StreamSession:
    """One-shot: consume an adapter's stream, emit SSE chunks, track usage,
    finalize cost + logging on completion. Not reusable; instantiate per request."""

    def __init__(self, *,
                 routing: RoutingInfo,
                 request: ChatCompletionRequest,
                 cost_tracker: CostTracker,
                 completions_logger: CompletionsLogger,
                 user_ctx: UserContext):
        ...

    async def stream(self, adapter_chunks: AsyncIterator[Chunk]) -> AsyncIterator[bytes]:
        """Generator: yield SSE-encoded bytes; handle errors; schedule
        logging + cost on completion."""

    @property
    def yielded_first_chunk(self) -> bool:
        """For fallback decision in the router (no fallback after first chunk)."""
```

Internal helpers (private to module): `_ToolCallAccumulator` (current lines 543-570), `_TTFTTracker`, `_ChunkSanitizer` thin wrapper.

### After: thinned `chat_completions`

Sketch (~200-250 lines total):

```python
@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    user_ctx: UserContext = Depends(verify_api_key),
    router_exec = Depends(get_router),
    cost_tracker: CostTracker = Depends(get_cost_tracker),
    completions_logger: CompletionsLogger = Depends(get_completions_logger),
    ...
):
    await enforce_user_concurrency(user_ctx)

    body = await request.json()
    chat_req = ChatCompletionRequest.model_validate(body)

    if not has_role(user_ctx, "free"):
        raise HTTPException(...)

    routing = build_initial_routing_info(chat_req, request_id=..., pin_provider=...)

    adapter, routing = await router_exec.select(chat_req.model, routing)

    started = time.monotonic()
    try:
        if chat_req.stream:
            session = StreamSession(
                routing=routing, request=chat_req,
                cost_tracker=cost_tracker,
                completions_logger=completions_logger,
                user_ctx=user_ctx,
            )
            return StreamingResponse(session.stream(adapter.stream(chat_req)), ...)
        else:
            response = await adapter.chat_completion(chat_req)
            usage = normalize_usage(response.usage)
            routing = await cost_tracker.schedule_increment(
                user_id=user_ctx.user_id, routing=routing,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                ...
            )
            completions_logger.schedule_request_log(
                request=chat_req, response=response, error=None,
                status_code=200, latency_ms=int((time.monotonic() - started) * 1000),
                routing=routing, user_ctx=user_ctx,
            )
            completions_logger.record_routing_observation(
                routing, success=True, latency_ms=..., ttft_ms=None,
                upstream_cost_usd=routing.upstream_cost_usd,
            )
            return response
    except Exception as e:
        completions_logger.schedule_request_log(
            request=chat_req, response=None, error=e,
            status_code=_status_code_from_exception(e),
            latency_ms=int((time.monotonic() - started) * 1000),
            routing=routing, user_ctx=user_ctx,
        )
        completions_logger.record_routing_observation(
            routing, success=False, latency_ms=..., ttft_ms=None, upstream_cost_usd=None,
        )
        raise
```

## PR breakdown

### PR A — Type foundation + logger extraction

**New files:**
- `apps/backend/serving/servers/routers/routing_info.py`
- `apps/backend/serving/servers/routers/completions_logging.py`

**Modified:**
- `apps/backend/serving/servers/routers/completions.py` — replace every `routing_info` dict access with `RoutingInfo` field access; replace 3 helper-function calls with `CompletionsLogger` method calls; remove module-level helpers (now methods on logger class).

**New tests:**
- `tests/unit/servers/test_routing_info.py` — dataclass construction, `replace()` semantics.
- `tests/unit/servers/test_completions_logging.py` — schedule_request_log payload shape; record_routing_observation forwards correctly to RouteWise; status_code_from_exception covers all 6 exception attribute paths.

**Smoke contract test (lands in PR A, all subsequent PRs preserve):** `tests/integration/servers/test_completions_log_payload_contract.py` asserts the structured log payload schema and `api_logs` row shape match production today — locks in compatibility for downstream consumers (admin/recent-requests UI, billing reports, alert rules from PR #372).

**Existing integration tests:** unchanged. They hit the FastAPI route through `TestClient`; observable behavior identical.

**Approx diff:** +400 / -120 lines.
**Risk:** low. Mechanical dict→dataclass swap; logger logic moved verbatim into class.

### PR B — Cost extraction

**Depends on:** PR A.

**New files:**
- `apps/backend/serving/servers/routers/completions_cost.py`

**Modified:**
- `apps/backend/serving/servers/routers/completions.py` — replace 4 inline pricing-lookup blocks with one `pricing_lookup.for_routing(routing)` call; replace `_schedule_cost_increment` with `cost_tracker.schedule_increment`.
- `apps/backend/serving/servers/deps.py` — add `get_pricing_lookup` and `get_cost_tracker` dependency functions; instances live on `AppServices`.
- `apps/backend/serving/servers/bootstrap.py` — instantiate `PricingLookup(model_router_registry)` and `CostTracker(op_store, pricing_lookup)` once at startup; attach to `AppServices`.

**New tests:**
- `tests/unit/servers/test_completions_cost.py` — pricing cache hit/miss; pricing returns `None` for adapters without pricing config; `schedule_increment` calls `op_store.increment_user_cost` with the right args; concurrent increments don't interfere; cache key falls back to `(provider, base_url)` when `endpoint_id` is absent.

**Cache invalidation:** none in v1. `PricingLookup.invalidate()` future-only.

**Approx diff:** +350 / -180 lines.
**Risk:** medium. Async scheduling semantics must match exactly; existing tests on `FailedRequestAlerter` and DB log paths catch any drift.

### PR C — Streaming extraction

**Depends on:** PR A + PR B.

**New file:**
- `apps/backend/serving/servers/routers/completions_stream.py`

**Modified:**
- `apps/backend/serving/servers/routers/completions.py` — `stream_generator` and `_adapter_reader` deleted; handler creates a `StreamSession` and returns `StreamingResponse(session.stream(adapter_chunks), ...)`.

**New tests:**
- `tests/unit/servers/test_completions_stream.py` — feed synthetic chunks through `StreamSession`; assert SSE byte output; assert tool-call merging across chunked deltas; assert TTFT measured at first chunk; assert cost+log scheduled on completion; assert error chunk emitted on exception; assert `yielded_first_chunk` flips correctly; assert no fallback after first chunk yielded.

**Existing FastAPI integration tests:** the safety net. Must continue to pass byte-for-byte.

**Approx diff:** +700 / -450 lines.
**Risk:** high. Streaming is the hot path; tool-call merging and error fallback decisions have provider-specific quirks. Mitigations: stage to dev → soak → prod; integration tests as golden master; per-method unit tests.

### Sequencing

A → soak 24-48h on staging → B → soak → C → soak → done. Each PR keeps the file working end-to-end. Land mid-week so soak completes before the work-week ends.

## Testing strategy

| Layer | What it does | Per PR |
|---|---|---|
| FastAPI integration tests (existing) | Hit the route through `TestClient`. **Golden master.** Must pass byte-for-byte at every PR. | A, B, C |
| Unit tests (new per PR) | Test each extracted class with mocked dependencies. | A, B, C |
| Streaming chunk tests | Synthetic chunk streams driven into `StreamSession`. | C |
| Smoke contract test | Asserts structured log payload shape + `api_logs` row shape unchanged. | A (locks in for B+C) |

### Critical compatibility invariants (no PR may break)

- The structured log payload emitted per request (`api_logs` row shape — admin/recent-requests UI consumes this).
- The Slack alert payloads from PR #372 (rule 1, rule 2 read these logs).
- The streaming SSE wire format (clients depend on it byte-for-byte).
- The cost-increment math.

## Risk + rollback

| PR | Risk | Rollback |
|---|---|---|
| A | Low | `git revert`; no data state involved. |
| B | Medium | `git revert`; cost increments fall back to inline `_schedule_cost_increment`. No data corruption (idempotent on user.daily_cost). |
| C | High | `git revert`; staging soak before prod deploy. Tail risk around tool-call merging or specific provider quirks. |

**Mitigations:**
- Each PR lands mid-week, deploys to staging, soaks 24h, monitors PR #372 rule-based alerts (failed-rate, 5xx, p95).
- Keep old `chat_completions` git history navigable — every PR commit message references the file path so `git log -- apps/backend/serving/servers/routers/completions.py` is informative.

## Performance

- `PricingLookup` cache eliminates per-request adapter introspection (lines 290-313 today, 4 calls per request → 1 cache lookup). Modest measurable win.
- `RoutingInfo` frozen dataclass with `slots=True` is faster than dict access; prevents accidental mutation.
- `StreamSession` should be a wash: same code paths, fewer closure captures (today, 4 nested helpers capture outer scope every request).

## Open questions (resolved during brainstorming)

| Question | Resolution |
|---|---|
| Pricing cache key | `endpoint_id` if available, fallback to `(provider, base_url)`. |
| Hedging interaction | Hedging is transparent at adapter layer; `StreamSession` sees only the winner's stream. Nothing to do. |
| Anthropic Messages router | Leave as-is; not flagged as god-object. |
| `_status_code_from_exception` heuristic | Centralize in PR A but don't fix; future fixes are now one-place changes. |

## Out-of-scope follow-ups (separate brainstorms)

- Make fire-and-forget side effects observable (#4): the `schedule_increment` and `schedule_request_log` calls today have no completion telemetry.
- Routing config expressiveness (#6).
- Schema migration framework (#3).
- Decompose admin page (#5).
