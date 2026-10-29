"""Batch capabilities and owner management over HTTP, against Postgres.

The history lookups (``BatchStore.has_batches``, ``BatchWebhookStore.has_webhook``)
are real queries here: a user's own batches count, another user's and deleted
ones (including one only marked for deletion while a download reads it) do
not, and a saved webhook alone is history. A user whose role is lowered below
``BATCH_MIN_ROLE`` keeps the entry, lists, cancels and deletes their batches,
reads and removes their webhook, but cannot change it; another user's batch
stays out of reach.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI, Header, HTTPException
from httpx import ASGITransport, AsyncClient

from serving.batch import config as batch_config_module
from serving.servers.deps import AppServices, get_current_user
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import batch_console
from serving.storage.batch_store import BatchStore
from serving.storage.batch_webhook_store import BatchWebhookStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

PATH = "/user/batch-capabilities"


@pytest_asyncio.fixture
async def pool():
    pool = await postgres.create_pool(min_size=1, max_size=4)
    await BatchStore(pool).initialize()
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def users(pool):
    tag = uuid.uuid4().hex[:10]
    ids = {name: f"u-bcaps-{name}-{tag}" for name in ("alice", "bob")}
    async with pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO users (id, email, password_hash, status) VALUES ($1, $2, 'x', 'active')",
            [(user_id, f"{user_id}@example.invalid") for user_id in ids.values()],
        )
    try:
        yield ids
    finally:
        store = BatchStore(pool)
        for user_id in ids.values():
            await store.delete_user_batches(user_id)
            await BatchWebhookStore(pool).delete_webhook(user_id)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM users WHERE id = ANY($1::text[])", list(ids.values()))


@pytest.fixture
def settings(monkeypatch) -> dict[str, str]:
    values: dict[str, str] = {}
    monkeypatch.setattr(
        batch_config_module, "config_value", lambda key, default=None: values.get(key, default)
    )
    return values


@pytest_asyncio.fixture
async def gateway(pool, users, settings, monkeypatch):
    callers = {"alice": {"user_id": users["alice"], "role": "internal"}}

    async def identity(authorization: str | None = Header(None)) -> dict[str, Any]:
        caller = callers.get((authorization or "").removeprefix("Bearer "))
        if caller is None:
            raise HTTPException(401, "no credentials")
        return dict(caller)

    async def resolves_public(url, *, allow_private):
        return None

    monkeypatch.setattr(batch_console, "check_resolves_public", resolves_public)
    monkeypatch.setattr(batch_console, "encrypt_api_key", lambda value: f"encrypted:{value}")
    app = FastAPI()
    app.state.services = AppServices(
        router=None, batch_store=BatchStore(pool), batch_webhook_store=BatchWebhookStore(pool)
    )
    app.dependency_overrides[get_current_user] = identity
    install_error_handlers(app)
    app.include_router(batch_console.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw") as client:
        yield client, callers["alice"]


async def _create(pool, user_id: str) -> str:
    batch_id = f"batch_{uuid.uuid4().hex}"
    await BatchStore(pool).create_batch(
        batch_id=batch_id,
        user_id=user_id,
        key_hash=None,
        endpoint="/v1/chat/completions",
        model_ids=["m"],
        items=[
            {
                "custom_id": "r-0",
                "model_id": "m",
                "request": {"model": "m", "messages": [{"role": "user", "content": "q"}]},
            }
        ],
        metadata=None,
        completion_window_seconds=3600,
    )
    return batch_id


AUTH = {"Authorization": "Bearer alice"}


async def test_history_counts_only_the_users_own_undeleted_batches_or_webhook(pool, users):
    batches, hooks = BatchStore(pool), BatchWebhookStore(pool)
    alice, bob = users["alice"], users["bob"]

    assert await batches.has_batches(alice) is False
    assert await hooks.has_webhook(alice) is False

    await _create(pool, bob)
    await hooks.set_webhook(user_id=bob, url="https://h.test/x", secret_encrypted="s", enabled=True)
    assert await batches.has_batches(alice) is False, "another user's batch counted"
    assert await hooks.has_webhook(alice) is False, "another user's webhook counted"

    batch_id = await _create(pool, alice)
    assert await batches.has_batches(alice) is True
    async with pool.acquire() as conn:
        # Marked for deletion, kept only while a download reads it.
        await conn.execute(
            "UPDATE batch_jobs SET delete_requested_at = now() WHERE id = $1", batch_id
        )
    assert await batches.has_batches(alice) is False, "a batch being deleted counted"

    await hooks.set_webhook(
        user_id=alice, url="https://h.test/a", secret_encrypted="s", enabled=False
    )
    assert await hooks.has_webhook(alice) is True


async def test_a_free_user_without_history_gets_no_entry(gateway, settings):
    client, caller = gateway
    caller["role"] = "free"

    body = (await client.get(PATH, headers=AUTH)).json()

    assert body["role_may_submit"] is False
    assert body["has_history"] is False
    assert body["show_entry"] is False


async def test_a_webhook_alone_keeps_the_entry_after_a_downgrade(gateway, pool):
    client, caller = gateway
    saved = await client.put("/user/batch-webhook", json={"url": "https://h.test/a"}, headers=AUTH)
    assert saved.status_code == 200
    caller["role"] = "free"

    body = (await client.get(PATH, headers=AUTH)).json()
    assert body["has_history"] is True and body["show_entry"] is True

    refused = await client.put(
        "/user/batch-webhook", json={"url": "https://h.test/b"}, headers=AUTH
    )
    read = await client.get("/user/batch-webhook", headers=AUTH)
    removed = await client.delete("/user/batch-webhook", headers=AUTH)
    assert refused.status_code == 403
    assert read.status_code == 200 and read.json()["url"] == "https://h.test/a"
    assert removed.status_code == 200 and removed.json() == {"deleted": True}

    after = (await client.get(PATH, headers=AUTH)).json()
    assert after["has_history"] is False and after["show_entry"] is False


async def test_a_downgraded_owner_manages_their_batches_and_not_anyone_elses(gateway, pool, users):
    client, caller = gateway
    mine = await _create(pool, users["alice"])
    other = await _create(pool, users["bob"])
    caller["role"] = "free"

    body = (await client.get(PATH, headers=AUTH)).json()
    assert body["role_may_submit"] is False
    assert body["has_history"] is True and body["show_entry"] is True

    listed = await client.get("/user/batches", headers=AUTH)
    assert [row["id"] for row in listed.json()["data"]] == [mine]

    for response in (
        await client.post(f"/user/batches/{other}/cancel", headers=AUTH),
        await client.get(f"/user/batches/{other}/results", headers=AUTH),
        await client.delete(f"/user/batches/{other}", headers=AUTH),
    ):
        assert response.status_code == 404
    assert await BatchStore(pool).get_batch(other) is not None

    cancelled = await client.post(f"/user/batches/{mine}/cancel", headers=AUTH)
    assert cancelled.status_code == 200
    deleted = await client.delete(f"/user/batches/{mine}", headers=AUTH)
    assert deleted.status_code == 200

    after = (await client.get(PATH, headers=AUTH)).json()
    assert after["has_history"] is False and after["show_entry"] is False


async def test_disabled_answers_200_and_keeps_the_management_routes_closed(
    gateway, pool, users, settings
):
    client, _ = gateway
    batch_id = await _create(pool, users["alice"])
    settings["BATCH_ENABLED"] = "0"

    body = (await client.get(PATH, headers=AUTH)).json()
    assert body["status"] == "disabled" and body["show_entry"] is False

    assert (await client.get("/user/batches", headers=AUTH)).status_code == 404
    assert (await client.delete(f"/user/batches/{batch_id}", headers=AUTH)).status_code == 404
    assert (await client.get("/user/batch-webhook", headers=AUTH)).status_code == 404
    assert (await client.get(PATH)).status_code == 401
