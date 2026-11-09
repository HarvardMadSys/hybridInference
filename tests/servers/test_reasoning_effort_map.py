"""A route's ``reasoning_effort_map``: public levels in, upstream names out, exactly once.

Everything here is driven through the real registry (a models document), the
real router and adapters, and the real Chat Completions, Responses and Messages
handlers, against fake OpenAI-compatible upstreams listening on localhost. What
each upstream received is read off the wire, so "sent as" means the HTTP body.

The configured route is the generic contract with a test map (``low -> high``,
``high -> xhigh``, ``max -> max``). That map is also a double-translation
detector: ``low`` translated twice would arrive as ``xhigh``.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import serving.config.runtime_settings as runtime_settings_module
from routing.routers import FixedRouter
from serving.adapters.base import ModelConfig
from serving.adapters.reasoning_effort import (
    ReasoningEffortRefused,
    effort_contract,
    translate_effort,
    validate_effort_map,
)
from serving.config.runtime_settings import RuntimeSettings
from serving.http import AsyncHTTPClient
from serving.servers.auth import optional_verify_api_key, verify_api_key
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.registry import register_from_models_text
from serving.servers.routers import anthropic_messages, completions, models, responses

from .test_auto_mode_surfaces import _SettingsStore
from .test_completions import VisibilityResolver

if TYPE_CHECKING:
    from serving.adapters.openai_compat import OpenAICompatAdapter

USER = {"user_id": "user-1", "role": "internal", "authenticated": True, "is_admin": False}
EFFORT_MAP = {"low": "high", "high": "xhigh", "max": "max"}


class _Upstream:
    """An OpenAI-compatible server on localhost that records every request body."""

    def __init__(self, *, status: int = 200) -> None:
        self.bodies: list[dict[str, Any]] = []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                del args

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                upstream.bodies.append(body)
                if status != 200:
                    data = json.dumps({"error": {"message": "upstream down"}}).encode()
                    self.send_response(status)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if body.get("stream"):
                    chunks = [
                        {
                            "choices": [
                                {"index": 0, "delta": {"role": "assistant", "content": "ok"}}
                            ]
                        },
                        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                        {
                            "choices": [],
                            "usage": {
                                "prompt_tokens": 3,
                                "completion_tokens": 1,
                                "total_tokens": 4,
                            },
                        },
                    ]
                    data = b"".join(
                        b"data: "
                        + json.dumps(
                            {"id": "c", "object": "chat.completion.chunk", "model": "m", **c}
                        ).encode()
                        + b"\n\n"
                        for c in chunks
                    )
                    data += b"data: [DONE]\n\n"
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                data = json.dumps(
                    {
                        "id": "c",
                        "object": "chat.completion",
                        "model": body.get("model"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "ok"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()

    def efforts(self) -> list[Any]:
        """``reasoning_effort`` of each request, ``<absent>`` when it was not sent."""
        return [body.get("reasoning_effort", "<absent>") for body in self.bodies]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _models_document(primary: str, down: str, healthy: str, legacy: str) -> str:
    """The models document: mapped, mapped with fallback, and two legacy routes."""
    route_map = "\n".join(f"          {k}: {v}" for k, v in EFFORT_MAP.items())
    common = """
    context_length: 32768
    max_output_length: 4096
    supported_params: [temperature, max_tokens]"""
    return f"""
models:
  - id: mapped
    name: mapped{common}
    route:
      - kind: vllm
        base_url: {primary}
        reasoning_effort_map:
{route_map}
  - id: mapped-fallback
    name: mapped-fallback{common}
    route:
      - kind: vllm
        base_url: {down}
        reasoning_effort_map:
{route_map}
      - kind: sglang
        base_url: {healthy}
        reasoning_effort_map:
{route_map}
  - id: legacy-passthrough
    name: legacy-passthrough
    context_length: 32768
    max_output_length: 4096
    supported_params: [temperature, max_tokens, reasoning_effort]
    route:
      - kind: vllm
        base_url: {legacy}
  - id: legacy-dropped
    name: legacy-dropped{common}
    route:
      - kind: vllm
        base_url: {legacy}
"""


class _Gateway:
    def __init__(self, mock_db_logger, mock_log_store) -> None:
        self.primary = _Upstream()
        self.down = _Upstream(status=500)
        self.healthy = _Upstream()
        self.legacy = _Upstream()
        self.router = FixedRouter()
        register_from_models_text(
            self.router,
            _models_document(self.primary.url, self.down.url, self.healthy.url, self.legacy.url),
        )
        self.app = FastAPI()
        self.app.state.services = AppServices(
            router=self.router,
            db_logger=mock_db_logger,
            log_store=mock_log_store,
            model_visibility_resolver=VisibilityResolver({}),
        )
        install_error_handlers(self.app)
        self.app.dependency_overrides[verify_api_key] = lambda: USER
        self.app.dependency_overrides[optional_verify_api_key] = lambda: USER
        for module in (completions, anthropic_messages, models, responses):
            self.app.include_router(module.router)
        runtime_settings_module._runtime_settings = RuntimeSettings(_SettingsStore({}))

    async def post(self, path: str, body: dict[str, Any]):
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, json=body)

    async def get(self, path: str, headers: dict[str, str] | None = None):
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path, headers=headers or {})

    def close(self) -> None:
        for upstream in (self.primary, self.down, self.healthy, self.legacy):
            upstream.close()


@pytest_asyncio.fixture
async def gateway(mock_db_logger, mock_log_store, monkeypatch):
    client = AsyncHTTPClient()  # one session per test, on this test's loop
    monkeypatch.setattr(AsyncHTTPClient, "_shared", client)
    gw = _Gateway(mock_db_logger, mock_log_store)
    yield gw
    gw.close()
    await client.close()


def _request(surface: str, model: str, level: str | None, *, stream: bool) -> tuple[str, dict]:
    """A request on ``surface`` naming ``level`` the way that protocol spells it."""
    if surface == "chat":
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": stream,
        }
        if level is not None:
            body["reasoning_effort"] = level
        return "/v1/chat/completions", body
    if surface == "responses":
        body = {"model": model, "input": "hi", "stream": stream}
        if level is not None:
            body["reasoning"] = {"effort": level}
        return "/v1/responses", body
    body = {
        "model": model,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
    }
    if level is not None:
        body["output_config"] = {"effort": level}
    return "/v1/messages", body


SURFACES = ["chat", "responses", "messages"]
STREAM = [pytest.param(False, id="unary"), pytest.param(True, id="stream")]


# -- the contract itself ------------------------------------------------------


def test_a_map_keeps_its_order_and_refuses_what_is_not_a_level():
    assert list(validate_effort_map({"max": "max", "low": "high"})) == ["max", "low"]
    for bad in ({}, [], {"low": ""}, {"low": "a b"}, {"": "high"}, {"low": 1}, "low=high"):
        with pytest.raises(ValueError):
            validate_effort_map(bad)
    with pytest.raises(ValueError, match="at most"):
        validate_effort_map({f"l{i}": "x" for i in range(17)})


def test_a_mapped_contract_translates_exactly_and_refuses_anything_else():
    config = ModelConfig(id="m", name="m", provider="p", base_url="http://u")
    config.reasoning_effort_map = dict(EFFORT_MAP)
    contract = effort_contract(config)
    assert contract.mapped and contract.forwards
    assert contract.levels == ("low", "high", "max")
    assert [translate_effort(contract, level) for level in contract.levels] == [
        "high",
        "xhigh",
        "max",
    ]
    for undeclared in ("xhigh", "medium", "none", "minimal", "HIGH", 5):
        with pytest.raises(ReasoningEffortRefused) as refused:
            translate_effort(contract, undeclared)
        assert refused.value.status == 400
        assert "supported: low, high, max" in refused.value.message


def test_an_unmapped_route_keeps_its_legacy_contract():
    plain = ModelConfig(id="m", name="m", provider="p", base_url="http://u")
    assert effort_contract(plain).forwards is False
    declared = ModelConfig(
        id="m",
        name="m",
        provider="p",
        base_url="http://u",
        supported_params=["reasoning_effort"],
        reasoning_efforts=["low", "high"],
    )
    assert effort_contract(declared).levels == ("low", "high")
    assert translate_effort(effort_contract(declared), "anything") == "anything"
    undeclared = ModelConfig(
        id="m", name="m", provider="p", base_url="http://u", supported_params=["reasoning_effort"]
    )
    # Unknown, not "none": no domain is declared.
    assert effort_contract(undeclared).levels is None


# -- the registry -------------------------------------------------------------


def _load(document: str) -> FixedRouter:
    router = FixedRouter()
    register_from_models_text(router, document)
    return router


def test_the_registry_reads_a_route_map_and_nothing_else_changes():
    router = _load(
        _models_document(
            "http://127.0.0.1:1", "http://127.0.0.1:2", "http://127.0.0.1:3", "http://127.0.0.1:4"
        )
    )
    config = router.routes["mapped"].adapters[0][0].config
    assert config.reasoning_effort_map == EFFORT_MAP
    # The declaration is the map alone: supported_params and the legacy domain
    # are what the document says.
    assert config.supported_params == ["temperature", "max_tokens"]
    assert config.reasoning_efforts == []
    assert router.routes["legacy-passthrough"].adapters[0][0].config.reasoning_effort_map == {}


@pytest.mark.parametrize(
    ("document", "message"),
    [
        pytest.param(
            """
models:
  - id: m
    name: m
    reasoning_effort_map: {low: high}
    route:
      - kind: vllm
        base_url: http://127.0.0.1:1
""",
            "must be declared on a route",
            id="model-level",
        ),
        pytest.param(
            """
models:
  - id: m
    name: m
    route:
      - kind: vllm
        base_url: http://127.0.0.1:1
        reasoning_effort_map: [low, high]
""",
            "must map 1 to 16 distinct public levels",
            id="not-a-mapping",
        ),
        pytest.param(
            """
models:
  - id: m
    name: m
    route:
      - kind: anthropic
        base_url: http://127.0.0.1:1
        api_key: test-key
        reasoning_effort_map: {low: high}
""",
            "needs an adapter that sends reasoning_effort",
            id="native-anthropic",
        ),
    ],
)
def test_the_registry_refuses_a_map_it_cannot_honor(document, message):
    with pytest.raises(ValueError, match=message):
        _load(document)


# -- the wire: every protocol, streamed and not --------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", STREAM)
@pytest.mark.parametrize("surface", SURFACES)
async def test_each_public_level_reaches_the_upstream_as_its_mapped_name(gateway, surface, stream):
    for public, upstream in EFFORT_MAP.items():
        gateway.primary.bodies.clear()
        path, body = _request(surface, "mapped", public, stream=stream)
        response = await gateway.post(path, body)
        assert response.status_code == 200, (public, response.text)
        assert gateway.primary.efforts() == [upstream], public


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", STREAM)
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("undeclared", ["xhigh", "medium", "none"])
async def test_an_undeclared_level_is_refused_before_the_upstream(
    gateway, surface, stream, undeclared
):
    path, body = _request(surface, "mapped", undeclared, stream=stream)
    response = await gateway.post(path, body)
    assert response.status_code == 400, response.text
    assert "supported: low, high, max" in response.text
    assert gateway.primary.bodies == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", STREAM)
@pytest.mark.parametrize("surface", SURFACES)
async def test_no_level_sends_no_parameter(gateway, surface, stream):
    """Auto: the upstream's own default applies; the gateway invents none."""
    path, body = _request(surface, "mapped", None, stream=stream)
    response = await gateway.post(path, body)
    assert response.status_code == 200, response.text
    assert gateway.primary.efforts() == ["<absent>"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", STREAM)
async def test_unmapped_routes_keep_their_behavior(gateway, stream):
    # Chat passes a value through unchanged where the parameter is declared ...
    path, body = _request("chat", "legacy-passthrough", "medium", stream=stream)
    assert (await gateway.post(path, body)).status_code == 200
    # ... and drops it where it is not.
    path, body = _request("chat", "legacy-dropped", "high", stream=stream)
    assert (await gateway.post(path, body)).status_code == 200
    # Messages drops an undeclared level, as it always did (no domain declared).
    path, body = _request("messages", "legacy-passthrough", "high", stream=stream)
    assert (await gateway.post(path, body)).status_code == 200
    assert gateway.legacy.efforts() == ["medium", "<absent>", "<absent>"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", STREAM)
@pytest.mark.parametrize("surface", ["chat", "responses"])
async def test_a_fallback_route_translates_the_public_level_once(
    gateway, monkeypatch, surface, stream
):
    # The draw picks the first route, which answers 500; the second serves.
    monkeypatch.setattr("routing.routers.random.random", lambda: 0.0)
    path, body = _request(surface, "mapped-fallback", "low", stream=stream)
    response = await gateway.post(path, body)
    assert response.status_code == 200, response.text
    # Translated from "low" on each attempt: "xhigh" would be a second translation.
    assert gateway.down.efforts() == ["high"]
    assert gateway.healthy.efforts() == ["high"]


@pytest.mark.asyncio
async def test_the_listings_publish_the_public_levels_only(gateway):
    openai = {e["id"]: e for e in (await gateway.get("/v1/models")).json()["data"]}
    assert openai["mapped"]["reasoning"] == {"supported_efforts": ["low", "high", "max"]}
    assert openai["mapped-fallback"]["reasoning"] == {"supported_efforts": ["low", "high", "max"]}
    # Unmapped: unknown stays unknown, a dropped parameter stays "none".
    assert openai["legacy-passthrough"]["reasoning"] is None
    assert openai["legacy-dropped"]["reasoning"] == {"supported_efforts": []}
    anthropic = {
        e["id"]: e
        for e in (
            await gateway.get(
                "/v1/models", headers={"x-api-key": "k", "anthropic-version": "2023-06-01"}
            )
        ).json()["data"]
    }
    effort = anthropic["mapped"]["capabilities"]["effort"]
    assert [level for level, v in effort.items() if level != "supported" and v["supported"]] == [
        "low",
        "high",
        "max",
    ]


@pytest.mark.asyncio
async def test_a_map_changed_mid_request_does_not_split_the_request(gateway, monkeypatch):
    """The adapter reads the map once, before its first await: one map per request."""
    adapter: OpenAICompatAdapter = gateway.router.routes["mapped"].adapters[0][0]
    real_inline = adapter._inline_remote_media

    async def edit_then_inline(messages):
        adapter.config.reasoning_effort_map = {"high": "high"}
        return await real_inline(messages)

    monkeypatch.setattr(adapter, "_inline_remote_media", edit_then_inline)
    path, body = _request("chat", "mapped", "low", stream=False)
    response = await gateway.post(path, body)
    assert response.status_code == 200, response.text
    assert gateway.primary.efforts() == ["high"]
    # The next request reads the new map: "low" is no longer declared.
    path, body = _request("chat", "mapped", "low", stream=False)
    assert (await gateway.post(path, body)).status_code == 400
    assert len(gateway.primary.bodies) == 1
