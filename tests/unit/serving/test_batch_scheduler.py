"""BatchScheduler: claiming, gating and settling batch items, against in-memory fakes.

The store, the per-model caps, the owner's authentication and the dispatch are
stand-ins; the per-user limiter is the real one. Poll intervals are long unless a
test needs time to pass, so whatever drains does so because the worker woke
itself, not because a timer fired.
"""

from __future__ import annotations

import asyncio
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
        self, *, model_id, worker_id, limit, max_running, lease_seconds, user_max_running=0
    ):
        self.claims.append({"model_id": model_id, "limit": limit, "max_running": max_running})
        now = datetime.now(UTC)
        live = self.count("running", model_id)
        room = min(limit, max_running - live)
        claimed = []
        for item in self.items.values():
            if len(claimed) >= room:
                break
            if item["model_id"] != model_id or item["status"] != "queued":
                continue
            if not self._claimable_batch(item["batch_id"], now):
                continue
            if item["next_attempt_at"] is not None and item["next_attempt_at"] > now:
                continue
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

    async def pause_user_batches(self, user_id, until):
        self.paused[user_id] = until
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

    def build(store, *, caps=None, timings=SLOW, load=None):
        services = types.SimpleNamespace(
            router=None,
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
    assert store.count("queued") == 2
    assert {item["attempts"] for item in store.items.values()} == {0}
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


async def test_full_user_slots_defer_the_item(worker, dispatch, auth, limiter):
    auth.outcome = _owner_ctx(max_concurrent_requests=1)
    granted, _, _ = await limiter.try_acquire(OWNER, "internal", False, 1)
    assert granted
    store = FakeStore()
    store.add(1)
    scheduler = worker(store)
    before = datetime.now(UTC)

    scheduler.start()
    await until(lambda: store.calls["release_item"] == 1)

    item = store.items[1]
    assert item["status"] == "queued"
    assert item["attempts"] == 0
    assert item["next_attempt_at"] >= before + timedelta(seconds=SLOW.slot_retry_seconds)
    assert dispatch.calls == []


async def test_the_owners_slot_is_held_during_dispatch_and_released_after(
    worker, dispatch, auth, limiter
):
    store = FakeStore()
    store.add(1)
    gate = dispatch.hold("m1")
    scheduler = worker(store)

    scheduler.start()
    await until(lambda: dispatch.running["m1"] == 1)
    assert limiter._slots[OWNER].in_use == 1

    gate.set()
    await until(lambda: store.count("completed") == 1)
    assert limiter._slots[OWNER].in_use == 0
    call = dispatch.calls[0]
    assert call["user_ctx"] == auth.outcome
    assert call["batch_id"] == "b1"


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
