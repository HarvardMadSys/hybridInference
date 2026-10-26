"""Independent webhook delivery lease regression using real PostgreSQL."""

from collections import Counter

import pytest

from serving.batch import webhooks
from serving.storage.batch_store import BatchStore
from serving.storage.batch_webhook_store import BatchWebhookStore
from tests.fixtures import postgres


@pytest.mark.dbtest
async def test_worker_does_not_send_event_reclaimed_while_waiting_in_local_wave(monkeypatch):
    """Advance only the queued event's lease, simulating a slow preceding send."""
    pool = await postgres.create_pool(min_size=1, max_size=4)
    try:
        await BatchStore(pool).initialize()
        store = BatchWebhookStore(pool)
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE batch_webhook_events, batch_webhooks")
        await store.set_webhook(
            user_id="review-webhook",
            url="https://receiver.example.test",
            secret_encrypted="test-encrypted",
            enabled=True,
        )
        async with pool.acquire() as conn:
            for event_id in ("event-1", "event-2"):
                await conn.execute(
                    "INSERT INTO batch_webhook_events (id,user_id,batch_id,event_type,payload) "
                    "VALUES ($1,'review-webhook','batch-review','batch.completed','{}')",
                    event_id,
                )
        # UPDATE RETURNING has no order guarantee. Choose the permitted order
        # with the slow send first so the lease race is deterministic.
        real_claim = store.claim_due_events

        async def ordered_claim(**kwargs):
            return sorted(await real_claim(**kwargs), key=lambda event: event["id"])

        monkeypatch.setattr(store, "claim_due_events", ordered_claim)
        calls = Counter()

        async def fake_delivery(**kwargs):
            event_id = kwargs["event_id"]
            calls[event_id] += 1
            if event_id == "event-1":
                # Event 2 has waited in worker 1's local wave until its lease expired.
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE batch_webhook_events SET lease_expires_at = now() - interval '1 second' "
                        "WHERE id = 'event-2' AND lease_owner = 'worker-1'"
                    )
                await webhooks.deliver_due_events(
                    webhook_store=store,
                    worker_id="worker-2",
                    allow_private=False,
                    decrypt=lambda _value: "test-secret",
                )
            return True, 200, None

        monkeypatch.setattr(webhooks, "deliver", fake_delivery)
        await webhooks.deliver_due_events(
            webhook_store=store,
            worker_id="worker-1",
            allow_private=False,
            decrypt=lambda _value: "test-secret",
        )
        assert calls["event-1"] == 1
        assert calls["event-2"] == 1, "Worker sent a queued event after another worker completed it"
    finally:
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE batch_webhook_events, batch_webhooks")
        await pool.close()
