"""A greedy model on /v1/messages: its own engine first, the cloud once the engine stays full.

This surface picks its own adapter, so it asks the model's greedy router where
(``GreedyRouter.surface_plan``): a local engine while it has a place, by the
router's own deadline. A request that leaves the engine's queue at the deadline
has sent nothing, so it is served once more by the cloud, which writes the only
log row, with the reason it left its engine.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from routing.model_router_registry import ModelRouterRegistry
from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.adapters.dispatch_watch import report_first_token
from serving.adapters.upstream_limiter import (
    UpstreamConcurrencyLimiter,
    get_upstream_limiter,
    reset_upstream_limiter,
    upstream_slot,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

MODEL = "glm-local"
ENGINE_URL = "http://localhost:18003/v1"
ENGINE_ADDRESS = "localhost:18003"
PRICING = {"prompt": "0.6", "completion": "2.2", "image": "0", "request": "0"}


def _auth() -> dict[str, str]:
    from tests.servers.conftest import ANTHROPIC_TEST_API_KEY

    return {"x-api-key": ANTHROPIC_TEST_API_KEY}


def _body(**overrides: Any) -> dict[str, Any]:
    return {
        "model": MODEL,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
        **overrides,
    }


def _event(kind: str, payload: dict[str, Any]) -> bytes:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **payload})}\n\n".encode()


class _Route(BaseAdapter):
    """A route of the model on the Messages surface, sending through an outbound slot."""

    native_format = "anthropic"

    def __init__(self, provider: str, base_url: str, endpoint_id: str) -> None:
        super().__init__(
            ModelConfig(
                id=MODEL,
                name=MODEL,
                provider=provider,
                base_url=base_url,
                endpoint_id=endpoint_id,
                api_key="key",
                max_output_length=1024,
                supported_params=["max_tokens", "stream"],
                pricing=PRICING,
            )
        )
        #: Requests that got past the outbound slot, so reached the upstream.
        self.reached = 0

    async def chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> Any:
        raise NotImplementedError

    async def stream_chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> Any:
        raise NotImplementedError

    async def messages(self, body: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        async with upstream_slot(
            self.config.provider, "key", base_url=self.config.base_url
        ) as slot:
            self.reached += 1
            report_first_token(slot)
            return {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": MODEL,
                "content": [{"type": "text", "text": self.config.provider}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 4, "output_tokens": 1},
            }

    async def stream_messages(
        self, body: dict[str, Any], **_kwargs: Any
    ) -> AsyncGenerator[bytes, None]:
        async with upstream_slot(
            self.config.provider, "key", base_url=self.config.base_url
        ) as slot:
            self.reached += 1
            yield _event(
                "message_start",
                {
                    "message": {
                        "id": "msg_test",
                        "model": MODEL,
                        "role": "assistant",
                        "content": [],
                        "usage": {"input_tokens": 4, "output_tokens": 0},
                    }
                },
            )
            yield _event(
                "content_block_start",
                {"index": 0, "content_block": {"type": "text", "text": ""}},
            )
            report_first_token(slot)
            yield _event(
                "content_block_delta",
                {"index": 0, "delta": {"type": "text_delta", "text": self.config.provider}},
            )
            yield _event("message_stop", {})


@pytest.fixture
def engine() -> _Route:
    return _Route("engine", ENGINE_URL, f"{MODEL}:local-18003")


@pytest.fixture
def cloud() -> _Route:
    return _Route("cloud", "https://cloud.example/v1", f"{MODEL}:cloud-api")


@pytest_asyncio.fixture
async def anthropic_compat_router(engine, cloud):
    """The model's routes, the cloud listed first: route order alone would pick it."""
    router = FixedRouter()
    router.register_route(MODEL, [(cloud, 1.0), (engine, 1.0)])
    return router


@pytest.fixture(autouse=True)
def _limiter():
    reset_upstream_limiter(
        UpstreamConcurrencyLimiter(initial_limit=8, max_limit=8, acquire_timeout=10.0)
    )
    yield
    reset_upstream_limiter()


@pytest.fixture
def logged(monkeypatch) -> list[dict[str, Any]]:
    """Every log row the surface writes, in order."""
    from serving.servers.routers import anthropic_messages as amod

    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        amod, "_schedule_log_store_task", lambda _log_store, **kwargs: rows.append(kwargs)
    )
    return rows


@pytest_asyncio.fixture
async def make_greedy(anthropic_app_services, anthropic_compat_router):
    """Route the model with ``router: greedy`` and the given params, its queues registered."""
    started: list[Any] = []

    async def make(**params: Any) -> Any:
        registry = ModelRouterRegistry(
            {MODEL: {"router": "greedy", "router_params": params}},
            shared_fixed_router=anthropic_compat_router,
        )
        anthropic_app_services.model_router_registry = registry
        router = registry.get_router(MODEL)
        await router.start()
        started.append(router)
        return router

    yield make
    for router in started:
        await router.stop()


async def _take_the_engine() -> Any:
    """Hold the engine's one place, as a request waiting for its first token does."""
    return await get_upstream_limiter().acquire("engine", "key", base_url=ENGINE_URL)


def _waiting() -> int:
    return get_upstream_limiter().engine_snapshot()[ENGINE_ADDRESS]["waiting"]


async def test_a_free_engine_takes_the_request_before_a_cloud_route_listed_first(
    anthropic_test_client, make_greedy, engine, cloud, logged
):
    await make_greedy()

    resp = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())

    assert resp.status_code == 200
    assert resp.json()["content"][0]["text"] == "engine"
    assert (engine.reached, cloud.reached) == (1, 0)
    assert [row["metadata"].get("offload") for row in logged] == [None]


async def test_spillover_sends_a_request_that_finds_no_place_to_the_cloud(
    anthropic_test_client, make_greedy, engine, cloud, logged
):
    await make_greedy()
    seat = await _take_the_engine()

    resp = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())

    assert resp.status_code == 200
    assert resp.json()["content"][0]["text"] == "cloud"
    assert (engine.reached, cloud.reached) == (0, 1)
    # One row, the cloud's, saying why the request left its engine.
    assert [(row["provider"], row["metadata"].get("offload")) for row in logged] == [
        ("cloud", "spillover")
    ]
    seat.release(status_code=200)


async def test_a_stream_that_finds_no_place_is_answered_by_the_cloud(
    anthropic_test_client, make_greedy, engine, cloud, logged
):
    await make_greedy()
    seat = await _take_the_engine()

    async with anthropic_test_client.stream(
        "POST", "/v1/messages", json=_body(stream=True), headers=_auth()
    ) as resp:
        body = b"".join([chunk async for chunk in resp.aiter_bytes()])

    assert resp.status_code == 200
    assert b'"text": "cloud"' in body
    assert b"event: error" not in body
    assert (engine.reached, cloud.reached) == (0, 1)
    assert [(row["provider"], row["metadata"].get("offload")) for row in logged] == [
        ("cloud", "spillover")
    ]
    seat.release(status_code=200)


async def test_cutoff_waits_for_a_place_and_takes_it_when_it_frees(
    anthropic_test_client, make_greedy, engine, cloud, logged
):
    await make_greedy(
        cutoff={
            "ttft_budget": {"fixed_ms": 6000, "per_token_ms": 0},
            "cloud_ttft": {"fixed_ms": 1000, "per_input_token_ms": 0},
        }
    )
    seat = await _take_the_engine()

    post = asyncio.create_task(
        anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())
    )
    async with asyncio.timeout(5):
        while _waiting() != 1:
            await asyncio.sleep(0.005)
    seat.release(status_code=200)
    resp = await post

    assert resp.json()["content"][0]["text"] == "engine"
    assert cloud.reached == 0
    assert [row["metadata"].get("offload") for row in logged] == [None]


async def test_a_request_no_local_engine_can_take_goes_to_the_cloud_as_a_last_resort(
    anthropic_test_client, anthropic_compat_router, make_greedy, engine, cloud, logged
):
    # Admin-disabled: still the model's engine, not a candidate.
    anthropic_compat_router.register_route(MODEL, [(cloud, 1.0), (engine, 0.0)])
    await make_greedy()

    resp = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())

    assert resp.json()["content"][0]["text"] == "cloud"
    assert [row["metadata"].get("offload") for row in logged] == ["last_resort"]


async def test_a_model_routed_by_fixed_keeps_this_surfaces_own_pick(
    anthropic_test_client, engine, cloud, logged
):
    resp = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())

    # Route order, the cloud listed first, as this surface always picked.
    assert resp.json()["content"][0]["text"] == "cloud"
    assert [row["metadata"].get("offload") for row in logged] == [None]
