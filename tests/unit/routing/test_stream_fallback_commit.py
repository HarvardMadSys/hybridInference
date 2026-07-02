"""Regression: streaming fallback must not splice a second provider into an
already-committed SSE stream.

Once a fallback provider has emitted at least one content chunk to the client,
the SSE response is committed to that provider. If that provider then fails
mid-stream, the router must re-raise (closing the stream) rather than trying the
next fallback and splicing a second provider's content into the same response.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _cfg(mid: str, provider: str) -> ModelConfig:
    return ModelConfig(
        id=mid,
        name=mid,
        provider=provider,
        base_url="http://test",
        context_length=8192,
        max_output_length=4096,
    )


class _FailBeforeChunk(BaseAdapter):
    """Primary that fails before yielding anything."""

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise RuntimeError("primary down")

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        raise RuntimeError("primary down")
        yield  # pragma: no cover - make this an async generator


class _YieldThenFail(BaseAdapter):
    """Fallback that yields one real content chunk then fails mid-stream."""

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise RuntimeError("mid-stream down")

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        yield self.format_stream_chunk(model=self.config.id, content="hello")
        raise RuntimeError("mid-stream down")


class _RecordingEcho(BaseAdapter):
    """Fallback that records whether it was ever invoked."""

    def __init__(self, config: ModelConfig) -> None:
        super().__init__(config)
        self.invoked = False

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        self.invoked = True
        return self.format_response(content="spliced", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        self.invoked = True
        yield self.format_stream_chunk(model=self.config.id, content="spliced")


@pytest.mark.unit
def test_fallback_does_not_splice_after_content_committed():
    router = FixedRouter()
    primary = _FailBeforeChunk(_cfg("m", provider="PRIMARY"))
    fb1 = _YieldThenFail(_cfg("m", provider="FB1"))
    fb2 = _RecordingEcho(_cfg("m", provider="FB2"))
    router.register_route("m", [(primary, 1.0), (fb1, 1.0), (fb2, 1.0)])

    # Force the primary to be the initial selection so the fallback loop runs
    # over [fb1, fb2] deterministically (selection is otherwise weighted-random).
    router._select_adapter = lambda *a, **k: primary  # type: ignore[assignment]

    async def _run() -> list[str]:
        collected: list[str] = []
        with pytest.raises(RuntimeError):
            async for chunk in router.stream_chat_completion("m", []):
                collected.append(chunk)
        return collected

    collected = asyncio.run(_run())

    # fb1's content chunk committed the stream; fb2 must never be reached.
    assert fb2.invoked is False
    assert not any("spliced" in c for c in collected)
    # fb1's real content should have been delivered before the stream aborted.
    assert any("hello" in c for c in collected)
