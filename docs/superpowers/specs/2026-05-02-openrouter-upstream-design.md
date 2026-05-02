# OpenRouter as Upstream Backend

**Date:** 2026-05-02
**Status:** Design approved
**Branch:** `jason/claude/openrouter-upstream`

## Problem

The gateway exposes an OpenRouter-compatible API surface to clients but cannot use OpenRouter itself as an upstream provider. We want to (a) reach models we do not host directly (Llama 3.3, Mistral, etc.) and (b) add OpenRouter as a last-resort fallback leg on existing routes when in-house and Chutes/Featherless legs are exhausted. End-user pricing stays fixed per model; OpenRouter's actual per-request cost is logged for internal accounting only.

## Goals

- Add a new adapter `kind` for OpenRouter, dispatched from `_make_adapter` in `serving/servers/registry.py`.
- Support a bracket syntax `kind: openrouter[<provider_slug>]` that pins the request to a specific OpenRouter upstream via `provider.order=[<slug>], allow_fallbacks=false`. Bare `kind: openrouter` lets OpenRouter pick.
- Reuse the existing `OpenAICompatAdapter` HTTP/streaming/retry path; add a thin subclass `OpenRouterAdapter` that injects the OpenRouter-specific request shape.
- Inject required attribution headers `HTTP-Referer: https://freeinference.org` and `X-Title: FreeInference` on every request.
- Always set `usage: {include: true}` (and `stream_options: {include_usage: true}` when streaming) so OpenRouter returns its actual cost.
- Extract OpenRouter's reported `usage.cost` into a new `UsageInfo.upstream_cost_usd` field and persist it to a new `api_logs.upstream_cost_usd` column.
- End-user billing (`api_logs.cost_usd`) remains tokens × model-level pricing — unchanged.
- Single `OPENROUTER_API_KEY` env var. No multi-key pool (Q7=A).
- Backwards compatible: existing routes are not affected.

## Non-Goals

- No proxying of OpenRouter's full model catalog through `/v1/models`. Only models explicitly listed in `config/models.yaml` are exposed.
- No `transforms: ["middle-out"]` or other OpenRouter optional features (Q8=A).
- No multi-key rotation for OpenRouter (Q7=A). Single account API key.
- No mid-stream re-routing on errors. Existing per-stream failure semantics apply.
- No admin UI for editing OpenRouter routes. Routes are managed in `models.yaml` like every other route.
- No special handling of OpenRouter's BYOK (user-supplied upstream keys) feature.
- No exposure of `upstream_cost_usd` to end users. Internal-only field.

## Known Limitations

- **`upstream_cost_usd` is best-effort, not authoritative.** OpenRouter omits `cost` when the account is in error state, when streaming chunk parsing fails, or when an upstream provider does not report cost. The adapter logs `NULL` in those cases and proceeds — no end-user impact.
- **Bare `openrouter` kind has no upstream isolation.** When OpenRouter picks freely, the same model id can hit different upstream providers across requests, with different latencies and (true) costs. The endpoint_id and circuit breaker stay tied to the route entry, so error-rate stats mix providers. Use the bracket form when isolation matters.
- **Per-pin endpoint_id requires the bracketed kind to flow through `_make_provider_id`.** If two route legs use the same `base_url` but different pinned providers, they need distinct endpoint_ids for circuit breaking; this is achieved by passing the full bracketed kind string to `_make_provider_id`.
- **403 (flagged input) does not fall back.** Other providers will likely also reject. Propagated to client as deterministic failure.

## Architecture

```
models.yaml route entry
  kind: openrouter[deepinfra]   ← bracket form pins upstream
  base_url: https://openrouter.ai/api/v1
  api_key: ${OPENROUTER_API_KEY}
  provider_model_id: meta-llama/llama-3.3-70b-instruct
       │
       ▼
serving/servers/registry.py::_make_adapter(kind, cfg)
  parse_openrouter_kind("openrouter[deepinfra]") → ("openrouter", "deepinfra")
  cfg["openrouter_pinned_provider"] = "deepinfra"
  cfg["provider_profile"] = "openrouter"
  → OpenRouterAdapter(model_cfg)            ← new subclass of OpenAICompatAdapter
       │
       ▼
OpenRouterAdapter.chat_completion()
  inject headers: HTTP-Referer, X-Title
  inject body: usage.include=true
               provider.order=[deepinfra], provider.allow_fallbacks=false
                 (only when bracket form set)
               stream_options.include_usage=true (when streaming)
  delegate to OpenAICompatAdapter.chat_completion (HTTP, retry, key-pool unchanged)
       │
       ▼
profiles.py::normalize_usage_openrouter(usage_data) → UsageInfo
  prompt_tokens, completion_tokens, total_tokens (default fields)
  upstream_cost_usd ← usage_data["cost"]   ← new field on UsageInfo
       │
       ▼
serving/storage/database.py
  api_logs schema: ADD COLUMN upstream_cost_usd DECIMAL(12, 8)
  log_request() reads usage.upstream_cost_usd, passes to INSERT
```

### Components

1. **Bracket-kind parser** — `parse_openrouter_kind(kind: str) -> (base_kind: str, pinned_provider: str | None)` in `serving/servers/registry.py`. Called once at the top of `_make_adapter` to extract the pinned provider into `cfg["openrouter_pinned_provider"]` and reduce the dispatch kind to `"openrouter"`. Validates: `openrouter` → `("openrouter", None)`; `openrouter[deepinfra]` → `("openrouter", "deepinfra")`; rejects `openrouter[]`, nested brackets, whitespace inside. `_make_adapter` is the only existing site that compares `kind` against an adapter name (verified by grep), so no other code paths need changes. The `_make_provider_id` call in the surrounding registry loop runs **before** `_make_adapter` and intentionally receives the raw bracketed string so distinct pins get distinct endpoint_ids.
2. **`OpenRouterAdapter`** — `serving/adapters/openrouter.py`. Subclass of `OpenAICompatAdapter`. Overrides the request payload + header construction hook. Reuses HTTP, streaming, retry, and key-pool plumbing from the parent.
3. **`ProviderProfile.OPENROUTER`** — new enum value in `serving/adapters/profiles.py`. New `normalize_usage_openrouter(usage_data)` that delegates to `normalize_usage_default` for tokens, then sets `upstream_cost_usd = usage_data.get("cost")` (None when absent). Cache tokens read from `prompt_tokens_details.cached_tokens` in Azure-style (OpenRouter mirrors that shape).
4. **`UsageInfo.upstream_cost_usd: float | None = None`** — new optional field on `serving/adapters/base.py::UsageInfo`. Serialized in `to_dict()` only when not None, under key `upstream_cost_usd`.
5. **`api_logs.upstream_cost_usd` column** — `DECIMAL(12, 8) NULL`. Idempotent migration in `serving/storage/database.py` next to the existing `cost_usd` migration.
6. **`log_request()` plumbing** — accepts `upstream_cost_usd` (extracted from `UsageInfo`) and passes it to the `INSERT INTO api_logs` statement.
7. **`OPENROUTER_API_KEY`** — added to `.env.example` and documented.
8. **`config/models.yaml`** — one commented example showing both bare and bracket forms.
9. **`docs/openrouter.md`** — short doc covering the kind syntax, attribution headers, and the cost-logging contract. README already references this path.

## Request / Response Flow

### Outbound (chat completion, non-stream)

```
POST https://openrouter.ai/api/v1/chat/completions
Authorization: Bearer ${OPENROUTER_API_KEY}
HTTP-Referer: https://freeinference.org
X-Title: FreeInference
Content-Type: application/json

{
  "model": "meta-llama/llama-3.3-70b-instruct",
  "messages": [...],
  "temperature": 0.7,
  "max_tokens": 1024,
  "usage": { "include": true },
  "provider": {
    "order": ["deepinfra"],
    "allow_fallbacks": false
  }
}
```

- The `provider` block is injected only when the bracket form was used. Bare `openrouter` omits the block, letting OpenRouter pick.
- `usage.include = true` is always set so `cost` is returned.

### Streaming variant

Adds `"stream": true` and `"stream_options": {"include_usage": true}`. The final SSE chunk carries the usage block with `cost`. The existing streaming usage normalization path runs `normalize_usage_openrouter` on that block.

### Inbound

```json
{
  "id": "...",
  "choices": [...],
  "usage": {
    "prompt_tokens": 123,
    "completion_tokens": 456,
    "total_tokens": 579,
    "cost": 0.00342,
    "prompt_tokens_details": { "cached_tokens": 100 }
  },
  "provider": "DeepInfra"
}
```

`normalize_usage_openrouter` extracts tokens (default fields), `cache_read_tokens` from `prompt_tokens_details.cached_tokens` if present, and `upstream_cost_usd` from `usage.cost` (None when absent).

### Cost-logging contract

- `UsageInfo.upstream_cost_usd` flows to `log_request()`.
- `api_logs.cost_usd` (existing): unchanged; tokens × model-level pricing — what the user is billed.
- `api_logs.upstream_cost_usd` (new): OpenRouter-reported `cost` when set, else `NULL`. Other adapters always store `NULL`.

### `/v1/models`

No special handling. OpenRouter-routed models appear via their `models.yaml` registration like any other model.

## Error Handling

| HTTP | Meaning | Adapter behavior |
|------|---------|------------------|
| 400 | Bad request (invalid params) | Raise `BadRequestError`. Propagate to client. No retry, no fallback. |
| 401 | Invalid/missing key | Raise as adapter failure → router fallback. `logger.error()` with key-state context. |
| 402 | Insufficient credits | Raise as adapter failure → router fallback. `logger.error()` (account topup needed). |
| 403 | Flagged input (moderation) | Propagate to client as deterministic failure. No fallback. `logger.info()`. |
| 408 / 524 | Upstream timeout | Raise → router fallback. `logger.warning()`. |
| 429 | Rate limited | Raise → router fallback. `logger.warning()`. Honor `Retry-After` via existing aiohttp retry layer. |
| 502 | Selected upstream provider error | Raise → router fallback. `logger.warning()`. |
| 503 | No available upstream provider | Raise → router fallback. `logger.warning()`. |

All non-2xx responses use the existing `OpenAICompatAdapter` exception path; differentiation is in log level and message context only.

**Mid-stream failures** behave the same as the multi-key spec: the in-flight request fails to the client; only the next request gets re-routed.

**`cost` absent from response:** `upstream_cost_usd = None`, DB stores `NULL`, single `logger.debug()`. Does not raise. End-user billing unaffected.

**Attribution headers** are hardcoded. OpenRouter only enforces them for accounts configured to require them; default accounts accept missing headers, but we always send them anyway.

## Testing

### Unit (`test/test_openrouter_adapter.py`)

1. `test_parse_openrouter_kind` — `"openrouter"`, `"openrouter[deepinfra]"`, and rejection of `"openrouter[]"`, nested brackets, whitespace.
2. `test_payload_no_pinned_provider` — bare `openrouter`: payload has `usage.include=true`, no `provider` block.
3. `test_payload_pinned_provider` — `openrouter[deepinfra]`: payload has `provider.order=["deepinfra"]`, `provider.allow_fallbacks=false`.
4. `test_attribution_headers` — request includes `HTTP-Referer` and `X-Title`.
5. `test_streaming_payload` — `stream=true`: payload sets `stream_options.include_usage=true`.
6. `test_normalize_usage_openrouter_with_cost` — usage with `cost: 0.00342` → `UsageInfo.upstream_cost_usd == 0.00342`.
7. `test_normalize_usage_openrouter_without_cost` — no `cost` key → `UsageInfo.upstream_cost_usd is None`.
8. `test_make_adapter_dispatches_openrouter` — `_make_adapter("openrouter[fireworks]", cfg)` returns `OpenRouterAdapter` with `openrouter_pinned_provider="fireworks"`.
9. `test_endpoint_id_distinct_per_pin` — `_make_provider_id` for `openrouter[deepinfra]` vs `openrouter[fireworks]` (same base_url) produces distinct ids.

### Integration (`test/test_openrouter_integration.py`, `@pytest.mark.integration`, skipped when `OPENROUTER_API_KEY` unset)

10. `test_real_chat_completion_no_pin` — small live model; assert 200, non-empty completion, `cost > 0`.
11. `test_real_streaming_with_cost` — stream a small request; final chunk usage has `cost`.
12. `test_pinned_provider_routes_through` — `provider.order=[deepinfra]`; response `provider == "DeepInfra"`.

### DB / log (extend `test/test_request_logging.py`)

13. `test_upstream_cost_logged` — mocked OpenRouter response with `cost: 0.005` → `api_logs.upstream_cost_usd == 0.005`.
14. `test_upstream_cost_null_for_other_adapters` — Zhipu request → `api_logs.upstream_cost_usd IS NULL`.

### Migration

15. `test_migration_adds_upstream_cost_column` — fresh schema → column exists with correct type.

### Manual verification (against staging)

- Add OpenRouter as last fallback leg on `glm-4.7` with `weight: 0` (disabled). Confirm `_make_adapter` accepts the kind.
- Flip `weight: 1.0` on the OpenRouter leg, break the preceding legs (bad keys), confirm the request lands on OpenRouter and `api_logs.upstream_cost_usd` is set.
- Verify `/admin` cost reporting numbers are unchanged (they read `cost_usd`).

## Open Items Resolved Inline

- **Top-level `provider` field for OpenRouter model entries** in `models.yaml`: set to `"openrouter"` for clarity. Not load-bearing; only metadata.
- **`subscription_type` for OpenRouter routes**: default `"api"` (existing default), no change required.
- **`upstream_cost_usd` shape**: float, USD, nullable. Stored as `DECIMAL(12, 8)` to match `cost_usd`.

## Out of Scope

- Streaming chunk-level cost aggregation (only the final chunk has `cost`).
- Admin dashboard surfacing of `upstream_cost_usd` (separate follow-up).
- Removal of stale `docs/openrouter.md` reference in README — addressed by writing the doc as part of this work.
