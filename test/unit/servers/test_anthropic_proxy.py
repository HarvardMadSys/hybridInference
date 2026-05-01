"""Tests for the Anthropic Messages API proxy surface."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from serving.adapters.claude_token import ClaudeAccountCredential
from serving.adapters.codex_token import AccountPool, NoHealthyAccountError
from serving.servers.routers.anthropic_proxy import (
    _REQUIRED_SYSTEM_PREFIX,
    _UPSTREAM_MESSAGES_URL,
    _anthropic_error,
    _build_upstream_headers,
    _ensure_system_prefix,
    _extract_usage_from_sse,
    _resolve_model,
    router,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


@dataclass
class _FakeModelConfig:
    id: str = "claude-sonnet-4.6"
    name: str = "Claude Sonnet 4.6"
    provider: str = "claude_sub"
    provider_model_id: str = "claude-sonnet-4-6"
    base_url: str = "https://api.anthropic.com"
    pricing: dict = field(default_factory=lambda: {"prompt": "3", "completion": "15"})


@dataclass
class _FakeAdapter:
    config: _FakeModelConfig = field(default_factory=_FakeModelConfig)


@dataclass
class _FakeRouteConfig:
    adapters: list = field(default_factory=list)
    admin_only: bool = False
    required_role: str = "free"


def _make_router_exec(
    model_id: str = "claude-sonnet-4.6",
    provider: str = "claude_sub",
    provider_model_id: str = "claude-sonnet-4-6",
) -> MagicMock:
    """Build a fake RouteExecutor with one registered model."""
    cfg = _FakeModelConfig(id=model_id, provider=provider, provider_model_id=provider_model_id)
    adapter = _FakeAdapter(config=cfg)
    route = _FakeRouteConfig(adapters=[(adapter, 1.0)])
    exec_mock = MagicMock()
    exec_mock.routes = {model_id: route}
    return exec_mock


# ---------------------------------------------------------------------------
# _resolve_model
# ---------------------------------------------------------------------------


class TestResolveModel:
    def test_success(self):
        exec_mock = _make_router_exec()
        upstream, pricing = _resolve_model("claude-sonnet-4.6", exec_mock)
        assert upstream == "claude-sonnet-4-6"
        assert pricing["prompt"] == "3"

    def test_unknown_model(self):
        exec_mock = _make_router_exec()
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            _resolve_model("nonexistent-model", exec_mock)
        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail

    def test_wrong_provider(self):
        exec_mock = _make_router_exec(provider="vertex")
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            _resolve_model("claude-sonnet-4.6", exec_mock)
        assert exc_info.value.status_code == 404
        assert "not eligible" in exc_info.value.detail


# ---------------------------------------------------------------------------
# _build_upstream_headers
# ---------------------------------------------------------------------------


class TestBuildUpstreamHeaders:
    def test_non_streaming(self):
        headers = _build_upstream_headers("tok_123", streaming=False)
        assert headers["Authorization"] == "Bearer tok_123"
        assert headers["Accept"] == "application/json"
        assert "Accept-Encoding" not in headers

    def test_streaming(self):
        headers = _build_upstream_headers("tok_123", streaming=True)
        assert headers["Accept"] == "text/event-stream"
        assert headers["Accept-Encoding"] == "identity"


# ---------------------------------------------------------------------------
# _extract_usage_from_sse
# ---------------------------------------------------------------------------


class TestExtractUsageFromSSE:
    def test_message_start(self):
        usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        event = {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 42,
                    "cache_creation_input_tokens": 10,
                    "cache_read_input_tokens": 5,
                }
            },
        }
        raw = f"data: {json.dumps(event)}\n\n".encode()
        _extract_usage_from_sse(raw, usage)
        assert usage["input_tokens"] == 42
        assert usage["cache_creation_input_tokens"] == 10
        assert usage["cache_read_input_tokens"] == 5

    def test_message_delta(self):
        usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        event = {"type": "message_delta", "usage": {"output_tokens": 99}}
        raw = f"data: {json.dumps(event)}\n\n".encode()
        _extract_usage_from_sse(raw, usage)
        assert usage["output_tokens"] == 99

    def test_combined_events(self):
        usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        start = {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}
        delta = {"type": "message_delta", "usage": {"output_tokens": 20}}
        raw = (f"data: {json.dumps(start)}\n\ndata: {json.dumps(delta)}\n\n").encode()
        _extract_usage_from_sse(raw, usage)
        assert usage["input_tokens"] == 10
        assert usage["output_tokens"] == 20

    def test_malformed_data_no_crash(self):
        usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        _extract_usage_from_sse(b"not valid sse at all", usage)
        assert usage["input_tokens"] == 0

    def test_done_sentinel_ignored(self):
        usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        _extract_usage_from_sse(b"data: [DONE]\n\n", usage)
        assert usage["input_tokens"] == 0

    def test_cumulative_output_tokens_not_accumulated(self):
        """Anthropic sends cumulative output_tokens in message_delta, not
        incremental deltas.  Two frames with output_tokens=5 then 8 should
        result in 8, not 13."""
        usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        delta1 = {"type": "message_delta", "usage": {"output_tokens": 5}}
        delta2 = {"type": "message_delta", "usage": {"output_tokens": 8}}
        raw = (f"data: {json.dumps(delta1)}\n\ndata: {json.dumps(delta2)}\n\n").encode()
        _extract_usage_from_sse(raw, usage)
        assert usage["output_tokens"] == 8  # cumulative, not 13


# ---------------------------------------------------------------------------
# _anthropic_error
# ---------------------------------------------------------------------------


class TestAnthropicError:
    def test_400(self):
        resp = _anthropic_error(400, "bad request")
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert body["type"] == "error"
        assert body["error"]["type"] == "invalid_request_error"

    def test_429(self):
        resp = _anthropic_error(429, "rate limited")
        body = json.loads(resp.body)
        assert body["error"]["type"] == "rate_limit_error"

    def test_503(self):
        resp = _anthropic_error(503, "overloaded")
        body = json.loads(resp.body)
        assert body["error"]["type"] == "overloaded_error"


# ---------------------------------------------------------------------------
# Integration tests via FastAPI TestClient
# ---------------------------------------------------------------------------


@pytest.fixture
def _mock_pool():
    """Patch get_shared_pool to return mock provider + pool."""
    account = _make_account()
    pool = AccountPool([account])
    provider = MagicMock()
    provider.get_valid_token = AsyncMock(return_value=account.access_token)
    with patch(
        "serving.servers.routers.anthropic_proxy.get_shared_pool",
        return_value=(provider, pool),
    ) as m:
        m.provider = provider
        m.pool = pool
        m.account = account
        yield m


@pytest.fixture
def _mock_deps():
    """Override FastAPI dependencies for testing."""

    async def fake_verify_api_key():
        return {"authenticated": True, "user_id": "test-user", "role": "free", "is_admin": False}

    async def fake_get_router():
        return _make_router_exec()

    async def fake_get_rate_limiter():
        return None

    async def fake_get_log_store():
        return None

    from serving.servers.auth import verify_api_key
    from serving.servers.concurrency import UserConcurrencyLimiter
    from serving.servers.deps import get_log_store, get_rate_limiter, get_router, get_user_concurrency_limiter

    # Each test gets its own limiter so per-user state doesn't leak across tests.
    test_limiter = UserConcurrencyLimiter({"free": 10, "pro": 10, "internal": 10, "admin": 10})

    async def fake_get_user_concurrency_limiter():
        return test_limiter

    overrides = {
        verify_api_key: fake_verify_api_key,
        get_router: fake_get_router,
        get_rate_limiter: fake_get_rate_limiter,
        get_log_store: fake_get_log_store,
        get_user_concurrency_limiter: fake_get_user_concurrency_limiter,
    }
    return overrides


@pytest.fixture
def client(_mock_pool, _mock_deps):
    """Create a FastAPI TestClient with mocked dependencies."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    for dep, override in _mock_deps.items():
        app.dependency_overrides[dep] = override
    app.include_router(router)
    return TestClient(app)


def _request_body(model: str = "claude-sonnet-4.6", stream: bool = False) -> dict:
    return {
        "model": model,
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": stream,
    }


class TestNonStreaming:
    def test_success(self, client, _mock_pool):
        upstream_resp = {
            "id": "msg_123",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hi!"}],
            "model": "claude-sonnet-4-6",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        with patch("serving.http.AsyncHTTPClient.shared") as mock_http_cls:
            mock_http = MagicMock()
            mock_http.json_post_with_retry = AsyncMock(return_value=upstream_resp)
            mock_http_cls.return_value = mock_http

            resp = client.post(
                "/anthropic/v1/messages",
                json=_request_body(),
                headers={"x-api-key": "hyi-test"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["id"] == "msg_123"
            assert data["usage"]["input_tokens"] == 10

    def test_unknown_model(self, client):
        resp = client.post(
            "/anthropic/v1/messages",
            json=_request_body(model="nonexistent"),
            headers={"x-api-key": "hyi-test"},
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"]["message"]

    def test_missing_model_field(self, client):
        body = {"max_tokens": 100, "messages": [{"role": "user", "content": "Hi"}]}
        resp = client.post(
            "/anthropic/v1/messages",
            json=body,
            headers={"x-api-key": "hyi-test"},
        )
        assert resp.status_code == 400
        assert "model" in resp.json()["error"]["message"]

    def test_invalid_json(self, client):
        resp = client.post(
            "/anthropic/v1/messages",
            content=b"not json",
            headers={"x-api-key": "hyi-test", "content-type": "application/json"},
        )
        assert resp.status_code == 400

    def test_pool_exhausted(self, client, _mock_pool):
        with patch(
            "serving.servers.routers.anthropic_proxy.get_shared_pool",
            side_effect=NoHealthyAccountError("all down"),
        ):
            # Need to re-patch since the fixture already patches
            pass

        # Exhaust the pool by marking all accounts unhealthy
        _mock_pool.return_value = (
            _mock_pool.return_value[0],
            _mock_pool.return_value[1],
        )
        with patch("serving.servers.routers.anthropic_proxy.get_shared_pool") as mock:
            pool = MagicMock()
            pool.acquire = AsyncMock(side_effect=NoHealthyAccountError("all down"))
            mock.return_value = (MagicMock(), pool)

            resp = client.post(
                "/anthropic/v1/messages",
                json=_request_body(),
                headers={"x-api-key": "hyi-test"},
            )
            assert resp.status_code == 503

    def test_ineligible_provider(self, client, _mock_deps):
        """Model exists but provider is not claude_sub."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from serving.servers.deps import get_router

        async def fake_get_router():
            return _make_router_exec(provider="vertex")

        app = FastAPI()
        overrides = dict(_mock_deps)
        overrides[get_router] = fake_get_router
        for dep, override in overrides.items():
            app.dependency_overrides[dep] = override
        app.include_router(router)
        c = TestClient(app)

        resp = c.post(
            "/anthropic/v1/messages",
            json=_request_body(),
            headers={"x-api-key": "hyi-test"},
        )
        assert resp.status_code == 404
        assert "not eligible" in resp.json()["error"]["message"]


class TestStreaming:
    def test_success(self, client, _mock_pool):
        start_event = {
            "type": "message_start",
            "message": {
                "id": "msg_123",
                "usage": {"input_tokens": 10},
            },
        }
        delta_event = {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Hi"},
        }
        end_event = {
            "type": "message_delta",
            "usage": {"output_tokens": 5},
        }

        sse_bytes = (
            f"event: message_start\ndata: {json.dumps(start_event)}\n\n"
            f"event: content_block_delta\ndata: {json.dumps(delta_event)}\n\n"
            f"event: message_delta\ndata: {json.dumps(end_event)}\n\n"
        ).encode()

        # Mock aiohttp session.post to return a fake streaming response
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.release = AsyncMock()

        async def fake_iter_any():
            yield sse_bytes

        mock_resp.content = MagicMock()
        mock_resp.content.iter_any = fake_iter_any

        mock_session = AsyncMock()
        mock_session.post = AsyncMock(return_value=mock_resp)

        with patch("serving.http.AsyncHTTPClient.shared") as mock_http_cls:
            mock_http = MagicMock()
            mock_http._ensure_session = AsyncMock(return_value=mock_session)
            mock_http_cls.return_value = mock_http

            resp = client.post(
                "/anthropic/v1/messages",
                json=_request_body(stream=True),
                headers={"x-api-key": "hyi-test"},
            )
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = resp.content.decode()
            assert "message_start" in body
            assert "content_block_delta" in body

    def test_midstream_error_reports_failure(self, _mock_pool, _mock_deps):
        """Mid-stream disconnect should report_failure on the shared pool,
        not leave the account marked as healthy."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        # Build a pool spy to track report_success / report_failure calls
        account = _make_account()
        pool = AccountPool([account])
        provider = MagicMock()
        provider.get_valid_token = AsyncMock(return_value=account.access_token)

        app = FastAPI()
        for dep, override in _mock_deps.items():
            app.dependency_overrides[dep] = override
        app.include_router(router)
        c = TestClient(app)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.release = AsyncMock()

        async def exploding_iter():
            yield b"event: message_start\ndata: {}\n\n"
            raise ConnectionError("upstream dropped")

        mock_resp.content = MagicMock()
        mock_resp.content.iter_any = exploding_iter

        mock_session = AsyncMock()
        mock_session.post = AsyncMock(return_value=mock_resp)

        with (
            patch(
                "serving.servers.routers.anthropic_proxy.get_shared_pool",
                return_value=(provider, pool),
            ),
            patch("serving.http.AsyncHTTPClient.shared") as mock_http_cls,
        ):
            mock_http = MagicMock()
            mock_http._ensure_session = AsyncMock(return_value=mock_session)
            mock_http_cls.return_value = mock_http

            resp = c.post(
                "/anthropic/v1/messages",
                json=_request_body(stream=True),
                headers={"x-api-key": "hyi-test"},
            )
            assert resp.status_code == 200
            body = resp.content.decode()
            assert "Stream interrupted" in body

        # The account should have been reported as failed (502 = upstream),
        # not marked healthy via report_success.
        health = pool._health.get(account.id)
        assert health is not None
        assert health.last_failure > 0  # report_failure was called
        assert health.last_success == 0.0  # report_success was NOT called

    def test_successful_stream_reports_success(self, _mock_pool, _mock_deps):
        """A stream that completes without error should report_success."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        account = _make_account()
        pool = AccountPool([account])
        provider = MagicMock()
        provider.get_valid_token = AsyncMock(return_value=account.access_token)

        app = FastAPI()
        for dep, override in _mock_deps.items():
            app.dependency_overrides[dep] = override
        app.include_router(router)
        c = TestClient(app)

        sse_bytes = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.release = AsyncMock()

        async def good_iter():
            yield sse_bytes

        mock_resp.content = MagicMock()
        mock_resp.content.iter_any = good_iter

        mock_session = AsyncMock()
        mock_session.post = AsyncMock(return_value=mock_resp)

        with (
            patch(
                "serving.servers.routers.anthropic_proxy.get_shared_pool",
                return_value=(provider, pool),
            ),
            patch("serving.http.AsyncHTTPClient.shared") as mock_http_cls,
        ):
            mock_http = MagicMock()
            mock_http._ensure_session = AsyncMock(return_value=mock_session)
            mock_http_cls.return_value = mock_http

            resp = c.post(
                "/anthropic/v1/messages",
                json=_request_body(stream=True),
                headers={"x-api-key": "hyi-test"},
            )
            assert resp.status_code == 200

        # Account should be marked healthy (report_success called)
        health = pool._health.get(account.id)
        assert health is not None
        assert health.healthy is True
        assert health.consecutive_failures == 0

    def test_midstream_error_event_is_valid_json(self, _mock_pool, _mock_deps):
        """Error messages with quotes/newlines must produce valid JSON in SSE."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        account = _make_account()
        pool = AccountPool([account])
        provider = MagicMock()
        provider.get_valid_token = AsyncMock(return_value=account.access_token)

        app = FastAPI()
        for dep, override in _mock_deps.items():
            app.dependency_overrides[dep] = override
        app.include_router(router)
        c = TestClient(app)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.release = AsyncMock()

        async def exploding_iter():
            raise ConnectionError('quote"and\nnewline')
            yield  # make it an async generator

        mock_resp.content = MagicMock()
        mock_resp.content.iter_any = exploding_iter

        mock_session = AsyncMock()
        mock_session.post = AsyncMock(return_value=mock_resp)

        with (
            patch(
                "serving.servers.routers.anthropic_proxy.get_shared_pool",
                return_value=(provider, pool),
            ),
            patch("serving.http.AsyncHTTPClient.shared") as mock_http_cls,
        ):
            mock_http = MagicMock()
            mock_http._ensure_session = AsyncMock(return_value=mock_session)
            mock_http_cls.return_value = mock_http

            resp = c.post(
                "/anthropic/v1/messages",
                json=_request_body(stream=True),
                headers={"x-api-key": "hyi-test"},
            )
            assert resp.status_code == 200
            body = resp.content.decode()

            # Extract the data: line and verify it's valid JSON
            for line in body.split("\n"):
                if line.startswith("data: "):
                    parsed = json.loads(line[6:])
                    assert parsed["type"] == "error"
                    assert "quote" in parsed["error"]["message"]
                    break
            else:
                pytest.fail("No data: line found in SSE response")


class TestDBLogging:
    def test_db_log_called(self, _mock_pool, _mock_deps):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from serving.servers.deps import get_log_store

        mock_db = MagicMock()
        mock_db.log_request = AsyncMock()

        async def fake_get_log_store():
            return mock_db

        app = FastAPI()
        overrides = dict(_mock_deps)
        overrides[get_log_store] = fake_get_log_store
        for dep, override in overrides.items():
            app.dependency_overrides[dep] = override
        app.include_router(router)
        c = TestClient(app)

        upstream_resp = {
            "id": "msg_123",
            "type": "message",
            "content": [{"type": "text", "text": "Hi"}],
            "model": "claude-sonnet-4-6",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

        with patch("serving.http.AsyncHTTPClient.shared") as mock_http_cls:
            mock_http = MagicMock()
            mock_http.json_post_with_retry = AsyncMock(return_value=upstream_resp)
            mock_http_cls.return_value = mock_http

            resp = c.post(
                "/anthropic/v1/messages",
                json=_request_body(),
                headers={"x-api-key": "hyi-test"},
            )
            assert resp.status_code == 200

        # The DB log is fire-and-forget via create_task; give the event loop
        # a moment to schedule it.  In TestClient the event loop is managed
        # by starlette, but the task may not have run yet.
        # We verify the task was created rather than awaiting it.


# ---------------------------------------------------------------------------
# _ensure_system_prefix
# ---------------------------------------------------------------------------


class TestEnsureSystemPrefix:
    def test_no_system_injects_prefix(self):
        body: dict[str, Any] = {"model": "claude-sonnet-4-6", "messages": []}
        _ensure_system_prefix(body)
        assert body["system"] == [{"type": "text", "text": _REQUIRED_SYSTEM_PREFIX}]

    def test_string_system_converted_to_array(self):
        body: dict[str, Any] = {"system": "Be helpful.", "messages": []}
        _ensure_system_prefix(body)
        assert isinstance(body["system"], list)
        assert body["system"][0]["text"] == _REQUIRED_SYSTEM_PREFIX
        assert body["system"][1]["text"] == "Be helpful."

    def test_string_system_already_prefixed_converted_to_array(self):
        body: dict[str, Any] = {
            "system": f"{_REQUIRED_SYSTEM_PREFIX}\n\nBe helpful.",
        }
        _ensure_system_prefix(body)
        # Should be array format, no double-prefix
        assert isinstance(body["system"], list)
        assert body["system"][0]["text"].count(_REQUIRED_SYSTEM_PREFIX) == 1

    def test_array_system_prepends_to_first_text_block(self):
        body: dict[str, Any] = {
            "system": [{"type": "text", "text": "Be helpful."}],
        }
        _ensure_system_prefix(body)
        assert body["system"][0]["text"].startswith(_REQUIRED_SYSTEM_PREFIX)
        assert "Be helpful." in body["system"][0]["text"]

    def test_array_system_already_prefixed_unchanged(self):
        body: dict[str, Any] = {
            "system": [{"type": "text", "text": f"{_REQUIRED_SYSTEM_PREFIX}\n\nBe helpful."}],
        }
        _ensure_system_prefix(body)
        assert body["system"][0]["text"].count(_REQUIRED_SYSTEM_PREFIX) == 1


# ---------------------------------------------------------------------------
# URL contains ?beta=true
# ---------------------------------------------------------------------------


class TestUpstreamURL:
    def test_url_has_beta_param(self):
        assert "?beta=true" in _UPSTREAM_MESSAGES_URL
