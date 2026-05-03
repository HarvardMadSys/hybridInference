"""Tests for ClaudeSubscriptionAdapter."""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.claude_sub import ClaudeSubscriptionAdapter
from serving.adapters.claude_token import (
    ClaudeAccountCredential,
    RefreshTokenRevokedError,
    RefreshTokenTransientError,
)
from serving.adapters.codex_token import AccountPool, NoHealthyAccountError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> ModelConfig:
    defaults = {
        "id": "claude-sonnet-4.6",
        "name": "Claude Sonnet 4.6",
        "provider": "claude_sub",
        "base_url": "https://api.anthropic.com",
        "provider_model_id": "claude-sonnet-4-6",
        "supports_tools": True,
    }
    defaults.update(overrides)
    return ModelConfig(**defaults)


def _make_account(id: str = "acct_01") -> ClaudeAccountCredential:
    return ClaudeAccountCredential(
        id=id,
        label=f"test-{id}",
        access_token=f"sk-ant-oat-{id}",
        refresh_token=f"refresh_{id}",
        expires_at=int(time.time() * 1000) + 3600_000,
        organization_id=f"org-uuid-{id}",
        email=f"{id}@test.com",
        plan="max",
    )


def _make_adapter_initialized(
    accounts: list[ClaudeAccountCredential] | None = None,
    fallback_key: str | None = None,
) -> ClaudeSubscriptionAdapter:
    """Create an adapter with mocked internals, skipping lazy init."""
    config = _make_config()
    adapter = ClaudeSubscriptionAdapter(config)

    if accounts is None:
        accounts = [_make_account()]

    adapter._initialized = True
    adapter._credential_provider = MagicMock()
    adapter._credential_provider.get_valid_token = AsyncMock(
        side_effect=lambda acct, **kwargs: acct.access_token
    )
    adapter._account_pool = AccountPool(accounts)
    adapter._fallback_api_key = fallback_key
    adapter.http = MagicMock()

    return adapter


# ---------------------------------------------------------------------------
# Helpers for Anthropic Messages API mocking
# ---------------------------------------------------------------------------


def _claude_response(
    text: str = "Hello!",
    stop_reason: str = "end_turn",
    input_tokens: int = 10,
    output_tokens: int = 5,
    tool_use: list[dict] | None = None,
) -> dict[str, Any]:
    """Build a standard Claude non-streaming response."""
    content = [{"type": "text", "text": text}]
    if tool_use:
        content.extend(tool_use)
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "content": content,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def _claude_stream_events(
    text: str = "Hello world",
    stop_reason: str = "end_turn",
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_read: int = 0,
    cache_write: int = 0,
) -> list[str]:
    """Build standard Claude SSE event lines for streaming."""
    message_start_usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": 0,
    }
    if cache_read:
        message_start_usage["cache_read_input_tokens"] = cache_read
    if cache_write:
        message_start_usage["cache_creation_input_tokens"] = cache_write

    events = [
        f"data: {json.dumps({'type': 'message_start', 'message': {'id': 'msg_1', 'role': 'assistant', 'model': 'claude-sonnet-4-6-20250514', 'usage': message_start_usage}})}",
        f"data: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}",
    ]
    # Split text into word-level deltas
    for word in text.split():
        events.append(
            f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': word + ' '}})}"
        )
    events.extend(
        [
            f"data: {json.dumps({'type': 'content_block_stop', 'index': 0})}",
            f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason}, 'usage': {'output_tokens': output_tokens}})}",
            f"data: {json.dumps({'type': 'message_stop'})}",
        ]
    )
    return events


def _claude_stream_with_tools() -> list[str]:
    """Build Claude SSE events that include a tool_use block."""
    return [
        f"data: {json.dumps({'type': 'message_start', 'message': {'id': 'msg_1', 'role': 'assistant', 'model': 'claude-sonnet-4-6-20250514', 'usage': {'input_tokens': 10, 'output_tokens': 0}}})}",
        f"data: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}",
        f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Let me help.'}})}",
        f"data: {json.dumps({'type': 'content_block_stop', 'index': 0})}",
        f"data: {json.dumps({'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': 'toolu_1', 'name': 'get_weather', 'input': {}}})}",
        "data: {}".format(
            json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '{"city":'},
                }
            )
        ),
        "data: {}".format(
            json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '"SF"}'},
                }
            )
        ),
        f"data: {json.dumps({'type': 'content_block_stop', 'index': 1})}",
        f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 15}})}",
        f"data: {json.dumps({'type': 'message_stop'})}",
    ]


# ---------------------------------------------------------------------------
# Non-streaming tests
# ---------------------------------------------------------------------------


class TestChatCompletion:
    @pytest.mark.asyncio
    async def test_basic(self):
        adapter = _make_adapter_initialized()

        response = _claude_response(text="Hello!")
        adapter.http.json_post_with_retry = AsyncMock(return_value=response)

        result = await adapter.chat_completion([{"role": "user", "content": "Hi"}])

        assert result["choices"][0]["message"]["content"] == "Hello!"
        assert result["_routing"]["provider"] == "claude_sub"
        assert result["usage"]["prompt_tokens"] == 10
        assert result["usage"]["completion_tokens"] == 5

    @pytest.mark.asyncio
    async def test_tool_use_response(self):
        adapter = _make_adapter_initialized()

        tool_use = [
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "get_weather",
                "input": {"city": "SF"},
            }
        ]
        response = _claude_response(text="", stop_reason="tool_use", tool_use=tool_use)
        adapter.http.json_post_with_retry = AsyncMock(return_value=response)

        result = await adapter.chat_completion([{"role": "user", "content": "weather?"}])

        assert result["choices"][0]["finish_reason"] == "tool_calls"
        tool_calls = result["choices"][0]["message"]["tool_calls"]
        assert tool_calls is not None
        assert tool_calls[0]["function"]["name"] == "get_weather"

    @pytest.mark.asyncio
    async def test_401_retry(self):
        adapter = _make_adapter_initialized()

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise aiohttp.ClientResponseError(
                    request_info=MagicMock(), history=(), status=401, message="Unauthorized"
                )
            return _claude_response(text="OK")

        adapter.http.json_post_with_retry = AsyncMock(side_effect=mock_post)

        result = await adapter.chat_completion([{"role": "user", "content": "test"}])
        assert result["choices"][0]["message"]["content"] == "OK"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_401_retry_uses_force_refresh(self):
        adapter = _make_adapter_initialized()

        call_args_list = []

        async def tracking_get_valid_token(acct, **kwargs):
            call_args_list.append(kwargs)
            return acct.access_token

        adapter._credential_provider.get_valid_token = AsyncMock(
            side_effect=tracking_get_valid_token
        )

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise aiohttp.ClientResponseError(
                    request_info=MagicMock(), history=(), status=401, message="Unauthorized"
                )
            return _claude_response(text="OK")

        adapter.http.json_post_with_retry = AsyncMock(side_effect=mock_post)

        await adapter.chat_completion([{"role": "user", "content": "test"}])

        assert len(call_args_list) == 2
        assert call_args_list[0].get("force_refresh") is not True
        assert call_args_list[1].get("force_refresh") is True

    @pytest.mark.asyncio
    async def test_401_force_refresh_revoked_marks_account(self):
        """If force_refresh on 401 hits invalid_grant, account is revoked."""
        adapter = _make_adapter_initialized()

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise aiohttp.ClientResponseError(
                request_info=MagicMock(), history=(), status=401, message="Unauthorized"
            )

        adapter.http.json_post_with_retry = AsyncMock(side_effect=mock_post)

        # First get_valid_token succeeds, second (force_refresh) raises revoked
        token_call_count = 0

        async def mock_get_token(acct, **kwargs):
            nonlocal token_call_count
            token_call_count += 1
            if kwargs.get("force_refresh"):
                raise RefreshTokenRevokedError(acct.id, "invalid_grant")
            return acct.access_token

        adapter._credential_provider.get_valid_token = AsyncMock(side_effect=mock_get_token)
        adapter._credential_provider.transition_state = AsyncMock()

        with pytest.raises(NoHealthyAccountError):
            await adapter.chat_completion([{"role": "user", "content": "test"}])

        # Should have marked account as revoked
        adapter._credential_provider.transition_state.assert_called_once()
        args = adapter._credential_provider.transition_state.call_args
        assert args[0][1] == "revoked"

    @pytest.mark.asyncio
    async def test_fallback_on_no_healthy_accounts(self):
        acct = _make_account()
        adapter = _make_adapter_initialized(accounts=[acct], fallback_key="sk-ant-paid")
        adapter._account_pool.report_failure(acct.id, 429)

        response = _claude_response(text="Fallback response")
        adapter.http.json_post_with_retry = AsyncMock(return_value=response)

        result = await adapter.chat_completion([{"role": "user", "content": "test"}])

        assert result["choices"][0]["message"]["content"] == "Fallback response"
        assert result["_routing"]["provider"] == "anthropic"
        assert result["_routing"]["fallback"] is True

    @pytest.mark.asyncio
    async def test_no_fallback_raises(self):
        acct = _make_account()
        adapter = _make_adapter_initialized(accounts=[acct], fallback_key=None)
        adapter._account_pool.report_failure(acct.id, 429)

        with pytest.raises(NoHealthyAccountError):
            await adapter.chat_completion([{"role": "user", "content": "test"}])


# ---------------------------------------------------------------------------
# Streaming tests
# ---------------------------------------------------------------------------


class TestStreamChatCompletion:
    @pytest.mark.asyncio
    async def test_basic_stream(self):
        adapter = _make_adapter_initialized()

        events = _claude_stream_events(text="Hello world")

        async def mock_stream(*args, **kwargs):
            for line in events:
                yield line

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "Hi"}]):
            chunks.append(chunk)

        # Should have text deltas + final usage + [DONE]
        assert len(chunks) >= 3
        # Last chunk should be [DONE]
        assert chunks[-1].strip() == "data: [DONE]"
        # Second-to-last should have usage
        final = json.loads(chunks[-2][6:])
        assert "usage" in final
        assert final["_routing"]["provider"] == "claude_sub"

    @pytest.mark.asyncio
    async def test_stream_with_tools(self):
        adapter = _make_adapter_initialized()

        events = _claude_stream_with_tools()

        async def mock_stream(*args, **kwargs):
            for line in events:
                yield line

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
            chunks.append(chunk)

        # Should contain tool call data
        tool_chunk_found = False
        for c in chunks:
            if c.startswith("data: ") and c.strip() != "data: [DONE]":
                data = json.loads(c[6:])
                tc = data.get("choices", [{}])[0].get("delta", {}).get("tool_calls")
                if tc:
                    tool_chunk_found = True
                    assert tc[0]["function"]["name"] == "get_weather"
                    assert '"city"' in tc[0]["function"]["arguments"]
        assert tool_chunk_found

        # Final chunk should have finish_reason=tool_calls
        final = json.loads(chunks[-2][6:])
        assert final["choices"][0]["finish_reason"] == "tool_calls"

    @pytest.mark.asyncio
    async def test_stream_pre_token_retry_account_error(self):
        acct_a = _make_account("a")
        acct_b = _make_account("b")
        adapter = _make_adapter_initialized(accounts=[acct_a, acct_b])

        call_count = 0

        async def mock_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise aiohttp.ClientResponseError(
                    request_info=MagicMock(), history=(), status=429, message="Rate Limited"
                )
            for line in _claude_stream_events(text="OK"):
                yield line

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
            chunks.append(chunk)

        assert any("OK" in c for c in chunks)
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_stream_no_retry_on_client_error(self):
        acct_a = _make_account("a")
        acct_b = _make_account("b")
        adapter = _make_adapter_initialized(accounts=[acct_a, acct_b])

        call_count = 0

        async def mock_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise aiohttp.ClientResponseError(
                request_info=MagicMock(), history=(), status=400, message="Bad Request"
            )
            yield  # unreachable

        adapter.http.stream_post = mock_stream

        with pytest.raises(aiohttp.ClientResponseError) as exc_info:
            async for _ in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
                pass

        assert exc_info.value.status == 400
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_stream_no_retry_on_upstream_error(self):
        acct_a = _make_account("a")
        acct_b = _make_account("b")
        adapter = _make_adapter_initialized(accounts=[acct_a, acct_b])

        call_count = 0

        async def mock_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise aiohttp.ClientResponseError(
                request_info=MagicMock(), history=(), status=500, message="Server Error"
            )
            yield  # unreachable

        adapter.http.stream_post = mock_stream

        with pytest.raises(aiohttp.ClientResponseError) as exc_info:
            async for _ in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
                pass

        assert exc_info.value.status == 500
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_stream_single_account_no_retry(self):
        acct = _make_account("a")
        adapter = _make_adapter_initialized(accounts=[acct])

        call_count = 0

        async def mock_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise aiohttp.ClientResponseError(
                request_info=MagicMock(), history=(), status=401, message="Unauthorized"
            )
            yield  # unreachable

        adapter.http.stream_post = mock_stream

        with pytest.raises(aiohttp.ClientResponseError) as exc_info:
            async for _ in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
                pass

        assert exc_info.value.status == 401
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_fallback_stream(self):
        acct = _make_account()
        adapter = _make_adapter_initialized(accounts=[acct], fallback_key="sk-ant-paid")
        adapter._account_pool.report_failure(acct.id, 429)

        events = _claude_stream_events(text="Fallback")

        async def mock_stream(*args, **kwargs):
            for line in events:
                yield line

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
            chunks.append(chunk)

        assert len(chunks) >= 2
        # Final chunk should have fallback routing
        final = json.loads(chunks[-2][6:])
        assert final["_routing"]["provider"] == "anthropic"
        assert final["_routing"]["fallback"] is True

    @pytest.mark.asyncio
    async def test_stream_with_cache_tokens(self):
        """Verify cache tokens flow through handle_stream_event into final usage."""
        adapter = _make_adapter_initialized()

        events = _claude_stream_events(
            text="Cached reply",
            input_tokens=400,
            output_tokens=20,
            cache_read=150,
            cache_write=80,
        )

        async def mock_stream(*args, **kwargs):
            for line in events:
                yield line

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
            chunks.append(chunk)

        # Final usage chunk (before [DONE])
        final = json.loads(chunks[-2][6:])
        usage = final["usage"]
        # Cache-inclusive prompt_tokens: 400 + 150 + 80
        assert usage["prompt_tokens"] == 630
        assert usage["completion_tokens"] == 20
        assert usage["total_tokens"] == 650  # 630 + 20
        assert usage["cache_read_tokens"] == 150
        assert usage["cache_write_tokens"] == 80


# ---------------------------------------------------------------------------
# Payload building
# ---------------------------------------------------------------------------


class TestBuildPayload:
    def test_basic_payload(self):
        adapter = _make_adapter_initialized()
        messages = [{"role": "user", "content": "Hello"}]

        payload = adapter._build_payload(messages, stream=False)

        assert payload["model"] == "claude-sonnet-4-6"
        assert payload["messages"] is not None
        assert "stream" not in payload

    def test_streaming_payload(self):
        adapter = _make_adapter_initialized()
        messages = [{"role": "user", "content": "Hello"}]

        payload = adapter._build_payload(messages, stream=True)

        assert payload["stream"] is True

    def test_system_extraction(self):
        adapter = _make_adapter_initialized()
        messages = [
            {"role": "system", "content": "Be helpful"},
            {"role": "user", "content": "Hello"},
        ]

        payload = adapter._build_payload(messages)

        from serving.adapters.claude_sub import _REQUIRED_SYSTEM_PREFIX

        # System should be array format with prefix + user system
        assert isinstance(payload["system"], list)
        assert payload["system"][0]["text"] == _REQUIRED_SYSTEM_PREFIX
        assert payload["system"][1]["text"] == "Be helpful"

    def test_tools_conversion(self):
        adapter = _make_adapter_initialized()
        messages = [{"role": "user", "content": "Hello"}]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        payload = adapter._build_payload(messages, tools=tools)

        assert len(payload["tools"]) == 1
        assert payload["tools"][0]["name"] == "get_weather"
        assert "input_schema" in payload["tools"][0]

    def test_tool_choice_conversion(self):
        adapter = _make_adapter_initialized()
        messages = [{"role": "user", "content": "Hello"}]
        tools = [
            {
                "type": "function",
                "function": {"name": "get_weather", "description": "d", "parameters": {}},
            }
        ]

        payload = adapter._build_payload(messages, tools=tools, tool_choice="required")

        assert payload["tool_choice"] == {"type": "any"}


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


class TestHeaders:
    def test_subscription_headers(self):
        adapter = _make_adapter_initialized()
        headers = adapter._build_headers("sk-ant-oat-test", streaming=False)

        assert headers["Authorization"] == "Bearer sk-ant-oat-test"
        assert "Anthropic-Version" in headers
        assert "Anthropic-Beta" in headers
        assert headers["Accept"] == "application/json"

    def test_streaming_headers(self):
        adapter = _make_adapter_initialized()
        headers = adapter._build_headers("sk-ant-oat-test", streaming=True)

        assert headers["Accept"] == "text/event-stream"
        assert headers["Accept-Encoding"] == "identity"

    def test_fallback_headers(self):
        adapter = _make_adapter_initialized(fallback_key="sk-ant-paid")
        headers = adapter._build_fallback_headers()

        assert headers["x-api-key"] == "sk-ant-paid"
        assert "Authorization" not in headers
        assert "Anthropic-Version" in headers


# ---------------------------------------------------------------------------
# _acquire_with_retry tests
# ---------------------------------------------------------------------------


class TestAcquireWithRetry:
    @pytest.mark.asyncio
    async def test_returns_account_and_token_on_success(self):
        adapter = _make_adapter_initialized()

        account, token = await adapter._acquire_with_retry()

        assert account.id == "acct_01"
        assert token == account.access_token

    @pytest.mark.asyncio
    async def test_skips_revoked_account(self):
        acct_a = _make_account("a")
        acct_b = _make_account("b")
        adapter = _make_adapter_initialized(accounts=[acct_a, acct_b])

        call_count = 0

        async def mock_get_valid_token(acct, **kwargs):
            nonlocal call_count
            call_count += 1
            if acct.id == "a":
                raise RefreshTokenRevokedError("a", "invalid_grant")
            return acct.access_token

        adapter._credential_provider.get_valid_token = AsyncMock(side_effect=mock_get_valid_token)
        adapter._credential_provider.transition_state = AsyncMock()

        account, token = await adapter._acquire_with_retry()

        assert account.id == "b"
        assert token == acct_b.access_token
        adapter._credential_provider.transition_state.assert_called_once()
        call_args = adapter._credential_provider.transition_state.call_args
        assert call_args[0][1] == "revoked"  # new_state
        assert call_args[0][2] == "invalid_grant"  # reason

    @pytest.mark.asyncio
    async def test_skips_transient_failure(self):
        acct_a = _make_account("a")
        acct_b = _make_account("b")
        adapter = _make_adapter_initialized(accounts=[acct_a, acct_b])

        call_count = 0

        async def mock_get_valid_token(acct, **kwargs):
            nonlocal call_count
            call_count += 1
            if acct.id == "a":
                raise RefreshTokenTransientError("a", 500, "server error")
            return acct.access_token

        adapter._credential_provider.get_valid_token = AsyncMock(side_effect=mock_get_valid_token)

        account, token = await adapter._acquire_with_retry()

        assert account.id == "b"
        assert token == acct_b.access_token

    @pytest.mark.asyncio
    async def test_all_accounts_fail_raises(self):
        acct_a = _make_account("a")
        adapter = _make_adapter_initialized(accounts=[acct_a])

        adapter._credential_provider.get_valid_token = AsyncMock(
            side_effect=RefreshTokenTransientError("a", 500, "fail")
        )

        with pytest.raises(NoHealthyAccountError):
            await adapter._acquire_with_retry()
