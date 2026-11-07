"""A model removed while one of its requests is upstream.

The admin API pops a runtime model's route when its last route is deleted, and
requests already dispatched to it are still running. When the primary then
fails, the fallback walk finds no route to continue with: the request must end
with the primary's own error, not a ``KeyError`` from the lookup.
"""

from __future__ import annotations

from typing import Any

import pytest

from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig

_MESSAGES = [{"role": "user", "content": "hi"}]


class _RateLimited(Exception):
    status_code = 429


class _RemovedWhileUpstream(BaseAdapter):
    """Fails with a 429 after its model's route has been removed."""

    router: FixedRouter

    async def chat_completion(self, messages: list[dict[str, Any]], **params: Any):
        self.router.routes.pop("m", None)
        raise _RateLimited("rate limited upstream")

    async def stream_chat_completion(self, messages: list[dict[str, Any]], **params: Any):
        self.router.routes.pop("m", None)
        raise _RateLimited("rate limited upstream")
        yield  # pragma: no cover


def _router() -> FixedRouter:
    router = FixedRouter()
    adapter = _RemovedWhileUpstream(
        ModelConfig(id="m", name="m", provider="p", base_url="http://x", endpoint_id="m:p-api")
    )
    adapter.router = router
    router.register_route("m", [(adapter, 1.0)])
    return router


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_primarys_error_surfaces_on_a_buffered_request():
    with pytest.raises(_RateLimited):
        await _router().chat_completion("m", _MESSAGES)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_primarys_error_surfaces_on_a_stream():
    with pytest.raises(_RateLimited):
        async for _ in _router().stream_chat_completion("m", _MESSAGES):
            pass
