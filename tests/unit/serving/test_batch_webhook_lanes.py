"""Webhook deliveries run side by side, one user at a time, and slow receivers wait (M29).

One receiver that answers slowly, or not at all until every hop has timed
out, used to hold the process's single delivery lane for every user. The
outbox here is an in-memory stand-in with the store's claim, lease and record
contract; ``deliver`` is replaced by a stand-in that takes as long as told.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import Any

import pytest

from serving.batch import webhooks


class Outbox:
    """Pending events, leased one at a time as the store leases them."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.claims: list[list[str]] = []

    def add(self, user_id: str, count: int = 1) -> None:
        for _ in range(count):
            self.events.append(
                {
                    "id": f"evt_{len(self.events)}",
                    "user_id": user_id,
                    "status": "pending",
                    "attempts": 0,
                    "leased": False,
                    "payload": {"id": "x"},
                    "url": f"https://{user_id}.example.com/hook",
                    "secret_encrypted": "s",
                    "enabled": True,
                }
            )

    async def claim_due_events(self, *, worker_id, limit, lease_seconds, exclude_users=None):
        excluded = set(exclude_users or ())
        self.claims.append(sorted(excluded))
        await asyncio.sleep(0)
        for event in self.events:
            if event["status"] == "pending" and not event["leased"]:
                if event["user_id"] in excluded:
                    continue
                event["leased"] = True
                event["attempts"] += 1
                return [dict(event)]
        return []

    def _settle(self, event_id: str, status: str) -> bool:
        event = next(e for e in self.events if e["id"] == event_id)
        event.update(status=status, leased=False)
        return True

    async def mark_delivered(self, *, event_id, worker_id, attempt, status_code):
        return self._settle(event_id, "delivered")

    async def mark_failed(
        self, *, event_id, worker_id, attempt, error, status_code, next_attempt_at
    ):
        return self._settle(event_id, "failed" if next_attempt_at is None else "retry")


class Receivers:
    """Stands in for ``deliver``: each user's receiver takes its own time."""

    def __init__(self, delays: dict[str, float], *, fail: set[str] | None = None) -> None:
        self.delays = delays
        self.fail = fail or set()
        self.running: Counter[str] = Counter()
        self.peak_user: Counter[str] = Counter()
        self.peak = 0
        self.done_at: dict[str, list[float]] = {}

    async def __call__(self, *, url, secret, event_id, payload, allow_private):
        user = url.split("//")[1].split(".")[0]
        self.running[user] += 1
        self.peak_user[user] = max(self.peak_user[user], self.running[user])
        self.peak = max(self.peak, sum(self.running.values()))
        try:
            await asyncio.sleep(self.delays.get(user, 0.0))
        finally:
            self.running[user] -= 1
        self.done_at.setdefault(user, []).append(time.monotonic())
        if user in self.fail:
            return False, None, "TimeoutError: no answer"
        return True, 200, None


async def _run(outbox: Outbox, **options: Any) -> int:
    return await webhooks.deliver_due_events(
        webhook_store=outbox,
        worker_id="w",
        allow_private=False,
        decrypt=lambda value: "whsec_c2VjcmV0",
        poll_seconds=0.01,
        **options,
    )


async def test_a_slow_receiver_holds_up_only_its_own_events(monkeypatch):
    receivers = Receivers({"tarpit": 0.3})
    monkeypatch.setattr(webhooks, "deliver", receivers)
    outbox = Outbox()
    outbox.add("tarpit", 3)
    outbox.add("alice")
    outbox.add("bob")
    started = time.monotonic()

    assert await _run(outbox, slow=webhooks._SlowReceivers()) == 5

    # Everyone else's arrived while the slow receiver's first was still out ...
    assert max(receivers.done_at["alice"] + receivers.done_at["bob"]) - started < 0.2
    # ... and the slow receiver's own went one after the other.
    assert receivers.peak_user["tarpit"] == 1
    assert {event["status"] for event in outbox.events} == {"delivered"}


async def test_deliveries_run_side_by_side_up_to_the_cap(monkeypatch):
    receivers = Receivers({f"user{i}": 0.1 for i in range(10)})
    monkeypatch.setattr(webhooks, "deliver", receivers)
    outbox = Outbox()
    for i in range(10):
        outbox.add(f"user{i}")

    assert await _run(outbox, concurrency=4, slow=webhooks._SlowReceivers()) == 10

    assert receivers.peak == 4
    assert {event["status"] for event in outbox.events} == {"delivered"}


async def test_a_receiver_that_keeps_failing_slowly_is_held_back(monkeypatch):
    monkeypatch.setattr(webhooks, "SLOW_FAILURE_SECONDS", 0.01)
    receivers = Receivers({"tarpit": 0.02}, fail={"tarpit"})
    monkeypatch.setattr(webhooks, "deliver", receivers)
    slow = webhooks._SlowReceivers()
    outbox = Outbox()
    outbox.add("tarpit", webhooks.SLOW_STREAK + 2)

    await _run(outbox, slow=slow)

    # SLOW_STREAK slow failures, then its events wait.
    assert len(receivers.done_at["tarpit"]) == webhooks.SLOW_STREAK
    assert slow.held(time.monotonic()) == ["tarpit"]
    assert sum(event["status"] == "pending" for event in outbox.events) == 2
    assert outbox.claims[-1] == ["tarpit"]


def test_a_hold_grows_with_each_slow_failure_and_any_other_outcome_ends_it():
    slow = webhooks._SlowReceivers()
    for _ in range(webhooks.SLOW_STREAK - 1):
        slow.record("u", slow_failure=True, now=0.0)
    assert slow.held(0.0) == []
    slow.record("u", slow_failure=True, now=0.0)
    assert slow.held(webhooks.SLOW_HOLD_SECONDS - 1) == ["u"]
    assert slow.held(webhooks.SLOW_HOLD_SECONDS + 1) == []
    slow.record("u", slow_failure=True, now=0.0)
    assert slow.held(2 * webhooks.SLOW_HOLD_SECONDS - 1) == ["u"]
    for _ in range(10):
        slow.record("u", slow_failure=True, now=0.0)
    assert slow.held(webhooks.SLOW_HOLD_CAP_SECONDS - 1) == ["u"]
    assert slow.held(webhooks.SLOW_HOLD_CAP_SECONDS + 1) == []
    slow.record("u", slow_failure=True, now=0.0)
    slow.record("u", slow_failure=False, now=0.0)
    assert slow.held(0.0) == []


@pytest.mark.parametrize("fails", [False, True])
async def test_a_claim_that_fails_lets_what_is_being_sent_finish(monkeypatch, fails):
    receivers = Receivers({"alice": 0.05})
    monkeypatch.setattr(webhooks, "deliver", receivers)
    outbox = Outbox()
    outbox.add("alice")
    outbox.add("bob")
    claim = outbox.claim_due_events
    calls = 0

    async def flaky_claim(**kwargs):
        nonlocal calls
        calls += 1
        if fails and calls == 2:
            raise ConnectionError("db blip")
        return await claim(**kwargs)

    outbox.claim_due_events = flaky_claim

    await _run(outbox, slow=webhooks._SlowReceivers())

    assert outbox.events[0]["status"] == "delivered"
    assert outbox.events[1]["status"] == ("pending" if fails else "delivered")
