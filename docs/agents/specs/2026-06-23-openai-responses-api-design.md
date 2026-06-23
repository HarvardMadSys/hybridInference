# OpenAI Responses API Compatibility — Design

**Date:** 2026-06-23
**Status:** Implemented
**Related:** mirrors the Anthropic Messages compatibility approach
(`2026-05-02-anthropic-compatibility-design.md`)

## 1. Goal

Accept the OpenAI **Responses API** (`POST /v1/responses`) on the northbound
surface for any registered model, including streaming, function calling, and
stateful conversations (`store` / `previous_response_id` / `GET` / `DELETE`).
The existing `/v1/chat/completions` surface is untouched.

## 2. Why

The Responses API is OpenAI's newer request/response envelope (a re-shaping of
Chat Completions). Clients and SDKs that target `/v1/responses` — the OpenAI
Python/JS SDKs' `client.responses.create(...)`, Agents SDK, etc. — could not
talk to freeinference before. It is not a new model call; it is a different
envelope over the same call.

## 3. Key decision — delegate, don't duplicate

Rather than re-implement routing, fallback, cost accounting, DB logging, RouteWise
observations, role gates, modality checks and concurrency, the router **translates
a Responses request into a Chat Completions request and calls the existing
`chat_completions` handler**, then translates the result back. This is the same
delegation pattern `compat.py` uses (overwrite `request._json` / `request._body`,
then call `chat_completions`).

```
client (Responses format)
  → POST /v1/responses
    → translate input/instructions/params → chat body
    → [previous_response_id] prepend stored conversation
    → chat_completions(request, ...)        # all existing machinery
    → translate chat completion → Responses object   (non-stream)
       or re-frame chat SSE → Responses SSE events    (stream)
    → [store] persist response + cumulative messages
```

`runtime_settings=None` is passed to the delegate so non-streaming always returns
a dict (the force-streaming keepalive path returns a `StreamingResponse`).

## 4. Components

| File | Role |
|---|---|
| `serving/responses_translator.py` | Pure translation. `responses_input_to_messages`, `responses_request_to_chat_params`, `chat_response_to_responses`, `assistant_message_from_chat`, and the `ResponsesStreamTranslator` state machine. |
| `serving/servers/routers/responses.py` | Router: `POST /v1/responses`, `GET /v1/responses/{id}`, `DELETE /v1/responses/{id}`. Auth/concurrency, dispatch, SSE re-framing, statefulness. |
| `serving/storage/responses_store.py` | `ResponseStore` — dedicated Postgres table `openai_responses`, kept separate from the `OperationalStore` ABC. |
| `servers/deps.py`, `servers/bootstrap.py`, `servers/app.py` | Wiring: `responses_store` on `AppServices`, `get_response_store` dependency, table init, router include. |

## 5. Translation summary

### Request → chat

| Responses | Chat |
|---|---|
| `input` (string) | single `user` message |
| `input` (items) | `message` → chat message (`input_text`/`output_text` → `text`, `input_image` → `image_url`); `function_call` → assistant `tool_calls`; `function_call_output` → `role:"tool"` |
| `instructions` | leading `system` message (per-turn; not carried across `previous_response_id`, matching OpenAI) |
| `max_output_tokens` | `max_tokens` |
| `reasoning.effort` | `reasoning_effort` |
| `text.format` | `response_format` (`json_schema` flattened → nested) |
| `tools` (flat function) | `tools` (nested function); hosted tools (`web_search`, …) dropped + warned |
| `tool_choice` | `tool_choice` (`{type:function,name}` → `{type:function,function:{name}}`) |

### Chat → Responses

`choices[0].message.content` → a `message` item with an `output_text` part;
`tool_calls` → `function_call` items. `finish_reason`: `length`/`content_filter`
→ `status:"incomplete"` with `incomplete_details`; otherwise `completed`. Usage is
remapped to `input_tokens`/`output_tokens`/`*_details`/`total_tokens`.

### Streaming events

`response.created` → `response.in_progress` → `output_item.added` →
`content_part.added` → `output_text.delta`* → `output_text.done` →
`content_part.done` → `output_item.done` → `response.completed`. Function calls
emit `function_call_arguments.delta`/`.done`. Upstream errors emit
`response.failed` + `error`. Each event carries an incrementing
`sequence_number`.

## 6. Statefulness

- `store` (default **true**): after the turn, persist the Responses object plus
  the **cumulative chat messages** (prior conversation + this turn's assistant
  output, excluding the system/instructions message) under `resp_…`.
- `previous_response_id`: load the stored cumulative messages (O(1), no chain
  walk) and prepend them before the new input. 404 if unknown / not owned by the
  caller / statefulness unavailable.
- `GET /v1/responses/{id}` returns the stored object; `DELETE` removes it. Both
  are owner-scoped.
- No DB → statefulness degrades gracefully: `store` is a no-op, `previous_response_id`
  and `GET` return 404.
- Statefulness is gated on `db_store_full_content` (the same privacy switch as
  prompt/response logging): in privacy mode (the default) the Responses store is
  not created, persistence no-ops, and the echoed `store` field is `false`.
  Operators opt into stateful `/v1/responses` by enabling full-content storage.

## 7. Errors

Responses API errors use the same `{"error": {...}}` envelope as Chat
Completions, so the global HTTP exception handler formats them with no
special-casing (unlike the Anthropic surface, which needed its own envelope).

## 8. Out of scope

- Hosted/built-in tools (`web_search`, `file_search`, `code_interpreter`, computer use) — dropped with a warning.
- Background mode (`background: true`), `response.output_text.annotation.*`, reasoning summary items.
- `GET /v1/responses/{id}/input_items` listing endpoint.
- A separate `surface: "responses"` tag in `api_logs` (the delegated request is logged as a chat request).
- `truncation: "auto"` context-window trimming — the request value is echoed but not honored (full auto-truncation needs token accounting + per-model context budgets). An over-long chained conversation surfaces the provider's context-length error rather than being silently truncated.

## 9. Testing

- `tests/unit/serving/test_responses_translator.py` — translation + the streaming state machine (text, tools, error, empty, split tool-name, monotonic sequence numbers).
- `tests/servers/test_responses_router.py` — end-to-end through `chat_completions` with stub adapters: text, tools, streaming, statefulness (store/get/previous_response_id/delete), error envelopes.
