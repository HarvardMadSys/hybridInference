"""Generic adapter for OpenAI-compatible APIs (gateways, local VLLM, etc.)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

from serving.config.settings import get_settings
from serving.stream import done_sentinel
from serving.utils.logging import get_logger
from serving.utils.tokens import estimate_prompt_tokens, estimate_text_tokens

from .base import BaseAdapter, UsageInfo
from .key_pool import KeyPool, KeyPoolExhausted
from .processors import get_processor
from .profiles import (
    ProviderProfile,
    default_chat_path,
    extract_tool_calls_for_profile,
    filter_response_format,
    function_call_delta_to_tool_calls,
    get_stream_idle_timeout_seconds,
    get_usage_normalizer,
    normalize_tools_for_profile,
    resolve_tool_choice_for_profile,
    supports_guided_json,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

logger = get_logger(__name__)


def _normalize_text_content(content: Any) -> Any:
    """Normalize structured content blocks into plain text when needed."""
    if not isinstance(content, list):
        return content

    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(p for p in parts if p)


class OpenAICompatAdapter(BaseAdapter):
    """Generic adapter for OpenAI-compatible APIs.

    Works with:
    - API gateways (Chutes, Featherless, OpenRouter)
    - Local deployments (VLLM, sglang)
    - Any OpenAI-compatible service

    Configuration is fully driven by ModelConfig fields.
    Optional overrides are applied when present on the config:
    - chat_path (default: /v1/chat/completions)
    - auth_header_name (default: Authorization)
    - auth_format (default: Bearer {api_key})
    - extra_headers / extra_query (dict[str,str])
    """

    def __init__(self, config):
        super().__init__(config)

        # Multi-key API rotation pool (None when single api_key is configured).
        # For RouteWise, this is intentionally modeled as one aggregate
        # endpoint candidate. Per-key scarcity must be represented by separate
        # route entries, not hidden behind this adapter-level pool.
        self._key_pool: KeyPool | None = None
        if config.api_keys:
            self._key_pool = KeyPool(
                keys=list(config.api_keys),
                provider_label=config.provider,
            )

        logger.info(f"[OpenAICompat] Initialized for {config.id} at {config.base_url}")

        # Store model ID for per-request processor creation (avoids shared mutable state)
        self._processor_model_id = config.provider_model_id or config.id
        self._processor_override = config.processor
        processor_name = get_processor(
            self._processor_model_id, override=self._processor_override
        ).__class__.__name__
        logger.debug(f"[OpenAICompat] Processor type: {processor_name}")

        # Provider profile for usage extraction (e.g. DeepSeek cache hit/miss semantics)
        profile_str = getattr(config, "provider_profile", None)
        try:
            self._usage_profile = (
                ProviderProfile(profile_str) if profile_str else ProviderProfile.DEFAULT
            )
        except ValueError:
            self._usage_profile = ProviderProfile.DEFAULT
        self._usage_normalizer = get_usage_normalizer(self._usage_profile)

    def _apply_supported_passthrough_params(
        self, payload: dict[str, Any], params: dict[str, Any]
    ) -> None:
        """Forward OpenAI-like params that do not need special normalization."""
        passthrough_params = (
            "top_k",
            "min_p",
            "frequency_penalty",
            "presence_penalty",
            "reasoning_effort",
            "thinking",
            "tool_stream",
        )
        for name in passthrough_params:
            if name in params and name in self.config.supported_params:
                payload[name] = params[name]

    def _prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normalize request messages for the active provider profile."""
        return [self._clean_message(msg) for msg in messages]

    def _normalize_tools(self, tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        """Normalize tool definitions for the active provider profile."""
        return normalize_tools_for_profile(self._usage_profile, tools)

    def _resolve_tool_choice(self, tool_choice: Any) -> Any:
        """Resolve the tool_choice value for the active provider profile."""
        return resolve_tool_choice_for_profile(self._usage_profile, tool_choice)

    def _build_stream_timeout(self) -> aiohttp.ClientTimeout | None:
        """Return a provider-specific streaming timeout, if configured."""
        idle_timeout = get_stream_idle_timeout_seconds(self._usage_profile)
        if idle_timeout is None:
            return None
        try:
            return aiohttp.ClientTimeout(total=None, sock_read=idle_timeout)
        except TypeError:
            # Test doubles may expose a simplified ClientTimeout(total=...) shim.
            return SimpleNamespace(total=None, sock_read=idle_timeout)

    def _format_passthrough_chunk(self, processed_chunk: dict[str, Any]) -> str:
        """Forward an upstream delta while normalizing model/role fields."""
        chunk_copy = dict(processed_chunk)
        chunk_copy["model"] = self.config.id

        choices = list(chunk_copy.get("choices") or [])
        if choices:
            choice = dict(choices[0])
            delta = dict(choice.get("delta") or {})
            # The router emits the initial role chunk, so avoid duplicating it here.
            delta.pop("role", None)
            choice["delta"] = delta
            choices[0] = choice
            chunk_copy["choices"] = choices

        return f"data: {json.dumps(chunk_copy)}\n\n"

    def _clean_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Remove None values and normalize text content for API compatibility.

        Structured content (lists of text/image/audio blocks) is forwarded
        verbatim when the model declares any non-text input modality. For
        text-only models the blocks are flattened to a plain string so that
        upstreams which only accept string content don't 400. Block types the
        model cannot handle are rejected earlier by the router pre-flight, so
        anything reaching here is safe to pass through.
        """
        cleaned = {k: v for k, v in message.items() if v is not None}
        modalities = self.config.input_modalities or ["text"]
        supports_structured_content = any(m != "text" for m in modalities)
        if not supports_structured_content and "content" in cleaned:
            cleaned["content"] = _normalize_text_content(cleaned["content"])
        return cleaned

    def _build_headers(self, api_key_override: str | None = None) -> dict[str, str]:
        """Build HTTP headers for request.

        Args:
            api_key_override: when set (multi-key flow), use this key instead
                of ``self.config.api_key``.
        """
        headers = {"Content-Type": "application/json"}
        raw = api_key_override if api_key_override is not None else self.config.api_key
        api_key = raw.strip() if isinstance(raw, str) else raw

        # Add standard OpenAI authentication
        if api_key and getattr(self.config, "use_bearer_auth", True):
            headers["Authorization"] = f"Bearer {api_key}"

        # Custom header formatting (if provided)
        auth_name = getattr(self.config, "auth_header_name", None)
        auth_format = getattr(self.config, "auth_format", None)
        if auth_name and auth_format and api_key:
            headers[auth_name] = auth_format.format(api_key=api_key)

        # Merge any extra headers
        extra_headers = getattr(self.config, "extra_headers", None)
        if isinstance(extra_headers, dict):
            headers.update(extra_headers)

        return headers

    async def _post_with_pool(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST JSON with key-pool rotation on 429s.

        When ``self._key_pool`` is None, falls through to the legacy single-key
        path with retries. When set, loops over keys: a 429 on key K cools K
        down and the loop tries the next least-loaded key. Pool exhaustion
        raises the last 429 (or KeyPoolExhausted if none was seen yet), which
        the caller surfaces as an upstream failure for the router fallback chain.
        """
        if self._key_pool is None:
            headers = self._build_headers()
            return await self.http.json_post_with_retry(
                url=url, json=payload, headers=headers, timeout=120, retries=2
            )

        from serving.utils import context as req_ctx

        affinity_key = req_ctx.get().get("auth_key_hash") or "_anon"
        provider = self.config.provider

        # Bound the loop to pool size — defensive; acquire already filters
        # cooled-down keys, so we shouldn't reacquire the same just-cooled one.
        max_attempts = self._key_pool.size()
        last_429_error: aiohttp.ClientResponseError | None = None

        for _ in range(max_attempts):
            try:
                api_key, lease = self._key_pool.acquire(affinity_key)
            except KeyPoolExhausted as exhausted:
                logger.warning(
                    "key_pool_exhausted",
                    extra={
                        "event": "key_pool_exhausted",
                        "provider": provider,
                        "stage": "acquire",
                    },
                )
                if last_429_error is not None:
                    raise last_429_error from exhausted
                raise

            logger.debug(
                "key_pool_request",
                extra={
                    "event": "key_pool_request",
                    "provider": provider,
                    "key_index": lease.key_index,
                },
            )

            headers = self._build_headers(api_key_override=api_key)
            try:
                response = await self.http.json_post(
                    url=url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120),
                )
                self._key_pool.release(lease, status_code=200, retry_after=None)
                logger.debug(
                    "key_pool_active_affinities",
                    extra={
                        "event": "key_pool_active_affinities",
                        "provider": provider,
                        "count": self._key_pool.affinity_count(),
                    },
                )
                return response
            except aiohttp.ClientResponseError as e:
                if e.status == 429:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    self._key_pool.release(lease, status_code=429, retry_after=retry_after)
                    reason = "retry_after" if retry_after else "default_2min"
                    logger.warning(
                        "key_pool_cooldown",
                        extra={
                            "event": "key_pool_cooldown",
                            "provider": provider,
                            "key_index": lease.key_index,
                            "reason": reason,
                        },
                    )
                    last_429_error = e
                    continue  # try next key
                # Non-429 error — release without cooldown, propagate.
                self._key_pool.release(lease, status_code=e.status, retry_after=None)
                raise

        # Loop exhausted naturally (every key returned 429 in this single call)
        logger.warning(
            "key_pool_exhausted",
            extra={
                "event": "key_pool_exhausted",
                "provider": provider,
                "stage": "all_429",
            },
        )
        assert last_429_error is not None
        raise last_429_error

    async def _open_stream_with_pool(
        self, url: str, payload: dict[str, Any], timeout: Any = None
    ) -> AsyncGenerator[tuple[Any, Any, str], None]:
        """Open a streaming POST with key-pool rotation on opening 429s.

        Yields exactly one tuple: ``(stream_iter, lease, first_chunk)``.

        - ``stream_iter`` is the underlying async iterator from ``stream_post``;
          the caller should continue iterating it after processing
          ``first_chunk``.
        - ``lease`` is the ``Lease`` to release (status 200) when the stream
          ends, or ``None`` when no pool is configured.
        - ``first_chunk`` is the first chunk already pulled from the iterator
          (must be processed first by the caller).

        Rotates keys internally on opening 429s (status check happens before
        any chunk is yielded). Mid-stream errors are not classified — they
        propagate to the caller as today.
        """
        if self._key_pool is None:
            headers = self._build_headers()
            stream_iter = self.http.stream_post(
                url=url, json=payload, headers=headers, timeout=timeout
            )
            try:
                first = await stream_iter.__anext__()
            except StopAsyncIteration:
                return  # empty stream — nothing to yield, fall out
            yield stream_iter, None, first
            return

        from serving.utils import context as req_ctx

        affinity_key = req_ctx.get().get("auth_key_hash") or "_anon"
        provider = self.config.provider
        max_attempts = self._key_pool.size()
        last_429: aiohttp.ClientResponseError | None = None

        for _ in range(max_attempts):
            try:
                api_key, lease = self._key_pool.acquire(affinity_key)
            except KeyPoolExhausted as exhausted:
                logger.warning(
                    "key_pool_exhausted",
                    extra={
                        "event": "key_pool_exhausted",
                        "provider": provider,
                        "stage": "stream_acquire",
                    },
                )
                if last_429 is not None:
                    raise last_429 from exhausted
                raise

            logger.debug(
                "key_pool_request",
                extra={
                    "event": "key_pool_request",
                    "provider": provider,
                    "key_index": lease.key_index,
                    "stage": "stream",
                },
            )

            headers = self._build_headers(api_key_override=api_key)
            stream_iter = self.http.stream_post(
                url=url, json=payload, headers=headers, timeout=timeout
            )
            try:
                first = await stream_iter.__anext__()
            except StopAsyncIteration:
                # Empty stream — treat as success
                self._key_pool.release(lease, status_code=200, retry_after=None)
                logger.debug(
                    "key_pool_active_affinities",
                    extra={
                        "event": "key_pool_active_affinities",
                        "provider": provider,
                        "count": self._key_pool.affinity_count(),
                    },
                )
                return
            except aiohttp.ClientResponseError as e:
                if e.status == 429:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    self._key_pool.release(lease, status_code=429, retry_after=retry_after)
                    reason = "retry_after" if retry_after else "default_2min"
                    logger.warning(
                        "key_pool_cooldown",
                        extra={
                            "event": "key_pool_cooldown",
                            "provider": provider,
                            "key_index": lease.key_index,
                            "reason": reason,
                            "stage": "stream",
                        },
                    )
                    last_429 = e
                    continue
                # Non-429 — release without cooldown, propagate
                self._key_pool.release(lease, status_code=e.status, retry_after=None)
                raise

            # First chunk read successfully — commit the lease (caller releases on stream end)
            yield stream_iter, lease, first
            return

        # Loop exhausted — every key returned 429
        logger.warning(
            "key_pool_exhausted",
            extra={
                "event": "key_pool_exhausted",
                "provider": provider,
                "stage": "stream_all_429",
            },
        )
        assert last_429 is not None
        raise last_429

    def _build_url(self) -> str:
        """Build full endpoint URL (standard OpenAI path)."""
        base = (self.config.base_url or "").rstrip("/")

        chat_path = getattr(self.config, "chat_path", None) or default_chat_path(
            self._usage_profile
        )
        if not chat_path and base.endswith("/v1"):
            chat_path = "/chat/completions"
        if not chat_path:
            chat_path = "/v1/chat/completions"
        if not chat_path.startswith("/"):
            chat_path = f"/{chat_path}"
        url = f"{base}{chat_path}"

        extra_query = getattr(self.config, "extra_query", None) or {}
        if not extra_query:
            return url

        parts = urlsplit(url)
        query_pairs = dict(parse_qsl(parts.query, keep_blank_values=True))
        query_pairs.update({str(k): str(v) for k, v in extra_query.items()})
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query_pairs), parts.fragment)
        )

    def _get_model_identifier(self) -> str:
        """Get model ID to send to upstream API."""
        return self.config.provider_model_id or self.config.id

    def _augment_payload(self, payload: dict[str, Any], *, stream: bool) -> dict[str, Any]:
        """Subclass extension point for provider-specific payload mutation.

        Called inside chat_completion / stream_chat_completion right before
        the request is dispatched, after profile-level transforms have run.
        Default implementation returns the payload unchanged.
        """
        return payload

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Execute non-streaming chat completion.

        Args:
            messages: Chat messages in OpenAI format
            **params: Optional parameters (temperature, max_tokens, tools, etc.)

        Returns:
            OpenAI-compatible response dict
        """
        validated = self.validate_params(params)

        # Clean messages to remove None fields (some APIs reject them)
        cleaned_messages = self._prepare_messages(messages)

        # Build request payload
        payload = {
            **self.config.extra_body,
            "messages": cleaned_messages,
            "model": self._get_model_identifier(),
            **validated,
        }
        self._apply_supported_passthrough_params(payload, params)

        # Add optional features
        if params.get("tools") and self.config.supports_tools:
            payload["tools"] = self._normalize_tools(params["tools"])
            tool_choice = self._resolve_tool_choice(params.get("tool_choice"))
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice

        filtered_rf = filter_response_format(self._usage_profile, params.get("response_format"))
        if filtered_rf and self.config.supports_structured_output:
            payload["response_format"] = filtered_rf
        payload = self._augment_payload(payload, stream=False)

        # Make request
        url = self._build_url()
        logger.debug(f"[OpenAICompat] POST {url} model={payload.get('model', '<omitted>')}")
        _log_payload = get_settings().log_full_payload
        try:
            from serving.config.runtime_settings import get_runtime_settings_instance

            rs = get_runtime_settings_instance()
            _log_payload = await rs.get_bool("log_full_payload")
        except (RuntimeError, KeyError):
            pass
        if _log_payload:
            logger.debug(f"[OpenAICompat] Payload: {payload}")

        response = await self._post_with_pool(url, payload)

        # Process output format (e.g. remove XML tags)
        processor = get_processor(self._processor_model_id, override=self._processor_override)
        processed_response = processor.process_response(response)

        # Parse response
        return self._parse_completion_response(processed_response)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        """Execute streaming chat completion.

        Args:
            messages: Chat messages in OpenAI format
            **params: Optional parameters

        Yields:
            SSE-formatted chunks
        """
        validated = self.validate_params(params)

        # Clean messages to remove None fields (some APIs reject them)
        cleaned_messages = self._prepare_messages(messages)

        payload = {
            **self.config.extra_body,
            "messages": cleaned_messages,
            "model": self._get_model_identifier(),
            "stream": True,
            **validated,
        }
        self._apply_supported_passthrough_params(payload, params)

        # Add optional features
        if params.get("tools") and self.config.supports_tools:
            payload["tools"] = self._normalize_tools(params["tools"])
            tool_choice = self._resolve_tool_choice(params.get("tool_choice"))
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice

        filtered_rf = filter_response_format(self._usage_profile, params.get("response_format"))
        if filtered_rf and self.config.supports_structured_output:
            payload["response_format"] = filtered_rf
            if supports_guided_json(self._usage_profile) and (
                schema := params.get("response_format", {}).get("schema")
            ):
                payload["guided_json"] = schema
        if getattr(self.config, "include_usage_in_stream", False):
            existing_options = payload.get("stream_options") or {}
            payload["stream_options"] = {**existing_options, "include_usage": True}
        payload = self._augment_payload(payload, stream=True)

        url = self._build_url()
        # NOTE: headers are built per-attempt inside _open_stream_with_pool

        # Fresh processor per request — avoids shared mutable state across concurrent streams
        processor = get_processor(self._processor_model_id, override=self._processor_override)

        total_content = ""
        finish_reason = "stop"
        upstream_usage: dict[str, Any] | None = None
        prompt_tokens_override: int | None = None
        saw_tool_calls = False

        # Helper to yield formatted chunks from processed data
        def format_and_yield(processed_chunk: dict[str, Any]) -> str | None:
            nonlocal \
                total_content, \
                finish_reason, \
                upstream_usage, \
                prompt_tokens_override, \
                saw_tool_calls

            choices = processed_chunk.get("choices") or []

            # Capture usage if present
            if processed_chunk.get("usage"):
                upstream_usage = processed_chunk["usage"]
                pt = upstream_usage.get("prompt_tokens")
                if isinstance(pt, int):
                    prompt_tokens_override = pt

            if not choices:
                return None

            delta = choices[0].get("delta", {})

            # Capture finish_reason FIRST (before any early returns)
            fr = choices[0].get("finish_reason")
            if fr:
                finish_reason = fr

            # Accumulate visible content and reasoning independently for the fallback
            content = delta.get("content")
            if isinstance(content, str) and content:
                total_content += content

            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                total_content += reasoning

            legacy_tool_calls = function_call_delta_to_tool_calls(
                self._usage_profile, delta.get("function_call")
            )
            if legacy_tool_calls:
                saw_tool_calls = True
                chunk_copy = dict(processed_chunk)
                chunk_copy["model"] = self.config.id
                new_choices = list(chunk_copy.get("choices") or [])
                if new_choices:
                    new_choice = dict(new_choices[0])
                    new_delta = dict(new_choice.get("delta") or {})
                    new_delta.pop("role", None)
                    new_delta.pop("function_call", None)
                    new_delta["tool_calls"] = legacy_tool_calls
                    new_choice["delta"] = new_delta
                    new_choices[0] = new_choice
                    chunk_copy["choices"] = new_choices
                return f"data: {json.dumps(chunk_copy)}\n\n"

            has_reasoning = bool(delta.get("reasoning_content"))
            has_tool_calls = isinstance(delta.get("tool_calls"), list) and bool(
                delta.get("tool_calls")
            )

            if has_reasoning or has_tool_calls:
                if has_tool_calls:
                    saw_tool_calls = True
                return self._format_passthrough_chunk(processed_chunk)

            if isinstance(content, str) and content:
                return self.format_stream_chunk(
                    content=content,
                    model=self.config.id,
                )

            return None

        stream_timeout = self._build_stream_timeout()

        # Open the stream via the key-pool-aware helper. The helper performs
        # 429 rotation BEFORE the first chunk is yielded; once we receive the
        # primed first chunk, the lease is committed for the lifetime of the
        # stream and any mid-stream errors propagate as before.
        primed: str | None = None
        stream_iter: AsyncIterator[str] | None = None
        active_lease = None

        async for it, lease, first in self._open_stream_with_pool(
            url, payload, timeout=stream_timeout
        ):
            stream_iter = it
            active_lease = lease
            primed = first
            break  # helper yields exactly once

        async def _drain() -> AsyncIterator[str]:
            if primed is not None:
                yield primed
            if stream_iter is not None:
                async for c in stream_iter:
                    yield c

        # Stream response
        try:
            async for chunk in _drain():
                if not chunk.strip():
                    continue

                if chunk.startswith("data: "):
                    data_str = chunk[6:]

                    if data_str.strip() == "[DONE]":
                        break

                    try:
                        data = json.loads(data_str)

                        # Process output format (returns a list of chunks)
                        processed_chunks = processor.process_stream_chunk(data)

                        for p_chunk in processed_chunks:
                            formatted = format_and_yield(p_chunk)
                            if formatted:
                                yield formatted

                    except json.JSONDecodeError:
                        logger.warning(f"[OpenAICompat] Failed to parse chunk: {data_str[:100]}")
        except asyncio.TimeoutError:
            logger.warning(
                "[OpenAICompat] Stream idle timeout for model=%s after %.1fs",
                self.config.id,
                getattr(stream_timeout, "sock_read", -1.0) if stream_timeout else -1.0,
            )
        finally:
            if active_lease is not None and self._key_pool is not None:
                self._key_pool.release(active_lease, status_code=200, retry_after=None)
                logger.debug(
                    "key_pool_active_affinities",
                    extra={
                        "event": "key_pool_active_affinities",
                        "provider": self.config.provider,
                        "count": self._key_pool.affinity_count(),
                    },
                )

        # Flush processor buffer at end of stream
        # This is crucial for buffered tool calls (e.g. GLM XML, Qwen XML)
        final_chunks = processor.flush()
        for p_chunk in final_chunks:
            formatted = format_and_yield(p_chunk)
            if formatted:
                yield formatted

        # Final usage and done sentinel
        if saw_tool_calls:
            finish_reason = "tool_calls"
        if upstream_usage:
            usage_info = self._usage_normalizer(upstream_usage)
            final_usage = usage_info.to_dict()
        else:
            usage_info = None
            final_usage = self._build_fallback_usage(
                messages=cleaned_messages,
                total_content=total_content,
                prompt_tokens_override=prompt_tokens_override,
            )
        final_chunk_str = self._build_final_chunk(
            usage=final_usage,
            finish_reason=finish_reason,
            usage_info=usage_info,
        )
        yield final_chunk_str
        yield done_sentinel()

    def _build_embeddings_url(self) -> str:
        """Build full endpoint URL for embeddings."""
        base = (self.config.base_url or "").rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/embeddings"
        return f"{base}/v1/embeddings"

    async def embeddings(self, input_data: str | list[str], **params: Any) -> dict[str, Any]:
        """Execute an embedding request against the upstream API.

        Args:
            input_data: Text string or list of strings to embed.
            **params: Optional parameters (encoding_format, dimensions).

        Returns:
            OpenAI-compatible embedding response dict.
        """
        payload: dict[str, Any] = {
            "model": self._get_model_identifier(),
            "input": input_data,
        }
        if params.get("encoding_format"):
            payload["encoding_format"] = params["encoding_format"]
        if params.get("dimensions"):
            payload["dimensions"] = params["dimensions"]

        url = self._build_embeddings_url()

        logger.debug(f"[OpenAICompat] POST {url} model={payload['model']}")

        return await self._post_with_pool(url, payload)

    def _parse_completion_response(self, response: dict[str, Any]) -> dict[str, Any]:
        """Parse response into OpenAI-compatible format."""
        choice = response["choices"][0]
        message = choice["message"]
        tool_calls = extract_tool_calls_for_profile(self._usage_profile, message)

        usage = self._parse_usage(response.get("usage", {}))

        return self.format_response(
            content=message.get("content", ""),
            model=self.config.id,
            usage=usage,
            tool_calls=tool_calls,
            reasoning_content=message.get("reasoning_content"),
            finish_reason=choice.get("finish_reason", "stop"),
        )

    def _parse_usage(self, usage_data: dict[str, Any]) -> UsageInfo:
        """Parse usage information from response using the adapter's profile."""
        return self._usage_normalizer(usage_data)

    def _build_fallback_usage(
        self,
        *,
        messages: list[dict[str, Any]],
        total_content: str,
        prompt_tokens_override: int | None,
    ) -> dict[str, int]:
        prompt_tokens = (
            int(prompt_tokens_override)
            if prompt_tokens_override is not None and prompt_tokens_override > 0
            else int(estimate_prompt_tokens(messages))
        )
        completion_tokens = int(estimate_text_tokens(total_content))
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    def _build_final_chunk(
        self,
        *,
        usage: dict[str, Any],
        finish_reason: str,
        usage_info: UsageInfo | None = None,
    ) -> str:
        routing: dict[str, Any] = {
            "provider": self.config.provider,
            "base_url": self.config.base_url,
            "endpoint_id": getattr(self.config, "endpoint_id", None) or self.config.provider,
        }
        if usage_info is not None and usage_info.upstream_cost_usd is not None:
            routing["upstream_cost_usd"] = usage_info.upstream_cost_usd

        chunk = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self.config.id,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "usage": usage,
            "_routing": routing,
        }
        return f"data: {json.dumps(chunk)}\n\n"
