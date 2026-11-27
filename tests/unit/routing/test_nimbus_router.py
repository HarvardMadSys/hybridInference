"""The Nimbus router (``router: nimbus``): the admission process beside the engine decides.

A real shared ``FixedRouter`` holds one route that declares the process and one
cloud route. The process is a fake adapter that serves, declines or fails as the
test says, and records what the gateway told it about each request.
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from routing.nimbus import (
    OFFLOAD_UNSUPPORTED_LOCAL,
    NimbusRouter,
    NimbusRoutingError,
    unsupported_locally,
)
from routing.offload import OFFLOAD_LAST_RESORT
from routing.protocols import RoutingRequestOptions
from routing.routers import AllCircuitsOpenError, FixedRouter
from routing.strategies import build_router, validate_router_config
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.adapters.engine_admission import AdmissionTerms
from serving.adapters.upstream_limiter import (
    EngineDeclined,
    UpstreamConcurrencyLimiter,
    get_upstream_limiter,
    reset_upstream_limiter,
)
from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

MODEL = "m"
MESSAGES = [{"role": "user", "content": "hi"}]
#: Estimated at about 2500 prompt tokens.
LONG_MESSAGES = [{"role": "user", "content": "x" * 10_000}]
PROCESS_URL = "http://localhost:18000/v1"
PROCESS_ID = f"{MODEL}:local-18000"
CLOUD_ID = f"{MODEL}:cloud-api"
CLOUD_URL = "https://cloud.example/v1"
#: The cloud answers in 400 ms plus 0.1 ms per prompt token.
CLOUD_TTFT = {"fixed_ms": 400, "per_input_token_ms": 0.1}


class _Process(BaseAdapter):
    """A route answered by the admission process: it serves, or raises what the test gives it."""

    def __init__(
        self,
        *,
        answer_with: BaseException | None = None,
        port: int = 18000,
        base_url: str | None = None,
        admission: str | None = "nimbus",
        **config: Any,
    ) -> None:
        super().__init__(
            ModelConfig(
                id=MODEL,
                name=MODEL,
                provider=f"process-{port}",
                base_url=base_url or f"http://localhost:{port}/v1",
                endpoint_id=f"{MODEL}:local-{port}",
                context_length=8192,
                admission=admission,
                **config,
            )
        )
        self.answer_with = answer_with
        self.told: list[Any] = []

    @property
    def calls(self) -> int:
        return len(self.told)

    def _asked(self) -> None:
        self.told.append(req_ctx.get().get(req_ctx.UPSTREAM_ADMISSION))
        if self.answer_with is not None:
            raise self.answer_with

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        self._asked()
        return self.format_response(content="engine", model=MODEL)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        self._asked()
        yield _chunk({"content": "engine"})
        yield _chunk({}, finish_reason="stop")


class _Cloud(BaseAdapter):
    """The model's cloud route."""

    def __init__(self, *, fail_with: BaseException | None = None, **config: Any) -> None:
        super().__init__(
            ModelConfig(
                id=MODEL,
                name=MODEL,
                provider="cloud",
                base_url=CLOUD_URL,
                endpoint_id=CLOUD_ID,
                context_length=8192,
                **config,
            )
        )
        self.fail_with = fail_with
        self.calls = 0
        self.told: list[Any] = []

    def _asked(self) -> None:
        self.calls += 1
        self.told.append(req_ctx.get().get(req_ctx.UPSTREAM_ADMISSION))
        if self.fail_with is not None:
            raise self.fail_with

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        self._asked()
        return self.format_response(content="cloud", model=MODEL)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        self._asked()
        yield _chunk({"content": "cloud"})
        yield _chunk({}, finish_reason="stop")


class _ServerError(Exception):
    """An upstream's 500."""

    status = 500


def _declined(decision: str = "declined_at_cutoff") -> EngineDeclined:
    return EngineDeclined(decision, ttft_left_ms=420.0, engine_request_id="r7")


def _chunk(delta: dict[str, str], finish_reason: str | None = None) -> str:
    payload = {
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _fixed(*adapters: BaseAdapter) -> FixedRouter:
    fixed = FixedRouter()
    fixed.register_route(MODEL, [(adapter, 1.0) for adapter in adapters])
    return fixed


async def _nimbus(*adapters: BaseAdapter, **params: Any) -> NimbusRouter:
    router = build_router("nimbus", {"cloud_ttft": CLOUD_TTFT, **params})
    assert isinstance(router, NimbusRouter)
    router.attach_route_table(_fixed(*adapters), model_scope={MODEL})
    await router.start()
    return router


@pytest.fixture(autouse=True)
def _limiter():
    reset_upstream_limiter(
        UpstreamConcurrencyLimiter(initial_limit=8, max_limit=8, acquire_timeout=10.0)
    )
    yield
    reset_upstream_limiter()


async def _stream(router: NimbusRouter, messages=MESSAGES, **params: Any) -> list[str]:
    return [chunk async for chunk in router.stream_chat_completion(MODEL, messages, **params)]


def _content(chunks: list[str]) -> str:
    text = ""
    for chunk in chunks:
        payload = json.loads(chunk.removeprefix("data: ").strip())
        for choice in payload.get("choices") or []:
            text += (choice.get("delta") or {}).get("content") or ""
    return text


def _routing(chunks: list[str]) -> dict[str, Any]:
    """Merge a stream's routing frames as the serving layer does."""
    merged: dict[str, Any] = {}
    for chunk in chunks:
        routing = json.loads(chunk.removeprefix("data: ").strip()).get("_routing")
        if not isinstance(routing, dict):
            continue
        merged = dict(routing) if routing.get("fallback") else {**merged, **routing}
    return merged


def _availability(router: NimbusRouter, endpoint_id: str) -> float:
    return float(router.get_provider_status().get(endpoint_id, {}).get("availability", 1.0))


# ------------------------------------------------------------ parameters


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"cloud_ttft": {"fixed_ms": -1, "per_input_token_ms": 0}},
        {"cloud_ttft": CLOUD_TTFT, "cloud_max_input_tokens": 0},
        {"cloud_ttft": CLOUD_TTFT, "engine_queue_limit": 1},
    ],
)
def test_nimbus_params_refuse_what_they_cannot_mean(params):
    with pytest.raises(ValidationError):
        validate_router_config("nimbus", params)


@pytest.mark.parametrize(
    ("params", "what"),
    [
        ({}, None),
        ({"response_format": {"type": "text"}}, None),
        ({"tool_choice": "auto", "tools": [{}]}, None),
        ({"tool_choice": "none"}, None),
        ({"response_format": {"type": "json_object"}}, "response_format"),
        ({"response_format": {"type": "json_schema", "json_schema": {}}}, "response_format"),
        ({"tool_choice": "required"}, "tool_choice"),
        ({"tool_choice": {"type": "function", "function": {"name": "f"}}}, "tool_choice"),
        ({"parallel_tool_calls": False}, "parallel_tool_calls"),
        ({"functions": [{}]}, "functions"),
    ],
)
def test_what_the_process_cannot_serve_as_asked(params, what):
    assert unsupported_locally(params) == what


# ---------------------------------------------------------------- routes


async def test_a_model_needs_a_declared_route_and_a_cloud_route():
    with pytest.raises(NimbusRoutingError, match="declares admission"):
        await _nimbus(_Cloud())
    with pytest.raises(NimbusRoutingError, match="cloud route"):
        await _nimbus(_Process())


async def test_a_declared_route_must_be_self_hosted():
    process = _Process(base_url="https://nimbus.example/v1")
    with pytest.raises(NimbusRoutingError, match="not self-hosted"):
        await _nimbus(process, _Cloud())


async def test_a_bare_engine_beside_the_process_is_refused():
    # A self-hosted route with no process in front has no queue at all here, and
    # one at the process's own address would reach past it.
    with pytest.raises(NimbusRoutingError, match="without declaring"):
        await _nimbus(_Process(), _Process(admission=None), _Cloud())
    with pytest.raises(NimbusRoutingError, match="self-hosted and declares no admission"):
        await _nimbus(_Process(), _Process(admission=None, port=30000), _Cloud())


async def test_a_declared_route_serves_text_only():
    process = _Process(input_modalities=["text", "image"])
    with pytest.raises(NimbusRoutingError, match="other than text"):
        await _nimbus(process, _Cloud())


async def test_a_declared_address_cannot_also_have_an_engine_queue():
    get_upstream_limiter().set_engine_queues("another-model", {PROCESS_URL: 1})
    with pytest.raises(NimbusRoutingError, match="one engine has one queue"):
        await _nimbus(_Process(), _Cloud())


async def test_this_router_registers_no_engine_queue():
    router = await _nimbus(_Process(), _Cloud())

    assert get_upstream_limiter().engine_queue_limit(PROCESS_URL) is None
    assert await router.stop() is True


async def test_an_address_that_gains_an_engine_queue_gets_no_more_requests():
    process, cloud = _Process(), _Cloud()
    router = await _nimbus(process, cloud)
    get_upstream_limiter().set_engine_queues("another-model", {PROCESS_URL: 1})
    router.refresh_route_table()

    chunks = await _stream(router)

    assert _content(chunks) == "cloud"
    assert _routing(chunks)["offload"] == OFFLOAD_LAST_RESORT
    assert process.calls == 0


# -------------------------------------------------------------- requests


@pytest.mark.parametrize("mode", ["stream", "chat"])
async def test_a_request_the_process_takes_stays_on_the_engine(mode):
    process, cloud = _Process(), _Cloud()
    router = await _nimbus(process, cloud)

    if mode == "stream":
        chunks = await _stream(router, session_id="s1", request_id="req-1")
        assert _content(chunks) == "engine"
        assert "offload" not in _routing(chunks)
    else:
        resp = await router.chat_completion(MODEL, MESSAGES, session_id="s1", request_id="req-1")
        assert resp["choices"][0]["message"]["content"] == "engine"
        assert "offload" not in resp["_routing"]

    assert cloud.calls == 0
    [terms] = process.told
    assert isinstance(terms, AdmissionTerms)
    assert (terms.session_id, terms.request_id) == ("s1", "req-1")
    # A short prompt: the cloud's fixed 400 ms, and a little for its tokens.
    assert 400.0 <= terms.remote_ttft_ms < 410.0


async def test_the_cloud_time_grows_with_the_prompt():
    process = _Process()
    router = await _nimbus(process, _Cloud())

    await _stream(router, LONG_MESSAGES)

    [terms] = process.told
    assert terms.remote_ttft_ms == pytest.approx(400 + 0.1 * 2500, rel=0.1)


@pytest.mark.parametrize("mode", ["stream", "chat"])
async def test_a_declined_request_goes_to_the_cloud_once(mode):
    process, cloud = _Process(answer_with=_declined()), _Cloud()
    router = await _nimbus(process, cloud)

    if mode == "stream":
        chunks = await _stream(router)
        content, routing = _content(chunks), _routing(chunks)
    else:
        resp = await router.chat_completion(MODEL, MESSAGES)
        content, routing = resp["choices"][0]["message"]["content"], resp["_routing"]

    assert content == "cloud"
    assert routing["offload"] == "declined_at_cutoff"
    # Nothing reached the engine: no failed attempt, and nothing against the route.
    assert not routing.get("failed_attempts")
    assert _availability(router, PROCESS_ID) == 1.0
    assert (process.calls, cloud.calls) == (1, 1)
    # The cloud route is told nothing about admission.
    assert cloud.told == [None]


@pytest.mark.parametrize("mode", ["stream", "chat"])
async def test_the_cloud_failing_after_a_decline_is_the_callers_answer(mode):
    process = _Process(answer_with=_declined())
    cloud = _Cloud(fail_with=_ServerError("cloud down"))
    router = await _nimbus(process, cloud)

    with pytest.raises(_ServerError):
        if mode == "stream":
            await _stream(router)
        else:
            await router.chat_completion(MODEL, MESSAGES)

    # Never back to the engine that declined it.
    assert (process.calls, cloud.calls) == (1, 1)


@pytest.mark.parametrize("mode", ["stream", "chat"])
async def test_a_request_no_cloud_route_takes_is_told_so_and_ends_503_shaped(mode):
    # Over the cloud's input limit: the process is told there is no cloud time at
    # all, and a request it declines anyway has nowhere to go.
    process, cloud = _Process(answer_with=_declined("declined_at_arrival")), _Cloud()
    router = await _nimbus(process, cloud, cloud_max_input_tokens=1000)

    with pytest.raises(AllCircuitsOpenError, match="declined_at_arrival") as caught:
        if mode == "stream":
            await _stream(router, LONG_MESSAGES)
        else:
            await router.chat_completion(MODEL, LONG_MESSAGES)

    [terms] = process.told
    assert math.isinf(terms.remote_ttft_ms)
    assert cloud.calls == 0
    # The decline is the cause, never the answer: it carries no status to give.
    assert not isinstance(caught.value, EngineDeclined)
    assert isinstance(caught.value.__cause__, EngineDeclined)


async def test_a_request_the_process_keeps_when_the_cloud_cannot_take_it_is_served():
    process, cloud = _Process(), _Cloud()
    router = await _nimbus(process, cloud, cloud_max_input_tokens=1000)

    chunks = await _stream(router, LONG_MESSAGES)

    assert _content(chunks) == "engine"
    assert math.isinf(process.told[0].remote_ttft_ms)


async def test_a_local_failure_before_output_goes_to_the_cloud_as_a_fallback():
    process, cloud = _Process(answer_with=_ServerError("engine down")), _Cloud()
    router = await _nimbus(process, cloud)

    chunks = await _stream(router)

    routing = _routing(chunks)
    assert _content(chunks) == "cloud"
    assert routing["fallback"] is True
    assert "offload" not in routing
    assert [a["endpoint_id"] for a in routing["failed_attempts"]] == [PROCESS_ID]


async def test_only_one_process_is_asked():
    # A second process would be asked later, with less of the budget left.
    first = _Process(answer_with=_declined())
    second = _Process(answer_with=_declined(), port=18001)
    cloud = _Cloud()
    router = await _nimbus(first, second, cloud)

    chunks = await _stream(router)

    assert _content(chunks) == "cloud"
    assert first.calls + second.calls == 1
    assert cloud.calls == 1


@pytest.mark.parametrize(
    "params",
    [
        {"response_format": {"type": "json_object"}},
        {"tools": [{"type": "function", "function": {"name": "f"}}], "tool_choice": "required"},
        {"parallel_tool_calls": False},
    ],
)
async def test_what_the_process_cannot_serve_goes_straight_to_the_cloud(params):
    process, cloud = _Process(supports_tools=True), _Cloud()
    router = await _nimbus(process, cloud)

    chunks = await _stream(router, **params)

    assert _content(chunks) == "cloud"
    assert _routing(chunks)["offload"] == OFFLOAD_UNSUPPORTED_LOCAL
    assert process.calls == 0


async def test_tools_go_to_a_process_whose_route_takes_them_and_past_one_that_does_not():
    tools = [{"type": "function", "function": {"name": "f"}}]

    reads, cloud = _Process(supports_tools=True), _Cloud()
    assert _content(await _stream(await _nimbus(reads, cloud), tools=tools)) == "engine"

    plain, cloud = _Process(supports_tools=False), _Cloud()
    chunks = await _stream(await _nimbus(plain, cloud), tools=tools)
    assert _content(chunks) == "cloud"
    assert _routing(chunks)["offload"] == OFFLOAD_UNSUPPORTED_LOCAL
    assert plain.calls == 0


async def test_a_pinned_request_is_dispatched_by_the_shared_router_as_pinned():
    process, cloud = _Process(), _Cloud()
    router = await _nimbus(process, cloud)

    resp = await router.chat_completion(
        MODEL, MESSAGES, routing_options=RoutingRequestOptions(pin_provider="cloud")
    )

    assert resp["choices"][0]["message"]["content"] == "cloud"
    assert process.calls == 0


@pytest.mark.parametrize("mode", ["stream", "chat"])
async def test_a_request_pinned_to_the_process_is_told_no_cloud_stands_behind_it(mode):
    process, cloud = _Process(), _Cloud()
    router = await _nimbus(process, cloud)
    pinned = RoutingRequestOptions(pin_provider="process-18000")

    if mode == "stream":
        chunks = [
            chunk
            async for chunk in router.stream_chat_completion(
                MODEL, MESSAGES, routing_options=pinned, session_id="s1", request_id="req-1"
            )
        ]
        assert _content(chunks) == "engine"
    else:
        resp = await router.chat_completion(
            MODEL, MESSAGES, routing_options=pinned, session_id="s1", request_id="req-1"
        )
        assert resp["choices"][0]["message"]["content"] == "engine"

    [terms] = process.told
    assert math.isinf(terms.remote_ttft_ms)
    assert (terms.session_id, terms.request_id) == ("s1", "req-1")
    assert cloud.calls == 0


@pytest.mark.parametrize("mode", ["stream", "chat"])
async def test_a_decline_where_the_caller_chose_the_endpoint_ends_503_shaped(mode):
    # No cloud step to go to: the decline is the end, and never the answer itself.
    process, cloud = _Process(answer_with=_declined("declined_at_arrival")), _Cloud()
    router = await _nimbus(process, cloud)
    pinned = RoutingRequestOptions(pin_provider="process-18000")

    with pytest.raises(AllCircuitsOpenError, match="declined_at_arrival") as caught:
        if mode == "stream":
            async for _ in router.stream_chat_completion(MODEL, MESSAGES, routing_options=pinned):
                pass
        else:
            await router.chat_completion(MODEL, MESSAGES, routing_options=pinned)

    assert isinstance(caught.value.__cause__, EngineDeclined)
    assert cloud.calls == 0
