"""Claude subscription adapter — routes requests through Claude subscription accounts.

Uses OAuth-authenticated Anthropic Messages API with multi-account pooling,
automatic token refresh, health-aware rotation, and optional paid API fallback.

Format translation is shared with the Vertex AI Claude adapter via
``claude_format``.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import aiohttp

from serving.stream import done_sentinel
from serving.utils.logging import get_logger

from .base import BaseAdapter
from .claude_format import (
    ToolCallAccumulator,
    build_final_usage,
    convert_messages,
    convert_tool_choice,
    convert_tools,
    extract_system,
    handle_stream_event,
    map_stop_reason,
    parse_response_content,
    parse_usage,
)
from .claude_token import (
    RefreshTokenRevokedError,
    TokenRefreshError,
)
from .codex_token import AccountPool, NoHealthyAccountError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from .claude_token import ClaudeCredentialProvider

logger = get_logger(__name__)

# ⚠️ Provisional — from CLIProxyAPI, not officially documented
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_BETA = (
    "claude-code-20250219,"
    "oauth-2025-04-20,"
    "interleaved-thinking-2025-05-14,"
    "context-management-2025-06-27,"
    "prompt-caching-scope-2026-01-05"
)
_REQUIRED_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."


class ClaudeSubscriptionAdapter(BaseAdapter):
    """Adapter that routes Chat Completions through Claude subscription accounts.

    On first request, lazily loads account credentials and initialises the
    account pool.  Requests are translated from Chat Completions format to
    the Anthropic Messages API using shared ``claude_format`` functions,
    sent via subscription OAuth, and the response is translated back.

    If all subscription accounts are unhealthy and a fallback API key is
    configured, falls back to the direct Anthropic paid API (same endpoint,
    ``x-api-key`` header instead of OAuth bearer).
    """

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self._initialized = False
        self._credential_provider: ClaudeCredentialProvider | None = None
        self._account_pool: AccountPool | None = None
        self._fallback_api_key: str | None = None

    def _ensure_init(self) -> None:
        """Lazy initialisation on first request."""
        if self._initialized:
            return

        from serving.config.settings import get_settings

        from .claude_pool import get_shared_pool

        settings = get_settings()
        self._fallback_api_key = settings.claude_sub_fallback_api_key or None

        self._credential_provider, self._account_pool = get_shared_pool()
        self._initialized = True
        logger.info(
            f"[ClaudeSub] Initialised (shared pool), "
            f"fallback={'yes' if self._fallback_api_key else 'no'}"
        )

    # ------------------------------------------------------------------
    # Account acquisition with token-refresh retry
    # ------------------------------------------------------------------

    async def _acquire_with_retry(self) -> tuple:
        """Acquire an account and get a valid token, retrying on refresh failure.

        On ``RefreshTokenRevokedError``, marks the account revoked and tries
        the next one. On transient refresh errors, reports failure to the pool
        and tries the next one. Returns ``(account, token)`` tuple.

        Raises:
            NoHealthyAccountError: If no account can produce a valid token.
        """
        assert self._account_pool is not None
        assert self._credential_provider is not None

        last_err: Exception | None = None
        for _ in range(len(self._account_pool._accounts)):
            account = await self._account_pool.acquire()
            try:
                token = await self._credential_provider.get_valid_token(account)
                return account, token
            except RefreshTokenRevokedError as exc:
                logger.warning(
                    f"[ClaudeSub] Refresh token revoked for {account.id}, marking revoked"
                )
                await self._credential_provider.transition_state(
                    account, "revoked", "invalid_grant", pool=self._account_pool
                )
                last_err = exc
            except TokenRefreshError as exc:
                logger.warning(f"[ClaudeSub] Token refresh failed for {account.id}: {exc}")
                self._account_pool.report_failure(account.id, 401)
                last_err = exc
        raise NoHealthyAccountError(f"All accounts failed token acquisition: {last_err}")

    # ------------------------------------------------------------------
    # Header / URL / payload builders
    # ------------------------------------------------------------------

    def _build_url(self) -> str:
        """Build the Messages API URL."""
        base = (self.config.base_url or "https://api.anthropic.com").rstrip("/")
        return f"{base}/v1/messages?beta=true"

    def _build_headers(self, token: str, *, streaming: bool = False) -> dict[str, str]:
        """Build HTTP headers for subscription requests."""
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Anthropic-Version": _ANTHROPIC_VERSION,
            "Anthropic-Beta": _ANTHROPIC_BETA,
            "Anthropic-Dangerous-Direct-Browser-Access": "true",
            "X-App": "cli",
            "User-Agent": "claude-cli/2.1.63 (external, cli)",
        }
        if streaming:
            headers["Accept"] = "text/event-stream"
            headers["Accept-Encoding"] = "identity"
        else:
            headers["Accept"] = "application/json"
        return headers

    def _build_fallback_headers(self) -> dict[str, str]:
        """Build headers for paid API fallback (x-api-key auth)."""
        return {
            "x-api-key": self._fallback_api_key or "",
            "Content-Type": "application/json",
            "Anthropic-Version": _ANTHROPIC_VERSION,
            "Accept": "application/json",
        }

    @staticmethod
    def _ensure_system_prefix(payload: dict[str, Any]) -> None:
        """Ensure required Claude Code identity prefix for OAuth subscription access.

        OAuth mode also requires array-of-blocks format for system prompts.
        """
        prefix = _REQUIRED_SYSTEM_PREFIX
        system = payload.get("system")
        if isinstance(system, str):
            if system.startswith(prefix):
                payload["system"] = [{"type": "text", "text": system}]
            else:
                payload["system"] = [
                    {"type": "text", "text": prefix},
                    {"type": "text", "text": system},
                ]
        elif isinstance(system, list):
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    if not block.get("text", "").startswith(prefix):
                        block["text"] = f"{prefix}\n\n{block['text']}"
                    return
            system.insert(0, {"type": "text", "text": prefix})
        else:
            payload["system"] = [{"type": "text", "text": prefix}]

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        *,
        stream: bool = False,
        **params: Any,
    ) -> dict[str, Any]:
        """Build a Messages API request payload from Chat Completions input."""
        validated_params = self.validate_params(params)

        payload: dict[str, Any] = {
            "model": self.config.provider_model_id or self.config.id,
            "messages": convert_messages(messages),
        }
        if stream:
            payload["stream"] = True

        # System prompt — ensure Claude Code identity prefix for OAuth access
        if params.get("system"):
            payload["system"] = params["system"]
        else:
            sys_text = extract_system(messages)
            if sys_text:
                payload["system"] = sys_text
        self._ensure_system_prefix(payload)

        # Standard parameters
        payload["max_tokens"] = validated_params.get("max_tokens", self.config.max_output_length)

        if "temperature" in validated_params:
            payload["temperature"] = validated_params["temperature"]
        if "top_p" in validated_params:
            payload["top_p"] = validated_params["top_p"]
        if "stop" in validated_params:
            payload["stop_sequences"] = validated_params["stop"]

        # Tools
        if params.get("tools") and self.config.supports_tools:
            converted = convert_tools(params["tools"])
            if converted:
                payload["tools"] = converted
                if params.get("tool_choice"):
                    convert_tool_choice(params["tool_choice"], payload)

        return payload

    # ------------------------------------------------------------------
    # Non-streaming
    # ------------------------------------------------------------------

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Route a chat completion request through the Claude subscription path."""
        params.pop("stream", None)
        self._ensure_init()
        assert self._account_pool is not None
        assert self._credential_provider is not None

        try:
            return await self._subscription_chat(messages, **params)
        except NoHealthyAccountError:
            if self._fallback_api_key:
                logger.warning("[ClaudeSub] All accounts unhealthy, falling back to paid API")
                return await self._fallback_chat(messages, **params)
            raise

    async def _subscription_chat(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Execute non-streaming request via subscription account with retry on 401."""
        assert self._account_pool is not None
        assert self._credential_provider is not None

        account, token = await self._acquire_with_retry()

        payload = self._build_payload(messages, stream=False, **params)
        headers = self._build_headers(token, streaming=False)
        url = self._build_url()

        try:
            data = await self.http.json_post_with_retry(
                url, json=payload, headers=headers, timeout=None, retries=2
            )
        except aiohttp.ClientResponseError as exc:
            if exc.status == 401:
                logger.info(
                    f"[ClaudeSub] 401 for {account.id}, force-refreshing token and retrying"
                )
                try:
                    token = await self._credential_provider.get_valid_token(
                        account, force_refresh=True
                    )
                except RefreshTokenRevokedError as revoked_exc:
                    await self._credential_provider.transition_state(
                        account, "revoked", "invalid_grant", pool=self._account_pool
                    )
                    raise NoHealthyAccountError(
                        f"Account {account.id} revoked during 401 retry"
                    ) from revoked_exc
                except TokenRefreshError as refresh_exc:
                    self._account_pool.report_failure(account.id, 401)
                    raise NoHealthyAccountError(
                        f"Token refresh failed for {account.id} during 401 retry"
                    ) from refresh_exc
                headers["Authorization"] = f"Bearer {token}"
                try:
                    data = await self.http.json_post_with_retry(
                        url, json=payload, headers=headers, timeout=None, retries=2
                    )
                except aiohttp.ClientResponseError as retry_exc:
                    self._account_pool.report_failure(account.id, retry_exc.status)
                    raise
            else:
                self._account_pool.report_failure(account.id, exc.status)
                raise

        self._account_pool.report_success(account.id)

        content, tool_calls = parse_response_content(data.get("content", []))
        usage = parse_usage(data.get("usage", {}))
        finish_reason = map_stop_reason(data.get("stop_reason", "end_turn"))

        result = self.format_response(
            content=content,
            model=self.config.id,
            usage=usage,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )
        result["_routing"] = {
            "provider": "claude_sub",
            "base_url": self.config.base_url,
            "endpoint_id": getattr(self.config, "endpoint_id", None) or "claude_sub",
            "pricing": self.config.pricing,
            "account_id": account.id,
        }
        return result

    async def _fallback_chat(self, messages: list[dict[str, Any]], **params: Any) -> dict[str, Any]:
        """Fall back to direct Anthropic paid API when subscription is unavailable."""
        payload = self._build_payload(messages, stream=False, **params)
        headers = self._build_fallback_headers()
        # Same endpoint, different auth
        base = (self.config.base_url or "https://api.anthropic.com").rstrip("/")
        url = f"{base}/v1/messages"

        data = await self.http.json_post_with_retry(
            url, json=payload, headers=headers, timeout=None, retries=2
        )

        content, tool_calls = parse_response_content(data.get("content", []))
        usage = parse_usage(data.get("usage", {}))
        finish_reason = map_stop_reason(data.get("stop_reason", "end_turn"))

        result = self.format_response(
            content=content,
            model=self.config.id,
            usage=usage,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )
        result["_routing"] = {
            "provider": "anthropic",
            "base_url": f"{base}/v1/messages",
            "endpoint_id": getattr(self.config, "endpoint_id", None) or "anthropic",
            "fallback": True,
            "pricing": self.config.pricing,
        }
        return result

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        """Stream a chat completion through the Claude subscription path."""
        params.pop("stream", None)
        self._ensure_init()
        assert self._account_pool is not None
        assert self._credential_provider is not None

        try:
            async for chunk in self._subscription_stream(messages, **params):
                yield chunk
        except NoHealthyAccountError:
            if self._fallback_api_key:
                logger.warning(
                    "[ClaudeSub] All accounts unhealthy (stream), falling back to paid API"
                )
                async for chunk in self._fallback_stream(messages, **params):
                    yield chunk
            else:
                raise

    async def _subscription_stream(
        self,
        messages: list[dict[str, Any]],
        _retry_count: int = 0,
        **params: Any,
    ) -> AsyncGenerator[str, None]:
        """Stream via subscription account with bounded pre-token retry."""
        assert self._account_pool is not None
        assert self._credential_provider is not None

        max_retries = len(self._account_pool._accounts) - 1
        account, token = await self._acquire_with_retry()

        payload = self._build_payload(messages, stream=True, **params)
        headers = self._build_headers(token, streaming=True)
        url = self._build_url()

        yielded_any_content = False
        total_content = ""
        input_tokens = 0
        output_tokens = 0
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0
        finish_reason = "stop"
        accumulator = ToolCallAccumulator()

        try:
            async for line in self.http.stream_post(url, json=payload, headers=headers):
                if not line.strip():
                    continue

                # Standard Anthropic SSE: "event: <type>\ndata: <json>"
                # Our stream_post yields individual lines; data lines start with "data: "
                data_str = line
                if line.startswith("event:"):
                    continue  # skip event type lines, we parse type from the data payload
                if line.startswith("data: "):
                    data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break

                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    logger.warning(f"[ClaudeSub] Failed to parse SSE: {data_str[:100]}")
                    continue

                result = handle_stream_event(event, accumulator)

                # Track usage
                if result.input_tokens:
                    input_tokens = result.input_tokens
                if result.output_tokens:
                    output_tokens = result.output_tokens
                if result.cache_read_tokens:
                    cache_read_input_tokens = result.cache_read_tokens
                if result.cache_write_tokens:
                    cache_creation_input_tokens = result.cache_write_tokens

                # Emit text deltas
                if result.text_delta:
                    total_content += result.text_delta
                    yield self.format_stream_chunk(content=result.text_delta, model=self.config.id)
                    yielded_any_content = True

                # Update finish reason
                if result.finish_reason:
                    finish_reason = result.finish_reason

                # On message_stop, emit collected tool calls and final usage
                if result.is_done:
                    completed_tools = accumulator.get_completed()
                    if completed_tools:
                        yield self.format_tool_chunk(
                            tool_calls=completed_tools, model=self.config.id
                        )
                        yielded_any_content = True
                        finish_reason = "tool_calls"
                    break

        except aiohttp.ClientResponseError as exc:
            self._account_pool.report_failure(account.id, exc.status)
            error_class = AccountPool._classify_error(exc.status)
            if not yielded_any_content and error_class == "account" and _retry_count < max_retries:
                logger.warning(
                    f"[ClaudeSub] Stream error pre-token on {account.id} "
                    f"(status={exc.status}, class={error_class}), "
                    f"retry {_retry_count + 1}/{max_retries}"
                )
                async for chunk in self._subscription_stream(
                    messages, _retry_count=_retry_count + 1, **params
                ):
                    yield chunk
                return
            raise

        self._account_pool.report_success(account.id)

        # Emit final usage chunk
        usage_obj = build_final_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
        )

        final_chunk = {
            "id": f"chatcmpl-{int(time.time() * 1000)}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self.config.id,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "usage": usage_obj,
            "_routing": {
                "provider": "claude_sub",
                "base_url": self.config.base_url,
                "endpoint_id": getattr(self.config, "endpoint_id", None) or "claude_sub",
                "pricing": self.config.pricing,
                "account_id": account.id,
            },
        }
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield done_sentinel()

    async def _fallback_stream(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        """Stream via direct Anthropic paid API as fallback."""
        payload = self._build_payload(messages, stream=True, **params)
        headers = self._build_fallback_headers()
        headers["Accept"] = "text/event-stream"
        base = (self.config.base_url or "https://api.anthropic.com").rstrip("/")
        url = f"{base}/v1/messages"

        total_content = ""
        input_tokens = 0
        output_tokens = 0
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0
        finish_reason = "stop"
        accumulator = ToolCallAccumulator()

        async for line in self.http.stream_post(url, json=payload, headers=headers):
            if not line.strip():
                continue

            data_str = line
            if line.startswith("event:"):
                continue
            if line.startswith("data: "):
                data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break

            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            result = handle_stream_event(event, accumulator)

            if result.input_tokens:
                input_tokens = result.input_tokens
            if result.output_tokens:
                output_tokens = result.output_tokens
            if result.cache_read_tokens:
                cache_read_input_tokens = result.cache_read_tokens
            if result.cache_write_tokens:
                cache_creation_input_tokens = result.cache_write_tokens

            if result.text_delta:
                total_content += result.text_delta
                yield self.format_stream_chunk(content=result.text_delta, model=self.config.id)

            if result.finish_reason:
                finish_reason = result.finish_reason

            if result.is_done:
                completed_tools = accumulator.get_completed()
                if completed_tools:
                    yield self.format_tool_chunk(tool_calls=completed_tools, model=self.config.id)
                    finish_reason = "tool_calls"
                break

        # Final usage chunk with fallback routing
        usage_obj = build_final_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
        )

        final_chunk = {
            "id": f"chatcmpl-{int(time.time() * 1000)}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self.config.id,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "usage": usage_obj,
            "_routing": {
                "provider": "anthropic",
                "base_url": f"{base}/v1/messages",
                "endpoint_id": getattr(self.config, "endpoint_id", None) or "anthropic",
                "fallback": True,
                "pricing": self.config.pricing,
            },
        }
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield done_sentinel()
