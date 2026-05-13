"""``StreamSession`` — one-shot orchestrator for streaming chat completions.

Replaces the inner ``stream_generator`` and ``_adapter_reader`` closures
that previously lived in ``apps/backend/serving/servers/routers/completions.py``.
The class is instantiated per request, consumes the adapter's chunk stream,
emits sanitized SSE bytes to the client, and finalizes cost + DB log + routing
observations on completion.

Public surface:

- ``__init__(...)`` — capture per-request state (routing, params, deps).
- ``async def stream(adapter_chunks)`` — async generator yielding SSE bytes.
- ``yielded_first_chunk`` — bool property telling the router whether a
  fallback is still safe (no fallback after the first SSE byte).

Internal helpers: ``ToolCallAccumulator`` for
delta-merging tool_calls across chunks, ``_TTFTTracker`` for the
time-to-first-token measurement.

The streaming SSE wire format must remain byte-for-byte identical to the
prior inline implementation; the existing FastAPI integration tests in
``tests/servers/test_completions.py`` are the safety net.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from routing.routers import AllCircuitsOpenError
from serving.exceptions import scrub_error_for_user
from serving.openai_chat_serializer import resolve_mode, sanitize_chunk
from serving.servers.routers.routing_info import RoutingInfo, merge_adapter_routing
from serving.stream import make_role_chunk
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger
from serving.utils.token_utils import normalize_usage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from serving.servers.routers.completions_cost import CostTracker, PricingLookup
    from serving.servers.routers.completions_logging import CompletionsLogger

logger = get_logger(__name__)

# Keepalive cadence: emit an SSE comment after this many idle seconds so
# intermediate proxies (Cloudflare 100s, Nginx 120s) see activity and don't
# drop the connection during long upstream pauses (e.g., reasoning).
_KEEPALIVE_INTERVAL = 15
_SENTINEL: Any = object()


def _extract_exception_status_code(exc: BaseException, default: int = 500) -> int:
    """Return the HTTP status carried by common upstream exception shapes."""
    if isinstance(exc, AllCircuitsOpenError):
        return 503

    status_code = None

    if hasattr(exc, "status_code") and exc.status_code is not None:
        status_code = exc.status_code
    elif hasattr(exc, "response") and exc.response is not None:
        if hasattr(exc.response, "status_code"):
            status_code = exc.response.status_code
        elif hasattr(exc.response, "status"):
            status_code = exc.response.status
    elif hasattr(exc, "status") and exc.status is not None:
        status_code = exc.status
    elif hasattr(exc, "code") and exc.code is not None:
        status_code = exc.code

    if status_code is None:
        return default

    try:
        return int(status_code)
    except (TypeError, ValueError):
        return default


def _format_exception_for_db(exc: BaseException, max_len: int = 4000) -> str:
    """Return capped, redacted operator-facing error text for api_logs.error."""
    exc_text = str(exc)
    upstream_body = getattr(exc, "error_body", None)
    if upstream_body is None:
        value = exc_text
    else:
        body_text = upstream_body if isinstance(upstream_body, str) else str(upstream_body)
        value = f"{exc_text} | upstream_body={body_text}"

    value = re.sub(
        r'(?i)("?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|authorization)"?\s*[:=]\s*)("?)[^"\s,}]+("?)',
        r"\1\2[REDACTED]\3",
        value,
    )
    value = re.sub(r"(?i)bearer\s+[a-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    if len(value) > max_len:
        return value[: max_len - 14] + "...[truncated]"
    return value


class ToolCallAccumulator:
    """Merge tool_calls deltas indexed by ``index`` into complete tool calls.

    Adapters emit tool_calls as deltas; the public response (and the DB log
    payload) needs the merged form. Mirrors the prior inline merge logic
    from completions.py lines 543-570.
    """

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, Any]] = {}

    def add(self, deltas: list[dict[str, Any]]) -> None:
        """Merge a list of tool_call deltas into the accumulator."""
        for tc_delta in deltas:
            idx = tc_delta.get("index", 0)
            if idx not in self._calls:
                self._calls[idx] = {
                    "index": idx,
                    "id": tc_delta.get("id", ""),
                    "type": tc_delta.get("type", "function"),
                    "function": {"name": "", "arguments": ""},
                }
            if "id" in tc_delta:
                self._calls[idx]["id"] = tc_delta["id"]
            if "type" in tc_delta:
                self._calls[idx]["type"] = tc_delta["type"]
            if "function" in tc_delta:
                fn_delta = tc_delta["function"]
                if "name" in fn_delta:
                    self._calls[idx]["function"]["name"] += fn_delta["name"]
                if "arguments" in fn_delta:
                    self._calls[idx]["function"]["arguments"] += fn_delta["arguments"]

    def __bool__(self) -> bool:
        return bool(self._calls)

    def to_list(self) -> list[dict[str, Any]]:
        """Return tool_calls sorted by ``index`` for the final message."""
        return [tc for _, tc in sorted(self._calls.items())]


_ToolCallAccumulator = ToolCallAccumulator


class _TTFTTracker:
    """Record the time-to-first-token measurement.

    The first non-empty delta (content, tool_calls, or reasoning_content)
    sets the value; subsequent deltas leave it untouched.
    """

    def __init__(self, start_time: float) -> None:
        self._start = start_time
        self._ttft_ms: int | None = None

    def maybe_record(self, has_meaningful_delta: bool) -> None:
        """Record TTFT if not already set and a meaningful delta is present."""
        if self._ttft_ms is None and has_meaningful_delta:
            self._ttft_ms = int((time.time() - self._start) * 1000)
            logger.debug(f"TTFT recorded: {self._ttft_ms}ms")

    @property
    def ttft_ms(self) -> int | None:
        return self._ttft_ms


class StreamSession:
    """One-shot streaming orchestrator for /v1/chat/completions.

    Consumes an adapter's async chunk stream, emits sanitized SSE bytes to
    the client, accumulates content / reasoning / tool_calls for the DB log
    payload, and finalizes cost increments + DB logging + routing
    observations once the stream completes (or fails).

    Not reusable — instantiate per request.
    """

    def __init__(
        self,
        *,
        routing: RoutingInfo,
        model: str,
        messages: list[dict[str, Any]],
        params: dict[str, Any],
        request_id: str,
        start_time: float,
        request_headers: Any,
        metadata: dict[str, Any],
        user_id: str,
        is_synthetic_probe: bool,
        log_store: Any,
        active_router: Any,
        cost_tracker: CostTracker,
        completions_logger: CompletionsLogger,
        pricing_lookup: PricingLookup,
        get_adapter_config_for_provider: Callable[[str, str | None], Any],
        request_payload: Any | None = None,
    ) -> None:
        """Capture per-request state and dependencies.

        Args:
            routing: Initial typed routing context. Enriched in-place
                (functionally; via ``dataclasses.replace``) as adapter
                ``_routing`` metadata surfaces in incoming chunks.
            model: Model id from the request payload.
            messages: Inbound messages list.
            params: Forwarded request params (already filtered).
            request_id: Per-request id used for DB log + error scrubbing.
            start_time: ``time.time()`` at request entry — used for TTFT
                and total latency.
            request_headers: Starlette headers (case-insensitive).
            metadata: Mutable metadata dict the handler will update with
                adapter routing keys (minus ``upstream_cost_usd``).
            user_id: Stable user identifier for cost increments.
            is_synthetic_probe: Skip DB / cost / observation side effects.
            log_store: ``LogStore`` instance — gating only; the
                ``CompletionsLogger`` performs the actual writes.
            active_router: Router used for the routing observation.
            cost_tracker: Shared :class:`CostTracker` for cost increments.
            completions_logger: Shared :class:`CompletionsLogger` for DB log
                + routing observation forwarding.
            pricing_lookup: Shared :class:`PricingLookup` for pricing dict
                resolution at finalization time.
            get_adapter_config_for_provider: Closure from the handler that
                resolves the adapter config for a given provider+base_url.
                Captured so the session can reconstruct DB params.
            request_payload: Original raw request payload, retained for
                downstream logging and replay reconstruction; ``None`` when
                the caller does not need to preserve it.
        """
        self._routing: RoutingInfo = routing
        self._model = model
        self._messages = messages
        self._params = params
        self._request_id = request_id
        self._start_time = start_time
        self._request_headers = request_headers
        self._metadata = metadata
        self._user_id = user_id
        self._is_synthetic_probe = is_synthetic_probe
        self._log_store = log_store
        self._active_router = active_router
        self._cost_tracker = cost_tracker
        self._completions_logger = completions_logger
        self._pricing_lookup = pricing_lookup
        self._get_adapter_config_for_provider = get_adapter_config_for_provider
        self._request_payload = request_payload

        # Streaming-loop state
        self._yielded_first_chunk = False
        self._chunk_count = 0
        self._final_text = ""
        self._final_reasoning = ""
        self._finish_reason_for_db = "stop"
        self._tool_calls = ToolCallAccumulator()
        self._ttft = _TTFTTracker(start_time)
        self._usage_data: dict[str, Any] | None = None
        # Most recent adapter ``_routing`` dict (raw); ``None`` until first
        # chunk that exposes one. Kept around because the success-path
        # finalizer takes a different code branch when this is unset.
        self._adapter_routing: dict[str, Any] | None = None
        self._provider_from_ctx: str | None = None

    # -- public surface ------------------------------------------------------

    @property
    def yielded_first_chunk(self) -> bool:
        """Return ``True`` once the first SSE byte chunk has been emitted.

        The router uses this to decide whether a fallback is still safe:
        once we've committed bytes to the client, we cannot start a new
        adapter without breaking the SSE wire contract.
        """
        return self._yielded_first_chunk

    async def stream(self, adapter_chunks: AsyncIterator[str]) -> AsyncIterator[str]:
        """Yield SSE bytes for the streaming response.

        Owns: initial role chunk emission, keepalive heartbeats, per-chunk
        sanitization, tool-call delta merging, reasoning-content extraction,
        TTFT measurement, final usage propagation, and DB-log + cost
        finalization. On error after the first byte was yielded, emits an
        error chunk to the client and schedules an error DB log.

        ``adapter_chunks`` is consumed via a queue + background task so
        idle pauses can be filled with keepalive comments without
        cancelling the upstream read.
        """
        try:
            # Initial assistant role chunk — many OpenAI-compatible clients
            # (e.g., Cursor) expect role:"assistant" before any content.
            role_chunk = make_role_chunk(model=self._model)
            logger.debug(f"Yielding initial role chunk: {role_chunk[:150]}")
            self._yielded_first_chunk = True
            yield role_chunk

            serializer_mode = resolve_mode(self._request_headers)
            if serializer_mode.value != "strict_openai":
                logger.debug(
                    f"OpenAI chat serializer mode={serializer_mode.value} for model={self._model}"
                )

            logger.debug(f"Starting to consume adapter stream for model: {self._model}")

            # Keepalive: a background task drains adapter_chunks into a
            # queue.  The generator pulls with a short timeout; on timeout
            # it yields an SSE comment so intermediate proxies see activity
            # and don't drop the connection during long upstream pauses.
            last_client_yield = time.monotonic()
            chunk_queue: asyncio.Queue = asyncio.Queue()

            async def _adapter_reader() -> None:
                try:
                    async for item in adapter_chunks:
                        await chunk_queue.put(item)
                except Exception as exc:
                    await chunk_queue.put(exc)
                finally:
                    await chunk_queue.put(_SENTINEL)

            reader_task = asyncio.create_task(_adapter_reader())
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(
                            chunk_queue.get(), timeout=_KEEPALIVE_INTERVAL
                        )
                    except asyncio.TimeoutError:
                        logger.debug(
                            f"Emitting SSE keepalive (no chunk in "
                            f"{_KEEPALIVE_INTERVAL}s) for model={self._model}"
                        )
                        yield ": keepalive\n\n"
                        last_client_yield = time.monotonic()
                        continue
                    if item is _SENTINEL:
                        break
                    if isinstance(item, Exception):
                        raise item

                    chunk = item
                    self._chunk_count += 1
                    self._capture_provider_from_ctx_once()

                    if self._chunk_count <= 10 or self._chunk_count % 10 == 0:
                        logger.debug(
                            f"Chunk {self._chunk_count} received from adapter: {chunk[:200]}"
                        )

                    # JSON SSE chunks: sanitize, accumulate state, forward.
                    if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                        try:
                            chunk_json = json.loads(chunk[6:])
                        except (json.JSONDecodeError, KeyError) as e:
                            logger.warning(f"Failed to parse chunk {self._chunk_count}: {e}")
                        else:
                            result = sanitize_chunk(chunk_json, serializer_mode)

                            if result.usage_data:
                                self._usage_data = result.usage_data
                                logger.debug(
                                    f"Extracted usage from chunk "
                                    f"{self._chunk_count}: {self._usage_data}"
                                )
                            if result.routing_info:
                                self._adapter_routing = result.routing_info
                                self._routing = merge_adapter_routing(
                                    self._routing, result.routing_info
                                )
                                logger.debug(
                                    f"Extracted routing from chunk "
                                    f"{self._chunk_count}: {result.routing_info}"
                                )

                            self._record_ttft_from_delta(chunk_json)
                            self._accumulate_state_from_chunk(chunk_json)

                            # Strict mode: reasoning-only chunks are not
                            # forwarded; emit keepalive when client idle
                            # long enough so proxies don't drop the connection.
                            if not result.should_forward:
                                now = time.monotonic()
                                if now - last_client_yield >= _KEEPALIVE_INTERVAL:
                                    logger.debug(
                                        f"Sending keepalive instead of "
                                        f"reasoning chunk {self._chunk_count} "
                                        f"for model={self._model}"
                                    )
                                    yield ": keepalive\n\n"
                                    last_client_yield = now
                                continue

                            out_json = result.chunk_json or chunk_json
                            sanitized_chunk = f"data: {json.dumps(out_json)}\n\n"
                            logger.debug(
                                f"Yielding sanitized chunk {self._chunk_count} "
                                f"to client: {sanitized_chunk[:150]}"
                            )
                            yield sanitized_chunk
                            last_client_yield = time.monotonic()
                            continue

                    # Non-JSON or [DONE] chunks pass through verbatim.
                    logger.debug(f"Yielding chunk {self._chunk_count} to client: {chunk[:150]}")
                    yield chunk
            finally:
                reader_task.cancel()
                with suppress(asyncio.CancelledError):
                    await reader_task

            logger.info(f"Stream complete: total_chunks={self._chunk_count}")

            if not self._final_text and not self._tool_calls:
                logger.warning(
                    f"Stream completed with no visible content or tool_calls: "
                    f"model={self._model}, chunks={self._chunk_count}, "
                    f"request_id={self._request_id}"
                )

            await self._finalize_success()
        except Exception as exc:
            await self._finalize_failure(exc)
            exc_status_code = _extract_exception_status_code(exc)
            user_msg = scrub_error_for_user(exc, self._request_id, exc_status_code)
            error_chunk = {
                "error": {
                    "message": user_msg,
                    "type": "server_error",
                    "code": exc_status_code,
                }
            }
            error_msg = f"data: {json.dumps(error_chunk)}\n\n"
            logger.error(f"Yielding error chunk: {error_msg}")
            yield error_msg

    # -- internal: per-chunk handlers ---------------------------------------

    def _capture_provider_from_ctx_once(self) -> None:
        """Lift provider from request context on first chunk (fallback path).

        When the adapter never emits a ``_routing`` block, the provider is
        still discoverable via the request-context contextvar populated by
        the routing layer.
        """
        if self._provider_from_ctx is not None:
            return
        ctx = req_ctx.get()
        if ctx and "provider" in ctx:
            self._provider_from_ctx = ctx["provider"]
            logger.debug(f"Extracted provider from context: {self._provider_from_ctx}")

    def _record_ttft_from_delta(self, chunk_json: dict[str, Any]) -> None:
        """Record TTFT on the first delta carrying any meaningful field."""
        try:
            choices_local = chunk_json.get("choices", [])
            if not choices_local:
                return
            delta_local = choices_local[0].get("delta", {})
            has_content = bool(delta_local.get("content"))
            has_tool_calls = bool(delta_local.get("tool_calls"))
            has_reasoning = bool(delta_local.get("reasoning_content"))
            self._ttft.maybe_record(has_content or has_tool_calls or has_reasoning)
        except Exception:
            # Swallow malformed-chunk parse errors; TTFT is best-effort.
            pass

    def _accumulate_state_from_chunk(self, chunk_json: dict[str, Any]) -> None:
        """Accumulate content / reasoning / tool_calls / finish_reason for DB."""
        choices = chunk_json.get("choices", [])
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta", {})

        content_piece = delta.get("content")
        if content_piece:
            # Belt-and-suspenders TTFT: matches the prior inline behavior
            # which set ttft_ms here too (covered by _record_ttft_from_delta
            # above, but kept for parity with the legacy code path).
            self._ttft.maybe_record(True)
            self._final_text += content_piece

        reasoning_piece = delta.get("reasoning_content")
        if not (isinstance(reasoning_piece, str) and reasoning_piece):
            reasoning_piece = delta.get("reasoning")
        if isinstance(reasoning_piece, str) and reasoning_piece:
            self._final_reasoning += reasoning_piece

        tool_calls_delta = delta.get("tool_calls")
        if tool_calls_delta:
            self._tool_calls.add(tool_calls_delta)

        fr = choice.get("finish_reason")
        if fr:
            self._finish_reason_for_db = fr

    # -- internal: finalization ---------------------------------------------

    def _build_response_for_db(self) -> dict[str, Any]:
        """Assemble the synthetic ``chat.completion`` for the api_logs row."""
        message_for_db: dict[str, Any] = {
            "role": "assistant",
            "content": self._final_text if self._final_text else None,
        }
        if self._final_reasoning:
            message_for_db["reasoning_content"] = self._final_reasoning
        response_for_db: dict[str, Any] = {
            "id": self._request_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self._model,
            "choices": [
                {
                    "index": 0,
                    "message": message_for_db,
                    "finish_reason": self._finish_reason_for_db,
                }
            ],
        }
        if self._tool_calls:
            response_for_db["choices"][0]["message"]["tool_calls"] = self._tool_calls.to_list()
        if self._usage_data:
            response_for_db["usage"] = normalize_usage(self._usage_data) or self._usage_data
        return response_for_db

    async def _finalize_success(self) -> None:
        """Schedule cost increment, DB log, and routing observation."""
        response_for_db = self._build_response_for_db()

        provider = "router"
        pricing: dict[str, str] | None = None
        if self._adapter_routing:
            provider = self._routing.provider or "router"
            pricing = self._pricing_lookup.raw_dict_for_routing(self._routing)
            # Strip upstream_cost_usd from metadata JSONB; the dedicated column
            # api_logs.upstream_cost_usd is the canonical store. Avoids leaking the
            # internal cost into any future admin route that returns raw metadata.
            # Persisted metadata intentionally keeps adapter-emitted legacy keys like
            # routewise for DB log compatibility; RoutingInfo.strategy_metadata is
            # only the in-process isolation boundary.
            self._metadata.update(
                {k: v for k, v in self._adapter_routing.items() if k != "upstream_cost_usd"}
            )
        elif self._provider_from_ctx:
            provider = self._provider_from_ctx
            pricing = self._pricing_lookup.raw_dict_for_routing(
                dataclasses.replace(self._routing, provider=provider)
            )
            logger.debug(f"Using provider from context for DB logging: {provider}")

        # Increment daily cost counter for billed requests via the
        # CostTracker (same fire-and-forget semantics as before; the
        # tracker also populates self._routing.upstream_cost_usd so the
        # subsequent log-payload assembly sees the new value).
        if not self._is_synthetic_probe and self._adapter_routing:
            _usage = response_for_db.get("usage") or {}
            self._routing = await self._cost_tracker.schedule_increment(
                user_id=self._user_id,
                routing=self._routing,
                prompt_tokens=int(_usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(_usage.get("completion_tokens", 0) or 0),
                cache_read_tokens=int(_usage.get("cache_read_tokens", 0) or 0),
                cache_write_tokens=int(_usage.get("cache_write_tokens", 0) or 0),
                reasoning_tokens=int(_usage.get("reasoning_tokens", 0) or 0),
            )

        if self._log_store and not self._is_synthetic_probe:
            self._completions_logger.schedule_log(
                self._request_id,
                {
                    "request_id": self._request_id,
                    "model_id": self._model,
                    "provider": provider,
                    "prompt": self._messages,
                    "response": response_for_db,
                    "usage": response_for_db.get("usage") or self._usage_data,
                    "latency_ms": int((time.time() - self._start_time) * 1000),
                    "status_code": 200,
                    "params": self._completions_logger.build_db_params(
                        self._params,
                        provider,
                        self._routing.base_url,
                        self._get_adapter_config_for_provider,
                    ),
                    "metadata": self._metadata,
                    "ttft_ms": self._ttft.ttft_ms,
                    "pricing": pricing,
                    "upstream_cost_usd": self._routing.upstream_cost_usd,
                    "request_payload": self._request_payload,
                },
            )

        if not self._is_synthetic_probe:
            stream_usage = normalize_usage(self._usage_data) if self._usage_data else {}
            self._completions_logger.record_routing_observation(
                self._active_router,
                self._model,
                self._routing,
                ttft_ms=float(self._ttft.ttft_ms) if self._ttft.ttft_ms is not None else None,
                total_latency_ms=(time.time() - self._start_time) * 1000,
                prompt_tokens=int(stream_usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(stream_usage.get("completion_tokens", 0) or 0),
                success=True,
            )

    async def _finalize_failure(self, exc: BaseException) -> None:
        """Record failure observation and schedule the error DB log."""
        if not self._is_synthetic_probe:
            # ``exc._routing`` is still a raw dict from the routing layer;
            # ``record_routing_observation`` accepts both shapes so we don't
            # need to coerce here.
            exc_routing = getattr(exc, "_routing", None)
            self._completions_logger.record_routing_observation(
                self._active_router,
                self._model,
                exc_routing or self._routing,
                ttft_ms=float(self._ttft.ttft_ms) if self._ttft.ttft_ms is not None else None,
                total_latency_ms=(time.time() - self._start_time) * 1000,
                prompt_tokens=0,
                completion_tokens=0,
                success=False,
            )

        ctx = req_ctx.get()
        provider_for_error = ctx.get("provider", "router") if ctx else "router"
        exc_status_code = _extract_exception_status_code(exc)

        if self._log_store and not self._is_synthetic_probe:
            metadata_for_error = self._metadata
            if isinstance(exc_routing, dict):
                metadata_for_error = {
                    **self._metadata,
                    **{k: v for k, v in exc_routing.items() if k != "upstream_cost_usd"},
                }
            self._completions_logger.schedule_log(
                self._request_id,
                {
                    "request_id": self._request_id,
                    "model_id": self._model,
                    "provider": provider_for_error,
                    "prompt": self._messages,
                    "response": None,
                    "usage": None,
                    "latency_ms": int((time.time() - self._start_time) * 1000),
                    "status_code": exc_status_code,
                    "error": _format_exception_for_db(exc),
                    "params": self._params,
                    "metadata": metadata_for_error,
                    "ttft_ms": self._ttft.ttft_ms,
                    "pricing": None,
                    "request_payload": self._request_payload,
                },
            )

        return
