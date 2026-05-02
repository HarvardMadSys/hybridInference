# Anthropic Messages API Compatibility — Design

**Date:** 2026-05-02
**Status:** Spec
**Author:** brainstorming session
**Related:** companion implementation plan (to be written via `superpowers:writing-plans`)

## 1. Goal

Accept Anthropic Messages API on the northbound surface for any registered model. Native Claude models pass upstream with zero translation. Non-Claude models translate Anthropic↔OpenAI in-process and reuse existing OpenAI adapter code paths. The existing OpenAI northbound (`/v1/chat/completions`) is untouched.

## 2. Why

- Users want to point Claude Code CLI, the `anthropic` Python SDK, and other Anthropic-format clients at freeinference and get them to work across all backends — not just Claude.
- Existing `/anthropic/v1/messages` endpoint is hard-wired to the `claude_sub` provider only. Other backends are unreachable from Anthropic-format clients today.
- Setup script `setupAnthropic.sh` already advertises `ANTHROPIC_BASE_URL=https://staging.freeinference.org` to users. The current route mount path (`/anthropic/v1/messages`) does not match what Anthropic SDKs hit (`/v1/messages`), so the documented setup is broken without a manual `/anthropic` suffix.
- Native Claude models (`claude-sonnet-4.6`, `claude-opus-4.6`, `claude-opus-4.7`) currently route via `kind: openai_compat` through a local `cli-proxy-api` shim that fronts Anthropic credentials. Two layers of OpenAI↔Anthropic translation occur on the southbound side. A direct adapter eliminates one hop and is required for true identity passthrough on the Anthropic northbound.

## 3. Decisions Locked During Brainstorming

| Decision | Choice |
|---|---|
| Northbound scope | Expand to all backends (Anthropic-format → any model). |
| Dependency stance | In-house translator (no LiteLLM, no new deps). |
| `claude_sub` identity passthrough | Out of scope for this spec. `claude_sub` adapter untouched here. Removal handled by a separate cleanup spec. |
| Model classification | Adapter declares `native_format` attribute (`"anthropic"` \| `"openai"`). |
| Endpoint scope | `/v1/messages` plus `/v1/models`. No `/v1/messages/count_tokens` — clients read tokens from response `usage`. |
| Path mounting | Mount Anthropic surface at root `/v1/messages`, `/v1/models` AND alias on `/anthropic/v1/messages`, `/anthropic/v1/models`. `/v1/models` disambiguates by `Anthropic-Version` header / `User-Agent`. |
| Anthropic-only fields on OpenAI backends | Best-effort map: drop `cache_control` and `thinking`, concat `system` array to string, map `tool_choice:{type:"any"}` → `tool_choice:"required"`. Log a warning. |
| Streaming on translated path | Stateful translator (Anthropic SSE event stream emitted from OpenAI delta chunks). |
| Model name aliasing | Strict registry IDs plus a small canonical alias table for common Anthropic display IDs. |

## 4. Architecture Overview

```
Client (Anthropic Messages format)
    │
    ▼
POST /v1/messages   (also /anthropic/v1/messages alias)
    │
    │  verify_api_key, rate_limit, concurrency, model resolve → adapter
    ▼
adapter.messages(body, ...)         ← new on BaseAdapter
    │
    ├── Anthropic-native adapter (AnthropicAdapter, native_format="anthropic")
    │       └── identity passthrough → api.anthropic.com/v1/messages
    │
    └── OpenAI-style adapters (default impl, native_format="openai")
            ├── translate Anthropic body → OpenAI body
            ├── call self.chat_completion() / stream_chat_completion()
            └── translate OpenAI response → Anthropic body / SSE
    │
    ▼
Client receives Anthropic-format JSON or SSE
```

OpenAI northbound (`POST /v1/chat/completions`) is unchanged. No translation added on that path.

## 5. Routing & Path Mounts

| Path | Method | Behavior |
|---|---|---|
| `POST /v1/messages` | new | Anthropic Messages API entry. Calls `adapter.messages()` / `stream_messages()`. |
| `POST /anthropic/v1/messages` | replaces existing handler | Alias of above; same handler function via two `@router.post` decorators. |
| `GET /v1/models` | modified | Detects format via header. Anthropic format if `Anthropic-Version` header present OR `User-Agent` matches `^(anthropic-|claude-cli|claude-sdk)`. Else OpenAI format (existing behavior). |
| `GET /anthropic/v1/models` | new | Always Anthropic format. |
| `POST /v1/chat/completions` | unchanged | OpenAI northbound. |

The existing router file `serving/servers/routers/anthropic_proxy.py` is replaced by `serving/servers/routers/anthropic_messages.py` and removed. Its claude_sub-specific logic is not carried forward (claude_sub passthrough was the entire purpose of the old file; that backend is out of scope for this spec).

Mount in `serving/servers/app.py` once, replacing the current `anthropic_proxy.router` include.

### 5.1 Anthropic-format `/v1/models` response

```json
{
  "data": [
    {
      "type": "model",
      "id": "claude-sonnet-4.6",
      "display_name": "Claude Sonnet 4.6",
      "created_at": "2026-01-01T00:00:00Z"
    }
  ],
  "has_more": false,
  "first_id": "claude-sonnet-4.6",
  "last_id": "qwen3.6-35b"
}
```

`first_id` / `last_id` reflect the actual returned slice. Pagination not implemented (single page returns all eligible models; same as current OpenAI behavior). `created_at` uses a fixed sentinel (Unix epoch in ISO-8601: `"1970-01-01T00:00:00Z"`) since the model registry does not track per-model creation timestamps. Clients tolerant of this — Anthropic SDK only reads `id` and `display_name`.

### 5.2 Auth

**Northbound (client → freeinference).** Reuse `verify_api_key` dependency. It already accepts both `Authorization: Bearer hyi-...` and `X-API-Key: hyi-...`. The `anthropic` Python SDK sends `x-api-key`; Claude Code CLI sends `Authorization: Bearer`. Both work without changes.

**Auth failures must return Anthropic-format errors** when the request hit one of the Anthropic surfaces. Approaches:

- (a) Add a FastAPI exception handler keyed on path prefix (`/v1/messages`, `/anthropic/`) that rewrites `HTTPException(401, ...)` to `{type:"error", error:{type:"authentication_error", message}}`.
- (b) Replace the dependency-raised exception inside the handler — call `verify_api_key` directly and catch.

Decision: **(a)** at app level. Single exception handler, applies to both `/v1/messages` and `/anthropic/v1/messages`.

**Southbound (freeinference → upstream Anthropic).** The new `AnthropicAdapter` sends `x-api-key: ${ANTHROPIC_API_KEY}` (configured via env var, **separate from any user API key**) plus `anthropic-version: 2023-06-01`.

### 5.3 setupAnthropic.sh update

Drop the implicit `/anthropic` suffix expectation: with `/v1/messages` mounted at root, `ANTHROPIC_BASE_URL=https://staging.freeinference.org` works as written.

## 6. BaseAdapter Interface

In `serving/adapters/base.py`:

```python
class BaseAdapter:
    native_format: str = "openai"  # "openai" | "anthropic"

    async def messages(
        self,
        body: dict,
        *,
        request_id: str,
    ) -> dict:
        """Anthropic Messages API non-streaming. Returns Anthropic-format dict.
        Default impl translates Anthropic→OpenAI, calls self.chat_completion(),
        translates OpenAI→Anthropic.
        """
        from serving.adapters.anthropic_translator import (
            anthropic_request_to_openai,
            openai_response_to_anthropic,
        )
        oai_messages, oai_params = anthropic_request_to_openai(body)
        oai_resp = await self.chat_completion(oai_messages, **oai_params)
        return openai_response_to_anthropic(oai_resp, model=body["model"])

    async def stream_messages(
        self,
        body: dict,
        *,
        request_id: str,
    ) -> AsyncIterator[bytes]:
        """Anthropic Messages API streaming. Yields raw Anthropic SSE bytes."""
        from serving.adapters.anthropic_translator import (
            anthropic_request_to_openai,
            OpenAIToAnthropicStreamTranslator,
        )
        oai_messages, oai_params = anthropic_request_to_openai(body)
        oai_params["stream"] = True
        translator = OpenAIToAnthropicStreamTranslator(model=body["model"])
        async for openai_chunk in self.stream_chat_completion(oai_messages, **oai_params):
            for ant_event in translator.feed(openai_chunk):
                yield ant_event
        for ant_event in translator.finalize():
            yield ant_event
```

`messages()` / `stream_messages()` are the only new abstract surfaces. Adapters that should bypass translation override these.

## 7. New AnthropicAdapter

New file `serving/adapters/anthropic.py`. `kind: anthropic` registered in the adapter registry.

### Class shape

```python
class AnthropicAdapter(BaseAdapter):
    native_format = "anthropic"

    DEFAULT_BASE = "https://api.anthropic.com"
    ANTHROPIC_VERSION = "2023-06-01"

    # ---- Anthropic-format northbound (identity passthrough) ----
    async def messages(self, body, *, request_id) -> dict: ...
    async def stream_messages(self, body, *, request_id) -> AsyncIterator[bytes]: ...

    # ---- OpenAI-format northbound (translate to Anthropic upstream) ----
    async def chat_completion(self, messages, **params) -> dict: ...
    async def stream_chat_completion(self, messages, **params) -> AsyncIterator[str]: ...
```

### Identity passthrough (`messages` / `stream_messages`)

- POST `f"{base}/v1/messages"` (no `?beta=true` — that flag is OAuth-subscription specific and belongs to claude_sub).
- Headers: `x-api-key: ${api_key}`, `anthropic-version: 2023-06-01`, `content-type: application/json`.
- Body forwarded as-is. Substitute `body["model"]` with `provider_model_id` from the model config (e.g., `claude-opus-4.7` → `claude-opus-4-7`).
- Streaming: stream raw bytes back to client. Best-effort SSE usage extraction so DB logging records token counts. The `_extract_usage_from_sse` helper currently lives inside `serving/servers/routers/anthropic_proxy.py`; before deleting that file, **move the helper to `serving/adapters/anthropic_translator.py`** (or a dedicated `serving/utils/anthropic_sse.py`) so both `AnthropicAdapter.stream_messages` and the new router can reuse it.
- Errors: forward upstream Anthropic error body verbatim (already in Anthropic format). Map connection errors to `_anthropic_error(502, ...)`.

### OpenAI-format southbound (`chat_completion` / `stream_chat_completion`)

- Build Anthropic payload using existing forward translation in `serving/adapters/claude_format.py` (`convert_messages`, `convert_tools`, `convert_tool_choice`, `extract_system`).
- POST upstream with same headers as identity path.
- Parse non-streaming response with `parse_response_content` + `parse_usage` + `map_stop_reason` → return OpenAI-format dict.
- Stream: feed each upstream Anthropic SSE event into `handle_stream_event` (existing helper) → emit OpenAI-format delta chunks. End with `make_final_usage_chunk` + `done_sentinel`.

This mirrors the existing Vertex `ClaudeAdapter` (`serving/adapters/claude.py`) but with Anthropic-direct auth/path instead of Vertex.

### Config schema in models.yaml

```yaml
- id: claude-opus-4.7
  name: Claude Opus 4.7
  provider: anthropic
  provider_model_id: claude-opus-4-7
  context_length: 200000
  max_output_length: 128000
  supports_tools: true
  supports_structured_output: true
  supported_params: [temperature, top_p, max_tokens, stop, stream, tools, tool_choice]
  input_modalities: [text, image]
  output_modalities: [text]
  pricing: { ... unchanged ... }
  route:
    - kind: anthropic
      weight: 1.0
      base_url: https://api.anthropic.com
      api_key: ${ANTHROPIC_API_KEY}
      provider_model_id: claude-opus-4-7
```

`claude-sonnet-4.6`, `claude-opus-4.6`, `claude-opus-4.7` are retargeted from `kind: openai_compat` (cli-proxy) → `kind: anthropic`. cli-proxy-api remains available for any other model that still uses it; not touched here.

`ANTHROPIC_API_KEY` env var must be set on staging and prod deploys.

## 8. Reverse Translator Module

New file `serving/adapters/anthropic_translator.py`. Pure functions plus one stateful streaming class. No I/O, no logging. Estimated ~350 LOC.

### Public API

```python
def anthropic_request_to_openai(body: dict) -> tuple[list[dict], dict]:
    """Returns (openai_messages, openai_params).
    openai_params keys: max_tokens, temperature, top_p, stop, stream, tools, tool_choice, user."""

def openai_response_to_anthropic(resp: dict, *, model: str) -> dict:
    """Convert OpenAI ChatCompletion JSON → Anthropic Messages JSON."""

class OpenAIToAnthropicStreamTranslator:
    def __init__(self, *, model: str): ...
    def feed(self, openai_sse_chunk: bytes) -> Iterator[bytes]:
        """Returns Anthropic SSE bytes for each fed OpenAI chunk."""
    def finalize(self) -> Iterator[bytes]:
        """Emit message_delta + message_stop after stream end."""

    @property
    def usage(self) -> dict: ...  # populated after finalize()
```

### Request translation (Anthropic → OpenAI)

| Anthropic field | OpenAI field | Notes |
|---|---|---|
| `messages[].role` (user, assistant) | same | |
| `messages[].content` string | `content: string` | |
| `messages[].content` array of blocks | `content: array` of OpenAI vision parts | text→`{type:"text", text}`; image→`{type:"image_url", image_url:{url}}` (data URL preserved); tool_use→synthesize parent message `tool_calls`; tool_result→split into a separate `role: "tool"` message with `tool_call_id` |
| `system` string | prepend `{role:"system", content}` | |
| `system` array of blocks | concat all `text` blocks separator `\n\n` → prepend system message | best-effort |
| `max_tokens` | `max_tokens` | |
| `temperature`, `top_p`, `stop_sequences` | `temperature`, `top_p`, `stop` | rename `stop_sequences` → `stop` |
| `tools` | `tools: [{type:"function", function:{name, description, parameters: input_schema}}]` | |
| `tool_choice: {type:"auto"}` | `tool_choice: "auto"` | |
| `tool_choice: {type:"any"}` | `tool_choice: "required"` | |
| `tool_choice: {type:"tool", name}` | `tool_choice: {type:"function", function:{name}}` | |
| `cache_control` blocks | drop silently, log warning once | |
| `thinking` field | drop silently, log warning once | |
| `metadata.user_id` | `user` field on OpenAI request | |
| `stream` | `stream` | |

### Response translation (OpenAI → Anthropic)

| OpenAI field | Anthropic field | Notes |
|---|---|---|
| `id` | `id` | prefix `msg_` if not already |
| `model` | `model` | use the original Anthropic-side model id (caller-supplied), not the upstream-mapped id |
| `choices[0].message.content` | `content: [{type:"text", text}]` | omitted if empty AND tool_calls present |
| `choices[0].message.tool_calls[]` | `content: [{type:"tool_use", id, name, input}]` | parse `function.arguments` JSON string → `input` dict |
| `choices[0].finish_reason` | `stop_reason` | `stop`→`end_turn`, `length`→`max_tokens`, `tool_calls`→`tool_use`, `content_filter`→`refusal`, anything else→`end_turn` |
| `usage.prompt_tokens` | `usage.input_tokens` | |
| `usage.completion_tokens` | `usage.output_tokens` | |
| `usage.prompt_tokens_details.cached_tokens` | `usage.cache_read_input_tokens` | if present |

Top-level fields always set: `type: "message"`, `role: "assistant"`, `stop_sequence: null`.

### Streaming translator state machine

OpenAI emits per-chunk deltas: `choices[0].delta.{role|content|tool_calls}`. Anthropic emits a richer event sequence:

```
event: message_start         { message: {id, role, model, content:[], usage:{input_tokens,...}} }
event: content_block_start   { index: 0, content_block: {type:"text", text:""} }
event: content_block_delta   { index: 0, delta: {type:"text_delta", text: "Hi"} }
event: content_block_stop    { index: 0 }
event: content_block_start   { index: 1, content_block: {type:"tool_use", id, name, input:{}} }
event: content_block_delta   { index: 1, delta: {type:"input_json_delta", partial_json: "{\""} }
event: content_block_stop    { index: 1 }
event: message_delta         { delta: {stop_reason}, usage: {output_tokens} }
event: message_stop
```

Translator state:
- `started: bool` — emitted `message_start` yet
- `current_text_index: int | None` — index of currently-open text block (or None)
- `tool_blocks: dict[tool_call_id → {index: int, name_emitted: bool}]`
- `next_index: int`
- `usage: dict` — accumulated, populated from final OpenAI chunk's `usage`
- `finish_reason: str | None`

`feed(chunk)`:
1. Decode and parse `data: ...` lines.
2. On first chunk: emit `message_start` with input usage if present, else zeros.
3. For each `delta.content` text fragment: open text block at `next_index` if no current text block; emit `content_block_delta` with `text_delta`.
4. For each `delta.tool_calls[i]`: if new id, close current text block (`content_block_stop`), open `content_block_start` with `type:tool_use`; for argument fragments emit `content_block_delta` with `input_json_delta`.
5. Capture `finish_reason` and `usage` if present.

`finalize()`:
1. Close any open block.
2. Emit `message_delta` with `stop_reason` mapped from buffered `finish_reason` (default `end_turn`) and `usage.output_tokens`.
3. Emit `message_stop`.

### Edge cases

- Empty assistant content with tool calls only — skip text block, jump to tool_use.
- Tool call `arguments` arrives as fragmented JSON — pass straight through as `input_json_delta`; client accumulates.
- OpenAI omits `usage` mid-stream — emit zeros in `message_start`; correct in `message_delta` if final chunk carries usage.
- OpenAI `finish_reason: null` until final chunk — buffer, emit on `finalize`.
- Unicode in JSON deltas — preserve raw bytes, no re-encode.
- Backend emits ZERO chunks (immediate completion) — `finalize` emits `message_start`+`message_delta`+`message_stop` with empty content.

### Reuse from existing claude_format.py

Forward direction (OpenAI → Anthropic) already implemented in `serving/adapters/claude_format.py` and used by Vertex `ClaudeAdapter`:

- Reused for `AnthropicAdapter.chat_completion`: `convert_messages`, `convert_tools`, `convert_content_blocks`, `extract_system`, `convert_tool_choice`.
- Reused for `AnthropicAdapter.stream_chat_completion`: `parse_response_content`, `parse_usage`, `map_stop_reason`, `handle_stream_event`, `ToolCallAccumulator`, `build_final_usage`.

Reverse direction (this module) is net new.

## 9. Router Handler

New file `serving/servers/routers/anthropic_messages.py`.

### Endpoint

```python
@router.post("/v1/messages", response_model=None)
@router.post("/anthropic/v1/messages", response_model=None)
async def anthropic_messages(
    request: Request,
    user_ctx: dict = Depends(verify_api_key),
    router_exec=Depends(get_router),
    rate_limiter=Depends(get_rate_limiter),
    db_logger=Depends(get_db_logger),
    _conc=Depends(enforce_user_concurrency),
): ...
```

Both decorators on the same function — FastAPI registers both paths.

### Lifecycle

1. **Parse JSON.** 400 on failure (Anthropic format).
2. **Resolve model.**
   - Apply `ANTHROPIC_MODEL_ALIASES`.
   - Look up route in `router_exec.routes`. 404 if not found or role-gated.
   - Pick first eligible adapter for the route (matching current behavior).
3. **Rate limit + concurrency.** Reuse existing dependencies. The rate limiter's `acquire_tokens` API expects OpenAI-style messages for its tiktoken pre-estimate. For Anthropic-format input, **flatten Anthropic body to a list of `{role, content}` dicts** (concat content blocks to text, ignore images for token estimate) and pass that — accuracy of pre-estimate is best-effort anyway. The actual usage recorded post-response is exact. 429 → Anthropic-format error.
4. **Field sanitization for OpenAI backends.** When `adapter.native_format == "openai"`:
   - Strip `cache_control` from blocks.
   - Drop `thinking`.
   - Translator handles `system` array→string and `tool_choice` mapping.
   - Emit a single warning log per request listing dropped fields.
5. **Dispatch.**
   ```python
   if body.get("stream"):
       gen = adapter.stream_messages(body, request_id=request_id)
       return StreamingResponse(gen, media_type="text/event-stream", headers=SSE_HEADERS)
   resp = await adapter.messages(body, request_id=request_id)
   return JSONResponse(content=resp)
   ```
6. **Errors.** Catch upstream errors at adapter boundary. Wrap into Anthropic format using `_anthropic_error()`.
7. **DB logging + metrics.** Same `_schedule_db_log` pattern as the current router. Surface tagged `surface: "anthropic_messages"`. Provider tag from adapter config. Usage extracted post-stream (translator exposes `.usage` after `finalize`; native passthrough reuses existing SSE usage scraper).

### Alias map

New file `serving/adapters/anthropic_aliases.py`:

```python
ANTHROPIC_MODEL_ALIASES = {
    "claude-3-5-sonnet-latest": "claude-sonnet-4.6",
    "claude-3-5-sonnet-20241022": "claude-sonnet-4.6",
    "claude-3-5-sonnet-20240620": "claude-sonnet-4.6",
    "claude-3-opus-latest": "claude-opus-4.7",
    "claude-3-opus-20240229": "claude-opus-4.6",
}
```

Strict for unknown IDs not in map and not in registry: 404. No fuzzy match.

### `/v1/models` Anthropic format detection

In existing `serving/servers/routers/models.py`:

```python
def _is_anthropic_client(request: Request) -> bool:
    if request.headers.get("anthropic-version"):
        return True
    ua = (request.headers.get("user-agent") or "").lower()
    return ua.startswith(("anthropic-", "claude-cli", "claude-sdk"))

@router.get("/v1/models")
async def list_models(request: Request, ...):
    if _is_anthropic_client(request):
        return JSONResponse(_format_anthropic_model_list(routes))
    return JSONResponse(_format_openai_model_list(routes))  # existing

@router.get("/anthropic/v1/models")
async def list_models_anthropic(...):
    return JSONResponse(_format_anthropic_model_list(routes))
```

### Error format

Anthropic-shaped errors via reused `_anthropic_error()` helper:

```json
{ "type": "error",
  "error": { "type": "invalid_request_error|authentication_error|...", "message": "..." } }
```

Status→type table same as the existing helper.

## 10. Best-Effort Field Drops

Logged once per request:

| Field | Anthropic-native path | OpenAI-translate path |
|---|---|---|
| `cache_control` blocks | preserved (passthrough) | stripped + warning |
| `thinking` | preserved (passthrough) | dropped + warning |
| `system` (array of blocks) | preserved | concatenated to string |
| `tool_choice: {type: "any"}` | preserved | mapped to `"required"` |
| `metadata.user_id` | preserved | mapped to OpenAI `user` field |
| `metadata` other keys | preserved | dropped silently |
| `stop_sequences` | preserved | renamed `stop` |

## 11. Testing

### Unit tests — translator (`test/unit/adapters/test_anthropic_translator.py`)

- Request translation: text-only, multi-turn, system string, system array, images (data URL + http URL), tools, tool_use → tool_result round-trip, cache_control drop, thinking drop, tool_choice variants, stop_sequences rename, metadata.user_id → user.
- Response translation: text only, tool_calls only, mixed text+tool_use, finish_reason mapping table, cached_tokens.
- Streaming: text-only, tool-call only, mixed, empty stream, mid-stream error, fragmented JSON deltas, multi-tool-call stream, usage extraction.
- Golden vectors: 5–10 captured real OpenAI responses (e.g., from zhipu/glm) → assert byte-equivalent Anthropic SSE output.

### Unit tests — adapter (`test/unit/adapters/test_anthropic_adapter.py`)

- `messages()` identity passthrough hits `api.anthropic.com` mocked via `aioresponses`.
- `stream_messages()` streaming, including SSE usage extraction.
- `chat_completion()` and `stream_chat_completion()` for OpenAI-format southbound (same paths as Vertex `ClaudeAdapter` covers).
- 401 retry behavior (if applicable to direct API key path — likely not, since not OAuth).
- Error forwarding preserves upstream Anthropic error body verbatim.

### Router tests (`test/servers/test_anthropic_messages_router.py`)

End-to-end with FastAPI TestClient:
- Anthropic-north → native model (mocked Anthropic upstream) → identity bytes returned.
- Anthropic-north → OpenAI model (mocked zhipu upstream) → translated.
- Streaming + non-streaming.
- Alias resolution: `claude-3-5-sonnet-latest` → `claude-sonnet-4.6`.
- 404 unknown model.
- 401 missing key.
- 429 rate limit.
- cache_control + thinking dropped on OpenAI backend; warning logged.
- Both `/v1/messages` and `/anthropic/v1/messages` reach same handler.
- `Anthropic-Version` header on `/v1/models` returns Anthropic format; absent → OpenAI format.

### Integration tests against staging

- `anthropic` Python SDK pointed at `https://staging.freeinference.org`:
  - Non-streaming completion on `claude-opus-4.7` (native).
  - Streaming completion on `glm-4.7` (translated path).
  - Tool use round-trip.
- Claude Code CLI smoke:
  ```
  ANTHROPIC_BASE_URL=https://staging.freeinference.org \
  ANTHROPIC_API_KEY=$HYI_KEY \
    claude --model claude-opus-4.7 'hello'
  ```
- Regression: existing OpenAI-format clients on `/v1/chat/completions` unaffected.

## 12. Rollout

1. Land translator module + unit tests.
2. Land `AnthropicAdapter` + unit tests.
3. Land router + tests.
4. Update `config/models.yaml` — retarget `claude-sonnet-4.6`, `claude-opus-4.6`, `claude-opus-4.7` to `kind: anthropic`. Set `ANTHROPIC_API_KEY` on staging.
5. Update `setupAnthropic.sh` — `BASE_URL` without `/anthropic` suffix; document both supported paths.
6. Update developer docs (`docs/source/developer/`) — Anthropic compat section.
7. Deploy staging, run integration tests.
8. Deploy prod.

## 13. Out of Scope (deferred / separate specs)

- `/v1/messages/count_tokens` endpoint — clients read tokens from response `usage`.
- Anthropic Batches API.
- Anthropic Files API.
- Citation blocks.
- claude_sub deprecation/removal — separate spec.
- Adapters other than the new `AnthropicAdapter` overriding `messages()` / `stream_messages()` — default base impl covers all currently-active backends.
- Per-model cache_control honoring on the Anthropic-native path beyond what passthrough naturally preserves (no enforcement layer).
- Vertex `ClaudeAdapter` retargeting — currently unused in `models.yaml`; remains an OpenAI-format adapter.

## 14. Success Criteria

- Claude Code CLI works on staging against `claude-opus-4.7` (native) and `glm-4.7` (translated) using `ANTHROPIC_BASE_URL=https://staging.freeinference.org`.
- `anthropic` Python SDK 1.x can complete + stream against any registered model.
- `/v1/chat/completions` regression-free for all existing models (existing tests still green).
- Unit + integration tests green; `make lint` and `make typecheck` clean.
- DB logs record `surface: "anthropic_messages"` rows with correct usage, pricing, latency for both native and translated dispatch.
