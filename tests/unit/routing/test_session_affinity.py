from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest

from routing.routers import (
    AFFINITY_SWEEP_THRESHOLD,
    AFFINITY_TTL_SECONDS,
    FixedRouter,
    _Affinity,
)
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _cfg(mid: str, provider: str = "p", base_url: str = "http://test") -> ModelConfig:
    return ModelConfig(
        id=mid,
        name=mid,
        provider=provider,
        base_url=base_url,
        context_length=8192,
        max_output_length=4096,
    )


class _EchoAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        yield self.format_stream_chunk(model=self.config.id, content="ok")


class _FailAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise RuntimeError("fail")

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        raise RuntimeError("fail")
        yield  # pragma: no cover


@pytest.mark.unit
def test_constants_exposed():
    assert AFFINITY_TTL_SECONDS == 300.0
    assert AFFINITY_SWEEP_THRESHOLD == 1000


@pytest.mark.unit
def test_affinity_dict_initialized_empty():
    r = FixedRouter()
    assert r._affinity == {}


@pytest.mark.unit
def test_drop_affinity_no_op_when_no_key():
    r = FixedRouter()
    req_ctx.set({})
    r._drop_affinity("m")  # must not raise
    assert r._affinity == {}


@pytest.mark.unit
def test_drop_affinity_removes_entry():
    r = FixedRouter()
    r._affinity[("u1", "m")] = _Affinity(endpoint_id="p:host:1", expires_at=time.monotonic() + 60)
    req_ctx.set({"affinity_key": "u1"})
    r._drop_affinity("m")
    assert ("u1", "m") not in r._affinity


@pytest.mark.unit
def test_drop_affinity_other_models_untouched():
    r = FixedRouter()
    r._affinity[("u1", "m1")] = _Affinity(endpoint_id="p:host:1", expires_at=time.monotonic() + 60)
    r._affinity[("u1", "m2")] = _Affinity(endpoint_id="p:host:2", expires_at=time.monotonic() + 60)
    req_ctx.set({"affinity_key": "u1"})
    r._drop_affinity("m1")
    assert ("u1", "m1") not in r._affinity
    assert ("u1", "m2") in r._affinity


@pytest.mark.unit
def test_maybe_sweep_below_threshold_is_noop():
    r = FixedRouter()
    now = time.monotonic()
    r._affinity[("u1", "m")] = _Affinity(endpoint_id="p:host:1", expires_at=now - 1)
    with r._lock:
        r._maybe_sweep_affinity_locked(now)
    assert ("u1", "m") in r._affinity


@pytest.mark.unit
def test_maybe_sweep_above_threshold_drops_expired():
    r = FixedRouter()
    now = time.monotonic()
    for i in range(AFFINITY_SWEEP_THRESHOLD + 1):
        r._affinity[(f"u{i}", "m")] = _Affinity(
            endpoint_id="p:host:1",
            expires_at=now + (60 if i % 2 == 0 else -1),
        )
    with r._lock:
        r._maybe_sweep_affinity_locked(now)
    expired = [k for k, a in r._affinity.items() if a.expires_at < now]
    assert expired == []
