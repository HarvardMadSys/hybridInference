"""Configuring a batch webhook needs the role that may create batches (``BATCH_MIN_ROLE``).

Both own-user surfaces -- ``/user/batch-webhook`` (console session) and
``/v1/batch_webhook`` (API key) -- check the caller's current role before the
URL is resolved or the store is touched, for every kind of write: create,
update, secret rotation and re-enabling. Reading and deleting stay open to the
owner after a downgrade, and ``BATCH_ENABLED`` still gates the whole surface.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI, Header, HTTPException
from httpx import ASGITransport, AsyncClient

from serving.batch import config as batch_config_module
from serving.servers.auth import verify_batch_management_identity
from serving.servers.deps import AppServices, get_current_user
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import batch_console

URL = "https://hooks.example.test/events"
SURFACES = {"console": "/user/batch-webhook", "api": "/v1/batch_webhook"}
WRITES = {
    "create": {"url": URL},
    "update": {"url": "https://hooks.example.test/other"},
    "rotate": {"url": URL, "rotate_secret": True},
    "re-enable": {"url": URL, "enabled": True},
}


class FakeWebhookStore:
    """The webhook store, recording every call the routes make."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    async def get_webhook(self, user_id: str) -> dict[str, Any] | None:
        self.calls.append("get")
        return self.rows.get(user_id)

    async def set_webhook(self, *, user_id, url, secret_encrypted, enabled, check_owner=True):
        self.calls.append("set")
        row = {**self.rows.get(user_id, {}), "url": url, "enabled": enabled}
        if secret_encrypted is not None:
            row["secret_encrypted"] = secret_encrypted
        self.rows[user_id] = row
        return row

    async def delete_webhook(self, user_id: str) -> bool:
        self.calls.append("delete")
        return self.rows.pop(user_id, None) is not None

    async def list_events(self, user_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        self.calls.append("events")
        return []


@pytest.fixture
def settings(monkeypatch) -> dict[str, str]:
    values: dict[str, str] = {}
    monkeypatch.setattr(
        batch_config_module, "config_value", lambda key, default=None: values.get(key, default)
    )
    return values


@pytest.fixture
def caller() -> dict[str, Any]:
    """The signed-in user / key owner; tests change the role in place, as a downgrade does."""
    return {"user_id": "alice", "role": "internal", "authenticated": True}


@pytest.fixture
def store() -> FakeWebhookStore:
    return FakeWebhookStore()


@pytest.fixture
def lookups(monkeypatch) -> list[str]:
    seen: list[str] = []

    async def resolves_public(url, *, allow_private):
        seen.append(url)
        return None

    monkeypatch.setattr(batch_console, "check_resolves_public", resolves_public)
    monkeypatch.setattr(batch_console, "encrypt_api_key", lambda value: f"encrypted:{value}")
    return seen


@pytest_asyncio.fixture
async def client(settings, caller, store, lookups):
    app = FastAPI()
    app.state.services = AppServices(router=None, batch_webhook_store=store)

    async def identity(authorization: str | None = Header(None)) -> dict[str, Any]:
        if authorization != "Bearer token":
            raise HTTPException(401, "no credentials")
        return dict(caller)

    app.dependency_overrides[get_current_user] = identity
    app.dependency_overrides[verify_batch_management_identity] = identity
    install_error_handlers(app)
    app.include_router(batch_console.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw") as ac:
        yield ac


AUTH = {"Authorization": "Bearer token"}


def _message(response) -> str:
    return response.json()["error"]["message"]


@pytest.mark.parametrize("write", sorted(WRITES))
@pytest.mark.parametrize("surface", sorted(SURFACES))
async def test_a_caller_below_the_min_role_cannot_write_before_any_lookup_or_store_call(
    client, caller, store, lookups, surface, write
):
    caller["role"] = "pro"

    response = await client.put(SURFACES[surface], json=WRITES[write], headers=AUTH)

    assert response.status_code == 403
    assert "'internal' role or higher" in _message(response)
    assert lookups == [], "the webhook URL was resolved for a caller who may not set one"
    assert store.calls == [], "the store was touched for a caller who may not set a webhook"


@pytest.mark.parametrize("surface", sorted(SURFACES))
async def test_every_write_works_for_a_caller_at_the_min_role(client, store, lookups, surface):
    path = SURFACES[surface]

    created = await client.put(path, json=WRITES["create"], headers=AUTH)
    updated = await client.put(path, json={**WRITES["update"], "enabled": False}, headers=AUTH)
    rotated = await client.put(path, json=WRITES["rotate"], headers=AUTH)
    enabled = await client.put(path, json=WRITES["re-enable"], headers=AUTH)

    assert [r.status_code for r in (created, updated, rotated, enabled)] == [200] * 4
    assert "secret" in created.json() and "secret" in rotated.json()
    assert "secret" not in updated.json() and "secret" not in enabled.json()
    assert store.rows["alice"]["enabled"] is True
    assert len(lookups) == 4


@pytest.mark.parametrize("surface", sorted(SURFACES))
async def test_the_threshold_is_read_live(client, settings, caller, store, surface):
    path = SURFACES[surface]
    caller["role"] = "pro"
    assert (await client.put(path, json=WRITES["create"], headers=AUTH)).status_code == 403

    settings["BATCH_MIN_ROLE"] = "pro"
    assert (await client.put(path, json=WRITES["create"], headers=AUTH)).status_code == 200

    settings["BATCH_MIN_ROLE"] = "admin"
    store.calls.clear()
    assert (await client.put(path, json=WRITES["rotate"], headers=AUTH)).status_code == 403
    assert store.calls == []


@pytest.mark.parametrize("surface", sorted(SURFACES))
async def test_an_administrator_configures_their_own_webhook_whatever_the_threshold(
    client, settings, caller, store, surface
):
    """There is no route for one user's webhook on another's behalf; an admin acts as an owner."""
    caller.update(user_id="root", role="admin")
    settings["BATCH_MIN_ROLE"] = "admin"

    response = await client.put(SURFACES[surface], json=WRITES["create"], headers=AUTH)

    assert response.status_code == 200
    assert set(store.rows) == {"root"}


@pytest.mark.parametrize("surface", sorted(SURFACES))
async def test_a_downgraded_owner_can_still_read_list_and_delete(client, caller, store, surface):
    path = SURFACES[surface]
    assert (await client.put(path, json=WRITES["create"], headers=AUTH)).status_code == 200
    caller["role"] = "free"

    read = await client.get(path, headers=AUTH)
    events = await client.get(f"{path}/events", headers=AUTH)
    refused = await client.put(path, json=WRITES["re-enable"], headers=AUTH)
    deleted = await client.delete(path, headers=AUTH)

    assert (read.status_code, read.json()["configured"], read.json()["url"]) == (200, True, URL)
    assert "secret" not in read.json()
    assert events.status_code == 200
    assert refused.status_code == 403
    assert (deleted.status_code, deleted.json()) == (200, {"deleted": True})
    assert store.rows == {}


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
@pytest.mark.parametrize("surface", sorted(SURFACES))
async def test_batch_enabled_off_still_hides_every_route(
    client, settings, caller, store, lookups, surface, method
):
    settings["BATCH_ENABLED"] = "0"
    caller["role"] = "admin"

    response = await client.request(method, SURFACES[surface], json=WRITES["create"], headers=AUTH)

    assert response.status_code == 404
    assert lookups == [] and store.calls == []
