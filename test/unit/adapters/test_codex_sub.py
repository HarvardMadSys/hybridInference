"""Tests for CodexSubscriptionAdapter."""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.codex_sub import CodexSubscriptionAdapter
from serving.adapters.codex_token import AccountCredential, AccountPool, NoHealthyAccountError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> ModelConfig:
    defaults = {
        "id": "gpt-5.1-codex",
        "name": "GPT-5.1 Codex",
        "provider": "codex_sub",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "supports_tools": True,
    }
    defaults.update(overrides)
    return ModelConfig(**defaults)


def _make_account(id: str = "acct_01") -> AccountCredential:
    return AccountCredential(
        id=id,
        label=f"test-{id}",
        access_token=f"token_{id}",
        refresh_token=f"refresh_{id}",
        expires_at=int(time.time() * 1000) + 3600_000,
        account_id=f"org_{id}",
    )


def _mock_settings(**overrides):
    defaults = {
        "codex_accounts_file": "/tmp/test_accounts.json",
        "codex_fallback_api_key": "",
        "codex_token_refresh_margin": 30,
        "codex_account_cooldown": 60,
        "codex_failure_threshold": 3,
    }
    defaults.update(overrides)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


def _make_adapter_initialized(
    accounts: list[AccountCredential] | None = None,
    fallback_key: str | None = None,
) -> CodexSubscriptionAdapter:
    """Create an adapter with mocked internals, skipping lazy init."""
    config = _make_config()
    adapter = CodexSubscriptionAdapter(config)

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
# Helpers for non-streaming tests (Codex backend requires stream=true,
# so non-streaming chat_completion uses _collect_stream internally)
# ---------------------------------------------------------------------------


def _mock_completed_stream(codex_response: dict[str, Any]):
    """Return a mock stream_post that yields a response.completed SSE event."""
    line = f"data: {json.dumps({'type': 'response.completed', 'response': codex_response})}"

    async def stream(*args, **kwargs):
        yield line

    return stream


def _mock_401_then_completed_stream(codex_response: dict[str, Any]):
    """First call raises 401; second call yields response.completed."""
    call_count = 0

    async def stream(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=401,
                message="Unauthorized",
            )
        yield f"data: {json.dumps({'type': 'response.completed', 'response': codex_response})}"

    return stream


# ---------------------------------------------------------------------------
# Non-streaming tests
# ---------------------------------------------------------------------------


class TestChatCompletion:
    @pytest.mark.asyncio
    async def test_basic(self):
        adapter = _make_adapter_initialized()

        codex_response = {
            "id": "resp_1",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Hello!"}],
                }
            ],
            "stop_reason": "stop",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        adapter.http.stream_post = _mock_completed_stream(codex_response)

        messages = [{"role": "user", "content": "Hi"}]
        result = await adapter.chat_completion(messages)

        assert result["choices"][0]["message"]["content"] == "Hello!"
        assert result["_routing"]["provider"] == "codex_sub"
        assert result["usage"]["prompt_tokens"] == 10
        assert result["usage"]["completion_tokens"] == 5

    @pytest.mark.asyncio
    async def test_401_retry(self):
        adapter = _make_adapter_initialized()

        good_response = {
            "id": "resp_2",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
            "stop_reason": "stop",
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }
        adapter.http.stream_post = _mock_401_then_completed_stream(good_response)

        result = await adapter.chat_completion([{"role": "user", "content": "test"}])
        assert result["choices"][0]["message"]["content"] == "OK"

    @pytest.mark.asyncio
    async def test_401_retry_uses_force_refresh(self):
        """401 handler must call get_valid_token with force_refresh=True."""
        adapter = _make_adapter_initialized()

        good_response = {
            "id": "resp_3",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
            "stop_reason": "stop",
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }
        adapter.http.stream_post = _mock_401_then_completed_stream(good_response)

        # Track calls to get_valid_token
        call_args_list = []

        async def tracking_get_valid_token(acct, **kwargs):
            call_args_list.append(kwargs)
            return acct.access_token

        adapter._credential_provider.get_valid_token = AsyncMock(
            side_effect=tracking_get_valid_token
        )

        result = await adapter.chat_completion([{"role": "user", "content": "test"}])
        assert result["choices"][0]["message"]["content"] == "OK"

        # First call: normal (no force_refresh)
        # Second call: force_refresh=True after 401
        assert len(call_args_list) == 2
        assert call_args_list[0].get("force_refresh") is not True
        assert call_args_list[1].get("force_refresh") is True

    @pytest.mark.asyncio
    async def test_fallback_on_no_healthy_accounts(self):
        acct = _make_account()
        adapter = _make_adapter_initialized(accounts=[acct], fallback_key="sk-fallback")

        # Mark account unhealthy
        adapter._account_pool.report_failure(acct.id, 429)

        # Mock OpenAI fallback response
        openai_response = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Fallback response"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        adapter.http.json_post = AsyncMock(return_value=openai_response)

        result = await adapter.chat_completion([{"role": "user", "content": "test"}])

        assert result["choices"][0]["message"]["content"] == "Fallback response"
        assert result["_routing"]["provider"] == "openai"
        assert result["_routing"]["fallback"] is True

    @pytest.mark.asyncio
    async def test_no_fallback_raises(self):
        acct = _make_account()
        adapter = _make_adapter_initialized(accounts=[acct], fallback_key=None)

        # Mark account unhealthy
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

        sse_lines = [
            'data: {"type": "response.output_text.delta", "delta": "Hello"}',
            'data: {"type": "response.output_text.delta", "delta": " world"}',
            'data: {"type": "response.completed", "response": {"stop_reason": "stop", "usage": {"input_tokens": 10, "output_tokens": 5}}}',
        ]

        async def mock_stream(*args, **kwargs):
            for line in sse_lines:
                yield line

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "Hi"}]):
            chunks.append(chunk)

        # Should have content chunks + final usage + [DONE]
        assert len(chunks) >= 3
        # First two chunks should contain content deltas
        first_data = json.loads(chunks[0][6:])
        assert first_data["choices"][0]["delta"]["content"] == "Hello"
        second_data = json.loads(chunks[1][6:])
        assert second_data["choices"][0]["delta"]["content"] == " world"
        # Last chunk should be [DONE]
        assert chunks[-1].strip() == "data: [DONE]"

    @pytest.mark.asyncio
    async def test_stream_pre_token_retry_account_error(self):
        """Account-level error (429) before any yield should retry on different account."""
        acct_a = _make_account("a")
        acct_b = _make_account("b")
        adapter = _make_adapter_initialized(accounts=[acct_a, acct_b])

        call_count = 0

        async def mock_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call fails with account-level error (429)
                raise aiohttp.ClientResponseError(
                    request_info=MagicMock(),
                    history=(),
                    status=429,
                    message="Rate Limited",
                )
            # Second call succeeds
            yield 'data: {"type": "response.output_text.delta", "delta": "OK"}'
            yield 'data: {"type": "response.completed", "response": {"stop_reason": "stop", "usage": {"input_tokens": 1, "output_tokens": 1}}}'

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
            chunks.append(chunk)

        # Should have content from retry + usage + [DONE]
        assert any("OK" in c for c in chunks)
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_stream_no_retry_on_client_error(self):
        """400 client error should NOT retry on different account — propagate immediately."""
        acct_a = _make_account("a")
        acct_b = _make_account("b")
        acct_c = _make_account("c")
        adapter = _make_adapter_initialized(accounts=[acct_a, acct_b, acct_c])

        call_count = 0

        async def mock_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=400,
                message="Bad Request",
            )
            yield  # unreachable; makes this an async generator

        adapter.http.stream_post = mock_stream

        with pytest.raises(aiohttp.ClientResponseError) as exc_info:
            async for _ in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
                pass

        assert exc_info.value.status == 400
        # Should only have been called once — no retry on client error
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_stream_no_retry_on_upstream_error(self):
        """500 upstream error should NOT retry on different account."""
        acct_a = _make_account("a")
        acct_b = _make_account("b")
        adapter = _make_adapter_initialized(accounts=[acct_a, acct_b])

        call_count = 0

        async def mock_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=500,
                message="Server Error",
            )
            yield  # unreachable; makes this an async generator

        adapter.http.stream_post = mock_stream

        with pytest.raises(aiohttp.ClientResponseError) as exc_info:
            async for _ in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
                pass

        assert exc_info.value.status == 500
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_stream_retry_bounded_no_wraparound(self):
        """With N accounts, at most N attempts (no wrap-around to first account)."""
        acct_a = _make_account("a")
        acct_b = _make_account("b")
        acct_c = _make_account("c")
        adapter = _make_adapter_initialized(accounts=[acct_a, acct_b, acct_c])

        attempted_accounts = []

        original_acquire = adapter._account_pool.acquire

        async def tracking_acquire():
            acct = await original_acquire()
            attempted_accounts.append(acct.account_id)
            return acct

        adapter._account_pool.acquire = tracking_acquire

        async def mock_stream(*args, **kwargs):
            # Always fail with account error
            raise aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=429,
                message="Rate Limited",
            )
            yield  # unreachable; makes this an async generator

        adapter.http.stream_post = mock_stream

        with pytest.raises(aiohttp.ClientResponseError):
            async for _ in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
                pass

        # With 3 accounts, max_retries = 3-1 = 2.
        # Initial attempt + 2 retries = 3 total, one per account, no wrap-around.
        assert len(attempted_accounts) == 3
        assert len(set(attempted_accounts)) == 3  # all unique

    @pytest.mark.asyncio
    async def test_stream_single_account_no_retry(self):
        """With only 1 account, pre-token 401 should NOT retry on the same account."""
        acct = _make_account("a")
        adapter = _make_adapter_initialized(accounts=[acct])

        call_count = 0

        async def mock_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=401,
                message="Unauthorized",
            )
            yield  # unreachable; makes this an async generator

        adapter.http.stream_post = mock_stream

        with pytest.raises(aiohttp.ClientResponseError) as exc_info:
            async for _ in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
                pass

        assert exc_info.value.status == 401
        # Single account: max_retries = 1-1 = 0, no retry at all
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_fallback_stream(self):
        acct = _make_account()
        adapter = _make_adapter_initialized(accounts=[acct], fallback_key="sk-fallback")
        adapter._account_pool.report_failure(acct.id, 429)

        # Mock OpenAI streaming response
        sse_lines = [
            'data: {"choices": [{"delta": {"content": "fallback"}, "finish_reason": null}]}',
            'data: {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}',
        ]

        async def mock_stream(*args, **kwargs):
            for line in sse_lines:
                yield line

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
            chunks.append(chunk)

        # Should get content + final + [DONE]
        assert len(chunks) >= 2
        # Check routing info in final chunk
        for c in chunks:
            if c.startswith("data: ") and c.strip() != "data: [DONE]":
                data = json.loads(c[6:])
                if "_routing" in data:
                    assert data["_routing"]["provider"] == "openai"
                    assert data["_routing"]["fallback"] is True


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestEnsureInit:
    @pytest.mark.asyncio
    async def test_lazy_init_loads_accounts(self, tmp_path, monkeypatch):
        accounts_data = {
            "accounts": [
                {
                    "id": "acct_01",
                    "label": "test",
                    "access_token": "tok",
                    "refresh_token": "ref",
                    "expires_at": int(time.time() * 1000) + 3600_000,
                    "account_id": "org1",
                    "tier": "plus",
                    "enabled": True,
                }
            ]
        }
        f = tmp_path / "accounts.json"
        f.write_text(json.dumps(accounts_data))

        settings = _mock_settings(codex_accounts_file=str(f))

        # Patch get_settings at the module level where it's imported inline
        import serving.adapters.codex_sub as codex_sub_mod

        def patched_ensure_init(self):
            # Inline the logic with our mock settings
            if self._initialized:
                return
            from serving.adapters.codex_token import AccountPool, CredentialProvider

            provider = CredentialProvider(
                accounts_file=settings.codex_accounts_file,
                refresh_margin=settings.codex_token_refresh_margin,
            )
            accounts = provider.load_accounts()
            self._credential_provider = provider
            self._account_pool = AccountPool(
                accounts=accounts,
                cooldown=settings.codex_account_cooldown,
                failure_threshold=settings.codex_failure_threshold,
            )
            self._fallback_api_key = settings.codex_fallback_api_key or None
            self._initialized = True

        config = _make_config()
        adapter = CodexSubscriptionAdapter(config)

        monkeypatch.setattr(
            codex_sub_mod.CodexSubscriptionAdapter, "_ensure_init", patched_ensure_init
        )
        adapter._ensure_init()

        assert adapter._initialized is True
        assert adapter._account_pool is not None
        assert len(adapter._account_pool._accounts) == 1
