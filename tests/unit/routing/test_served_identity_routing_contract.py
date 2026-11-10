"""The served identity is read from the routing contract, not re-derived.

Every surface that can publish the serving endpoint's identity -- chat
(non-streaming and streaming), ``/v1/messages`` and ``/v1/embeddings`` --
must answer the same question the same way: may this endpoint's identity be
published at all? The registry decides that from the route's kind, and the
answer travels with the routing state, so a surface cannot disagree with
another about the same endpoint.

These tests pin the routing contract itself: the flag reaches the routing
state, and a route that may not publish stays withheld.
"""

from __future__ import annotations

import json
from typing import Any

from routing.endpoints import endpoint_publishable_for_adapter
from routing.telemetry import routing_chunk
from serving.adapters.base import BaseAdapter, ModelConfig


class _Adapter(BaseAdapter):
    """A minimal adapter carrying only what the routing contract reads."""

    def __init__(self, *, publishable: bool) -> None:
        super().__init__(
            ModelConfig(
                id="model-x",
                name="model-x",
                provider="zai",
                base_url="https://example.test",
                chat_path="/chat/completions",
                endpoint_id="model-x:zai-api",
                endpoint_identity_publishable=publishable,
            )
        )

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise AssertionError("not used")

    async def stream_chat_completion(self, messages, **params):
        raise AssertionError("not used")
        yield  # pragma: no cover -- an async generator needs a yield


def test_the_registry_decision_is_read_from_the_adapter_config() -> None:
    """Publishability is the registry's decision, carried on the adapter."""
    assert endpoint_publishable_for_adapter(_Adapter(publishable=True)) is True
    assert endpoint_publishable_for_adapter(_Adapter(publishable=False)) is False


def test_an_adapter_with_no_flag_publishes_nothing() -> None:
    """An adapter that carries no decision must fail closed."""

    class _Bare(_Adapter):
        def __init__(self) -> None:
            super().__init__(publishable=False)
            del self.config.__dict__["endpoint_identity_publishable"]

    assert endpoint_publishable_for_adapter(_Bare()) is False


def test_the_streaming_routing_chunk_carries_the_flag() -> None:
    """The streaming path publishes the same answer as the non-streaming one."""
    chunk = routing_chunk(_Adapter(publishable=True))
    payload = json.loads(chunk.removeprefix("data: ").strip())
    assert payload["_routing"]["endpoint_publishable"] is True

    chunk = routing_chunk(_Adapter(publishable=False))
    payload = json.loads(chunk.removeprefix("data: ").strip())
    assert payload["_routing"]["endpoint_publishable"] is False
