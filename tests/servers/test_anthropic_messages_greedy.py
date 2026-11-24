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
import logging
import time
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

    def __init__(
        self,
        provider: str,
        base_url: str,
        endpoint_id: str,
        *,
        input_modalities: list[str] | None = None,
        context_length: int = 8192,
    ) -> None:
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
                input_modalities=input_modalities or ["text"],
                context_length=context_length,
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


#: A Cutoff whose deadline is 0.1 s after arrival for any prompt.
SHORT_CUTOFF = {
    "ttft_budget": {"fixed_ms": 1100, "per_token_ms": 0},
    "cloud_ttft": {"fixed_ms": 1000, "per_input_token_ms": 0},
    "margin_ms": 0,
}
#: A prompt the estimate puts at about 2500 tokens.
LONG_TEXT = "x" * 10_000


async def _until_waiting() -> None:
    async with asyncio.timeout(5):
        while _waiting() != 1:
            await asyncio.sleep(0.005)


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


async def test_a_second_pass_with_no_cloud_route_left_is_refused_not_sent_to_the_engine(
    anthropic_test_client, anthropic_compat_router, make_greedy, engine, cloud, logged
):
    await make_greedy(cutoff=SHORT_CUTOFF)
    seat = await _take_the_engine()

    post = asyncio.create_task(
        anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())
    )
    await _until_waiting()
    # The cloud route is disabled while the request waits for its engine.
    anthropic_compat_router.register_route(MODEL, [(cloud, 0.0), (engine, 1.0)])
    async with asyncio.timeout(5):
        resp = await post

    assert resp.status_code == 503
    assert (engine.reached, cloud.reached) == (0, 0)
    # The request is logged once, as the failure it was, with why it left its engine.
    assert [
        (row["provider"], row["status_code"], row["metadata"]["offload"]) for row in logged
    ] == [("router", 503, "queue_wait")]
    seat.release(status_code=200)


async def test_a_stream_whose_second_pass_is_refused_logs_the_failure(
    anthropic_test_client, anthropic_compat_router, make_greedy, engine, cloud, logged
):
    await make_greedy(cutoff=SHORT_CUTOFF)
    seat = await _take_the_engine()

    async def read() -> bytes:
        async with anthropic_test_client.stream(
            "POST", "/v1/messages", json=_body(stream=True), headers=_auth()
        ) as resp:
            return b"".join([chunk async for chunk in resp.aiter_bytes()])

    stream = asyncio.create_task(read())
    await _until_waiting()
    anthropic_compat_router.register_route(MODEL, [(cloud, 0.0), (engine, 1.0)])
    async with asyncio.timeout(5):
        body = await stream

    # The stream had answered 200: the failure is an error event, and a log row.
    assert b"event: error" in body
    assert [(row["status_code"], row["metadata"]["offload"]) for row in logged] == [
        (503, "queue_wait")
    ]
    seat.release(status_code=200)


async def test_a_prompt_no_route_can_fit_is_refused_as_too_long(
    anthropic_test_client, anthropic_compat_router, make_greedy, logged
):
    small_engine = _Route("engine", ENGINE_URL, f"{MODEL}:local-18003", context_length=100)
    small_cloud = _Route(
        "cloud", "https://cloud.example/v1", f"{MODEL}:cloud-api", context_length=100
    )
    anthropic_compat_router.register_route(MODEL, [(small_cloud, 1.0), (small_engine, 1.0)])
    await make_greedy()

    resp = await anthropic_test_client.post(
        "/v1/messages",
        json=_body(messages=[{"role": "user", "content": LONG_TEXT}]),
        headers=_auth(),
    )

    assert resp.status_code == 400
    assert (small_engine.reached, small_cloud.reached) == (0, 0)


async def test_a_prompt_over_the_cloud_limit_with_no_local_engine_is_refused(
    anthropic_test_client, anthropic_compat_router, make_greedy, engine, cloud, logged
):
    anthropic_compat_router.register_route(MODEL, [(cloud, 1.0), (engine, 0.0)])
    await make_greedy(cloud_max_input_tokens=100)

    resp = await anthropic_test_client.post(
        "/v1/messages",
        json=_body(messages=[{"role": "user", "content": LONG_TEXT}]),
        headers=_auth(),
    )

    assert resp.status_code == 503
    assert cloud.reached == 0


async def test_a_second_pass_takes_the_cloud_routes_as_they_are_when_it_runs(
    anthropic_test_client, anthropic_compat_router, make_greedy, engine, cloud, logged
):
    router = await make_greedy(cutoff=SHORT_CUTOFF)
    seat = await _take_the_engine()
    new_cloud = _Route("cloud-new", "https://cloud-new.example/v1", f"{MODEL}:cloud-new")

    post = asyncio.create_task(
        anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())
    )
    await _until_waiting()
    # The engine leads the new table, so only the router's live cloud range
    # (not the surface's own pick) can take the request to the new cloud route.
    anthropic_compat_router.register_route(MODEL, [(engine, 1.0), (new_cloud, 1.0)])
    router.refresh_route_table()
    async with asyncio.timeout(5):
        resp = await post

    assert resp.json()["content"][0]["text"] == "cloud-new"
    assert cloud.reached == 0
    seat.release(status_code=200)


async def test_the_second_pass_is_the_same_request_with_its_wait_counted(
    anthropic_test_client, make_greedy, logged, caplog, monkeypatch
):
    from serving.servers.routers import anthropic_messages as amod

    pins: list[bool] = []
    reroute = amod._maybe_reroute_small_reasoning_call

    async def spy(*args: Any, pin_model: bool = False, **kwargs: Any) -> Any:
        pins.append(pin_model)
        return await reroute(*args, pin_model=pin_model, **kwargs)

    monkeypatch.setattr(amod, "_maybe_reroute_small_reasoning_call", spy)
    await make_greedy(cutoff=SHORT_CUTOFF)
    seat = await _take_the_engine()

    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger=amod.__name__):
        resp = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())

    assert resp.json()["content"][0]["text"] == "cloud"
    (row,) = logged
    # The queue wait is in its latency, and the route_offload line names its row.
    assert row["latency_ms"] >= 90
    assert time.monotonic() - started >= 0.09
    offloads = [record for record in caplog.records if record.getMessage() == "route_offload"]
    assert [record.request_id for record in offloads] == [row["request_id"]]
    # The second pass may not reroute the request to another model.
    assert pins == [False, True]
    seat.release(status_code=200)


async def test_a_request_with_an_image_skips_an_engine_that_cannot_read_it(
    anthropic_test_client, anthropic_compat_router, make_greedy, logged, caplog
):
    from serving.servers.routers import anthropic_messages as amod

    text_engine = _Route("engine", ENGINE_URL, f"{MODEL}:local-18003")
    image_cloud = _Route(
        "cloud",
        "https://cloud.example/v1",
        f"{MODEL}:cloud-api",
        input_modalities=["text", "image"],
    )
    anthropic_compat_router.register_route(MODEL, [(text_engine, 1.0), (image_cloud, 1.0)])
    await make_greedy()
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AA=="},
    }

    with caplog.at_level(logging.INFO, logger=amod.__name__):
        resp = await anthropic_test_client.post(
            "/v1/messages",
            json=_body(messages=[{"role": "user", "content": [image]}]),
            headers=_auth(),
        )

    assert resp.json()["content"][0]["text"] == "cloud"
    assert text_engine.reached == 0
    assert [row["metadata"].get("offload") for row in logged] == ["last_resort"]
    assert [
        record.reason for record in caplog.records if record.getMessage() == "route_offload"
    ] == ["last_resort"]


async def test_a_request_with_an_image_waits_for_its_engine_when_no_cloud_route_reads_it(
    anthropic_test_client, anthropic_compat_router, make_greedy, logged
):
    image_engine = _Route(
        "engine", ENGINE_URL, f"{MODEL}:local-18003", input_modalities=["text", "image"]
    )
    text_cloud = _Route("cloud", "https://cloud.example/v1", f"{MODEL}:cloud-api")
    anthropic_compat_router.register_route(MODEL, [(text_cloud, 1.0), (image_engine, 1.0)])
    await make_greedy(cutoff=SHORT_CUTOFF)
    seat = await _take_the_engine()
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AA=="},
    }

    post = asyncio.create_task(
        anthropic_test_client.post(
            "/v1/messages",
            json=_body(messages=[{"role": "user", "content": [image]}]),
            headers=_auth(),
        )
    )
    await _until_waiting()
    # Well past the deadline a text-only cloud would have set: no cloud route
    # can read the image, so the request has none and keeps its place in line.
    await asyncio.sleep(0.3)
    assert not post.done()
    seat.release(status_code=200)
    async with asyncio.timeout(5):
        resp = await post

    assert resp.json()["content"][0]["text"] == "engine"
    assert text_cloud.reached == 0
    assert [row["metadata"].get("offload") for row in logged] == [None]


async def test_a_full_engine_is_passed_for_one_with_a_free_place(
    anthropic_test_client, anthropic_compat_router, make_greedy, engine, cloud, logged
):
    second = _Route("engine-b", "http://localhost:18004/v1", f"{MODEL}:local-18004")
    anthropic_compat_router.register_route(MODEL, [(cloud, 1.0), (engine, 1.0), (second, 1.0)])
    await make_greedy()
    seat = await _take_the_engine()

    resp = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())

    assert resp.json()["content"][0]["text"] == "engine-b"
    assert (engine.reached, cloud.reached) == (0, 0)
    assert [row["metadata"].get("offload") for row in logged] == [None]
    seat.release(status_code=200)


async def test_a_model_routed_by_fixed_keeps_this_surfaces_own_pick(
    anthropic_test_client, engine, cloud, logged
):
    resp = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())

    # Route order, the cloud listed first, as this surface always picked.
    assert resp.json()["content"][0]["text"] == "cloud"
    assert [row["metadata"].get("offload") for row in logged] == [None]
