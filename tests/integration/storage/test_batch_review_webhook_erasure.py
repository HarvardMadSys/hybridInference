"""An authenticated webhook update cannot resurrect data after account erasure."""

import asyncio
import uuid

import pytest

from serving.servers.routers import batch_console
from serving.storage.batch_store import BatchStore
from serving.storage.batch_webhook_store import BatchWebhookStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_webhook_dns_wait_cannot_write_settings_after_owner_erasure(monkeypatch):
    pool = await postgres.create_pool(min_size=1, max_size=6)
    batches, webhooks = BatchStore(pool), BatchWebhookStore(pool)
    owner = f"review-webhook-{uuid.uuid4().hex}"
    entered, resume = asyncio.Event(), asyncio.Event()
    save = None
    await batches.initialize()

    async def paused_dns(*args, **kwargs):
        entered.set()
        await resume.wait()

    monkeypatch.setattr(batch_console, "check_resolves_public", paused_dns)
    monkeypatch.setattr(batch_console, "encrypt_api_key", lambda value: "test-encrypted-secret")
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, password_hash, status) VALUES ($1, $2, 'x', 'active')",
                owner,
                f"{owner}@example.invalid",
            )
        # Identity already authenticated when the request entered this handler.
        save = asyncio.create_task(
            batch_console._save_webhook(
                webhooks,
                owner,
                batch_console.WebhookSettings(url="https://webhook.example.com/events"),
                role="internal",
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        async with pool.acquire() as conn:
            await conn.execute("UPDATE users SET hard_delete_pending = TRUE WHERE id = $1", owner)
        await batches.delete_user_batches(owner)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM users WHERE id = $1", owner)
        resume.set()
        await asyncio.gather(save, return_exceptions=True)
        assert await webhooks.get_webhook(owner) is None, (
            "A request resumed after account erasure persisted an orphan webhook URL/secret"
        )
    finally:
        resume.set()
        if save is not None:
            await asyncio.gather(save, return_exceptions=True)
        await batches.delete_user_batches(owner)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM users WHERE id = $1", owner)
        await pool.close()
