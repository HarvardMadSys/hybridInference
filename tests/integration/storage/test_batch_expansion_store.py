"""The batch store's expansion contracts, against real PostgreSQL.

Surfaces and phases (a validating batch is never claimed, a sealing one never
expires or cancels), Anthropic admission failures settling at create, file
batches staged and published or failed whole, sealing guarded by generation,
attempt accounting (billed once, unknown never zero, replays change nothing),
idempotency (replay, conflict, tombstone, concurrency), atomic per-user caps,
the per-user running cap, deletion and erasure fences, and the shared
interactive-load view.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio

from serving.storage.batch_file_store import BatchFileStore
from serving.storage.batch_store import (
    BatchDeleteInProgress,
    BatchLimitExceeded,
    BatchNotEnded,
    BatchStore,
    IdempotencyConflict,
    IdempotencyTombstoned,
)
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

CHAT = "/v1/chat/completions"


@pytest_asyncio.fixture
async def pool():
    pool = await postgres.create_pool(min_size=1, max_size=8)
    store = BatchStore(pool)
    await store.initialize()
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def store(pool):
    store = BatchStore(pool)
    users: list[str] = []
    store.test_users = users  # type: ignore[attr-defined]
    try:
        yield store
    finally:
        for user in users:
            await store.delete_user_batches(user)


def _user(store: BatchStore) -> str:
    user = f"exp-{uuid.uuid4().hex[:10]}"
    store.test_users.append(user)  # type: ignore[attr-defined]
    return user


def _model() -> str:
    return f"m-{uuid.uuid4().hex[:8]}"


def _items(model: str, n: int, *, prefix: str = "r") -> list[dict[str, Any]]:
    return [
        {"custom_id": f"{prefix}-{i}", "model_id": model, "request": {"model": model, "i": i}}
        for i in range(n)
    ]


async def _create(store: BatchStore, user: str, items: list[dict[str, Any]], **kwargs: Any):
    defaults = {
        "batch_id": f"batch_{uuid.uuid4().hex}",
        "user_id": user,
        "key_hash": None,
        "endpoint": CHAT,
        "model_ids": sorted({item["model_id"] for item in items if not item.get("error")}),
        "items": items,
        "metadata": None,
        "completion_window_seconds": 3600,
        "retention_seconds": 7 * 86400,
    }
    defaults.update(kwargs)
    return await store.create_batch(**defaults)


async def _claim(store: BatchStore, model: str, **kwargs: Any) -> list[dict[str, Any]]:
    options = {"worker_id": "w1", "limit": 10, "max_running": 100, "lease_seconds": 300}
    options.update(kwargs)
    return await store.claim_items(model_id=model, **options)


async def _complete(store: BatchStore, item: dict[str, Any], **kwargs: Any) -> bool:
    options = {
        "item_id": item["id"],
        "worker_id": "w1",
        "attempt": item["attempt_seq"],
        "response": {"ok": item["custom_id"]},
        "error": None,
        "status_code": 200,
    }
    options.update(kwargs)
    return await store.complete_item(**options)


async def _fetch(pool: Any, sql: str, *args: Any) -> list[Any]:
    async with pool.acquire() as conn:
        return await conn.fetch(sql, *args)


# -- phases and surfaces ------------------------------------------------------------


async def test_a_validating_batch_is_never_claimed_settled_or_listed_as_model_work(pool, store):
    user, model = _user(store), _model()
    batch = await _create(
        store, user, [], api_surface="openai_file", phase="validating", model_ids=[model]
    )
    # Stage items as a validator would, then try every path that could run them.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE batch_jobs SET work_owner = 'v1', work_expires_at = now() + interval '1 min' "
            "WHERE id = $1",
            batch["id"],
        )
    assert await store.stage_items(
        batch_id=batch["id"], worker_id="v1", user_id=user, items=_items(model, 3), max_queued=None
    )
    assert model not in await store.active_models()
    assert await _claim(store, model) == []
    await store.sweep(retention_days=7)
    row = await store.get_batch(batch["id"])
    assert (row["status"], row["phase"], row["request_count"]) == ("validating", "validating", 3)


async def test_publishing_flips_the_phase_and_makes_the_items_claimable(store):
    user, model = _user(store), _model()
    batch = await _create(store, user, [], api_surface="openai_file", phase="validating")
    (leased,) = await store.claim_batch_work(phase="validating", worker_id="v1", lease_seconds=60)
    assert leased["id"] == batch["id"]
    assert await store.stage_items(
        batch_id=batch["id"], worker_id="v1", user_id=user, items=_items(model, 2), max_queued=None
    )
    published = await store.publish_validated(
        batch_id=batch["id"], worker_id="v1", model_ids=[model]
    )
    assert (published["phase"], published["status"]) == ("runnable", "in_progress")
    assert len(await _claim(store, model)) == 2


async def test_a_failed_validation_runs_nothing_and_counts_nothing(store):
    user, model = _user(store), _model()
    batch = await _create(store, user, [], api_surface="openai_file", phase="validating")
    await store.claim_batch_work(phase="validating", worker_id="v1", lease_seconds=60)
    await store.stage_items(
        batch_id=batch["id"], worker_id="v1", user_id=user, items=_items(model, 5), max_queued=None
    )
    errors = {"object": "list", "data": [{"code": "invalid_json_line", "line": 6}]}
    assert await store.fail_validation(batch_id=batch["id"], worker_id="v1", errors=errors)
    row = await store.get_batch(batch["id"])
    assert row["status"] in ("validating", "failed")
    assert row["request_count"] == 0
    assert await store.list_results(batch["id"]) == []
    assert await _claim(store, model) == []
    # A file batch seals its (empty) outputs before showing its final status.
    (sealing,) = await store.claim_batch_work(phase="sealing", worker_id="s1", lease_seconds=60)
    sealed = await store.commit_seal(
        batch_id=batch["id"],
        worker_id="s1",
        generation=sealing["seal_generation"],
        files=[],
        event=None,
    )
    assert (sealed["status"], sealed["phase"]) == ("failed", "sealed")
    assert sealed["validation_error"] == errors
    assert sealed["output_file_id"] is None and sealed["error_file_id"] is None


async def test_cancelling_while_validating_ends_cancelled_without_running(store):
    user = _user(store)
    batch = await _create(store, user, [], api_surface="openai_file", phase="validating")
    cancelled = await store.cancel_batch(batch["id"], user_id=user)
    assert cancelled["status"] == "cancelling"
    # No validator is holding it: the sweep's cleanup ends it.
    assert batch["id"] in await store.stale_validations()
    assert await store.fail_validation(batch_id=batch["id"], worker_id=None, errors={})
    row = await store.get_batch(batch["id"])
    assert (row["status"], row["phase"]) == ("cancelling", "sealing")
    assert row["pending_terminal_status"] == "cancelled"
    (lease,) = await store.claim_batch_work(phase="sealing", worker_id="s1", lease_seconds=60)
    sealed = await store.commit_seal(
        batch_id=batch["id"],
        worker_id="s1",
        generation=lease["seal_generation"],
        files=[],
        event=None,
    )
    assert (sealed["status"], sealed["request_count"]) == ("cancelled", 0)
    assert sealed["cancelled_at"] is not None


async def test_anthropic_items_refused_at_admission_are_errored_and_never_claimed(store):
    user, model = _user(store), _model()
    items = [
        *_items(model, 2),
        {
            "custom_id": "bad",
            "model_id": "hidden",
            "request": {"model": "hidden"},
            "error": {
                "code": "model_not_found",
                "message": "Model 'hidden' not found",
                "status": 404,
            },
        },
    ]
    batch = await _create(
        store,
        user,
        items,
        api_surface="anthropic",
        target="messages",
        protocol_profile="2023-06-01",
        endpoint="/v1/messages",
    )
    assert (batch["request_count"], batch["failed_count"], batch["phase"]) == (3, 1, "runnable")
    claimed = await _claim(store, model)
    assert sorted(item["custom_id"] for item in claimed) == ["r-0", "r-1"]
    assert all(item["target"] == "messages" for item in claimed)
    assert claimed[0]["protocol_profile"] == "2023-06-01"


async def test_a_batch_whose_every_item_failed_admission_has_ended_at_create(store):
    user = _user(store)
    items = [
        {
            "custom_id": f"bad-{i}",
            "model_id": "",
            "request": {},
            "error": {"code": "invalid_request", "message": "no", "status": 400},
        }
        for i in range(2)
    ]
    batch = await _create(
        store, user, items, api_surface="anthropic", target="messages", endpoint="/v1/messages"
    )
    assert (batch["phase"], batch["status"]) == ("sealed", "completed")
    assert (batch["request_count"], batch["failed_count"]) == (2, 2)


async def test_sealing_is_not_changed_by_expiry_or_cancel(pool, store):
    user, model = _user(store), _model()
    batch = await _create(store, user, _items(model, 1), api_surface="openai_file")
    (item,) = await _claim(store, model)
    assert await _complete(store, item)
    row = await store.get_batch(batch["id"])
    assert (row["phase"], row["pending_terminal_status"]) == ("sealing", "completed")
    # The window ends while it is still sealing: its outcome stays completed.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE batch_jobs SET expires_at = now() - interval '1 second' WHERE id = $1",
            batch["id"],
        )
    await store.sweep(retention_days=7)
    unchanged = await store.cancel_batch(batch["id"], user_id=user)
    assert (unchanged["status"], unchanged["phase"]) == ("in_progress", "sealing")
    assert unchanged["pending_terminal_status"] == "completed"
    (lease,) = await store.claim_batch_work(phase="sealing", worker_id="s1", lease_seconds=60)
    sealed = await store.commit_seal(
        batch_id=batch["id"],
        worker_id="s1",
        generation=lease["seal_generation"],
        files=[
            {
                "id": f"file-{uuid.uuid4().hex[:12]}",
                "kind": "output",
                "filename": "o.jsonl",
                "bytes": 10,
                "sha256": "x",
                "line_count": 1,
                "renderer_version": 1,
            }
        ],
        event=None,
    )
    assert (sealed["status"], sealed["phase"]) == ("completed", "sealed")
    assert sealed["output_file_id"] is not None


async def test_only_the_newest_seal_generation_can_publish(store):
    user, model = _user(store), _model()
    batch = await _create(store, user, _items(model, 1), api_surface="openai_file")
    (item,) = await _claim(store, model)
    await _complete(store, item)
    (first,) = await store.claim_batch_work(phase="sealing", worker_id="s1", lease_seconds=1)
    await asyncio.sleep(1.2)
    (second,) = await store.claim_batch_work(phase="sealing", worker_id="s2", lease_seconds=60)
    assert second["seal_generation"] == first["seal_generation"] + 1
    stale = await store.commit_seal(
        batch_id=batch["id"],
        worker_id="s1",
        generation=first["seal_generation"],
        files=[],
        event=None,
    )
    assert stale is None
    fresh = await store.commit_seal(
        batch_id=batch["id"],
        worker_id="s2",
        generation=second["seal_generation"],
        files=[],
        event=None,
    )
    assert fresh["phase"] == "sealed"


# -- attempts and cost ---------------------------------------------------------------


async def test_an_attempt_is_billed_once_and_a_replayed_receipt_changes_nothing(pool, store):
    user, model = _user(store), _model()
    batch = await _create(store, user, _items(model, 1))
    (item,) = await _claim(store, model)
    assert await store.mark_dispatched(
        item_id=item["id"], worker_id="w1", attempt=item["attempt_seq"]
    )
    usage = {"prompt_tokens": 7, "completion_tokens": 3}
    for _ in range(2):
        await _complete(
            store,
            item,
            cost_usd=0.5,
            usage=usage,
            prompt_tokens=7,
            completion_tokens=3,
            request_id="req-1",
        )
    row = await store.get_batch(batch["id"])
    assert (row["cost_usd"], row["prompt_tokens"], row["completion_tokens"]) == (0.5, 7, 3)
    (attempt,) = await _fetch(pool, "SELECT * FROM batch_attempts WHERE item_id = $1", item["id"])
    assert (attempt["outcome"], attempt["cost_status"], float(attempt["cost_usd"])) == (
        "succeeded",
        "billed",
        0.5,
    )
    assert attempt["request_id"] == "req-1"


async def test_a_success_without_a_billing_report_is_unknown_not_zero(pool, store):
    user, model = _user(store), _model()
    batch = await _create(store, user, _items(model, 1))
    (item,) = await _claim(store, model)
    await _complete(store, item, cost_status="unknown")
    row = await store.get_batch(batch["id"])
    assert (row["cost_usd"], row["unknown_cost_count"]) == (0.0, 1)
    (result,) = await store.list_results(batch["id"])
    assert (result["cost_usd"], result["cost_status"]) == (None, "unknown")


async def test_an_abandoned_dispatched_attempt_is_unknown_and_its_retry_costs_add_up(pool, store):
    user, model = _user(store), _model()
    batch = await _create(store, user, _items(model, 1))
    (lost,) = await _claim(store, model, lease_seconds=60)
    await store.mark_dispatched(item_id=lost["id"], worker_id="w1", attempt=lost["attempt_seq"])
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE batch_items SET lease_expires_at = now() - interval '1 second' WHERE id = $1",
            lost["id"],
        )
    (again,) = await _claim(store, model, worker_id="w2")
    assert again["attempt_seq"] == lost["attempt_seq"] + 1
    # The retry was billed; the batch counts that, and the lost attempt as unknown.
    await store.retry_item(
        item_id=again["id"],
        worker_id="w2",
        attempt=again["attempt_seq"],
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        error={"code": "rate_limited", "message": "x", "status": 429},
        cost_usd=0.25,
    )
    (third,) = await _claim(store, model, worker_id="w2")
    await _complete(store, third, worker_id="w2", cost_usd=0.5)
    row = await store.get_batch(batch["id"])
    assert (row["cost_usd"], row["unknown_cost_count"]) == (0.75, 1)
    (result,) = await store.list_results(batch["id"])
    assert (result["cost_usd"], result["cost_status"]) == (0.75, "unknown")
    outcomes = [
        (r["outcome"], r["cost_status"])
        for r in await _fetch(
            pool,
            "SELECT outcome, cost_status FROM batch_attempts WHERE item_id = $1 ORDER BY attempt_no",
            lost["id"],
        )
    ]
    assert outcomes == [("abandoned", "unknown"), ("retry", "billed"), ("succeeded", "billed")]


async def test_a_release_before_dispatch_costs_nothing_and_after_dispatch_is_unknown(pool, store):
    user, model = _user(store), _model()
    batch = await _create(store, user, _items(model, 2))
    first, second = await _claim(store, model)
    assert await store.release_item(
        item_id=first["id"], worker_id="w1", attempt=first["attempt_seq"]
    )
    await store.mark_dispatched(item_id=second["id"], worker_id="w1", attempt=second["attempt_seq"])
    assert await store.release_item(
        item_id=second["id"], worker_id="w1", attempt=second["attempt_seq"], dispatched=True
    )
    rows = {
        r["item_id"]: (r["outcome"], r["cost_status"])
        for r in await _fetch(
            pool,
            "SELECT item_id, outcome, cost_status FROM batch_attempts WHERE batch_id = $1",
            batch["id"],
        )
    }
    assert rows == {
        first["id"]: ("released", "not_billed"),
        second["id"]: ("abandoned", "unknown"),
    }
    assert (await store.get_batch(batch["id"]))["unknown_cost_count"] == 1


async def test_an_oversized_result_fails_its_item_and_keeps_its_cost(store):
    user, model = _user(store), _model()
    batch = await _create(store, user, _items(model, 1))
    (item,) = await _claim(store, model)
    await _complete(store, item, response={"text": "x" * 5000}, cost_usd=0.1, max_result_bytes=1000)
    (result,) = await store.list_results(batch["id"])
    assert result["status"] == "failed"
    assert result["error"]["code"] == "result_too_large"
    assert (await store.get_batch(batch["id"]))["cost_usd"] == 0.1


# -- idempotency and caps -------------------------------------------------------------


async def test_idempotent_replay_returns_the_batch_and_a_different_request_conflicts(store):
    user, model = _user(store), _model()
    first = await _create(store, user, _items(model, 1), idempotency={"key": "k1", "digest": "d1"})
    replay = await _create(store, user, _items(model, 1), idempotency={"key": "k1", "digest": "d1"})
    assert replay["id"] == first["id"] and replay["replayed"]
    with pytest.raises(IdempotencyConflict):
        await _create(store, user, _items(model, 2), idempotency={"key": "k1", "digest": "d2"})
    # Another surface, another owner: a separate key space.
    other = await _create(
        store,
        user,
        _items(model, 1),
        idempotency={"key": "k1", "digest": "d1"},
        api_surface="anthropic",
        target="messages",
    )
    assert other["id"] != first["id"]


async def test_concurrent_creates_with_one_key_make_one_batch(store):
    user, model = _user(store), _model()
    results = await asyncio.gather(
        *(
            _create(store, user, _items(model, 3), idempotency={"key": "race", "digest": "same"})
            for _ in range(6)
        )
    )
    assert len({row["id"] for row in results}) == 1
    assert len(await store.list_batches(user_id=user, limit=10)) == 1


async def test_a_deleted_batch_leaves_a_tombstone_for_its_key(store):
    user, model = _user(store), _model()
    batch = await _create(store, user, _items(model, 1), idempotency={"key": "t", "digest": "d"})
    assert await store.delete_batch(batch["id"], user_id=user)
    record = await store.find_idempotent(owner_id=user, api_surface="openai_inline", key="t")
    assert record is not None and record["deleted"]
    with pytest.raises(IdempotencyTombstoned):
        await _create(store, user, _items(model, 1), idempotency={"key": "t", "digest": "d"})


async def test_the_active_batch_cap_holds_under_concurrent_creates(store):
    user, model = _user(store), _model()

    async def attempt(i: int) -> str:
        try:
            await _create(store, user, _items(model, 1, prefix=str(i)), max_active=3)
            return "created"
        except BatchLimitExceeded:
            return "refused"

    outcomes = await asyncio.gather(*(attempt(i) for i in range(10)))
    assert outcomes.count("created") == 3
    assert await store.count_active_batches(user) == 3


async def test_the_queued_request_cap_cannot_be_split_around(store):
    user, model = _user(store), _model()
    await _create(store, user, _items(model, 6, prefix="a"), max_queued=10)
    with pytest.raises(BatchLimitExceeded) as refused:
        await _create(store, user, _items(model, 5, prefix="b"), max_queued=10)
    assert refused.value.kind == "queued_requests"
    await _create(store, user, _items(model, 4, prefix="c"), max_queued=10)


async def test_one_users_running_items_are_capped_across_models(store):
    user, first_model, second_model = _user(store), _model(), _model()
    await _create(store, user, _items(first_model, 5, prefix="x"))
    await _create(store, user, _items(second_model, 5, prefix="y"))
    first = await _claim(store, first_model, user_max_running=3)
    second = await _claim(store, second_model, user_max_running=3)
    assert len(first) + len(second) == 3


async def test_splitting_into_more_batches_earns_no_more_turns(store):
    model = _model()
    splitter, single = _user(store), _user(store)
    for part in range(5):
        await _create(store, splitter, _items(model, 6, prefix=f"s{part}"))
    await _create(store, single, _items(model, 30, prefix="one"))
    served = {splitter: 0, single: 0}
    for _ in range(8):
        claimed = await _claim(store, model, limit=3, max_running=3)
        for item in claimed:
            served[item["user_id"]] += 1
            await _complete(store, item)
    assert abs(served[splitter] - served[single]) <= 1, served


# -- deletion, erasure, leases ---------------------------------------------------------


async def test_anthropic_style_delete_needs_an_ended_batch(store):
    user, model = _user(store), _model()
    batch = await _create(store, user, _items(model, 1), api_surface="anthropic", target="messages")
    with pytest.raises(BatchNotEnded):
        await store.delete_batch(batch["id"], user_id=user, require_ended=True)
    (item,) = await _claim(store, model)
    with pytest.raises(BatchDeleteInProgress):
        await store.delete_batch(batch["id"], user_id=user)
    await _complete(store, item)
    assert await store.delete_batch(batch["id"], user_id=user, require_ended=True)


async def test_a_delete_during_a_download_hides_the_batch_until_the_sweep(pool, store):
    user, model = _user(store), _model()
    batch = await _create(store, user, _items(model, 1))
    (item,) = await _claim(store, model)
    await _complete(store, item)
    lease = await store.take_read_lease(batch_id=batch["id"], ttl_seconds=60)
    assert await store.delete_batch(batch["id"], user_id=user)
    assert await store.get_batch(batch["id"]) is None
    assert await store.batch_exists(batch["id"]), "rows stay while the download reads them"
    with pytest.raises(LookupError):
        await store.take_read_lease(batch_id=batch["id"], ttl_seconds=60)
    await store.sweep(retention_days=7)
    assert await store.batch_exists(batch["id"])
    await store.drop_read_lease(lease)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE batch_jobs SET read_lease_until = now() - interval '1 second' WHERE id = $1",
            batch["id"],
        )
    await store.sweep(retention_days=7)
    assert not await store.batch_exists(batch["id"])


async def test_erasure_removes_everything_and_late_writes_find_nothing(pool, store):
    user, model = _user(store), _model()
    files = BatchFileStore(pool)
    file_id = await files.create_upload(
        user_id=user,
        filename="in.jsonl",
        purpose="batch",
        max_total_bytes=10**6,
        max_uploads_per_hour=10,
    )
    batch = await _create(store, user, _items(model, 1), idempotency={"key": "e", "digest": "d"})
    (item,) = await _claim(store, model)
    assert await store.delete_user_batches(user) == 1
    assert await _complete(store, item) is False
    assert await files.get_file(file_id, include_deleted=True) is None
    assert await store.find_idempotent(owner_id=user, api_surface="openai_inline", key="e") is None
    assert not await store.batch_exists(batch["id"])


# -- shared interactive load -----------------------------------------------------------


async def test_the_shared_view_counts_fresh_rows_of_other_processes(pool, store):
    model = _model()
    await store.publish_interactive_load(process_id="p-a", load={model: 2})
    await store.publish_interactive_load(process_id="p-b", load={model: 3})
    assert (await store.interactive_load(ttl_seconds=10, exclude_process="p-a"))[model] == 3
    assert (await store.interactive_load(ttl_seconds=10))[model] == 5
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE batch_interactive_load SET updated_at = now() - interval '1 minute' "
            "WHERE process_id = 'p-b'"
        )
    assert (await store.interactive_load(ttl_seconds=10)).get(model) == 2
    await store.withdraw_interactive_load("p-a")
    await store.withdraw_interactive_load("p-b")
    assert model not in await store.interactive_load(ttl_seconds=10)
