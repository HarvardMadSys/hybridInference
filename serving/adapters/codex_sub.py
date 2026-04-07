"""Codex subscription adapter — routes requests through ChatGPT subscription accounts.

Uses OAuth-authenticated Codex Responses API with multi-account pooling,
automatic token refresh, health-aware rotation, and optional API key fallback.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiohttp

from serving.stream import done_sentinel, make_final_usage_chunk
from serving.utils.logging import get_logger

from .base import BaseAdapter, UsageInfo
from .codex_token import AccountPool, CredentialProvider, NoHealthyAccountError
from .codex_translator import translate_request, translate_response, translate_stream_event

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = get_logger(__name__)

_CODEX_RESPONSES_PATH = "/responses"
_OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"


class CodexSubscriptionAdapter(BaseAdapter):
    """Adapter that routes Chat Completions through Codex subscription accounts.

    On first request, lazily loads account credentials and initialises the
    account pool. Requests are translated from Chat Completions format to
    Codex Responses API format, sent via the subscription path, and the
    response is translated back. If all subscription accounts are unhealthy
    and a fallback API key is configured, falls back to the standard
    OpenAI API.
    """

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self._initialized = False
        self._credential_provider: CredentialProvider | None = None
        self._account_pool: AccountPool | None = None
        self._fallback_api_key: str | None = None
        self._default_session_id = str(uuid4())

    def _ensure_init(self) -> None:
        """Lazy initialisation on first request."""
        if self._initialized:
            return

        from serving.config.settings import get_settings

        settings = get_settings()

        accounts_file = settings.codex_accounts_file
        self._fallback_api_key = settings.codex_fallback_api_key or None

        provider = CredentialProvider(
            accounts_file=accounts_file,
            refresh_margin=settings.codex_token_refresh_margin,
        )
        accounts = provider.load_accounts()

        self._credential_provider = provider
        self._account_pool = AccountPool(
            accounts=accounts,
            cooldown=settings.codex_account_cooldown,
            failure_threshold=settings.codex_failure_threshold,
        )
        self._initialized = True
        logger.info(
            f"[CodexSub] Initialised with {len(accounts)} accounts, "
            f"fallback={'yes' if self._fallback_api_key else 'no'}"
        )

    def _build_headers(self, account: Any, token: str, streaming: bool = False) -> dict[str, str]:
        """Build HTTP headers for the Codex backend."""
        headers = {
            "Authorization": f"Bearer {token}",
            "ChatGPT-Account-Id": account.account_id,
            "Content-Type": "application/json",
            "Originator": "codex",
            "User-Agent": "codex-cli/0.1.0",
        }
        if streaming:
            headers["Accept"] = "text/event-stream"
        else:
            headers["Accept"] = "application/json"
        return headers

    def _get_session_id(self, params: dict[str, Any]) -> str:
        """Reuse gateway X-Session-ID if available; otherwise use default."""
        return params.get("session_id") or self._default_session_id

    def _build_url(self) -> str:
        """Build the full Codex Responses API URL."""
        base = (self.config.base_url or "").rstrip("/")
        return f"{base}{_CODEX_RESPONSES_PATH}"

    # ------------------------------------------------------------------
    # Non-streaming
    # ------------------------------------------------------------------

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Route a chat completion request through the Codex subscription path."""
        self._ensure_init()
        assert self._account_pool is not None
        assert self._credential_provider is not None

        try:
            return await self._subscription_chat(messages, **params)
        except NoHealthyAccountError:
            if self._fallback_api_key:
                logger.warning("[CodexSub] All accounts unhealthy, falling back to API key")
                return await self._fallback_chat(messages, **params)
            raise

    async def _subscription_chat(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Execute via subscription account with one retry on 401.

        The Codex backend requires ``stream: true`` for all requests, so we
        stream the SSE response and collect the ``response.completed`` event
        which contains the full response payload.
        """
        assert self._account_pool is not None
        assert self._credential_provider is not None

        account = await self._account_pool.acquire()
        token = await self._credential_provider.get_valid_token(account)

        chat_params = {k: v for k, v in params.items() if k != "stream"}
        body = translate_request(messages, self.config.id, **chat_params)
        session_id = self._get_session_id(params)
        body["prompt_cache_key"] = session_id

        headers = self._build_headers(account, token, streaming=True)
        headers["session_id"] = session_id
        url = self._build_url()

        logger.debug(f"[CodexSub] POST {url} body={json.dumps(body)[:500]}")
        logger.debug(
            f"[CodexSub] headers={{{', '.join(f'{k}: {v[:20]}...' if len(str(v)) > 20 else f'{k}: {v}' for k, v in headers.items())}}}"
        )

        try:
            completed_response = await self._collect_stream(url, body, headers)
        except aiohttp.ClientResponseError as exc:
            error_body = getattr(exc, "error_body", "")
            logger.error(f"[CodexSub] HTTP {exc.status} from codex: {error_body[:500]}")
            if exc.status == 401:
                logger.info(f"[CodexSub] 401 for {account.id}, force-refreshing token and retrying")
                token = await self._credential_provider.get_valid_token(account, force_refresh=True)
                headers["Authorization"] = f"Bearer {token}"
                try:
                    completed_response = await self._collect_stream(url, body, headers)
                except aiohttp.ClientResponseError as retry_exc:
                    self._account_pool.report_failure(account.id, retry_exc.status)
                    raise
            else:
                self._account_pool.report_failure(account.id, exc.status)
                raise

        self._account_pool.report_success(account.id)
        translated = translate_response(completed_response, self.config.id)
        translated["_routing"] = {
            "provider": "codex_sub",
            "base_url": self.config.base_url,
            "endpoint_id": getattr(self.config, "endpoint_id", None) or "codex_sub",
            "account_id": account.id,
        }
        return translated

    async def _collect_stream(
        self, url: str, body: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        """Stream SSE and return the full response from the ``response.completed`` event."""
        completed: dict[str, Any] | None = None
        async for line in self.http.stream_post(url, json=body, headers=headers):
            if not line.strip():
                continue
            data_str = line[6:] if line.startswith("data: ") else line
            if data_str.strip() == "[DONE]":
                break
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "response.completed":
                completed = event.get("response", {})
                break
        if completed is None:
            raise RuntimeError("Codex stream ended without response.completed event")
        return completed

    async def _fallback_chat(self, messages: list[dict[str, Any]], **params: Any) -> dict[str, Any]:
        """Fall back to standard OpenAI API when subscription is unavailable."""
        validated = self.validate_params(params)
        reasoning_effort = params.get("reasoning_effort")
        if reasoning_effort and reasoning_effort != "none":
            validated["reasoning_effort"] = reasoning_effort
            validated.pop("temperature", None)
        payload: dict[str, Any] = {
            "model": self.config.provider_model_id or self.config.id,
            "messages": messages,
            **validated,
        }
        if params.get("tools"):
            payload["tools"] = params["tools"]
        if params.get("tool_choice") is not None:
            payload["tool_choice"] = params["tool_choice"]

        headers = {
            "Authorization": f"Bearer {self._fallback_api_key}",
            "Content-Type": "application/json",
        }

        resp = await self.http.json_post(
            _OPENAI_CHAT_URL,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=120),
        )

        # Parse into standard format
        choice = resp["choices"][0]
        message = choice["message"]
        usage_data = resp.get("usage", {})

        usage = UsageInfo(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
        )

        result = self.format_response(
            content=message.get("content", ""),
            model=self.config.id,
            usage=usage,
            tool_calls=message.get("tool_calls"),
            finish_reason=choice.get("finish_reason", "stop"),
        )
        result["_routing"] = {
            "provider": "openai",
            "base_url": _OPENAI_CHAT_URL,
            "endpoint_id": getattr(self.config, "endpoint_id", None) or "openai",
            "fallback": True,
            "pricing": {
                "prompt": "2.50",
                "completion": "10.00",
            },
        }
        return result

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        """Stream a chat completion through the Codex subscription path."""
        self._ensure_init()
        assert self._account_pool is not None
        assert self._credential_provider is not None

        try:
            async for chunk in self._subscription_stream(messages, **params):
                yield chunk
        except NoHealthyAccountError:
            if self._fallback_api_key:
                logger.warning(
                    "[CodexSub] All accounts unhealthy (stream), falling back to API key"
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
        account = await self._account_pool.acquire()
        token = await self._credential_provider.get_valid_token(account)

        stream_params = {k: v for k, v in params.items() if k != "stream"}
        body = translate_request(messages, self.config.id, **stream_params)
        session_id = self._get_session_id(params)
        body["prompt_cache_key"] = session_id

        headers = self._build_headers(account, token, streaming=True)
        headers["session_id"] = session_id
        url = self._build_url()

        logger.debug(f"[CodexSub] Stream POST {url} body={json.dumps(body)[:500]}")

        yielded_any_content = False
        total_content = ""
        finish_reason = "stop"
        usage_data: dict[str, Any] | None = None

        try:
            async for line in self.http.stream_post(url, json=body, headers=headers):
                if not line.strip():
                    continue

                data_str = line[6:] if line.startswith("data: ") else line
                if data_str.strip() == "[DONE]":
                    break

                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    logger.warning(f"[CodexSub] Failed to parse SSE: {data_str[:100]}")
                    continue

                content_delta, tool_delta, is_done = translate_stream_event(event)

                if content_delta:
                    total_content += content_delta
                    yield self.format_stream_chunk(content=content_delta, model=self.config.id)
                    yielded_any_content = True

                if tool_delta:
                    yield self.format_tool_chunk(tool_calls=[tool_delta], model=self.config.id)
                    yielded_any_content = True

                if is_done:
                    # Extract usage from completed event
                    resp_data = event.get("response", {})
                    raw_usage = resp_data.get("usage", {})
                    if raw_usage:
                        usage_data = {
                            "prompt_tokens": raw_usage.get("input_tokens", 0),
                            "completion_tokens": raw_usage.get("output_tokens", 0),
                            "total_tokens": raw_usage.get("input_tokens", 0)
                            + raw_usage.get("output_tokens", 0),
                        }
                    stop_reason = resp_data.get("stop_reason", "stop")
                    if stop_reason == "max_output_tokens":
                        finish_reason = "length"
                    elif stop_reason == "tool_use":
                        finish_reason = "tool_calls"
                    break

        except aiohttp.ClientResponseError as exc:
            error_body = getattr(exc, "error_body", "")
            logger.error(f"[CodexSub] Stream HTTP {exc.status} from codex: {error_body[:500]}")
            self._account_pool.report_failure(account.id, exc.status)
            error_class = AccountPool._classify_error(exc.status)
            if not yielded_any_content and error_class == "account" and _retry_count < max_retries:
                # Pre-first-token account-level error (401/403/429):
                # try a different account (bounded to one attempt per account)
                logger.warning(
                    f"[CodexSub] Stream error pre-token on {account.id} "
                    f"(status={exc.status}, class={error_class}), "
                    f"retry {_retry_count + 1}/{max_retries}"
                )
                async for chunk in self._subscription_stream(
                    messages, _retry_count=_retry_count + 1, **params
                ):
                    yield chunk
                return
            else:
                # Non-account error (400/5xx), post-first-token, or
                # retries exhausted: propagate immediately
                raise

        self._account_pool.report_success(account.id)

        # Emit final usage chunk
        final_chunk = make_final_usage_chunk(
            model=self.config.id,
            messages=messages,
            total_content=total_content,
            prompt_tokens_override=usage_data.get("prompt_tokens") if usage_data else None,
            completion_tokens_override=usage_data.get("completion_tokens") if usage_data else None,
            finish_reason=finish_reason,
            provider="codex_sub",
            base_url=self.config.base_url,
        )
        yield final_chunk
        yield done_sentinel()

    async def _fallback_stream(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        """Stream via standard OpenAI API as fallback."""
        validated = self.validate_params(params)
        reasoning_effort = params.get("reasoning_effort")
        if reasoning_effort and reasoning_effort != "none":
            validated["reasoning_effort"] = reasoning_effort
            validated.pop("temperature", None)
        payload: dict[str, Any] = {
            "model": self.config.provider_model_id or self.config.id,
            "messages": messages,
            "stream": True,
            **validated,
        }
        if params.get("tools"):
            payload["tools"] = params["tools"]
        if params.get("tool_choice") is not None:
            payload["tool_choice"] = params["tool_choice"]

        headers = {
            "Authorization": f"Bearer {self._fallback_api_key}",
            "Content-Type": "application/json",
        }

        total_content = ""
        finish_reason = "stop"
        upstream_usage: dict[str, Any] | None = None

        async for line in self.http.stream_post(_OPENAI_CHAT_URL, json=payload, headers=headers):
            if not line.strip():
                continue

            data_str = line[6:] if line.startswith("data: ") else line
            if data_str.strip() == "[DONE]":
                break

            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            if data.get("usage"):
                upstream_usage = data["usage"]

            choices = data.get("choices", [])
            if not choices:
                continue

            delta = choices[0].get("delta", {})
            fr = choices[0].get("finish_reason")
            if fr:
                finish_reason = fr

            content = delta.get("content")
            if content:
                total_content += content
                yield self.format_stream_chunk(content=content, model=self.config.id)

            tool_calls = delta.get("tool_calls")
            if tool_calls:
                yield self.format_tool_chunk(tool_calls=tool_calls, model=self.config.id)

        # Final usage chunk with fallback routing info
        final_chunk_data = {
            "id": f"chatcmpl-{int(time.time() * 1000)}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self.config.id,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "usage": upstream_usage
            or {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            "_routing": {
                "provider": "openai",
                "base_url": _OPENAI_CHAT_URL,
                "endpoint_id": getattr(self.config, "endpoint_id", None) or "openai",
                "fallback": True,
                "pricing": {
                    "prompt": "2.50",
                    "completion": "10.00",
                },
            },
        }
        yield f"data: {json.dumps(final_chunk_data)}\n\n"
        yield done_sentinel()
