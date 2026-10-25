"""The cached per-model batch concurrency caps the batch worker reads."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from serving.batch import model_limits
from serving.batch.model_limits import BatchModelLimits


class _Clock:
    """Stands in for the module's ``time``: ``monotonic()`` returns ``now``."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(model_limits, "time", clock)
    return clock


def _store(*results) -> SimpleNamespace:
    """A store whose ``list_model_limits`` answers *results* in turn (an exception raises)."""
    return SimpleNamespace(list_model_limits=AsyncMock(side_effect=list(results)))


async def test_an_override_wins_and_other_models_take_the_default():
    limits = BatchModelLimits(_store({"glm-4.7": 0, "qwen3.6-35b": 12}))

    assert await limits.limit_for("glm-4.7", 4) == 0
    assert await limits.limit_for("qwen3.6-35b", 4) == 12
    assert await limits.limit_for("deepseek-v4", 4) == 4
    assert await limits.overrides() == {"glm-4.7": 0, "qwen3.6-35b": 12}


async def test_the_store_is_read_once_per_ttl(clock):
    store = _store({"glm-4.7": 2}, {"glm-4.7": 3})
    limits = BatchModelLimits(store, ttl=10.0)

    assert await limits.limit_for("glm-4.7", 4) == 2
    clock.now += 9.9
    assert await limits.limit_for("glm-4.7", 4) == 2
    assert await limits.limit_for("qwen3.6-35b", 4) == 4
    assert store.list_model_limits.await_count == 1

    clock.now += 0.1
    assert await limits.limit_for("glm-4.7", 4) == 3
    assert store.list_model_limits.await_count == 2


async def test_invalidate_makes_the_next_read_query_the_store(clock):
    store = _store({"glm-4.7": 2}, {})
    limits = BatchModelLimits(store, ttl=60.0)

    assert await limits.limit_for("glm-4.7", 4) == 2
    limits.invalidate()

    assert await limits.limit_for("glm-4.7", 4) == 4
    assert store.list_model_limits.await_count == 2


async def test_invalidation_fences_a_read_already_in_flight():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def list_model_limits():
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            return {"glm-4.7": 8}  # read before the administrator paused the model
        return {"glm-4.7": 0}

    limits = BatchModelLimits(SimpleNamespace(list_model_limits=list_model_limits))
    task = asyncio.create_task(limits.limit_for("glm-4.7", 4))
    await started.wait()
    limits.invalidate()
    release.set()

    assert await task == 0
    assert await limits.limit_for("glm-4.7", 4) == 0
    assert calls == 2


async def test_a_failed_read_keeps_the_last_snapshot_until_another_ttl(clock, caplog):
    store = _store({"glm-4.7": 0}, ConnectionError("database unreachable"), {"glm-4.7": 6})
    limits = BatchModelLimits(store, ttl=10.0)
    assert await limits.limit_for("glm-4.7", 4) == 0

    clock.now += 10.0
    with caplog.at_level(logging.WARNING, logger=model_limits.__name__):
        assert await limits.limit_for("glm-4.7", 4) == 0
    assert "keeping the last snapshot" in caplog.text
    # The failure counts as this ttl's read: the store is not hammered.
    clock.now += 9.9
    assert await limits.limit_for("glm-4.7", 4) == 0
    assert store.list_model_limits.await_count == 2

    clock.now += 0.1
    assert await limits.limit_for("glm-4.7", 4) == 6
    assert store.list_model_limits.await_count == 3


async def test_a_failed_read_after_invalidation_still_keeps_the_last_snapshot():
    store = _store({"glm-4.7": 1}, ConnectionError("database unreachable"), {"glm-4.7": 2})
    limits = BatchModelLimits(store)
    assert await limits.limit_for("glm-4.7", 4) == 1

    limits.invalidate()
    assert await limits.limit_for("glm-4.7", 4) == 1
    limits.invalidate()
    assert await limits.limit_for("glm-4.7", 4) == 2


async def test_nothing_is_assumed_before_the_first_successful_read():
    # An unread table is unknown, not empty: a model paused there must not run
    # at the default because the first read failed.
    store = _store(ConnectionError("database unreachable"), {"glm-4.7": 0})
    limits = BatchModelLimits(store)

    with pytest.raises(ConnectionError):
        await limits.limit_for("glm-4.7", 4)
    assert await limits.limit_for("glm-4.7", 4) == 0


async def test_a_malformed_answer_counts_as_a_failed_read():
    store = _store({"glm-4.7": 3}, {"glm-4.7": None})
    limits = BatchModelLimits(store)
    assert await limits.limit_for("glm-4.7", 4) == 3

    limits.invalidate()
    assert await limits.limit_for("glm-4.7", 4) == 3


async def test_overrides_returns_a_copy():
    limits = BatchModelLimits(_store({"glm-4.7": 2}))

    (await limits.overrides())["glm-4.7"] = 99

    assert await limits.limit_for("glm-4.7", 4) == 2
