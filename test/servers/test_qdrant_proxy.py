"""Unit tests for the Qdrant reverse proxy router.

Covers pure helper functions (allowlist, namespace scoping, response filtering),
auth gating, endpoint allowlist enforcement, upstream proxy forwarding, response
de-scoping, and structured logging.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import AppServices
from serving.servers.routers.qdrant_proxy import (
    _filter_collections_response,
    _is_allowed,
    _rewrite_path,
    _scope_collection,
    _unscope_collection,
    _unscope_collection_info,
    _verify_qdrant_user,
    router,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_USER_ID = "user-abc-123"
OTHER_USER_ID = "user-xyz-999"


def _scoped(name: str, uid: str = TEST_USER_ID) -> str:
    """Shortcut for expected scoped name."""
    return _scope_collection(name, uid)


# ---------------------------------------------------------------------------
# Mock upstream that implements AsyncHTTPClient.request()
# ---------------------------------------------------------------------------


class _MockQdrantUpstream:
    """Controllable stand-in for ``AsyncHTTPClient.shared()``."""

    def __init__(self) -> None:
        self.last_request: dict[str, Any] | None = None
        self._status = 200
        self._body = b"{}"
        self._content_type = "application/json"

    def set_response(
        self, status: int = 200, body: bytes = b"{}", content_type: str = "application/json"
    ) -> None:
        self._status = status
        self._body = body
        self._content_type = content_type

    async def request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: Any = None,
    ) -> tuple[int, bytes, str]:
        self.last_request = {
            "method": method,
            "url": url,
            "data": data,
            "headers": headers,
        }
        return self._status, self._body, self._content_type


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _qdrant_settings(monkeypatch):
    """Set Qdrant env vars and clear settings cache."""
    monkeypatch.setenv("QDRANT_BASE_URL", "http://qdrant:6333")
    monkeypatch.setenv("QDRANT_API_KEY", "test-qdrant-key")
    from serving.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def mock_upstream(monkeypatch) -> _MockQdrantUpstream:
    """Replace AsyncHTTPClient.shared() with a controllable mock."""
    from serving.http import AsyncHTTPClient

    mock = _MockQdrantUpstream()
    monkeypatch.setattr(AsyncHTTPClient, "shared", classmethod(lambda cls: mock))
    return mock


@pytest.fixture
def qdrant_app(mock_db_logger, mock_rate_limiter, mock_upstream) -> FastAPI:
    """Create a FastAPI app with the qdrant_proxy router and fake auth."""

    app = FastAPI(title="Test Qdrant Proxy")
    app.state.services = AppServices(
        router=AsyncMock(),  # not used by qdrant proxy
        db_logger=mock_db_logger,
        rate_limiter=mock_rate_limiter,
        operational_store=None,
        log_store=None,
    )

    # Override auth dependency to return a fake authenticated user
    app.dependency_overrides[_verify_qdrant_user] = lambda: {
        "user_id": TEST_USER_ID,
        "authenticated": True,
        "tier": "free",
    }

    app.include_router(router)
    return app


@pytest.fixture
async def client(qdrant_app: FastAPI):
    transport = ASGITransport(app=qdrant_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ============================================================================
# Category 1 — Pure functions (no app needed)
# ============================================================================


class TestIsAllowed:
    """Tests for the endpoint allowlist."""

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "collections",
            "collections/my_col",
            "collections/my_col/points",
            "collections/my_col/points/search",
            "collections/my_col/points/payload",
            "collections/my_col/index",
        ],
    )
    def test_valid_endpoints(self, path: str):
        assert _is_allowed(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "snapshots",
            "cluster",
            "aliases",
            "collections/x/snapshots",
            "collections/x/aliases",
        ],
    )
    def test_rejects_admin_paths(self, path: str):
        assert _is_allowed(path) is False

    def test_strips_trailing_slashes(self):
        assert _is_allowed("collections/") is True
        assert _is_allowed("/collections") is True


class TestScopeUnscope:
    """Tests for namespace scoping and unscoping."""

    def test_roundtrip(self):
        name = "my_collection"
        scoped = _scope_collection(name, TEST_USER_ID)
        unscoped = _unscope_collection(scoped, TEST_USER_ID)
        assert unscoped == name

    def test_scope_format(self):
        scoped = _scope_collection("test", TEST_USER_ID)
        assert scoped.startswith("fi_")
        parts = scoped.split("_", 2)
        assert len(parts) == 3
        assert len(parts[1]) == 12  # sha256[:12]
        assert parts[2] == "test"

    def test_unscope_wrong_user(self):
        scoped = _scope_collection("test", TEST_USER_ID)
        assert _unscope_collection(scoped, OTHER_USER_ID) is None


class TestRewritePath:
    """Tests for URL path rewriting."""

    def test_scopes_collection(self):
        result = _rewrite_path("collections/x", TEST_USER_ID)
        assert result == f"collections/{_scoped('x')}"

    def test_preserves_subresource(self):
        result = _rewrite_path("collections/x/points/search", TEST_USER_ID)
        assert result == f"collections/{_scoped('x')}/points/search"

    def test_list_unchanged(self):
        result = _rewrite_path("collections", TEST_USER_ID)
        assert result == "collections"


class TestFilterCollectionsResponse:
    """Tests for collection list response filtering."""

    def test_filters_to_user_collections(self):
        body = json.dumps(
            {
                "result": {
                    "collections": [
                        {"name": _scoped("mine")},
                        {"name": _scoped("also_mine")},
                        {"name": _scoped("not_mine", OTHER_USER_ID)},
                    ]
                }
            }
        ).encode()
        result = json.loads(_filter_collections_response(body, TEST_USER_ID))
        names = [c["name"] for c in result["result"]["collections"]]
        assert names == ["mine", "also_mine"]

    def test_empty_when_no_match(self):
        body = json.dumps(
            {
                "result": {
                    "collections": [
                        {"name": _scoped("not_mine", OTHER_USER_ID)},
                    ]
                }
            }
        ).encode()
        result = json.loads(_filter_collections_response(body, TEST_USER_ID))
        assert result["result"]["collections"] == []

    def test_invalid_json_returns_unchanged(self):
        body = b"not json"
        assert _filter_collections_response(body, TEST_USER_ID) == body


class TestUnscopeCollectionInfo:
    """Tests for single-collection info response de-scoping."""

    def test_unscopes_name(self):
        body = json.dumps({"result": {"name": _scoped("my_col")}}).encode()
        result = json.loads(_unscope_collection_info(body, TEST_USER_ID))
        assert result["result"]["name"] == "my_col"


# ============================================================================
# Category 2 — Auth
# ============================================================================


class TestAuth:
    """Auth dependency enforcement."""

    @pytest.mark.asyncio
    async def test_no_auth_returns_401(self, qdrant_app: FastAPI, mock_upstream):
        # Remove the override so real auth runs — but with no headers it should 401
        qdrant_app.dependency_overrides.pop(_verify_qdrant_user, None)
        transport = ASGITransport(app=qdrant_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/v1/qdrant/collections")
            assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_anonymous_rejected(self, qdrant_app: FastAPI, mock_upstream):
        # Override to return anonymous user
        qdrant_app.dependency_overrides[_verify_qdrant_user] = lambda: {
            "user_id": "anonymous",
            "authenticated": False,
            "tier": "free",
        }
        transport = ASGITransport(app=qdrant_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/v1/qdrant/collections")
            assert resp.status_code == status.HTTP_401_UNAUTHORIZED


# ============================================================================
# Category 3 — Allowlist
# ============================================================================


class TestAllowlist:
    """Endpoint allowlist enforcement via ASGI client."""

    @pytest.mark.asyncio
    async def test_allowed_endpoint_proxied(self, client: AsyncClient, mock_upstream):
        mock_upstream.set_response(200, b'{"result":{"collections":[]}}')
        resp = await client.get("/v1/qdrant/collections")
        assert resp.status_code == status.HTTP_200_OK

    @pytest.mark.asyncio
    async def test_disallowed_endpoint_403(self, client: AsyncClient):
        resp = await client.get("/v1/qdrant/snapshots")
        assert resp.status_code == status.HTTP_403_FORBIDDEN


# ============================================================================
# Category 4 — Proxy forwarding
# ============================================================================


class TestProxyForwarding:
    """Upstream request forwarding."""

    @pytest.mark.asyncio
    async def test_forwards_to_upstream(self, client: AsyncClient, mock_upstream):
        mock_upstream.set_response(200, b'{"result":{}}')
        await client.get("/v1/qdrant/collections/my_col")
        assert mock_upstream.last_request is not None
        url = mock_upstream.last_request["url"]
        assert url.startswith("http://qdrant:6333/collections/")
        assert _scoped("my_col") in url

    @pytest.mark.asyncio
    async def test_injects_qdrant_api_key(self, client: AsyncClient, mock_upstream):
        mock_upstream.set_response(200, b'{"result":{}}')
        await client.get("/v1/qdrant/collections/my_col")
        assert mock_upstream.last_request is not None
        assert mock_upstream.last_request["headers"]["api-key"] == "test-qdrant-key"

    @pytest.mark.asyncio
    async def test_preserves_query_params(self, client: AsyncClient, mock_upstream):
        mock_upstream.set_response(200, b'{"result":{}}')
        await client.put("/v1/qdrant/collections/my_col/points?wait=true", content=b'{"points":[]}')
        assert mock_upstream.last_request is not None
        assert "wait=true" in mock_upstream.last_request["url"]

    @pytest.mark.asyncio
    async def test_upstream_4xx_forwarded(self, client: AsyncClient, mock_upstream):
        mock_upstream.set_response(404, b'{"status":{"error":"Not found"}}')
        resp = await client.get("/v1/qdrant/collections/no_exist")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_client_error_returns_502(self, client: AsyncClient, mock_upstream):
        async def _raise(*a, **kw):
            raise aiohttp.ClientError("connection refused")

        mock_upstream.request = _raise
        resp = await client.get("/v1/qdrant/collections/x")
        assert resp.status_code == 502

    @pytest.mark.asyncio
    async def test_timeout_returns_502(self, client: AsyncClient, mock_upstream):
        async def _raise(*a, **kw):
            raise TimeoutError("timed out")

        mock_upstream.request = _raise
        resp = await client.get("/v1/qdrant/collections/x")
        assert resp.status_code == 502


# ============================================================================
# Category 5 — Namespace in responses
# ============================================================================


class TestNamespaceResponses:
    """Tenant isolation in response payloads."""

    @pytest.mark.asyncio
    async def test_get_collections_filtered(self, client: AsyncClient, mock_upstream):
        body = json.dumps(
            {
                "result": {
                    "collections": [
                        {"name": _scoped("mine")},
                        {"name": _scoped("alien", OTHER_USER_ID)},
                    ]
                }
            }
        ).encode()
        mock_upstream.set_response(200, body)
        resp = await client.get("/v1/qdrant/collections")
        data = resp.json()
        names = [c["name"] for c in data["result"]["collections"]]
        assert names == ["mine"]

    @pytest.mark.asyncio
    async def test_get_collection_info_unscoped(self, client: AsyncClient, mock_upstream):
        body = json.dumps({"result": {"name": _scoped("my_col")}}).encode()
        mock_upstream.set_response(200, body)
        resp = await client.get("/v1/qdrant/collections/my_col")
        data = resp.json()
        assert data["result"]["name"] == "my_col"


# ============================================================================
# Category 6 — Structured logging
# ============================================================================


class TestStructuredLogging:
    """Verify structured log messages on different outcomes."""

    @pytest.mark.asyncio
    async def test_log_on_success(self, client: AsyncClient, mock_upstream):
        mock_upstream.set_response(200, b'{"result":{}}')
        with patch("serving.servers.routers.qdrant_proxy.logger") as mock_logger:
            await client.get("/v1/qdrant/collections/x")
            mock_logger.info.assert_called_once()
            args, kwargs = mock_logger.info.call_args
            assert args[0] == "qdrant_proxy ok"
            extra = kwargs["extra"]
            assert "latency_ms" in extra
            assert extra["method"] == "GET"
            assert extra["user_id"] == TEST_USER_ID

    @pytest.mark.asyncio
    async def test_log_on_upstream_error(self, client: AsyncClient, mock_upstream):
        mock_upstream.set_response(404, b'{"status":{"error":"Not found"}}')
        with patch("serving.servers.routers.qdrant_proxy.logger") as mock_logger:
            await client.get("/v1/qdrant/collections/x")
            mock_logger.warning.assert_called_once()
            args, _ = mock_logger.warning.call_args
            assert args[0] == "qdrant_proxy upstream_error"

    @pytest.mark.asyncio
    async def test_log_on_unreachable(self, client: AsyncClient, mock_upstream):
        async def _raise(*a, **kw):
            raise aiohttp.ClientError("refused")

        mock_upstream.request = _raise
        with patch("serving.servers.routers.qdrant_proxy.logger") as mock_logger:
            await client.get("/v1/qdrant/collections/x")
            mock_logger.error.assert_called_once()
            args, _ = mock_logger.error.call_args
            assert args[0] == "qdrant_proxy upstream_unreachable"

    @pytest.mark.asyncio
    async def test_log_on_endpoint_blocked(self, client: AsyncClient):
        with patch("serving.servers.routers.qdrant_proxy.logger") as mock_logger:
            await client.get("/v1/qdrant/snapshots")
            mock_logger.warning.assert_called_once()
            args, _ = mock_logger.warning.call_args
            assert args[0] == "qdrant_proxy endpoint_blocked"
