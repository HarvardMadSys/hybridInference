"""What the console offers a signed-in user for Batch (``GET /user/batch-capabilities``).

``BATCH_ENABLED`` decides whether Batch is offered at all; ``BATCH_MIN_ROLE``
whether the user's current role may submit; a user below it keeps the entry
while they have batches or a webhook to manage. Off, nothing batch-related is
read. A failed history lookup reads as "may have history", never as "none".
The owner routes themselves never look at the role.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI, Header, HTTPException
from httpx import ASGITransport, AsyncClient

from serving.batch import config as batch_config_module
from serving.servers.deps import AppServices, get_current_user
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import batch_console

ROLES = ("free", "pro", "internal", "admin")
RANK = {role: rank for rank, role in enumerate(ROLES)}
PATH = "/user/batch-capabilities"
AUTH = {"Authorization": "Bearer token"}


class FakeBatchStore:
    """Batches by owner; records every call, and can be made to fail."""

    def __init__(self) -> None:
        self.owners: set[str] = set()
        self.calls: list[str] = []
        self.fail = False

    async def has_batches(self, user_id: str) -> bool:
        self.calls.append("has_batches")
        if self.fail:
            raise ConnectionError("database unreachable")
        return user_id in self.owners


class FakeWebhookStore:
    """Webhooks by owner; records every call, and can be made to fail."""

    def __init__(self) -> None:
        self.owners: set[str] = set()
        self.calls: list[str] = []
        self.fail = False

    async def has_webhook(self, user_id: str) -> bool:
        self.calls.append("has_webhook")
        if self.fail:
            raise ConnectionError("database unreachable")
        return user_id in self.owners


@pytest.fixture
def settings(monkeypatch) -> dict[str, str]:
    values: dict[str, str] = {}
    monkeypatch.setattr(
        batch_config_module, "config_value", lambda key, default=None: values.get(key, default)
    )
    return values


@pytest.fixture
def caller() -> dict[str, Any]:
    return {"user_id": "alice", "role": "free"}


@pytest.fixture
def batches() -> FakeBatchStore:
    return FakeBatchStore()


@pytest.fixture
def hooks() -> FakeWebhookStore:
    return FakeWebhookStore()


@pytest.fixture
def services(batches, hooks) -> AppServices:
    return AppServices(router=None, batch_store=batches, batch_webhook_store=hooks)


@pytest_asyncio.fixture
async def client(settings, caller, services):
    app = FastAPI()
    app.state.services = services

    async def identity(authorization: str | None = Header(None)) -> dict[str, Any]:
        if authorization != "Bearer token":
            raise HTTPException(401, "no credentials")
        return dict(caller)

    app.dependency_overrides[get_current_user] = identity
    install_error_handlers(app)
    app.include_router(batch_console.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw") as ac:
        yield ac


async def _capabilities(client) -> dict[str, Any]:
    response = await client.get(PATH, headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


@pytest.mark.parametrize("history", ["none", "batches", "webhook", "someone_else"])
@pytest.mark.parametrize("min_role", ROLES)
@pytest.mark.parametrize("role", ROLES)
async def test_role_and_history_decide_submission_and_the_entry(
    client, settings, caller, batches, hooks, role, min_role, history
):
    settings["BATCH_MIN_ROLE"] = min_role
    caller["role"] = role
    if history == "batches":
        batches.owners.add("alice")
    elif history == "webhook":
        hooks.owners.add("alice")
    elif history == "someone_else":
        batches.owners.add("bob")
        hooks.owners.add("bob")

    body = await _capabilities(client)

    may_submit = RANK[role] >= RANK[min_role]
    has_history = history in ("batches", "webhook")
    assert body == {
        "user_id": "alice",
        "status": "enabled",
        "enabled": True,
        "min_role": min_role,
        "role_may_submit": may_submit,
        "has_history": has_history,
        "history_error": False,
        "show_entry": may_submit or has_history,
    }


async def test_the_default_min_role_is_internal(client, caller):
    for role, may_submit in (("free", False), ("pro", False), ("internal", True), ("admin", True)):
        caller["role"] = role
        body = await _capabilities(client)
        assert body["min_role"] == "internal"
        assert body["role_may_submit"] is may_submit
        assert body["show_entry"] is may_submit


@pytest.mark.parametrize("role", ROLES)
async def test_disabled_offers_nothing_and_reads_no_batch_data(
    client, settings, caller, batches, hooks, role
):
    settings["BATCH_ENABLED"] = "0"
    caller["role"] = role
    batches.owners.add("alice")
    hooks.owners.add("alice")

    body = await _capabilities(client)

    assert body["status"] == "disabled"
    assert body["enabled"] is False
    assert body["role_may_submit"] is False
    assert body["has_history"] is None
    assert body["history_error"] is False
    assert body["show_entry"] is False
    assert batches.calls == [] and hooks.calls == []


async def test_a_store_that_is_not_ready_is_unavailable_and_reads_nothing(
    client, services, caller, hooks
):
    services.batch_store = None
    caller["role"] = "admin"

    body = await _capabilities(client)

    assert body["status"] == "unavailable"
    assert body["enabled"] is True
    assert body["role_may_submit"] is False
    assert body["show_entry"] is False
    assert hooks.calls == []


async def test_an_existing_batch_skips_the_webhook_lookup(client, batches, hooks):
    batches.owners.add("alice")

    assert (await _capabilities(client))["has_history"] is True
    assert hooks.calls == []


async def test_a_failed_batch_lookup_keeps_a_possible_entry_and_reports_the_error(client, batches):
    batches.fail = True

    body = await _capabilities(client)

    assert body["has_history"] is None
    assert body["history_error"] is True
    assert body["show_entry"] is True


async def test_a_failed_batch_lookup_still_finds_a_webhook(client, batches, hooks):
    batches.fail = True
    hooks.owners.add("alice")

    body = await _capabilities(client)

    assert body["has_history"] is True
    assert body["history_error"] is False
    assert body["show_entry"] is True


async def test_a_failed_webhook_lookup_is_not_read_as_no_history(client, hooks):
    hooks.fail = True

    body = await _capabilities(client)

    assert body["has_history"] is None
    assert body["history_error"] is True
    assert body["show_entry"] is True


async def test_no_webhook_store_means_no_webhook_to_manage(client, services, batches):
    services.batch_webhook_store = None

    body = await _capabilities(client)

    assert body["has_history"] is False
    assert body["history_error"] is False
    assert body["show_entry"] is False
    assert batches.calls == ["has_batches"]


async def test_an_unauthenticated_request_is_refused_before_any_lookup(client, batches, hooks):
    response = await client.get(PATH)

    assert response.status_code == 401
    assert batches.calls == [] and hooks.calls == []


async def test_a_config_change_applies_to_the_next_request(client, settings, caller):
    caller["role"] = "pro"
    assert (await _capabilities(client))["role_may_submit"] is False

    settings["BATCH_MIN_ROLE"] = "pro"
    assert (await _capabilities(client))["role_may_submit"] is True

    settings["BATCH_ENABLED"] = "0"
    assert (await _capabilities(client))["status"] == "disabled"


# -- the owner routes do not look at the role ----------------------------------------


class OwnerStore:
    """Just enough of the batch store for the owner routes, scoped by owner."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def add(self, batch_id: str, user_id: str, **fields: Any) -> None:
        self.rows[batch_id] = {
            "id": batch_id,
            "user_id": user_id,
            "api_surface": "openai_inline",
            "status": "in_progress",
            "phase": "runnable",
            "request_count": 1,
            **fields,
        }

    def _own(self, batch_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.rows.get(batch_id)
        return row if row is not None and row["user_id"] == user_id else None

    async def list_batches(self, *, user_id, limit, after=None):
        return [row for row in self.rows.values() if row["user_id"] == user_id][:limit]

    async def get_batch(self, batch_id, *, user_id=None):
        return self._own(batch_id, user_id)

    async def cancel_batch(self, batch_id, *, user_id):
        row = self._own(batch_id, user_id)
        if row is not None:
            row["status"] = "cancelling"
        return row

    async def delete_batch(self, batch_id, *, user_id):
        return self.rows.pop(batch_id, None) if self._own(batch_id, user_id) else None


@pytest.fixture
def owner_store(services) -> OwnerStore:
    store = OwnerStore()
    store.add("batch_mine", "alice")
    store.add("batch_done", "alice", status="completed", phase="sealed")
    store.add("batch_bobs", "bob")
    services.batch_store = store
    return store


async def test_a_user_below_the_min_role_still_manages_their_own_batches(
    client, caller, owner_store, monkeypatch
):
    caller["role"] = "free"

    async def lines(**kwargs):
        yield b'{"custom_id": "a"}\n'

    monkeypatch.setattr(batch_console, "stream_lines", lines)
    monkeypatch.setattr(batch_console, "results_final", lambda row: row["status"] == "completed")

    listed = await client.get("/user/batches", headers=AUTH)
    cancelled = await client.post("/user/batches/batch_mine/cancel", headers=AUTH)
    downloaded = await client.get("/user/batches/batch_done/results", headers=AUTH)
    deleted = await client.delete("/user/batches/batch_mine", headers=AUTH)

    assert listed.status_code == 200
    assert {row["id"] for row in listed.json()["data"]} == {"batch_mine", "batch_done"}
    assert cancelled.status_code == 200
    assert downloaded.status_code == 200 and downloaded.content == b'{"custom_id": "a"}\n'
    assert deleted.status_code == 200
    assert "batch_mine" not in owner_store.rows


async def test_another_users_batch_stays_out_of_reach(client, caller, owner_store):
    caller["role"] = "admin"

    cancelled = await client.post("/user/batches/batch_bobs/cancel", headers=AUTH)
    downloaded = await client.get("/user/batches/batch_bobs/results", headers=AUTH)
    deleted = await client.delete("/user/batches/batch_bobs", headers=AUTH)

    assert [r.status_code for r in (cancelled, downloaded, deleted)] == [404, 404, 404]
    assert owner_store.rows["batch_bobs"]["status"] == "in_progress"
