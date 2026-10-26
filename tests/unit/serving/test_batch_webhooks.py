"""The completion webhook sender: attempts, payload, refusal and shutdown."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from serving.batch import webhooks as webhooks_module
from serving.batch.webhooks import (
    WebhookSender,
    WebhookTimings,
    webhook_payload,
)

URL = "https://hooks.example.com/batch"


def _row(**overrides: Any) -> dict[str, Any]:
    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    row = {
        "id": "batch_abc",
        "user_id": "u1",
        "key_hash": "hash",
        "endpoint": "/v1/chat/completions",
        "status": "completed",
        "model_ids": ["m1"],
        "request_count": 2,
        "completed_count": 2,
        "failed_count": 0,
        "metadata": {"project": "eval"},
        "error": None,
        "cost_usd": 0.5,
        "created_at": now - timedelta(hours=1),
        "in_progress_at": now - timedelta(minutes=30),
        "completed_at": now,
        "failed_at": None,
        "expired_at": None,
        "cancelling_at": None,
        "cancelled_at": None,
        "expires_at": now + timedelta(hours=23),
        "paused_until": None,
        "webhook_url": URL,
        "webhook_status": "delivering",
        "webhook_attempts": 0,
        "webhook_next_attempt_at": now,
        "webhook_last_status_code": None,
        "webhook_last_error": None,
        "webhook_last_attempt_at": None,
        "webhook_delivered_at": None,
    }
    row.update(overrides)
    return row


class FakeStore:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.claimed: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self.finished: list[dict[str, Any]] = []
        self.released: list[str] = []

    async def claim_webhook_deliveries(self, *, worker_id, limit, lease_seconds, max_attempts):
        claimed, self.rows = self.rows[:limit], self.rows[limit:]
        self.claimed.extend(claimed)
        return claimed

    async def record_webhook_attempt(self, *, batch_id, worker_id, status_code, error):
        self.attempts.append({"batch_id": batch_id, "status_code": status_code, "error": error})
        return len(self.attempts)

    async def finish_webhook_delivery(
        self, *, batch_id, worker_id, delivered, max_attempts, retry=True
    ):
        self.finished.append({"batch_id": batch_id, "delivered": delivered, "retry": retry})
        return True

    async def release_webhook_delivery(self, *, batch_id, worker_id):
        self.released.append(batch_id)
        return True


FAST = WebhookTimings(
    poll_seconds=0.01,
    lease_seconds=60,
    max_in_flight=4,
    stop_grace_seconds=0.2,
    retry_wait_seconds=(0.0, 0.0),
    timeout_seconds=1.0,
)


def _sender(store: FakeStore, handler=None, transport=None) -> WebhookSender:
    if transport is None:
        transport = httpx.MockTransport(handler)
    return WebhookSender(store=store, worker_id="worker-1", timings=FAST, transport=transport)


@pytest.fixture(autouse=True)
def _resolve(monkeypatch):
    async def resolve(_url: str) -> None:
        return None

    monkeypatch.setattr(webhooks_module, "resolve_public_webhook", resolve)


def test_the_default_lease_covers_the_whole_retry_window():
    timings = WebhookTimings()

    assert timings.lease_seconds > sum(timings.retry_wait_seconds) + (
        timings.max_attempts * timings.timeout_seconds
    )


def test_the_payload_is_the_batch_object_plus_its_results_url():
    payload = webhook_payload(_row(), webhooks_module.batch_config())

    assert payload["id"] == "evt_batch_abc"
    assert payload["object"] == "batch.event"
    assert payload["type"] == "batch.finished"
    assert payload["created_at"] == int(_row()["completed_at"].timestamp())
    assert payload["data"]["results_url"] == "/v1/batches/batch_abc/results"
    assert payload["data"]["batch"]["status"] == "completed"
    assert payload["data"]["batch"]["request_counts"] == {
        "total": 2,
        "completed": 2,
        "failed": 0,
    }
    assert URL not in json.dumps(payload)


@pytest.mark.asyncio
async def test_a_2xx_finishes_the_delivery():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    store = FakeStore()
    await _sender(store, handler)._deliver(_row())

    assert len(seen) == 1
    assert seen[0].url == URL
    assert seen[0].headers["X-HybridInference-Event"] == "batch.finished"
    assert seen[0].headers["X-HybridInference-Delivery"] == "evt_batch_abc"
    body = json.loads(seen[0].content)
    assert body["data"]["batch"]["id"] == "batch_abc"
    assert store.attempts == [{"batch_id": "batch_abc", "status_code": 200, "error": None}]
    assert store.finished == [{"batch_id": "batch_abc", "delivered": True, "retry": True}]


@pytest.mark.asyncio
async def test_a_failure_is_retried_and_can_still_succeed():
    codes = iter([500, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(codes))

    store = FakeStore()
    await _sender(store, handler)._deliver(_row())

    assert [attempt["status_code"] for attempt in store.attempts] == [500, 200]
    assert store.attempts[0]["error"] == "HTTP 500"
    assert store.finished == [{"batch_id": "batch_abc", "delivered": True, "retry": True}]


@pytest.mark.asyncio
async def test_the_delivery_gives_up_after_the_attempt_budget():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    store = FakeStore()
    await _sender(store, handler)._deliver(_row())

    assert len(store.attempts) == 3
    assert store.finished == [{"batch_id": "batch_abc", "delivered": False, "retry": True}]


@pytest.mark.asyncio
async def test_a_network_error_counts_as_an_attempt():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    store = FakeStore()
    await _sender(store, handler)._deliver(_row(webhook_attempts=2))

    assert len(store.attempts) == 1
    assert store.attempts[0]["status_code"] is None
    assert "ConnectError" in store.attempts[0]["error"]


@pytest.mark.asyncio
async def test_a_refused_target_fails_without_retrying(monkeypatch):
    async def resolve(_url: str) -> None:
        raise webhooks_module.UnsafeWebhookTarget(
            "host 'localhost' resolves to a non-public address"
        )

    monkeypatch.setattr(webhooks_module, "resolve_public_webhook", resolve)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never sent
        raise AssertionError("a refused target is not posted to")

    store = FakeStore()
    await _sender(store, handler)._deliver(_row())

    assert len(store.attempts) == 1
    assert "non-public" in store.attempts[0]["error"]
    assert store.finished == [{"batch_id": "batch_abc", "delivered": False, "retry": False}]


@pytest.mark.asyncio
async def test_a_lost_claim_is_not_reported_as_delivered():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    class LostClaimStore(FakeStore):
        async def record_webhook_attempt(self, **kwargs):
            return None

    store = LostClaimStore()
    await _sender(store, handler)._deliver(_row())

    assert store.finished == []
    assert store.released == []


@pytest.mark.asyncio
async def test_stopping_hands_an_in_flight_delivery_back():
    gate = asyncio.Event()

    class Blocking(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await gate.wait()
            return httpx.Response(200)

    store = FakeStore([_row()])
    sender = _sender(store, transport=Blocking())
    sender.start()
    for _ in range(100):
        if store.claimed:
            break
        await asyncio.sleep(0.01)

    await sender.stop()

    assert store.claimed
    assert store.released == ["batch_abc"]
    assert store.finished == []
