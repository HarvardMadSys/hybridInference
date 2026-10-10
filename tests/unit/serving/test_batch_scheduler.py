"""Unit tests for the in-process batch scheduler's pure logic."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from serving.batch_gate import ModelLoad
from serving.batch_scheduler import BatchScheduler, ModelAvailability


def _scheduler(**kwargs) -> BatchScheduler:
    services = SimpleNamespace(user_concurrency_limiter=None)
    store = SimpleNamespace(pool=None)
    return BatchScheduler(app=None, services=services, store=store, **kwargs)


def test_availability_cools_down_after_repeated_failures() -> None:
    availability = ModelAvailability(failure_threshold=2, cooldown_seconds=100)
    assert availability.is_available("m")
    availability.record_failure("m")
    assert availability.is_available("m")
    availability.record_failure("m")
    assert not availability.is_available("m")
    availability.record_success("m")
    assert availability.is_available("m")


def test_model_yields_when_others_present() -> None:
    scheduler = _scheduler()
    assert not scheduler._model_allowed("m", ModelLoad(10.0, 0.0, 1000.0))


def test_model_opens_slice_when_free(monkeypatch) -> None:
    scheduler = _scheduler(slice_seconds=900, tick_seconds=600)
    monkeypatch.setattr("serving.batch_scheduler.time.time", lambda: 1000.0)
    assert scheduler._model_allowed("m", ModelLoad(0.0, 0.0, 1000.0))
    assert scheduler._leases["m"].active_until == 1000.0 + 900


def test_model_pauses_at_headroom_within_slice(monkeypatch) -> None:
    scheduler = _scheduler()
    monkeypatch.setattr("serving.batch_scheduler.time.time", lambda: 1000.0)
    assert scheduler._model_allowed("m", ModelLoad(0.0, 0.0, 1000.0))
    assert not scheduler._model_allowed("m", ModelLoad(0.0, 900.0, 1000.0))


def test_model_cools_down_between_slices(monkeypatch) -> None:
    scheduler = _scheduler(slice_seconds=900, tick_seconds=600)
    clock = [1000.0]
    monkeypatch.setattr("serving.batch_scheduler.time.time", lambda: clock[0])

    assert scheduler._model_allowed("m", ModelLoad(0.0, 0.0, 1000.0))
    clock[0] = 1000.0 + 901
    assert not scheduler._model_allowed("m", ModelLoad(0.0, 0.0, 1000.0))
    assert scheduler._leases["m"].cooldown_until == clock[0] + 600
    clock[0] += 601
    assert scheduler._model_allowed("m", ModelLoad(0.0, 0.0, 1000.0))


def test_item_body_forces_non_stream_and_tags_batch() -> None:
    scheduler = _scheduler()
    body = scheduler._item_body({"id": "batch_abc"}, {"request": {"model": "m", "messages": []}})
    assert body["stream"] is False
    assert body["metadata"]["batch_job_id"] == "batch_abc"


@pytest.mark.parametrize("attempts", [1, 2, 3])
def test_backoff_is_bounded(attempts) -> None:
    scheduler = _scheduler(backoff_base_seconds=2.0, backoff_cap_seconds=60.0)
    delay = min(scheduler._backoff_base * (2 ** (attempts - 1)), scheduler._backoff_cap)
    assert 0 < delay <= 60.0
