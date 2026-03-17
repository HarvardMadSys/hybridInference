# Anthropic Messages API Surface (`/anthropic/v1/messages`)

## Problem

hybridInference only exposes OpenAI-compatible endpoints (`/v1/chat/completions`). Claude Code CLI speaks the Anthropic Messages API (`POST /v1/messages`). Users cannot currently point Claude Code at hybridInference as its backend.

## Goal

Add an Anthropic-compatible northbound surface at `/anthropic/v1/messages` — Claude Code sends Messages API requests to hybridInference, which resolves the model, injects subscription OAuth credentials, and forwards to `api.anthropic.com`.

This is an **identity surface translator**: both the client-facing protocol and the upstream protocol are Anthropic Messages API, so the format translation is trivially the identity function. The architecture follows the northbound surface pattern defined in [subscription-adapter-architecture.md](subscription-adapter-architecture.md#41-northbound-api-surfaces), with the simplification that no IR conversion is needed in this degenerate case.

## Architecture

```
                  /anthropic/v1/messages                     /v1/messages
┌────────────┐   x-api-key: hyi-xxx    ┌──────────────────┐  Bearer <oauth>   ┌───────────────┐
│ Claude Code │ ──────────────────────▶ │  hybridInference │ ───────────────▶  │  Anthropic    │
│    CLI      │                         │  (surface route) │                   │  Messages API │
│             │ ◀────────────────────── │                  │ ◀───────────────  │               │
└────────────┘    SSE / JSON            └──────────────────┘   SSE / JSON      └───────────────┘
                 (pass-through)              │        │        (pass-through)
                                             │        │
                                     ┌───────▼──┐  ┌──▼──────────┐
                                     │ Account  │  │ Model       │
                                     │ Pool     │  │ Registry    │
                                     │ (shared  │  │ (models.yaml│
                                     │ singleton│  │  lookup)    │
                                     │  )       │  │             │
                                     └──────────┘  └─────────────┘
```

### What the surface does

1. **Client auth** — validate `x-api-key: hyi-xxx` via `verify_api_key` dependency (`Depends(verify_api_key)`)
2. **Rate limiting** — apply `PersistentRateLimiter` via `Depends(get_rate_limiter)`, same as `/v1/chat/completions`
3. **Model resolution** — map public model ID (e.g. `claude-sonnet-4.6`) to upstream `provider_model_id` (e.g. `claude-sonnet-4-6`) via model registry; reject unknown models; enforce `provider == "claude_sub"` eligibility
4. **Credential injection** — acquire subscription account from shared pool, get valid OAuth token
5. **Forward request** — replace auth header + model field, forward body to Anthropic
6. **Forward response** — pass through response bytes unmodified (SSE streaming or JSON)
7. **Usage extraction** — parse `input_tokens`/`output_tokens` from response for cost tracking
8. **DB logging** — log usage, latency, model, account_id for analytics

### What the surface does NOT do

- **No format translation** — both sides speak Messages API (identity translator)
- **No schema validation** — forward whatever Claude Code sends; Anthropic validates
- **No response mutation** — raw bytes forwarded, zero added latency from parsing

## Design Decisions

### 1. Shared account pool via module-level singleton

The proxy and `ClaudeSubscriptionAdapter` share a single `AccountPool` + `ClaudeCredentialProvider` instance.

**Why**: `AccountPool` health state (cooldown, consecutive failures, unhealthy marks) is purely in-memory and does not persist to disk. If the proxy and adapter each maintained independent pools, a 429 cooldown on one side would be invisible to the other — both would keep hitting the same exhausted account. A shared singleton ensures cooldown/health state is consistent across all code paths.

**Implementation challenge**: `ClaudeSubscriptionAdapter` is constructed by `registry.py:_make_adapter()` which only receives a `ModelConfig` — it has no access to `AppServices` or `Request`. `AppServices` itself is assembled at the end of `bootstrap.initialize()` (line 336), after adapters are already constructed and registered. Injecting `AppServices` into adapters would require threading it through `_make_adapter()` → `register_from_models_yaml()` → `_init_router_and_models()`, touching the entire bootstrap chain.

**Chosen approach**: A **module-level lazy singleton** in a new file `serving/adapters/claude_pool.py`:

```python
# serving/adapters/claude_pool.py
_pool: AccountPool | None = None
_provider: ClaudeCredentialProvider | None = None
_lock = threading.Lock()

def get_shared_pool() -> tuple[ClaudeCredentialProvider, AccountPool]:
    """Return the process-wide shared pool, initialising on first call."""
    ...
```

Both `ClaudeSubscriptionAdapter._ensure_init()` and the proxy router call `get_shared_pool()`. This avoids touching the bootstrap/registry chain while guaranteeing a single shared instance. The module-level singleton is acceptable here because:
- The pool is stateless w.r.t. configuration after init (reads settings once)
- Python module imports are inherently process-wide singletons
- Both consumers already do lazy init on first request, so startup ordering is not a concern

### 2. Model resolution via model registry with provider eligibility check

The proxy resolves client-sent model IDs through the existing model registry (`models.yaml`). Public IDs like `claude-sonnet-4.6` are mapped to upstream `provider_model_id` values like `claude-sonnet-4-6`.

**Why**: Without model resolution, there are two failure modes:
- Client sends our public model ID → upstream rejects it (unknown model)
- Client sends upstream's native model ID → bypasses our model registry, routing policy, and pricing

A lightweight lookup from the model registry resolves this cleanly. Models not registered in `models.yaml` are rejected with 400, maintaining consistency with how `/v1/chat/completions` operates.

**Provider eligibility constraint**: This surface only accepts models whose `provider` is `claude_sub` (direct Anthropic subscription). If a `claude-*` public model is registered with a different provider (e.g. `vertex`, `claude` with direct API key), it must not be forwarded through subscription OAuth. The proxy checks `model_entry.provider == "claude_sub"` and returns 400 for ineligible providers. This prevents misrouting if future `models.yaml` entries map Claude model names to non-subscription backends.

Pricing is also derived from the registry entry rather than hardcoded prefix matching.

### 3. Rate limiter integration (same as `/v1/chat/completions`)

The proxy applies `PersistentRateLimiter` via `Depends(get_rate_limiter)`, the same dependency used by `/v1/chat/completions` (`completions.py:77`).

**Why**: Without rate limiting, this surface would be strictly easier to abuse than the OpenAI surface — same auth, same accounts, but no throughput/concurrency/TPM controls. `verify_api_key` only checks API key validity and daily cost quota; it does not enforce per-model token-per-minute or concurrent-request limits.

**Implementation**: The handler signature includes `rate_limiter=Depends(get_rate_limiter)` and calls `rate_limiter.check()` before forwarding. Rate limit configuration for `claude_sub` models in `bootstrap._configure_rate_limiter()` applies uniformly to both surfaces.

### 4. Override client headers (vs. pass-through)

The proxy overrides `anthropic-version`, `anthropic-beta`, and auth headers with our subscription values. Client-sent values are ignored.

**Why**: Our subscription OAuth tokens require specific beta flags (e.g., `oauth-2025-04-20`). If Claude Code sends different beta flags, the request would fail. The subscription headers are known to work; client headers are unpredictable.

### 5. Raw byte forwarding for streaming (vs. parse-and-reserialize)

Streaming responses are forwarded using `resp.content.iter_any()` — raw bytes from the upstream connection go directly to the client without JSON parsing or reserialization.

**Why**: Minimal latency. The only parsing happens on a parallel buffer to extract usage numbers from `message_start` and `message_delta` events. This parsing is best-effort and does not affect the forwarded stream.

### 6. Identity surface translator (vs. full RouteExecutor integration)

The proxy does not go through `RouteExecutor`. It manages its own upstream communication with simple retry logic.

**Why**: `RouteExecutor` assumes OpenAI Chat Completions format for request/response. Adding Messages API support would require significant refactoring for no practical benefit in this case — both sides already speak the same protocol. This is explicitly an **architectural exception**: the identity case where input format = output format makes a full IR round-trip unnecessary.

**What we DO reuse from the platform**: model registry (for model resolution + pricing), `AccountPool` (shared singleton for health state), `verify_api_key` (client auth), DB logger (usage tracking). The proxy is not a rogue bypass — it participates in the platform's model governance and account health management.

**Future evolution**: If hybridInference adds non-Anthropic backends that serve the Messages API (e.g. a local model with an Anthropic-compatible wrapper), this surface would need to grow into a proper surface translator with IR conversion. The current design leaves room for that extension.

## Streaming Data Flow

```
Upstream SSE events:
  event: message_start    ←── extract input_tokens, cache tokens
  event: content_block_start
  event: content_block_delta  (text chunks)
  event: ping
  event: content_block_stop
  event: message_delta    ←── extract output_tokens
  event: message_stop

All events forwarded as raw bytes to client.
Usage numbers extracted via lightweight parallel buffer parsing.
```

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Client sends invalid API key | 401 from `verify_api_key` dependency (never reaches handler) |
| Rate limit exceeded | 429 from `PersistentRateLimiter` (before upstream call) |
| Unknown model ID | 400 with "model not found in registry" |
| Model provider is not `claude_sub` | 400 with "model not eligible for this surface" |
| No healthy accounts | 503 with message |
| Upstream 401 | Force-refresh token, retry once; if still fails, forward error |
| Upstream 429 | Report account cooldown (shared pool), forward error with `retry-after` |
| Upstream 4xx | Forward error as-is (client error) |
| Upstream 5xx | Forward error as-is, report upstream failure |
| Connection failure | 502 Bad Gateway |
| Mid-stream disconnect | Emit Anthropic-format error event |

All error responses are in Anthropic Messages API error format so Claude Code handles them correctly.

## Configuration

### Server side

No new settings needed. Reuses existing `claude_sub_*` settings:
- `claude_sub_accounts_file` — path to accounts JSON
- `claude_sub_token_refresh_margin` — seconds before expiry to refresh
- `claude_sub_account_cooldown` — seconds to cool down failed accounts
- `claude_sub_failure_threshold` — consecutive failures before marking unhealthy

### Client side (Claude Code CLI)

```bash
# In ~/.bashrc or ~/.zshrc:
export ANTHROPIC_BASE_URL=https://freeinference.org/anthropic
export ANTHROPIC_AUTH_TOKEN=hyi-your-api-key-here
```

Or in `~/.claude/settings.json`:
```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://freeinference.org/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "hyi-your-api-key-here"
  }
}
```

Claude Code automatically appends `/v1/messages` to the base URL.

## Files

| File | Change |
|------|--------|
| `serving/adapters/claude_pool.py` | **New** — ~30 lines, module-level singleton for shared `AccountPool` + `ClaudeCredentialProvider` |
| `serving/adapters/claude_sub.py` | ~5 lines changed (`_ensure_init` calls `claude_pool.get_shared_pool()` instead of creating its own) |
| `serving/servers/routers/anthropic_proxy.py` | **New** — surface route (~300 lines, incl. rate limiter + provider eligibility) |
| `serving/servers/app.py` | +2 lines (import + register) |
| `test/unit/servers/test_anthropic_proxy.py` | **New** — unit tests |

**Not changed**: `deps.py` (`AppServices` dataclass unchanged), `registry.py` (adapter construction unchanged), `bootstrap.py` (init sequence unchanged).
