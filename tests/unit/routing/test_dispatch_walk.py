"""What FixedRouter records about its walk on the error it raises (``DispatchWalk``).

A caller that composes pools decides its next step from every attempt of a walk,
not only the one whose error is raised: which routes the request was sent to
(the dispatch report's own signal, not whether the breaker was charged), and
which refused it as malformed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from routing.protocols import RoutingRequestOptions
from routing.routers import FixedRouter, dispatch_walk
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.adapters.upstream_limiter import (
    UpstreamConcurrencyLimiter,
    UpstreamSaturated,
    reset_upstream_limiter,
    upstream_slot,
)
from serving.exceptions import UpstreamRequestTimedOut

MODEL = "m"
MESSAGES = [{"role": "user", "content": "hi"}]


class _BadRequest(Exception):
    """An upstream's 400: the request itself is malformed."""

    status = 400


class _Route(BaseAdapter):
    """A cloud route that fails, before its request leaves the gateway or after."""

    def __init__(
        self,
        name: str,
        *,
        fail_with: BaseException | None = None,
        unsent_with: BaseException | None = None,
    ) -> None:
        super().__init__(
            ModelConfig(
                id=MODEL,
                name=MODEL,
                provider=name,
                base_url=f"https://{name}.example/v1",
                endpoint_id=f"{MODEL}:{name}",
            )
        )
        self.fail_with = fail_with
        self.unsent_with = unsent_with

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        if self.unsent_with is not None:
            raise self.unsent_with
        async with upstream_slot(self.config.provider, "key", base_url=self.config.base_url):
            if self.fail_with is not None:
                raise self.fail_with
            return self.format_response(content=self.config.provider, model=MODEL)

    async def stream_chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> Any:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _limiter():
    reset_upstream_limiter(UpstreamConcurrencyLimiter(initial_limit=4, max_limit=4))
    yield
    reset_upstream_limiter()


@pytest.fixture(autouse=True)
def _first_draw_wins(monkeypatch):
    monkeypatch.setattr("random.random", lambda: 0.0)


def _router(*routes: _Route) -> FixedRouter:
    router = FixedRouter()
    router.register_route(MODEL, [(route, 1.0) for route in routes])
    return router


async def test_the_walk_names_the_routes_the_request_left_the_gateway_for():
    throttled = _Route("throttled", unsent_with=UpstreamSaturated("no outbound slot"))
    timed_out = _Route(
        "timed-out",
        fail_with=UpstreamRequestTimedOut(
            request_info=SimpleNamespace(real_url="https://timed-out.example/v1"),  # type: ignore[arg-type]
            history=(),
            status=504,
            message="upstream_timeout",
        ),
    )

    with pytest.raises(UpstreamSaturated) as caught:
        await _router(throttled, timed_out).chat_completion(MODEL, MESSAGES)

    # The saturation is the error raised; the 504 was sent all the same, though
    # the breaker charges neither.
    walk = dispatch_walk(caught.value)
    assert walk is not None
    assert (walk.sent, walk.refused, walk.unsent) == ((f"{MODEL}:timed-out",), (), ())


async def test_the_walk_names_every_route_that_refused_the_request():
    first = _Route("first", fail_with=_BadRequest("unknown field"))
    second = _Route("second", fail_with=_BadRequest("unknown field"))

    with pytest.raises(_BadRequest) as caught:
        await _router(first, second).chat_completion(MODEL, MESSAGES)

    walk = dispatch_walk(caught.value)
    assert walk is not None
    assert walk.refused == (f"{MODEL}:first", f"{MODEL}:second")
    assert walk.sent == (f"{MODEL}:first", f"{MODEL}:second")


async def test_an_error_raised_at_once_carries_no_walk():
    pinned = _Route("pinned", fail_with=RuntimeError("down"))
    router = _router(pinned, _Route("other"))

    with pytest.raises(RuntimeError) as caught:
        await router.chat_completion(
            MODEL, MESSAGES, routing_options=RoutingRequestOptions(pin_provider="pinned")
        )

    assert dispatch_walk(caught.value) is None
