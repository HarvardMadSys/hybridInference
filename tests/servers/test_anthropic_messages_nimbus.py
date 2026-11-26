"""A nimbus model on /v1/messages: the admission process beside its engine first, the cloud once it declines.

This surface picks its own adapter, so it asks the model's nimbus router where
(``NimbusRouter.surface_plan``) and tells the process what the router would. A
request the process declines reached no engine, so it is served once more by the
cloud, which writes the only log row, with the process's own reason.
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from routing.model_router_registry import ModelRouterRegistry
from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.adapters.engine_admission import AdmissionTerms
from serving.adapters.upstream_limiter import (
    EngineDeclined,
    UpstreamConcurrencyLimiter,
    reset_upstream_limiter,
)
from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

MODEL = "qwen-local"
PROCESS_URL = "http://localhost:18000/v1"
PRICING = {"prompt": "0.6", "completion": "2.2", "image": "0", "request": "0"}
CLOUD_TTFT = {"fixed_ms": 400, "per_input_token_ms": 0.1}
TOOL = {"name": "read", "description": "Read a file", "input_schema": {"type": "object"}}


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
    """A route of the model on the Messages surface; ``answer_with`` is raised instead of a reply."""

    native_format = "anthropic"

    def __init__(self, provider: str, base_url: str, endpoint_id: str, **config: Any) -> None:
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
                context_length=8192,
                **config,
            )
        )
        self.answer_with: BaseException | None = None
        #: What the gateway told this route about each request it was sent.
        self.told: list[Any] = []

    @property
    def reached(self) -> int:
        return len(self.told)

    def _asked(self) -> None:
        self.told.append(req_ctx.get().get(req_ctx.UPSTREAM_ADMISSION))
        if self.answer_with is not None:
            raise self.answer_with

    async def chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> Any:
        raise NotImplementedError

    async def stream_chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> Any:
        raise NotImplementedError

    async def messages(self, body: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        self._asked()
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
        self._asked()
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
            "content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}
        )
        yield _event(
            "content_block_delta",
            {"index": 0, "delta": {"type": "text_delta", "text": self.config.provider}},
        )
        yield _event("message_stop", {})


@pytest.fixture
def process() -> _Route:
    return _Route(
        "engine", PROCESS_URL, f"{MODEL}:local-18000", admission="nimbus", supports_tools=True
    )


@pytest.fixture
def cloud() -> _Route:
    return _Route("cloud", "https://cloud.example/v1", f"{MODEL}:cloud-api", supports_tools=True)


@pytest_asyncio.fixture
async def anthropic_compat_router(process, cloud):
    """The model's routes, the cloud listed first: route order alone would pick it."""
    router = FixedRouter()
    router.register_route(MODEL, [(cloud, 1.0), (process, 1.0)])
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
async def nimbus(anthropic_app_services, anthropic_compat_router):
    """Route the model with ``router: nimbus``."""
    registry = ModelRouterRegistry(
        {MODEL: {"router": "nimbus", "router_params": {"cloud_ttft": CLOUD_TTFT}}},
        shared_fixed_router=anthropic_compat_router,
    )
    anthropic_app_services.model_router_registry = registry
    router = registry.get_router(MODEL)
    await router.start()
    yield router
    await router.stop()


async def _post(client, *, stream: bool, **overrides: Any) -> tuple[int, str]:
    """Send one request; return its status and the text it was answered with, or its body."""
    if not stream:
        resp = await client.post("/v1/messages", json=_body(**overrides), headers=_auth())
        payload = resp.json()
        content = payload.get("content") if isinstance(payload, dict) else None
        return resp.status_code, content[0]["text"] if content else json.dumps(payload)
    async with client.stream(
        "POST", "/v1/messages", json=_body(stream=True, **overrides), headers=_auth()
    ) as resp:
        body = b"".join([chunk async for chunk in resp.aiter_bytes()]).decode()
    for name in ("engine", "cloud"):
        if f'"text": "{name}"' in body:
            return resp.status_code, name
    return resp.status_code, body


@pytest.mark.parametrize("stream", [False, True], ids=["whole", "stream"])
async def test_the_process_takes_the_request_before_a_cloud_route_listed_first(
    anthropic_test_client, nimbus, process, cloud, logged, stream
):
    status, text = await _post(anthropic_test_client, stream=stream)

    assert (status, text) == (200, "engine")
    assert (process.reached, cloud.reached) == (1, 0)
    [terms] = process.told
    assert isinstance(terms, AdmissionTerms)
    # The cloud's fixed 400 ms and a little for a short prompt's tokens.
    assert 400.0 <= terms.remote_ttft_ms < 410.0
    assert terms.request_id
    assert [row["metadata"].get("offload") for row in logged] == [None]


@pytest.mark.parametrize("stream", [False, True], ids=["whole", "stream"])
async def test_a_declined_request_is_served_once_more_by_the_cloud(
    anthropic_test_client, nimbus, process, cloud, logged, stream
):
    process.answer_with = EngineDeclined("declined_at_cutoff", ttft_left_ms=300.0)

    status, text = await _post(anthropic_test_client, stream=stream)

    assert (status, text) == (200, "cloud")
    assert (process.reached, cloud.reached) == (1, 1)
    # One row, the cloud's, with the process's own reason; the cloud route is told
    # nothing about admission.
    assert [(row["provider"], row["metadata"].get("offload")) for row in logged] == [
        ("cloud", "declined_at_cutoff")
    ]
    assert cloud.told == [None]


@pytest.mark.parametrize("stream", [False, True], ids=["whole", "stream"])
async def test_a_call_the_request_forces_never_reaches_the_process(
    anthropic_test_client, nimbus, process, cloud, logged, stream
):
    status, text = await _post(
        anthropic_test_client, stream=stream, tools=[TOOL], tool_choice={"type": "any"}
    )

    assert (status, text) == (200, "cloud")
    assert (process.reached, cloud.reached) == (0, 1)
    assert [row["metadata"].get("offload") for row in logged] == ["unsupported_local"]


async def test_tools_the_model_may_choose_to_call_go_to_the_process(
    anthropic_test_client, nimbus, process, cloud
):
    status, text = await _post(
        anthropic_test_client, stream=False, tools=[TOOL], tool_choice={"type": "auto"}
    )

    assert (status, text) == (200, "engine")
    assert (process.reached, cloud.reached) == (1, 0)


async def test_a_request_with_an_image_goes_to_a_cloud_route_that_reads_one(
    anthropic_test_client, nimbus, process, cloud, logged
):
    # The process serves text only, so the request goes straight to the cloud
    # route that reads images, as one no local route can take.
    cloud.config.input_modalities = ["text", "image"]
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AA=="},
    }

    status, text = await _post(
        anthropic_test_client,
        stream=False,
        messages=[{"role": "user", "content": [image, {"type": "text", "text": "what is it"}]}],
    )

    assert (status, text) == (200, "cloud")
    assert process.reached == 0
    assert [row["metadata"].get("offload") for row in logged] == ["last_resort"]


@pytest.mark.parametrize("stream", [False, True], ids=["whole", "stream"])
async def test_a_decline_nothing_carries_to_the_cloud_is_answered_overloaded(
    anthropic_test_client, anthropic_app_services, anthropic_compat_router, process, cloud, stream
):
    # The model is not on router: nimbus here, so no second pass stands behind its
    # declared route: the decline is no rate limit, and no internal fault.
    anthropic_compat_router.register_route(MODEL, [(process, 1.0)])
    anthropic_app_services.model_router_registry = None
    process.answer_with = EngineDeclined("declined_at_arrival")

    status, body = await _post(anthropic_test_client, stream=stream)

    if stream:
        assert "overloaded_error" in body
        assert "rate_limit_error" not in body
    else:
        assert status == 503
    assert cloud.reached == 0
    # No router described the request to the process.
    assert process.told == [None]


@pytest.mark.parametrize("stream", [False, True], ids=["whole", "stream"])
async def test_a_declined_request_with_no_usable_cloud_route_is_answered_503(
    anthropic_test_client, anthropic_app_services, anthropic_compat_router, process, cloud, stream
):
    # Over cloud_max_input_tokens: the process is told inf, and when it still
    # declines at its latest local start, the second pass has no cloud route and
    # answers 503 overloaded_error.
    registry = ModelRouterRegistry(
        {
            MODEL: {
                "router": "nimbus",
                "router_params": {"cloud_ttft": CLOUD_TTFT, "cloud_max_input_tokens": 1},
            }
        },
        shared_fixed_router=anthropic_compat_router,
    )
    anthropic_app_services.model_router_registry = registry
    router = registry.get_router(MODEL)
    await router.start()
    try:
        process.answer_with = EngineDeclined("declined_at_latest_start")
        status, body = await _post(
            anthropic_test_client,
            stream=stream,
            messages=[{"role": "user", "content": "x" * 100}],
        )
    finally:
        await router.stop()

    if stream:
        assert "overloaded_error" in body
        assert "rate_limit_error" not in body
    else:
        assert status == 503
        assert "overloaded_error" in body
    assert (process.reached, cloud.reached) == (1, 0)
    [terms] = process.told
    assert isinstance(terms, AdmissionTerms)
    assert math.isinf(terms.remote_ttft_ms)
