"""BatchScheduler: claiming, gating and settling batch items, against in-memory fakes.

The store, the per-model caps, the owner's authentication and the dispatch are
stand-ins; the per-user limiter is the real one. Poll intervals are long unless a
test needs time to pass, so whatever drains does so because the worker woke
itself, not because a timer fired.
"""

from __future__ import annotations

import asyncio
import dataclasses
import types
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from serving.batch import config as batch_config_module, scheduler as scheduler_module
from serving.batch.dispatch import DispatchResult
from serving.batch.interactive_load import InteractiveLoad
from serving.batch.scheduler import BatchScheduler, SchedulerTimings
from serving.servers.auth import StoredKeyQuotaExceeded, StoredKeyRejected
from serving.servers.concurrency import UserConcurrencyLimiter, static_limits_provider
from serving.spend_ledger import SPEND_CHECK_KEY, SpendCheck, SpendLedger

OWNER = "owner-1"
OK = DispatchResult(
    status_code=200,
    response={"id": "chatcmpl-1", "object": "chat.completion", "choices": []},
    prompt_tokens=10,
    completion_tokens=5,
    cost_usd=0.25,
    request_id="req_abc",
)
UNAVAILABLE = DispatchResult(
    status_code=503,
    error={"code": "service_unavailable", "message": "down", "status": 503},
    retryable=True,
)
INVALID = DispatchResult(
    status_code=400,
    error={"code": "invalid_request", "message": "bad", "status": 400},
)
TIMED_OUT = DispatchResult(
    status_code=504,
    error={"code": "timeout", "message": "slow", "status": 504},
    retryable=True,
    timed_out=True,
)

# Long waits unless a test shortens them: progress must come from wake-ups.
SLOW = SchedulerTimings(
    poll_seconds=60,
    idle_poll_seconds=60,
    sweep_seconds=3600,
    heartbeat_seconds=3600,
    backoff_base_seconds=0.001,
    backoff_cap_seconds=0.002,
    stop_grace_seconds=0.05,
    record_retry_seconds=(0.0,),
)


class FakeStore:
    """The worker's surface of BatchStore, in memory, with its guards."""

    def __init__(self) -> None:
        self.items: dict[int, dict[str, Any]] = {}
        self.batches: dict[str, dict[str, Any]] = {}
        self.paused: dict[str, datetime] = {}
        self.pause_reasons: dict[str, str | None] = {}
        self.pause_key_hashes: dict[str, str | None] = {}
        self.claims: list[dict[str, Any]] = []
        self.calls: Counter[str] = Counter()
        self.heartbeats: list[list[int]] = []
        self.sweeps: list[int] = []

    def add(self, n: int = 1, *, model: str = "m1", batch: str = "b1", user: str = OWNER, **fields):
        self.batches.setdefault(batch, {"status": "in_progress", "user_id": user, "error": None})
        for _ in range(n):
            item_id = len(self.items) + 1
            self.items[item_id] = {
                "id": item_id,
                "batch_id": batch,
                "custom_id": f"req-{item_id}",
                "model_id": model,
                "request": {"model": model, "messages": [{"role": "user", "content": "hi"}]},
                "attempts": 0,
                "attempt_seq": 0,
                "error": None,
                "user_id": user,
                "key_hash": "owner-key-hash",
                "status": "queued",
                "next_attempt_at": None,
                "claimed_by": None,
                **fields,
            }

    def status(self, item_id: int) -> str:
        return self.items[item_id]["status"]

    def count(self, status: str, model: str | None = None) -> int:
        return sum(
            1
            for item in self.items.values()
            if item["status"] == status and (model is None or item["model_id"] == model)
        )

    def _claimable_batch(self, batch_id: str, now: datetime) -> bool:
        batch = self.batches[batch_id]
        paused = self.paused.get(batch["user_id"])
        return batch["status"] in ("validating", "in_progress") and (
            paused is None or paused <= now
        )

    def _owned(self, item_id: int, worker_id: str, attempt: int) -> dict[str, Any] | None:
        item = self.items.get(item_id)
        if (
            item
            and item["status"] == "running"
            and item["claimed_by"] == worker_id
            and item["attempt_seq"] == attempt
        ):
            return item
        return None

    async def active_models(self) -> list[str]:
        self.calls["active_models"] += 1
        now = datetime.now(UTC)
        return sorted(
            {
                item["model_id"]
                for item in self.items.values()
                if item["status"] in ("queued", "running")
                and self._claimable_batch(item["batch_id"], now)
            }
        )

    async def claim_items(
        self,
        *,
        model_id,
        worker_id,
        limit,
        max_running,
        lease_seconds,
        user_rooms=None,
        exclude_ids=None,
    ):
        rooms = dict(user_rooms or {})
        self.claims.append(
            {
                "model_id": model_id,
                "limit": limit,
                "max_running": max_running,
                "user_rooms": rooms,
                "exclude_ids": sorted(exclude_ids or ()),
            }
        )
        now = datetime.now(UTC)
        live = self.count("running", model_id)
        room = min(limit, max_running - live)
        claimed = []
        per_user: Counter[str] = Counter()
        for item in self.items.values():
            if len(claimed) >= room:
                break
            if item["model_id"] != model_id or item["status"] != "queued":
                continue
            if not self._claimable_batch(item["batch_id"], now):
                continue
            if item["next_attempt_at"] is not None and item["next_attempt_at"] > now:
                continue
            user_room = rooms.get(item["user_id"])
            if user_room is not None and per_user[item["user_id"]] >= user_room:
                continue
            per_user[item["user_id"]] += 1
            item.update(
                status="running",
                attempts=item["attempts"] + 1,
                attempt_seq=item["attempt_seq"] + 1,
                claimed_by=worker_id,
            )
            item["next_attempt_at"] = None
            claimed.append(dict(item))
        return claimed

    async def heartbeat(self, *, worker_id, item_ids, lease_seconds):
        self.heartbeats.append(list(item_ids))
        return len(item_ids)

    async def complete_item(
        self, *, item_id, worker_id, attempt, response, error, status_code, **extra
    ):
        self.calls["complete_item"] += 1
        item = self._owned(item_id, worker_id, attempt)
        if item is None:
            return False
        item.update(
            status="completed" if error is None else "failed",
            response=response,
            error=error,
            status_code=status_code,
            claimed_by=None,
            **extra,
        )
        return True

    async def retry_item(self, *, item_id, worker_id, attempt, next_attempt_at, error, **extra):
        self.calls["retry_item"] += 1
        item = self._owned(item_id, worker_id, attempt)
        if item is None:
            return False
        item.update(status="queued", next_attempt_at=next_attempt_at, error=error, claimed_by=None)
        return True

    async def release_item(
        self, *, item_id, worker_id, attempt, next_attempt_at=None, dispatched=False
    ):
        self.calls["release_item"] += 1
        item = self._owned(item_id, worker_id, attempt)
        if item is None:
            return False
        item.update(
            status="queued",
            attempts=item["attempts"] - 1,
            next_attempt_at=next_attempt_at,
            claimed_by=None,
            released_dispatched=dispatched,
        )
        return True

    async def mark_dispatched(self, *, item_id, worker_id, attempt):
        self.calls["mark_dispatched"] += 1
        return self._owned(item_id, worker_id, attempt) is not None

    async def claim_batch_work(self, *, phase, worker_id, lease_seconds, limit=1):
        return []

    async def stale_validations(self):
        return []

    async def seal_backlog(self):
        return 0

    async def fail_batch(self, batch_id, error):
        batch = self.batches[batch_id]
        if batch["status"] not in ("validating", "in_progress"):
            return False
        batch.update(status="failed", error=error)
        for item in self.items.values():
            if item["batch_id"] == batch_id and item["status"] == "queued":
                item.update(status="failed", error={"code": "batch_failed", "message": "failed"})
        return True

    async def pause_user_batches(self, user_id, until, reason=None, *, key_hash=None):
        self.paused[user_id] = until
        self.pause_reasons[user_id] = reason
        self.pause_key_hashes[user_id] = key_hash
        return sum(1 for b in self.batches.values() if b["user_id"] == user_id)

    async def sweep(self, *, retention_days):
        self.sweeps.append(retention_days)
        return {"expired": 0, "finalized": 0, "purged": 0}


class FakeLimits:
    def __init__(self, caps: dict[str, int] | None = None) -> None:
        self.caps = caps or {}

    async def limit_for(self, model_id: str, default: int) -> int:
        return self.caps.get(model_id, default)


class FakeDispatch:
    """Stands in for dispatch_item: records calls, can hold them, answers per model."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.results: dict[str, DispatchResult] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.running: Counter[str] = Counter()
        self.peak: Counter[str] = Counter()

    def hold(self, model: str) -> asyncio.Event:
        self.gates[model] = asyncio.Event()
        return self.gates[model]

    async def __call__(
        self,
        *,
        app,
        services,
        user_ctx,
        body,
        batch_id,
        target="chat",
        item_id=None,
        attempt=None,
        protocol_profile=None,
        timeout_s=None,
    ):
        model = body["model"]
        self.calls.append(
            {
                "user_ctx": user_ctx,
                "body": body,
                "batch_id": batch_id,
                "target": target,
                "item_id": item_id,
                "attempt": attempt,
            }
        )
        self.running[model] += 1
        self.peak[model] = max(self.peak[model], self.running[model])
        try:
            await asyncio.sleep(0)
            gate = self.gates.get(model)
            if gate is not None:
                await gate.wait()
            return self.results.get(model, OK)
        finally:
            self.running[model] -= 1


def _owner_ctx(**overrides) -> dict[str, Any]:
    ctx = {
        "user_id": OWNER,
        "role": "internal",
        "authenticated": True,
        "is_admin": False,
        "max_concurrent_requests": None,
        "auth_key_hash": "owner-key-hash",
    }
    ctx.update(overrides)
    return ctx


@pytest.fixture
def settings(monkeypatch) -> dict[str, str]:
    values: dict[str, str] = {}
    monkeypatch.setattr(
        batch_config_module, "config_value", lambda key, default=None: values.get(key, default)
    )
    return values


@pytest.fixture
def auth(monkeypatch):
    """The owner's re-authentication: a context, or whatever ``outcome`` raises."""
    state = types.SimpleNamespace(outcome=_owner_ctx(), calls=[])

    async def authenticate(key_hash, op_store):
        state.calls.append(key_hash)
        if isinstance(state.outcome, BaseException):
            raise state.outcome
        return state.outcome

    monkeypatch.setattr(scheduler_module, "authenticate_stored_key", authenticate)
    return state


@pytest.fixture
def dispatch(monkeypatch) -> FakeDispatch:
    fake = FakeDispatch()
    monkeypatch.setattr(scheduler_module, "dispatch_item", fake)
    return fake


@pytest.fixture
def limiter() -> UserConcurrencyLimiter:
    return UserConcurrencyLimiter(
        static_limits_provider({"free": 1, "pro": 3, "internal": 100, "admin": 100})
    )


@pytest.fixture
async def worker(settings, auth, dispatch, limiter):
    """Build schedulers on a store; every one built is stopped afterwards."""
    built: list[BatchScheduler] = []

    def build(store, *, caps=None, timings=SLOW, load=None, router=None):
        services = types.SimpleNamespace(
            router=router,
            operational_store=object(),
            user_concurrency_limiter=limiter,
            model_concurrency_resolver=None,
            embedding_adapters=None,
        )
        scheduler = BatchScheduler(
            app=object(),
            services=services,
            store=store,
            limits=FakeLimits(caps),
            load=load or InteractiveLoad(),
            worker_id="worker-a",
            timings=timings,
        )
        built.append(scheduler)
        return scheduler

    yield build
    for scheduler in built:
        await scheduler.stop()


async def until(predicate, *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.002)


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


# -- draining ---------------------------------------------------------------------


async def test_drains_many_rounds_without_waiting_for_a_poll(worker, dispatch):
    store = FakeStore()
    store.add(10)
    scheduler = worker(store, caps={"m1": 2})

    scheduler.start()
    await until(lambda: store.count("completed") == 10)

    assert dispatch.peak["m1"] <= 2
    assert len(dispatch.calls) == 10
    assert len(store.claims) >= 5
    item = store.items[1]
    assert (item["response"], item["cost_usd"], item["request_id"]) == (
        OK.response,
        0.25,
        "req_abc",
    )
    assert (item["prompt_tokens"], item["completion_tokens"]) == (10, 5)


async def test_dispatch_pins_an_alias_to_the_canonical_model_claimed_at_creation(worker, dispatch):
    store = FakeStore()
    request = {"model": "alias", "messages": [{"role": "user", "content": "hi"}]}
    store.add(model="model-a", request=request)
    scheduler = worker(store, caps={"model-a": 1})

    scheduler.start()
    await until(lambda: store.count("completed") == 1)

    # The persisted request remains byte-for-byte caller-shaped, while dispatch
    # is pinned to the canonical model whose deployment-wide slot was claimed.
    assert store.items[1]["request"] == request
    assert dispatch.calls[0]["body"] == {**request, "model": "model-a"}


async def test_notify_wakes_an_idle_loop(worker):
    store = FakeStore()
    scheduler = worker(store)
    scheduler.start()
    await until(lambda: store.calls["active_models"] >= 1)

    store.add(3)
    scheduler.notify()

    await until(lambda: store.count("completed") == 3)


async def test_runs_no_more_than_the_models_cap(worker, dispatch):
    store = FakeStore()
    store.add(6)
    gate = dispatch.hold("m1")
    scheduler = worker(store, caps={"m1": 3})

    scheduler.start()
    await until(lambda: dispatch.running["m1"] == 3)
    scheduler.notify()
    await settle()

    assert store.count("running") == 3
    assert store.claims[-1]["max_running"] == 3
    gate.set()
    await until(lambda: store.count("completed") == 6)
    assert dispatch.peak["m1"] == 3


async def test_a_zero_cap_pauses_the_model(worker, dispatch):
    store = FakeStore()
    store.add(2)
    scheduler = worker(store, caps={"m1": 0})

    scheduler.start()
    await until(lambda: store.calls["active_models"] >= 1)
    await settle()

    assert store.claims == []
    assert dispatch.calls == []


async def test_interactive_requests_take_batch_slots(worker, dispatch, settings):
    store = FakeStore()
    store.add(4)
    gate = dispatch.hold("m1")
    load = InteractiveLoad()
    scheduler = worker(store, caps={"m1": 3}, load=load)

    with load.track("m1"), load.track("m1"):
        scheduler.start()
        await until(lambda: dispatch.running["m1"] == 1)
        await settle()
        assert store.count("running") == 1
        assert store.claims[-1]["max_running"] == 1

    scheduler.notify()
    await until(lambda: dispatch.running["m1"] == 3)
    gate.set()
    await until(lambda: store.count("completed") == 4)


async def test_yield_can_be_turned_off(worker, dispatch, settings):
    settings["BATCH_YIELD_TO_INTERACTIVE"] = "0"
    store = FakeStore()
    store.add(3)
    gate = dispatch.hold("m1")
    load = InteractiveLoad()
    scheduler = worker(store, caps={"m1": 3}, load=load)

    with load.track("m1"):
        scheduler.start()
        await until(lambda: dispatch.running["m1"] == 3)
    gate.set()


async def test_an_idle_model_is_not_starved_by_a_busy_one(worker, dispatch):
    store = FakeStore()
    store.add(50, model="busy", batch="b-busy")
    store.add(1, model="idle", batch="b-idle")
    dispatch.hold("busy")
    scheduler = worker(store, caps={"busy": 2})

    scheduler.start()

    await until(lambda: store.count("completed", "idle") == 1)
    assert dispatch.running["busy"] == 2
    assert store.count("queued", "busy") == 48


# -- owner gating -----------------------------------------------------------------


async def test_a_rejected_owner_fails_the_batch(worker, dispatch, auth):
    auth.outcome = StoredKeyRejected("the API key was revoked")
    store = FakeStore()
    store.add(3)
    scheduler = worker(store, caps={"m1": 1})

    scheduler.start()
    await until(lambda: store.batches["b1"]["status"] == "failed")

    error = store.batches["b1"]["error"]
    assert error["code"] == "owner_unauthorized"
    assert "revoked" in error["message"]
    assert store.count("failed") == 3
    assert {item["error"]["code"] for item in store.items.values()} == {"batch_failed"}
    assert dispatch.calls == []
    assert auth.calls[0] == "owner-key-hash"


async def test_a_verdict_that_cannot_be_recorded_does_not_spin(worker, dispatch, auth):
    auth.outcome = StoredKeyRejected("the API key was revoked")
    store = FakeStore()
    store.add(1)

    async def fail_batch(batch_id, error):
        raise ConnectionError("db blip")

    store.fail_batch = fail_batch
    scheduler = worker(store)
    before = datetime.now(UTC)

    scheduler.start()
    await until(lambda: store.calls["release_item"] == 1)
    await asyncio.sleep(0.02)

    # Handed back with a delay, so it is not claimed, refused and released again.
    item = store.items[1]
    assert item["status"] == "queued"
    assert item["next_attempt_at"] >= before + timedelta(seconds=SLOW.owner_retry_seconds)
    assert store.calls["release_item"] == 1
    assert dispatch.calls == []


async def test_a_spent_quota_pauses_the_owners_batches_until_it_renews(worker, dispatch, auth):
    reset_at = datetime.now(UTC) + timedelta(hours=3)
    auth.outcome = StoredKeyQuotaExceeded(quota_usd=10.0, spent_usd=10.0, reset_at=reset_at)
    store = FakeStore()
    store.add(2)
    scheduler = worker(store)

    scheduler.start()
    await until(lambda: OWNER in store.paused)
    await settle()

    assert store.paused[OWNER] == reset_at
    assert store.pause_reasons[OWNER] == "quota"
    assert store.count("queued") == 2
    assert {item["attempts"] for item in store.items.values()} == {0}
    assert dispatch.calls == []


async def test_a_spent_monthly_quota_pauses_the_owners_batches_and_checks_again_hourly(
    worker, dispatch, auth
):
    """Not until next month: an admin who raises the quota sees the batches resume."""
    auth.outcome = StoredKeyQuotaExceeded(
        quota_usd=30.0,
        spent_usd=30.0,
        reset_at=datetime.now(UTC) + timedelta(days=20),
        scope="account",
        window="month",
    )
    store = FakeStore()
    store.add(2)
    scheduler = worker(store)
    before = datetime.now(UTC)

    scheduler.start()
    await until(lambda: OWNER in store.paused)
    await settle()

    assert store.pause_reasons[OWNER] == "monthly_quota"
    # The whole account's batches, not one key's.
    assert store.pause_key_hashes[OWNER] is None
    assert before + timedelta(seconds=SLOW.budget_recheck_seconds) <= store.paused[OWNER]
    assert store.paused[OWNER] <= datetime.now(UTC) + timedelta(seconds=SLOW.budget_recheck_seconds)
    assert store.count("queued") == 2
    assert dispatch.calls == []


async def test_a_spent_key_budget_pauses_only_that_keys_batches(worker, dispatch, auth):
    """The account's other keys still have budget; only this key's batches wait."""
    auth.outcome = StoredKeyQuotaExceeded(
        quota_usd=5.0,
        spent_usd=5.0,
        reset_at=datetime.now(UTC) + timedelta(days=20),
        scope="key",
        window="month",
    )
    store = FakeStore()
    store.add(1)
    scheduler = worker(store)
    before = datetime.now(UTC)

    scheduler.start()
    await until(lambda: OWNER in store.paused)
    await settle()

    assert store.pause_reasons[OWNER] == "key_budget"
    assert store.pause_key_hashes[OWNER] == "owner-key-hash"
    # Checked again within the hour rather than at the start of next month,
    # so a budget the owner raises takes effect.
    assert store.paused[OWNER] <= datetime.now(UTC) + timedelta(seconds=SLOW.budget_recheck_seconds)
    assert store.paused[OWNER] >= before + timedelta(seconds=SLOW.budget_recheck_seconds)
    assert store.status(1) == "queued"
    assert dispatch.calls == []


async def test_a_role_that_no_longer_qualifies_pauses_the_owner(worker, dispatch, auth):
    auth.outcome = _owner_ctx(role="pro")
    store = FakeStore()
    store.add(1)
    scheduler = worker(store)
    before = datetime.now(UTC)

    scheduler.start()
    await until(lambda: OWNER in store.paused)

    assert store.paused[OWNER] >= before + timedelta(seconds=SLOW.role_pause_seconds)
    assert store.pause_reasons[OWNER] == "role"
    assert store.status(1) == "queued"
    assert store.items[1]["attempts"] == 0
    assert dispatch.calls == []


async def test_the_minimum_role_is_read_live(worker, dispatch, auth, settings):
    settings["BATCH_MIN_ROLE"] = "pro"
    auth.outcome = _owner_ctx(role="pro")
    store = FakeStore()
    store.add(1)
    scheduler = worker(store)

    scheduler.start()

    await until(lambda: store.count("completed") == 1)


async def test_an_unreachable_store_defers_the_item(worker, dispatch, auth):
    auth.outcome = ConnectionError("db down")
    store = FakeStore()
    store.add(1)
    scheduler = worker(store)
    before = datetime.now(UTC)

    scheduler.start()
    await until(lambda: store.calls["release_item"] == 1)

    item = store.items[1]
    assert item["status"] == "queued"
    assert item["attempts"] == 0
    assert item["next_attempt_at"] >= before + timedelta(seconds=SLOW.owner_retry_seconds)
    assert OWNER not in store.paused
    assert store.batches["b1"]["status"] == "in_progress"


async def test_a_full_batch_quota_defers_the_item(worker, dispatch, settings):
    settings["BATCH_ROLE_CONCURRENCY_INTERNAL"] = "1"
    store = FakeStore()
    store.add(2)
    gate = dispatch.hold("m1")
    scheduler = worker(store)
    before = datetime.now(UTC)

    scheduler.start()
    await until(lambda: dispatch.running["m1"] == 1 and store.calls["release_item"] == 1)

    deferred = [item for item in store.items.values() if item["status"] == "queued"]
    assert len(deferred) == 1
    assert deferred[0]["attempts"] == 0
    assert deferred[0]["next_attempt_at"] >= before + timedelta(seconds=SLOW.slot_retry_seconds)
    assert len(dispatch.calls) == 1
    gate.set()


# Rounds every few milliseconds, and a deferred item claimable again at once: an
# owner at their quota would be claimed, looked up and handed back every round.
BUSY = SchedulerTimings(
    poll_seconds=0.005,
    idle_poll_seconds=0.005,
    sweep_seconds=3600,
    heartbeat_seconds=3600,
    slot_retry_seconds=0.001,
    stop_grace_seconds=0.05,
    record_retry_seconds=(0.0,),
)


async def test_an_owner_at_their_batch_quota_is_skipped_by_the_claim(
    worker, dispatch, settings, auth
):
    settings["BATCH_ROLE_CONCURRENCY_INTERNAL"] = "2"
    store = FakeStore()
    store.add(500)
    gate = dispatch.hold("m1")
    scheduler = worker(store, timings=BUSY)

    scheduler.start()
    await until(lambda: dispatch.running["m1"] == 2 and store.calls["release_item"] == 62)
    lookups = len(auth.calls)
    await until(lambda: len(store.claims) >= 20)

    # Only the first claim, made before the owner's quota was known, handed
    # back what it took (64 claimed, 2 run); every later one was told the
    # owner has no room, so nothing more is claimed, looked up or handed back.
    assert store.calls["release_item"] == 62
    assert len(auth.calls) == lookups <= 64
    assert len(dispatch.calls) == 2
    assert all(claim["user_rooms"] == {OWNER: 0} for claim in store.claims[1:])
    gate.set()
    await until(lambda: store.count("completed") >= 10)


async def test_a_claim_is_told_how_much_of_the_owners_quota_is_left(worker, dispatch, settings):
    settings["BATCH_ROLE_CONCURRENCY_INTERNAL"] = "5"
    store = FakeStore()
    store.add(1, model="m1", batch="b1")
    gates = (dispatch.hold("m1"), dispatch.hold("m2"))
    scheduler = worker(store)

    scheduler.start()
    await until(lambda: dispatch.running["m1"] == 1)
    store.add(10, model="m2", batch="b2")
    scheduler.notify()
    await until(lambda: dispatch.running["m2"] == 4)

    claim = next(claim for claim in store.claims if claim["model_id"] == "m2")
    assert claim["user_rooms"] == {OWNER: 4}
    assert store.count("queued", "m2") == 6
    assert store.calls["release_item"] == 0
    for gate in gates:
        gate.set()


async def test_handing_an_item_back_to_wait_does_not_wake_the_loop(worker, dispatch, settings):
    settings["BATCH_ROLE_CONCURRENCY_INTERNAL"] = "1"
    store = FakeStore()
    store.add(3)
    gate = dispatch.hold("m1")
    scheduler = worker(store)  # SLOW: only wake-ups start a round

    scheduler.start()
    await until(lambda: store.calls["release_item"] == 2)
    await settle()

    assert store.calls["active_models"] == 1
    gate.set()
    # The item that ran frees a slot, and that does wake the loop.
    await until(lambda: store.calls["active_models"] >= 2)


async def test_an_owner_known_to_be_at_quota_is_not_looked_up_again(
    worker, dispatch, settings, auth
):
    """A claim that hands such an owner's item over anyway is deferred before the lookup."""
    settings["BATCH_ROLE_CONCURRENCY_INTERNAL"] = "1"

    class RoomlessStore(FakeStore):
        async def claim_items(self, **options):
            options.pop("user_rooms", None)
            return await super().claim_items(**options)

    store = RoomlessStore()
    store.add(3)
    gate = dispatch.hold("m1")
    scheduler = worker(store, timings=BUSY)

    scheduler.start()
    await until(lambda: dispatch.running["m1"] == 1 and store.calls["release_item"] >= 2)
    lookups = len(auth.calls)
    await until(lambda: store.calls["release_item"] >= 20)

    # Claimed and handed back again and again, but looked up no more.
    assert len(auth.calls) == lookups <= 3
    assert len(dispatch.calls) == 1
    gate.set()


async def test_the_owners_batch_slot_is_held_during_dispatch_and_released_after(
    worker, dispatch, auth
):
    store = FakeStore()
    store.add(1)
    gate = dispatch.hold("m1")
    scheduler = worker(store)

    scheduler.start()
    await until(lambda: dispatch.running["m1"] == 1)
    assert scheduler._role_slots._slots[OWNER].in_use == 1

    gate.set()
    await until(lambda: store.count("completed") == 1)
    assert scheduler._role_slots._slots[OWNER].in_use == 0
    call = dispatch.calls[0]
    assert call["user_ctx"] == auth.outcome
    assert call["batch_id"] == "b1"


async def test_batch_work_does_not_use_the_interactive_concurrency_limit(
    worker, dispatch, auth, limiter
):
    # The owner's interactive budget -- a per-user override of 1 -- is spent.
    auth.outcome = _owner_ctx(max_concurrent_requests=1)
    granted, _, _ = await limiter.try_acquire(OWNER, "internal", False, 1)
    assert granted
    store = FakeStore()
    store.add(2)
    gate = dispatch.hold("m1")
    scheduler = worker(store)

    scheduler.start()
    await until(lambda: dispatch.running["m1"] == 2)

    # Both run on the batch quota; the interactive slot is neither needed nor taken.
    assert scheduler._role_slots._slots[OWNER].in_use == 2
    assert limiter._slots[OWNER].in_use == 1
    gate.set()
    await until(lambda: store.count("completed") == 2)


# -- failures -------------------------------------------------------------------------


async def test_a_retryable_failure_backs_off_then_becomes_final(worker, dispatch):
    dispatch.results["m1"] = UNAVAILABLE
    store = FakeStore()
    store.add(1)
    timings = SchedulerTimings(
        poll_seconds=0.005,
        idle_poll_seconds=60,
        sweep_seconds=3600,
        heartbeat_seconds=3600,
        max_attempts=3,
        backoff_base_seconds=0.001,
        backoff_cap_seconds=0.002,
        cooldown_failures=100,
        stop_grace_seconds=0.05,
    )
    scheduler = worker(store, timings=timings)

    scheduler.start()
    await until(lambda: store.status(1) == "failed")

    item = store.items[1]
    assert item["attempts"] == 3
    assert item["error"] == UNAVAILABLE.error
    assert item["status_code"] == 503
    assert store.calls["retry_item"] == 2
    assert len(dispatch.calls) == 3


def _fast_retries(**overrides) -> SchedulerTimings:
    return SchedulerTimings(
        **{
            "poll_seconds": 0.005,
            "idle_poll_seconds": 60,
            "sweep_seconds": 3600,
            "heartbeat_seconds": 3600,
            "backoff_base_seconds": 0.001,
            "backoff_cap_seconds": 0.002,
            "cooldown_failures": 10_000,
            "stop_grace_seconds": 0.05,
            **overrides,
        }
    )


async def test_a_retryable_failure_keeps_retrying_while_the_window_is_open(worker, dispatch):
    dispatch.results["m1"] = UNAVAILABLE
    store = FakeStore()
    store.add(1, expires_at=datetime.now(UTC) + timedelta(hours=24))
    scheduler = worker(store, timings=_fast_retries())

    scheduler.start()
    # Well past the eight attempts a failure used to get, it is still retried.
    await until(lambda: len(dispatch.calls) >= 12)
    assert store.status(1) != "failed"
    dispatch.results["m1"] = OK
    await until(lambda: store.status(1) == "completed")

    assert store.items[1]["attempts"] >= 13
    assert store.calls["retry_item"] >= 12


async def test_a_retry_that_would_start_after_the_window_fails_now_with_its_error(worker, dispatch):
    dispatch.results["m1"] = UNAVAILABLE
    store = FakeStore()
    store.add(1, expires_at=datetime.now(UTC) + timedelta(seconds=30))
    # The first retry would wait 60-120 s: after the window closes.
    timings = _fast_retries(backoff_base_seconds=120.0, backoff_cap_seconds=120.0)
    scheduler = worker(store, timings=timings)

    scheduler.start()
    await until(lambda: store.status(1) == "failed")

    # Failed with the error it got, not left to expire as though it never ran.
    assert store.items[1]["error"] == UNAVAILABLE.error
    assert store.items[1]["status_code"] == 503
    assert store.calls["retry_item"] == 0
    assert len(dispatch.calls) == 1


async def test_a_batch_with_no_window_retries_however_long_the_backoff(worker, dispatch):
    # No window (BATCH_COMPLETION_WINDOW_HOURS 0): a retry two minutes away is
    # still scheduled, where a closing window would fail the item now.
    dispatch.results["m1"] = UNAVAILABLE
    store = FakeStore()
    store.add(1, expires_at=None)
    timings = _fast_retries(backoff_base_seconds=120.0, backoff_cap_seconds=120.0)
    scheduler = worker(store, timings=timings)

    scheduler.start()
    await until(lambda: store.calls["retry_item"] == 1)

    assert store.status(1) == "queued"
    assert store.items[1]["next_attempt_at"] > datetime.now(UTC) + timedelta(seconds=30)
    assert len(dispatch.calls) == 1


async def test_a_batch_with_no_window_still_stops_at_the_attempt_guard(worker, dispatch):
    dispatch.results["m1"] = UNAVAILABLE
    store = FakeStore()
    store.add(1, expires_at=None)
    scheduler = worker(store, timings=_fast_retries(max_attempts=4))

    scheduler.start()
    await until(lambda: store.status(1) == "failed")

    assert store.items[1]["attempts"] == 4
    assert store.items[1]["error"] == UNAVAILABLE.error
    assert store.calls["retry_item"] == 3


def test_the_attempt_guard_outlasts_a_week_long_window_at_the_backoff_cap():
    timings = SchedulerTimings()
    week = 168 * 3600
    # The shortest jittered delay at the cap is half of it.
    assert timings.max_attempts > week / (timings.backoff_cap_seconds / 2)


def test_backoff_doubles_with_jitter_up_to_the_cap():
    scheduler = BatchScheduler(
        app=None,
        services=None,
        store=None,
        limits=None,
        timings=SchedulerTimings(backoff_base_seconds=5.0, backoff_cap_seconds=60.0),
    )

    for attempt, ceiling in [(1, 5.0), (2, 10.0), (3, 20.0), (4, 40.0), (5, 60.0), (30, 60.0)]:
        for _ in range(20):
            assert ceiling / 2 <= scheduler._backoff(attempt) <= ceiling


async def test_a_caller_error_fails_at_once_and_never_cools_the_model(worker, dispatch):
    dispatch.results["m1"] = INVALID
    store = FakeStore()
    store.add(4)
    timings = SchedulerTimings(
        poll_seconds=60,
        idle_poll_seconds=60,
        sweep_seconds=3600,
        heartbeat_seconds=3600,
        cooldown_failures=1,
        stop_grace_seconds=0.05,
    )
    scheduler = worker(store, caps={"m1": 1}, timings=timings)

    scheduler.start()
    await until(lambda: store.count("failed") == 4)

    assert {item["attempts"] for item in store.items.values()} == {1}
    assert store.calls["retry_item"] == 0
    assert store.items[1]["error"] == INVALID.error


async def test_a_timeout_fails_at_once_and_never_cools_the_model(worker, dispatch):
    # A request generating past every limit: a retry would only run it away
    # again, and it says nothing about the model, which keeps taking work.
    dispatch.results["m1"] = TIMED_OUT
    store = FakeStore()
    store.add(4)
    scheduler = worker(store, caps={"m1": 1}, timings=_fast_retries(cooldown_failures=1))

    scheduler.start()
    await until(lambda: store.count("failed") == 4)

    assert {item["attempts"] for item in store.items.values()} == {1}
    assert store.calls["retry_item"] == 0
    assert store.items[1]["error"] == TIMED_OUT.error
    assert store.items[1]["status_code"] == 504
    assert scheduler._cooldown_until == {}


@pytest.mark.parametrize("threshold", [1, 2])
async def test_retryable_failures_cool_the_model_down(worker, dispatch, threshold):
    dispatch.results["flaky"] = UNAVAILABLE
    store = FakeStore()
    store.add(4, model="flaky", batch="b-flaky")
    timings = SchedulerTimings(
        poll_seconds=0.005,
        idle_poll_seconds=60,
        sweep_seconds=3600,
        heartbeat_seconds=3600,
        backoff_base_seconds=0.001,
        backoff_cap_seconds=0.002,
        cooldown_failures=threshold,
        cooldown_seconds=60,
        stop_grace_seconds=0.05,
    )
    scheduler = worker(store, caps={"flaky": 1}, timings=timings)

    scheduler.start()
    await until(lambda: len(dispatch.calls) == threshold)
    await until(lambda: store.count("running", "flaky") == 0)
    claims = len(store.claims)
    store.add(1, model="healthy", batch="b-healthy")
    scheduler.notify()
    await until(lambda: store.count("completed", "healthy") == 1)
    await asyncio.sleep(0.05)

    # The flaky model's attempts stop at the threshold; the healthy one still runs.
    assert len(dispatch.calls) == threshold + 1
    assert all(c["model_id"] != "flaky" for c in store.claims[claims:])


def _route_table(model: str, base_url: str) -> Any:
    from routing.routers import FixedRouter
    from serving.adapters.base import ModelConfig

    adapter = types.SimpleNamespace(
        config=ModelConfig(id=model, name=model, provider="p", base_url=base_url)
    )
    router = FixedRouter()
    router.register_route(model, [(adapter, 1.0)])
    return router


async def test_an_item_whose_model_has_no_batch_route_fails_without_running(worker, dispatch):
    """A queued item fails with the reason a new batch is refused for, not left to wait."""
    store = FakeStore()
    store.add(2)
    scheduler = worker(store, caps={"m1": 1}, router=_route_table("m1", "https://cloud.test"))

    scheduler.start()
    await until(lambda: store.count("failed") == 2)

    assert dispatch.calls == []
    assert store.calls["mark_dispatched"] == 0
    assert store.calls["retry_item"] == 0
    error = store.items[1]["error"]
    assert error["code"] == "batch_route_unavailable"
    assert error["status"] == 400
    assert "no route that batch requests may use" in error["message"]
    assert store.items[1]["status_code"] == 400


async def test_an_item_whose_model_has_a_batch_route_runs(worker, dispatch):
    store = FakeStore()
    store.add(1)
    scheduler = worker(store, caps={"m1": 1}, router=_route_table("m1", "http://localhost:8000"))

    scheduler.start()
    await until(lambda: store.count("completed") == 1)

    assert len(dispatch.calls) == 1


async def test_an_item_past_its_last_attempt_fails_with_its_last_error(worker, dispatch):
    last = {"code": "timeout", "message": "too slow", "status": 504}
    store = FakeStore()
    store.add(1, attempts=SLOW.max_attempts, error=last)
    scheduler = worker(store)

    scheduler.start()
    await until(lambda: store.status(1) == "failed")

    assert store.items[1]["error"] == last
    assert store.items[1]["status_code"] == 504
    assert dispatch.calls == []


# -- lifecycle ------------------------------------------------------------------------


async def test_stop_releases_running_items(worker, dispatch):
    store = FakeStore()
    store.add(2)
    dispatch.hold("m1")
    scheduler = worker(store)

    scheduler.start()
    await until(lambda: dispatch.running["m1"] == 2)
    await scheduler.stop()

    assert store.count("queued") == 2
    assert {item["attempts"] for item in store.items.values()} == {0}
    assert {item["claimed_by"] for item in store.items.values()} == {None}
    assert dispatch.running["m1"] == 0


async def test_stop_lets_quick_items_finish(worker, dispatch):
    store = FakeStore()
    store.add(1)
    gate = dispatch.hold("m1")
    scheduler = worker(
        store,
        timings=SchedulerTimings(
            poll_seconds=60, idle_poll_seconds=60, sweep_seconds=3600, stop_grace_seconds=2.0
        ),
    )

    scheduler.start()
    await until(lambda: dispatch.running["m1"] == 1)
    stopping = asyncio.create_task(scheduler.stop())
    await settle()
    gate.set()
    await stopping

    assert store.status(1) == "completed"
    assert store.calls["release_item"] == 0


async def test_start_is_idempotent_and_stop_is_safe_before_start(worker):
    store = FakeStore()
    scheduler = worker(store)
    await scheduler.stop()

    scheduler.start()
    first = scheduler._task
    scheduler.start()

    assert scheduler._task is first


async def test_disabled_claims_nothing(worker, dispatch, settings):
    settings["BATCH_ENABLED"] = "0"
    store = FakeStore()
    store.add(2)
    scheduler = worker(store)

    scheduler.start()
    await asyncio.sleep(0.02)
    scheduler.notify()
    await asyncio.sleep(0.02)

    assert store.calls["active_models"] == 0
    assert store.claims == []
    assert store.sweeps == []
    assert dispatch.calls == []


async def test_sweeps_with_the_configured_retention(worker, settings):
    settings["BATCH_RETENTION_DAYS"] = "3"
    store = FakeStore()
    scheduler = worker(store)

    scheduler.start()

    await until(lambda: store.sweeps == [3])


async def test_heartbeats_extend_the_leases_of_running_items(worker, dispatch):
    store = FakeStore()
    store.add(2)
    gate = dispatch.hold("m1")
    scheduler = worker(
        store,
        timings=SchedulerTimings(
            poll_seconds=0.005,
            idle_poll_seconds=0.005,
            sweep_seconds=3600,
            heartbeat_seconds=0.005,
            stop_grace_seconds=0.05,
        ),
    )

    scheduler.start()
    await until(lambda: any(sorted(ids) == [1, 2] for ids in store.heartbeats))
    gate.set()


class LeasedStore(FakeStore):
    """FakeStore with leases as the SQL keeps them.

    A running item whose lease ran out is claimable again unless the claimant
    names it as its own (``honor_exclusions``), and a heartbeat extends only
    leases still live. A sweep takes ``sweep_delay`` seconds.
    """

    def __init__(self, *, sweep_delay: float = 0.0, honor_exclusions: bool = True) -> None:
        super().__init__()
        self.lease_until: dict[int, datetime] = {}
        self.sweep_delay = sweep_delay
        self.honor_exclusions = honor_exclusions

    async def claim_items(
        self,
        *,
        model_id,
        worker_id,
        limit,
        max_running,
        lease_seconds,
        user_rooms=None,
        exclude_ids=None,
    ):
        self.claims.append({"model_id": model_id, "exclude_ids": sorted(exclude_ids or ())})
        now = datetime.now(UTC)
        excluded = set(exclude_ids or ()) if self.honor_exclusions else set()
        live = sum(
            1
            for item_id, item in self.items.items()
            if item["status"] == "running"
            and item["model_id"] == model_id
            and self.lease_until.get(item_id, now) > now
        )
        room = min(limit, max_running - live)
        claimed = []
        for item_id, item in self.items.items():
            if len(claimed) >= room:
                break
            if item["model_id"] != model_id or item_id in excluded:
                continue
            lapsed = item["status"] == "running" and self.lease_until.get(item_id, now) <= now
            waiting = item["next_attempt_at"] is not None and item["next_attempt_at"] > now
            if not (lapsed or (item["status"] == "queued" and not waiting)):
                continue
            item.update(
                status="running",
                attempts=item["attempts"] + 1,
                attempt_seq=item["attempt_seq"] + 1,
                claimed_by=worker_id,
                next_attempt_at=None,
            )
            self.lease_until[item_id] = now + timedelta(seconds=lease_seconds)
            claimed.append(dict(item))
        return claimed

    async def heartbeat(self, *, worker_id, item_ids, lease_seconds):
        now = datetime.now(UTC)
        for item_id in item_ids:
            item = self.items[item_id]
            if (
                item["status"] == "running"
                and item["claimed_by"] == worker_id
                and self.lease_until[item_id] > now
            ):
                self.lease_until[item_id] = now + timedelta(seconds=lease_seconds)
        self.heartbeats.append(list(item_ids))
        return len(item_ids)

    async def sweep(self, *, retention_days):
        self.sweeps.append(retention_days)
        await asyncio.sleep(self.sweep_delay)
        return {"expired": 0, "finalized": 0, "purged": 0}


def _leases(*, lease: int = 1, heartbeat: float = 0.2, sweep: float = 0.05) -> SchedulerTimings:
    """Production's lease, heartbeat and sweep (300 s, 60 s, 60 s), scaled down."""
    return SchedulerTimings(
        poll_seconds=0.02,
        idle_poll_seconds=0.02,
        sweep_seconds=sweep,
        lease_seconds=lease,
        heartbeat_seconds=heartbeat,
        stop_grace_seconds=0.05,
        record_retry_seconds=(0.0,),
    )


async def test_a_slow_sweep_holds_up_neither_claims_nor_leases(worker, dispatch):
    store = LeasedStore(sweep_delay=1.5)
    store.add(1, model="m1", batch="b1")
    gate = dispatch.hold("m1")
    scheduler = worker(store, timings=_leases())

    scheduler.start()
    await until(lambda: dispatch.running["m1"] == 1 and store.sweeps)
    # The sweep is still running: the worker claims new work all the same ...
    store.add(1, model="m2", batch="b2")
    await until(lambda: store.count("completed", "m2") == 1)
    assert len(store.sweeps) == 1
    # ... and keeps the running item's lease, which outlasts the sweep.
    await asyncio.sleep(1.6)
    assert len(dispatch.calls) == 2
    assert store.items[1]["attempt_seq"] == 1
    assert len(store.heartbeats) >= 4
    gate.set()
    await until(lambda: store.count("completed", "m1") == 1)


async def test_the_heartbeat_keeps_leases_while_a_round_is_stuck(worker, dispatch):
    store = LeasedStore()
    store.add(1)
    gate = dispatch.hold("m1")
    stuck = asyncio.Event()
    active_models = store.active_models

    async def slow_active_models():
        if dispatch.calls:
            await stuck.wait()
        return await active_models()

    store.active_models = slow_active_models
    scheduler = worker(store, timings=_leases(heartbeat=0.1))

    scheduler.start()
    await until(lambda: dispatch.running["m1"] == 1)
    beats = len(store.heartbeats)
    await asyncio.sleep(0.5)

    assert len(store.heartbeats) >= beats + 3
    stuck.set()
    gate.set()
    await until(lambda: store.count("completed") == 1)


async def test_an_item_running_here_is_never_claimed_again(worker, dispatch):
    """Not even once its lease has run out (the heartbeat failing all along)."""
    store = LeasedStore()
    store.add(1)

    async def heartbeat_fails(**_kwargs):
        raise ConnectionError("db blip")

    store.heartbeat = heartbeat_fails
    gate = dispatch.hold("m1")
    scheduler = worker(store, timings=_leases(heartbeat=0.05))

    scheduler.start()
    await until(lambda: dispatch.running["m1"] == 1)
    await asyncio.sleep(1.3)  # past the 1 s lease

    assert len(dispatch.calls) == 1
    assert store.claims[-1]["exclude_ids"] == [1]
    gate.set()
    await until(lambda: store.count("completed") == 1)


async def test_a_claim_that_takes_back_a_running_item_does_not_run_it_twice(worker, dispatch):
    """The run in progress goes on under the newer claim, and its outcome counts."""
    store = LeasedStore(honor_exclusions=False)
    store.add(1)
    gate = dispatch.hold("m1")
    scheduler = worker(store, timings=_leases(heartbeat=3600))

    scheduler.start()
    await until(lambda: dispatch.running["m1"] == 1)
    await until(lambda: store.items[1]["attempt_seq"] == 2, timeout=3.0)
    await asyncio.sleep(0.1)

    assert len(dispatch.calls) == 1
    gate.set()
    await until(lambda: store.count("completed") == 1)
    assert store.calls["mark_dispatched"] == 2
    assert len(dispatch.calls) == 1


def _queued(retry_after: float) -> DispatchResult:
    return DispatchResult(
        status_code=503,
        error={"code": "model_queued", "message": "queued", "status": 503},
        retryable=True,
        requeue_after=retry_after,
    )


async def test_a_model_queued_answer_requeues_without_spending_attempts(worker, dispatch):
    # An on-demand model waiting for a node answers "model queued" for as long
    # as it waits: more answers than the item has attempts must not fail it.
    dispatch.results["m1"] = _queued(0.01)
    store = FakeStore()
    store.add(1)
    timings = SchedulerTimings(
        poll_seconds=0.005,
        idle_poll_seconds=60,
        sweep_seconds=3600,
        heartbeat_seconds=3600,
        max_attempts=2,
        cooldown_failures=1,
        stop_grace_seconds=0.05,
    )
    scheduler = worker(store, timings=timings)

    scheduler.start()
    await until(lambda: len(dispatch.calls) >= 4)
    dispatch.results["m1"] = OK
    await until(lambda: store.status(1) == "completed")

    assert store.calls["retry_item"] == 0
    assert store.calls["release_item"] >= 4
    assert store.items[1]["error"] is None


async def test_a_model_queued_answer_holds_the_model(worker, dispatch):
    dispatch.results["m1"] = _queued(60)
    store = FakeStore()
    store.add(2)
    timings = SchedulerTimings(
        poll_seconds=0.005,
        idle_poll_seconds=60,
        sweep_seconds=3600,
        heartbeat_seconds=3600,
        stop_grace_seconds=0.05,
    )
    scheduler = worker(store, caps={"m1": 1}, timings=timings)

    scheduler.start()
    await until(lambda: store.calls["release_item"] == 1)
    await asyncio.sleep(0.05)

    # The model gets no new claims while it waits: the second item stays put.
    assert len(dispatch.calls) == 1
    assert store.count("queued") == 2


async def test_a_week_long_queued_hint_holds_the_model_only_briefly(worker, dispatch, monkeypatch):
    # The answered item waits out its hint, capped at a week; the model's other
    # items must not wait with it. The hold has a short cap of its own.
    monkeypatch.setattr(scheduler_module, "MAX_MODEL_HOLD_SECONDS", 0.05)
    dispatch.results["m1"] = _queued(7 * 86400)
    store = FakeStore()
    store.add(2)
    timings = SchedulerTimings(
        poll_seconds=0.005,
        idle_poll_seconds=60,
        sweep_seconds=3600,
        heartbeat_seconds=3600,
        stop_grace_seconds=0.05,
    )
    scheduler = worker(store, caps={"m1": 1}, timings=timings)

    scheduler.start()
    await until(lambda: len(dispatch.calls) == 2)
    await until(lambda: store.calls["release_item"] == 2)


async def test_a_model_hold_is_capped_and_only_extends(worker):
    now = [1000.0]
    scheduler = worker(FakeStore())
    scheduler._clock = lambda: now[0]
    cap = scheduler_module.MAX_MODEL_HOLD_SECONDS

    scheduler._hold_model("m1", 7 * 86400)
    assert scheduler._cooldown_until["m1"] == 1000.0 + cap

    # A shorter hint later does not cut the hold short.
    now[0] += 60
    scheduler._hold_model("m1", 120)
    assert scheduler._cooldown_until["m1"] == 1000.0 + cap


async def test_a_model_queued_answer_fails_once_the_window_would_close(worker, dispatch):
    # The next try would start after the batch stops starting requests: the
    # item fails with the queued answer rather than expiring unexplained.
    dispatch.results["m1"] = _queued(3600)
    store = FakeStore()
    store.add(1, expires_at=datetime.now(UTC) + timedelta(seconds=60))
    scheduler = worker(store)

    scheduler.start()
    await until(lambda: store.status(1) == "failed")

    assert store.items[1]["error"]["code"] == "model_queued"
    assert store.calls["release_item"] == 0
    assert store.calls["retry_item"] == 0


async def test_a_model_queued_answer_waits_however_long_with_no_window(worker, dispatch):
    # A batch with no window waits out an hour-long (or longer) queue for a
    # node rather than failing.
    dispatch.results["m1"] = _queued(3600)
    store = FakeStore()
    store.add(1, expires_at=None)
    scheduler = worker(store)

    scheduler.start()
    await until(lambda: store.calls["release_item"] == 1)

    assert store.status(1) == "queued"
    assert store.items[1]["error"] is None
    assert store.calls["retry_item"] == 0


@pytest.fixture
def queued_alerts(monkeypatch):
    sent: list[dict[str, Any]] = []

    async def fake_alert(**kwargs):
        sent.append({**kwargs, "context": kwargs["context"]()})
        return True

    monkeypatch.setattr(scheduler_module, "alert_on_transition", fake_alert)
    return sent


async def test_a_long_wait_for_capacity_alerts_once_and_resolves(worker, queued_alerts):
    # Waiting for a GPU job is normal: no alert per answer, one only once the
    # wait outlasts the setting, and its resolution when the model serves.
    now = [0.0]
    scheduler = worker(FakeStore())
    scheduler._clock = lambda: now[0]

    for _ in range(int(6 * 3600 / 120)):
        scheduler._note_queued("m1", alert_hours=6)
        now[0] += 120
    await asyncio.sleep(0)
    assert queued_alerts == []

    scheduler._note_queued("m1", alert_hours=6)
    scheduler._note_queued("m1", alert_hours=6)
    await asyncio.sleep(0)
    assert [a["breached"] for a in queued_alerts] == [True]
    assert queued_alerts[0]["key"] == "batch_model_queued:m1"
    assert queued_alerts[0]["context"]["waiting_hours"] == 6.0

    scheduler._model_succeeded("m1")
    scheduler._model_succeeded("m1")
    await asyncio.sleep(0)
    assert [a["breached"] for a in queued_alerts] == [True, False]


async def test_a_gap_in_queued_answers_starts_a_new_wait(worker, queued_alerts):
    now = [0.0]
    scheduler = worker(FakeStore())
    scheduler._clock = lambda: now[0]

    scheduler._note_queued("m1", alert_hours=6)
    now[0] += 5 * 3600  # no batch work for the model in between
    scheduler._note_queued("m1", alert_hours=6)
    now[0] += 3 * 3600
    scheduler._note_queued("m1", alert_hours=6)  # 8h since the first, 3h of this wait
    scheduler._note_queued("m1", alert_hours=0)  # 0: never
    await asyncio.sleep(0)

    assert queued_alerts == []


async def test_a_queued_answer_counts_toward_the_wait(worker, dispatch, settings, queued_alerts):
    settings["BATCH_QUEUED_ALERT_HOURS"] = "12"
    dispatch.results["m1"] = _queued(60)
    store = FakeStore()
    store.add(1)
    scheduler = worker(store)
    noted: list[tuple[str, int]] = []
    scheduler._note_queued = lambda model, hours: noted.append((model, hours))

    scheduler.start()
    await until(lambda: store.calls["release_item"] == 1)

    assert noted == [("m1", 12)]


# -- the owner's spend, reserved while an item runs ----------------------------------


def _spend_check(ledger: SpendLedger, *, quota_usd: float) -> SpendCheck:
    return SpendCheck(
        ledger=ledger,
        user_id=OWNER,
        key_id=11,
        read_at=0.0,
        account_quota_usd=quota_usd,
        account_spent_usd=0.0,
    )


async def test_an_items_estimated_cost_is_reserved_while_it_runs(worker, dispatch, auth):
    """Counted against the owner's quota with their interactive requests, then given back."""
    ledger = SpendLedger(object())
    auth.outcome = _owner_ctx(**{SPEND_CHECK_KEY: _spend_check(ledger, quota_usd=1.0)})
    store = FakeStore()
    store.add(1)
    gate = dispatch.hold("m1")
    scheduler = worker(store)

    scheduler.start()
    await until(lambda: dispatch.running["m1"] == 1)
    assert ledger._reserved_users[OWNER][1] == 1
    assert ledger._reserved_keys[11][1] == 1

    gate.set()
    await until(lambda: store.count("completed") == 1)
    assert ledger._reserved_users == {}
    assert ledger._reserved_keys == {}


async def test_an_item_that_no_longer_fits_is_deferred_and_its_slot_given_back(
    worker, dispatch, auth
):
    """The owner's interactive requests took the rest of the budget since the check.

    Only their reservation refuses it: the quota is not spent, so the item
    waits a moment rather than pausing the owner's batches until it renews.
    """
    ledger = SpendLedger(object())
    check = _spend_check(ledger, quota_usd=0.015)
    interactive, _ = check.admit()  # in flight elsewhere
    auth.outcome = _owner_ctx(**{SPEND_CHECK_KEY: check})
    store = FakeStore()
    store.add(1)
    scheduler = worker(store)
    before = datetime.now(UTC)

    scheduler.start()
    await until(lambda: store.calls["release_item"] == 1)
    await settle()

    assert OWNER not in store.paused
    assert store.status(1) == "queued"
    assert store.items[1]["next_attempt_at"] >= before + timedelta(seconds=SLOW.spend_retry_seconds)
    assert dispatch.calls == []
    assert scheduler._role_slots._slots[OWNER].in_use == 0
    interactive.release()
    assert ledger._reserved_users == {}


async def test_a_limit_spent_in_the_database_still_pauses_at_admission(worker, dispatch, auth):
    ledger = SpendLedger(object())
    check = dataclasses.replace(_spend_check(ledger, quota_usd=1.0), account_spent_usd=1.0)
    auth.outcome = _owner_ctx(**{SPEND_CHECK_KEY: check})
    store = FakeStore()
    store.add(1)
    scheduler = worker(store)

    scheduler.start()
    await until(lambda: OWNER in store.paused)

    assert store.pause_reasons[OWNER] == "quota"
    assert dispatch.calls == []


async def test_reservations_alone_never_pause_the_owner_until_midnight(worker, dispatch, auth):
    """$1.00 quota, $0.50 spent, and the owner's own items in flight hold $0.01 each.

    Once enough are in flight, the next is refused on their reservations; the
    quota is half unspent, so the owner's items wait a moment, not until the
    day renews, and the next ones run once those in flight are done.
    """
    ledger = SpendLedger(object())
    check = dataclasses.replace(_spend_check(ledger, quota_usd=1.0), account_spent_usd=0.5)
    auth.outcome = _owner_ctx(**{SPEND_CHECK_KEY: check})
    store = FakeStore()
    store.add(80)
    gate = dispatch.hold("m1")
    scheduler = worker(
        store,
        timings=dataclasses.replace(SLOW, spend_retry_seconds=0.05, poll_seconds=0.02),
    )

    scheduler.start()
    await until(lambda: store.calls["release_item"] >= 1)
    in_flight = dispatch.running["m1"]
    assert 0 < in_flight < 80
    assert OWNER not in store.paused
    gate.set()
    await until(lambda: store.count("completed") == 80, timeout=10.0)
    assert OWNER not in store.paused


async def test_a_refusal_at_authentication_on_reservations_alone_waits_a_moment(
    worker, dispatch, auth
):
    """The stored key's check refuses first, counting what is in flight; same rule."""
    ledger = SpendLedger(object())
    check = _spend_check(ledger, quota_usd=0.015)
    interactive, _ = check.admit()
    refusal = check.refusal()
    assert refusal is not None
    auth.outcome = scheduler_module.stored_key_quota_exceeded(refusal, check=check)
    store = FakeStore()
    store.add(2)
    scheduler = worker(store)

    scheduler.start()
    await until(lambda: store.calls["release_item"] >= 1)
    await settle()

    assert OWNER not in store.paused
    assert store.count("queued") == 2
    # The owner is held back here meanwhile: claims give them no room.
    scheduler.notify()
    await until(lambda: len(store.claims) >= 2)
    assert store.claims[-1]["user_rooms"].get(OWNER) == 0
    # Spent in the database, the same refusal pauses as before.
    spent = dataclasses.replace(check, account_spent_usd=0.015)
    auth.outcome = scheduler_module.stored_key_quota_exceeded(spent.refusal(), check=spent)
    scheduler._owner_held.clear()
    for item in store.items.values():
        item["next_attempt_at"] = None
    scheduler.notify()
    await until(lambda: OWNER in store.paused)
    assert store.pause_reasons[OWNER] == "quota"
    interactive.release()


async def test_a_failed_dispatch_gives_the_reservation_back(worker, dispatch, auth, monkeypatch):
    ledger = SpendLedger(object())
    auth.outcome = _owner_ctx(**{SPEND_CHECK_KEY: _spend_check(ledger, quota_usd=1.0)})

    async def broken(**_kwargs):
        raise RuntimeError("dispatch broke")

    monkeypatch.setattr(scheduler_module, "dispatch_item", broken)
    store = FakeStore()
    store.add(1)
    scheduler = worker(store)

    scheduler.start()
    await until(lambda: store.calls["mark_dispatched"] >= 1)
    await settle()

    assert ledger._reserved_users == {}
    assert scheduler._role_slots._slots[OWNER].in_use == 0


# -- the batch's result budget ----------------------------------------------------


async def test_a_batch_whose_results_are_full_runs_nothing_more(worker, dispatch, settings):
    settings["BATCH_MAX_RESULTS_MB_PER_BATCH"] = "1"
    store = FakeStore()
    store.add(2, batch="full")
    store.add(1, batch="room")
    asked = []

    def result_budget_spent(batch_id, budget):
        asked.append((batch_id, budget))
        return batch_id == "full"

    store.result_budget_spent = result_budget_spent
    scheduler = worker(store)

    scheduler.start()
    await until(lambda: store.count("queued") == 0 and store.count("running") == 0)

    assert [c["batch_id"] for c in dispatch.calls] == ["room"]
    full = [item for item in store.items.values() if item["batch_id"] == "full"]
    assert {item["status"] for item in full} == {"failed"}
    assert {item["error"]["code"] for item in full} == {"result_too_large"}
    assert {item["cost_status"] for item in full} == {"not_billed"}
    assert ("full", 1024 * 1024) in asked


async def test_a_stored_result_is_held_to_the_batchs_budget(worker, dispatch, settings):
    settings["BATCH_MAX_RESULTS_MB_PER_BATCH"] = "3"
    store = FakeStore()
    store.add(1)
    scheduler = worker(store)

    scheduler.start()
    await until(lambda: store.count("completed") == 1)

    assert store.items[1]["max_batch_result_bytes"] == 3 * 1024 * 1024


async def test_no_result_budget_when_it_is_turned_off(worker, dispatch, settings):
    settings["BATCH_MAX_RESULTS_MB_PER_BATCH"] = "0"
    store = FakeStore()
    store.add(1)
    store.result_budget_spent = lambda batch_id, budget: True
    scheduler = worker(store)

    scheduler.start()
    await until(lambda: store.count("completed") == 1)

    assert store.items[1]["max_batch_result_bytes"] is None


async def test_after_a_spend_hold_one_item_tries_the_limit_before_the_rest(worker, settings):
    clock = [100.0]
    store = FakeStore()
    scheduler = worker(store)
    scheduler._clock = lambda: clock[0]
    cfg = batch_config_module.batch_config()
    scheduler._owner_held[OWNER] = 130.0

    assert scheduler._owner_rooms(cfg) == {OWNER: 0}
    clock[0] = 131.0
    assert scheduler._owner_rooms(cfg) == {OWNER: 1}
    # Once one of theirs is admitted, the owner is claimed as usual again.
    scheduler._owner_probing.discard(OWNER)
    assert scheduler._owner_rooms(cfg) == {}


@pytest.mark.asyncio
async def test_project_cap_is_shared_by_batch_and_interactive_work():
    limiter = UserConcurrencyLimiter(static_limits_provider({"free": 10}))
    scheduler = object.__new__(BatchScheduler)
    scheduler._role_slots = UserConcurrencyLimiter(static_limits_provider({"free": 10}))
    scheduler._services = types.SimpleNamespace(user_concurrency_limiter=limiter)
    scheduler._owner_slots = Counter()
    user = {"user_id": OWNER, "project_id": "proj_shared", "project_max_concurrent_requests": 1}
    slot = "scope:project:proj_shared"
    granted, _, _ = await limiter.try_acquire(slot, "free", False, 1)
    assert granted

    assert await scheduler._take_user_slot(user, "model") is None
    assert scheduler._role_slots._slots[OWNER].in_use == 0
    limiter.release(slot)

    release = await scheduler._take_user_slot(user, "model")
    assert release is not None
    assert limiter._slots[slot].in_use == 1
    release()
    assert limiter._slots[slot].in_use == 0
    assert scheduler._role_slots._slots[OWNER].in_use == 0
