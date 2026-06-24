"""Unit tests for ``FallbackEmbeddingAdapter``.

Embedding models dispatch to a single adapter object, so multi-route embedding
models are wired through ``FallbackEmbeddingAdapter``, which tries each backend
in order and falls through on failure.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from serving.servers.embedding_fallback import FallbackEmbeddingAdapter


class _StubAdapter:
    """Minimal adapter stub exposing the embedding-relevant surface."""

    def __init__(
        self,
        *,
        provider: str,
        response: dict[str, Any] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.config = SimpleNamespace(
            provider=provider,
            pricing={"prompt": "0", "completion": "0"},
            endpoint_id=f"emb:{provider}",
        )
        self._response = response if response is not None else {"provider": provider}
        self._raises = raises
        self.calls = 0

    async def embeddings(self, input_data, **params):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._response


class _RoutingStub:
    """Adapter stub that raises for inputs in ``fail_inputs`` and otherwise
    echoes which backend served — used to route concurrent requests to
    different backends through the same wrapper.
    """

    def __init__(self, *, provider: str, fail_inputs: frozenset[str] = frozenset()) -> None:
        self.config = SimpleNamespace(
            provider=provider,
            pricing={"prompt": "0", "completion": "0"},
            endpoint_id=f"emb:{provider}",
        )
        self._fail = set(fail_inputs)

    async def embeddings(self, input_data, **params):
        if input_data in self._fail:
            raise RuntimeError(f"{self.config.provider} refuses {input_data}")
        return {"served_by": self.config.provider, "input": input_data}


def test_requires_at_least_one_adapter():
    with pytest.raises(ValueError):
        FallbackEmbeddingAdapter([])


def test_config_reflects_primary():
    primary = _StubAdapter(provider="primary")
    secondary = _StubAdapter(provider="staging")
    wrapper = FallbackEmbeddingAdapter([primary, secondary])
    assert wrapper.config.provider == "primary"
    # serving_config starts at the primary before any request runs.
    assert wrapper.serving_config.provider == "primary"


@pytest.mark.asyncio
async def test_primary_success_does_not_touch_fallback():
    primary = _StubAdapter(provider="primary", response={"ok": "primary"})
    secondary = _StubAdapter(provider="staging")
    wrapper = FallbackEmbeddingAdapter([primary, secondary])

    result = await wrapper.embeddings("hello")

    assert result == {"ok": "primary"}
    assert primary.calls == 1
    assert secondary.calls == 0
    assert wrapper.serving_config.provider == "primary"


@pytest.mark.asyncio
async def test_falls_back_to_secondary_on_primary_failure():
    primary = _StubAdapter(provider="primary", raises=RuntimeError("primary down"))
    secondary = _StubAdapter(provider="staging", response={"ok": "staging"})
    wrapper = FallbackEmbeddingAdapter([primary, secondary])

    result = await wrapper.embeddings("hello")

    assert result == {"ok": "staging"}
    assert primary.calls == 1
    assert secondary.calls == 1
    # serving_config now points at the backend that actually served.
    assert wrapper.serving_config.provider == "staging"


@pytest.mark.asyncio
async def test_raises_last_exception_when_all_fail():
    primary = _StubAdapter(provider="primary", raises=RuntimeError("primary down"))
    secondary = _StubAdapter(provider="staging", raises=ValueError("staging down"))
    wrapper = FallbackEmbeddingAdapter([primary, secondary])

    with pytest.raises(ValueError, match="staging down"):
        await wrapper.embeddings("hello")

    assert primary.calls == 1
    assert secondary.calls == 1


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed():
    """BaseException (e.g. cancellation) must propagate, not trigger fallback."""
    primary = _StubAdapter(provider="primary", raises=asyncio.CancelledError())
    secondary = _StubAdapter(provider="staging", response={"ok": "staging"})
    wrapper = FallbackEmbeddingAdapter([primary, secondary])

    with pytest.raises(asyncio.CancelledError):
        await wrapper.embeddings("hello")

    assert secondary.calls == 0


@pytest.mark.asyncio
async def test_concurrent_requests_get_isolated_serving_config():
    """Two concurrent requests through the same shared wrapper, routed to
    different backends, must each see their own serving_config — even when the
    attribution is read after a further await (where a shared instance attribute
    would be clobbered by the other request).
    """
    primary = _RoutingStub(provider="primary", fail_inputs=frozenset({"to-staging"}))
    staging = _RoutingStub(provider="staging")
    wrapper = FallbackEmbeddingAdapter([primary, staging])

    both_served = asyncio.Event()
    pending = {"count": 2}

    async def call(text):
        resp = await wrapper.embeddings(text)
        served = resp["served_by"]
        # Force an await between serving and reading attribution: wait until
        # BOTH requests have served (and thus both have written their served
        # config) before either reads it back.
        pending["count"] -= 1
        if pending["count"] == 0:
            both_served.set()
        await both_served.wait()
        return served, wrapper.serving_config.provider

    results = await asyncio.gather(call("to-staging"), call("normal"))

    # "to-staging" is refused by primary and served by staging; "normal" is
    # served by primary. Each request's attribution must match who served it.
    for served_by, attributed in results:
        assert served_by == attributed, (served_by, attributed)
    assert sorted(served for served, _ in results) == ["primary", "staging"]
