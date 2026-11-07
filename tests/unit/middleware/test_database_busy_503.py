"""A request-path lookup that gets no connection in time is a 503 the client may retry.

``DatabaseBusyError`` (serving.storage.pools) is raised from the auth and
session lookups, usually inside a dependency. It must reach the client as a 503
with ``Retry-After``, shaped like any other HTTP error on its path -- never as a
500, and never as an authentication failure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from serving.servers import auth
from serving.servers.app import create_app
from serving.storage.pools import (
    DATABASE_BUSY_DETAIL,
    DATABASE_BUSY_RETRY_AFTER_SEC,
    DatabaseBusyError,
)


def _busy() -> None:
    raise DatabaseBusyError("no request-path database connection within 3s")


@pytest.fixture
def client() -> TestClient:
    app = create_app()

    async def needs_the_database(_: None = Depends(_busy)) -> dict:
        return {}

    app.add_api_route("/v1/__busy_probe", needs_the_database)
    app.add_api_route("/anthropic/v1/__busy_probe", needs_the_database)
    return TestClient(app, raise_server_exceptions=False)


def test_the_openai_surface_answers_503_with_retry_after(client):
    resp = client.get("/v1/__busy_probe")

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == str(DATABASE_BUSY_RETRY_AFTER_SEC)
    body = resp.json()
    assert body["error"]["message"] == DATABASE_BUSY_DETAIL
    assert body["error"]["code"] == 503
    # Nothing about the database leaks into the body.
    assert "connection" not in resp.text


def test_the_anthropic_surface_gets_its_own_envelope(client):
    resp = client.get("/anthropic/v1/__busy_probe")

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == str(DATABASE_BUSY_RETRY_AFTER_SEC)
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["message"] == DATABASE_BUSY_DETAIL


@pytest.mark.asyncio
async def test_the_lightweight_lookup_does_not_turn_busy_into_a_500(monkeypatch):
    """/v1/models' identity lookup used to wrap every store error as a 500."""
    monkeypatch.setattr(auth, "hash_api_key", lambda key: "hash")
    store = MagicMock()
    store.get_auth_context_lightweight = AsyncMock(
        side_effect=DatabaseBusyError("no request-path database connection within 3s")
    )

    with pytest.raises(DatabaseBusyError):
        await auth.optional_verify_api_key(
            request=MagicMock(),
            authorization="Bearer hyi-anything",
            x_api_key=None,
            op_store=store,
        )
