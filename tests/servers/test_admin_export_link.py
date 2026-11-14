"""The console downloads an export through a short-lived signed link.

The browser saves a file as it arrives only when it requests the file itself,
from a plain URL and without the console's ``Authorization`` header. So the
console asks ``POST /admin/export/requests/link`` for a link, and the browser
follows it to ``GET /admin/export/requests/download``. These tests hold the
link to standing in for that header and nothing more: it is the admin's
session, checked again when it is followed, for the filters it was issued for,
for a minute.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config.settings import settings
from serving.servers.deps import get_db_logger, get_operational_store
from serving.servers.middleware.timeout import TimeoutMiddleware
from serving.servers.routers.admin import export
from serving.utils.jwt import create_access_token

ADMIN_ID = "u-admin"
SESSION = "sess_admin"
LINK = "/admin/export/requests/link"
QUERY = {
    "start_time": "2024-01-01T00:00:00Z",
    "end_time": "2024-12-31T23:59:59Z",
    "user_id": "SpiritedAway",
    "include_content": "true",
}


def _row(number: int) -> dict[str, Any]:
    return {
        "request_id": f"req-{number}",
        "user_id": "user-1",
        "user_name": "alice",
        "user_email": "alice@example.com",
        "model_id": "m",
        "provider": "p",
        "timestamp": datetime(2024, 6, 1, tzinfo=UTC) - timedelta(seconds=number),
        "status_code": 200,
        "latency_ms": 1,
        "ttft_ms": 1,
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "reasoning_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "total_tokens": 2,
        "cost_usd": None,
        "error": None,
        "batch_job_id": None,
        "prompt": "hello",
        "response": "world",
        "tools": None,
        "metadata": None,
        "request_payload": None,
    }


class _Log:
    """A request log whose pages are given, each optionally slow to come back."""

    def __init__(self, pages: list[list[dict[str, Any]]], *, delay: float = 0.0) -> None:
        self._pages = iter(pages)
        self.delay = delay
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        self.log_admin_action = AsyncMock()
        conn = MagicMock()
        conn.fetch = self._fetch
        acquired = MagicMock()
        acquired.__aenter__ = AsyncMock(return_value=conn)
        acquired.__aexit__ = AsyncMock(return_value=None)
        self.pool = MagicMock()
        self.pool.acquire = MagicMock(return_value=acquired)

    async def _fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        self.queries.append((sql, params))
        if self.delay:
            await asyncio.sleep(self.delay)
        return next(self._pages, [])


@pytest.fixture
def account() -> dict[str, Any]:
    return {
        "id": ADMIN_ID,
        "email": "admin@example.com",
        "login_name": None,
        "role": "admin",
        "status": "active",
        "email_verified": True,
    }


@pytest.fixture
def op_store(account: dict[str, Any]) -> MagicMock:
    store = MagicMock()
    store.get_user_by_id = AsyncMock(
        side_effect=lambda user_id: dict(account) if user_id == ADMIN_ID else None
    )
    store.session_is_live = AsyncMock(return_value=True)
    return store


@pytest.fixture
def log() -> _Log:
    return _Log([[_row(1), _row(2)]])


@pytest.fixture
def app(op_store: MagicMock, log: _Log, monkeypatch) -> FastAPI:
    monkeypatch.setattr(settings, "signup_require_email_verification", False)
    application = FastAPI()
    application.include_router(export.router)
    application.dependency_overrides[get_operational_store] = lambda: op_store
    application.dependency_overrides[get_db_logger] = lambda: log
    return application


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _admin_session() -> dict[str, str]:
    token, _jti = create_access_token(ADMIN_ID, "admin@example.com", SESSION, role="admin")
    return _bearer(token)


def _client(app: Any) -> AsyncClient:
    transport = ASGITransport(app=app, client=("203.0.113.7", 4321))
    return AsyncClient(transport=transport, base_url="http://test")


async def _issue(client: AsyncClient, **query: str) -> str:
    response = await client.post(LINK, params={**QUERY, **query}, headers=_admin_session())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["expires_in"] == export.LINK_TTL_SECONDS == 60
    return body["url"]


def _token(url: str) -> str:
    return parse_qs(urlsplit(url).query)["token"][0]


@pytest.mark.asyncio
async def test_a_link_downloads_the_export_it_was_issued_for(app, log):
    async with _client(app) as client:
        url = await _issue(client)
        assert url.startswith("/admin/export/requests/download?token=")
        # Followed as the browser follows it: no Authorization header.
        response = await client.get(url)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert response.headers["content-disposition"] == (
        'attachment; filename="requests-20240101-20241231.jsonl"'
    )
    assert response.headers["cache-control"] == "no-store"
    records = [json.loads(line) for line in response.text.splitlines()]
    assert [r["request_id"] for r in records] == ["req-1", "req-2"]
    assert records[0]["prompt"] == "hello"

    # The filters are the link's: the user filter it was issued with is applied.
    _sql, params = log.queries[0]
    assert "SpiritedAway" in params
    # Audited when it is followed, as the admin who was issued it, from where
    # the download came.
    audit = log.log_admin_action.await_args.kwargs
    assert audit["action"] == "export_requests"
    assert audit["actor"].type == "admin"
    assert audit["actor"].id == ADMIN_ID
    assert audit["details"]["via"] == "download_link"
    assert audit["details"]["user_id"] == "SpiritedAway"
    assert audit["details"]["include_content"] is True


@pytest.mark.asyncio
async def test_issuing_a_link_exports_nothing_yet(app, log):
    async with _client(app) as client:
        await _issue(client)

    assert log.queries == []
    log.log_admin_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_link_is_refused_without_an_admin_session(app):
    async with _client(app) as client:
        anonymous = await client.post(LINK, params=QUERY)
        shared_token = await client.post(LINK, params=QUERY, headers=_bearer("test-admin-token"))

    assert anonymous.status_code == 401
    # ADMIN_TOKEN has no session to bind a link to; a script downloads directly.
    assert shared_token.status_code == 403
    assert "GET /admin/export/requests" in shared_token.json()["detail"]


@pytest.mark.asyncio
async def test_a_link_is_not_issued_for_filters_the_export_would_refuse(app):
    async with _client(app) as client:
        bad_source = await client.post(
            LINK, params={**QUERY, "source": "elsewhere"}, headers=_admin_session()
        )
        naive_start = await client.post(
            LINK, params={**QUERY, "start_time": "2024-01-01T00:00:00"}, headers=_admin_session()
        )

    assert bad_source.status_code == 422
    assert naive_start.status_code == 422


@pytest.mark.asyncio
async def test_an_expired_link_is_refused(app, log, monkeypatch):
    monkeypatch.setattr(export, "LINK_TTL_SECONDS", -1)
    async with _client(app) as client:
        response = await client.post(LINK, params=QUERY, headers=_admin_session())
        download = await client.get(response.json()["url"])

    assert download.status_code == 401
    assert "expired" in download.json()["detail"]
    assert log.queries == []


@pytest.mark.asyncio
async def test_a_changed_link_is_refused(app, log):
    async with _client(app) as client:
        url = await _issue(client)
        token = _token(url)
        header, payload, signature = token.split(".")
        # Widen the export: the same signature over other filters.
        claims = json.loads(jwt.utils.base64url_decode(payload))
        claims["f"]["user_id"] = None
        forged = ".".join(
            [header, jwt.utils.base64url_encode(json.dumps(claims).encode()).decode(), signature]
        )
        download = await client.get("/admin/export/requests/download", params={"token": forged})

    assert download.status_code == 401
    assert log.queries == []


@pytest.mark.asyncio
async def test_an_access_token_is_not_a_link_and_a_link_is_not_an_access_token(app, log):
    access_token = _admin_session()["Authorization"].removeprefix("Bearer ")
    async with _client(app) as client:
        url = await _issue(client)
        as_link = await client.get(
            "/admin/export/requests/download", params={"token": access_token}
        )
        as_bearer = await client.get(
            "/admin/export/requests", params=QUERY, headers=_bearer(_token(url))
        )

    assert as_link.status_code == 401
    assert as_bearer.status_code == 401
    assert log.queries == []


@pytest.mark.asyncio
async def test_a_link_is_refused_once_its_session_has_ended(app, op_store, log):
    async with _client(app) as client:
        url = await _issue(client)
        op_store.session_is_live.return_value = False  # signed out meanwhile
        download = await client.get(url)

    assert download.status_code == 401
    op_store.session_is_live.assert_awaited_with(ADMIN_ID, SESSION)
    assert log.queries == []


@pytest.mark.asyncio
async def test_a_link_is_refused_once_its_admin_has_lost_the_role(app, account, log):
    async with _client(app) as client:
        url = await _issue(client)
        account["role"] = "free"
        download = await client.get(url)

    assert download.status_code == 403
    assert log.queries == []


@pytest.mark.asyncio
async def test_a_download_slower_than_the_request_timeout_is_served_whole(app, monkeypatch):
    """The link's download is the same long stream as the direct export."""
    rows = [[_row(page * 500 + i) for i in range(500)] for page in range(3)] + [[_row(1500)]]
    slow = _Log(rows, delay=0.1)
    app.dependency_overrides[get_db_logger] = lambda: slow
    wrapped = TimeoutMiddleware(app, timeout_s=0.25, stream_timeout_s=30)

    async with _client(wrapped) as client:
        url = await _issue(client)
        response = await client.get(url)

    assert response.status_code == 200
    assert len(response.text.splitlines()) == 1501
