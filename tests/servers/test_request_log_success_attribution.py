"""A successful request's log line names the model and the upstream that served it.

The routers set ``model`` and ``provider`` in the request context only around
each attempt, so by the time ``RequestLogMiddleware`` wrote its line both had
unwound; only an error path published the provider durably. Every successful
line carried ``model=None`` and no provider, and ``p95_latency_per_provider``,
which keys on the provider, judged failures alone: a provider answering slowly
but successfully never paged.

These drive the real chat route behind the real middleware stack, then feed
the captured lines into the real rule.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.observability import alert_rules
from serving.observability.alert_config import LatencyRule
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.middleware.request_id import RequestIdMiddleware
from serving.servers.middleware.request_log import RequestLogMiddleware
from serving.servers.routers import completions
from serving.stream import done_sentinel, make_final_usage_chunk

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

MODEL = "served-model"
PROVIDER = "upstream-x"
_REQUEST_LOG = "serving.servers.middleware.request_log"


@pytest.fixture(autouse=True)
def _auth_off(monkeypatch):
    from serving.config.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("serving.servers.auth.is_user_auth_enabled", lambda: False)
    yield
    get_settings.cache_clear()


class _SlowUpstream(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        await asyncio.sleep(0.02)
        return self.format_response(content="done", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        await asyncio.sleep(0.02)
        yield self.format_stream_chunk(model=self.config.id, content="do")
        yield self.format_stream_chunk(model=self.config.id, content="ne")
        yield make_final_usage_chunk(model=self.config.id, messages=messages, total_content="done")
        yield done_sentinel()


@pytest.fixture
def app(mock_db_logger, mock_log_store) -> FastAPI:
    cfg = ModelConfig(
        id=MODEL,
        name=MODEL,
        provider=PROVIDER,
        base_url="http://upstream.test/v1",
        context_length=8192,
        max_output_length=4096,
        supported_params=["temperature", "max_tokens"],
    )
    router = FixedRouter()
    router.register_route(MODEL, [(_SlowUpstream(cfg), 1.0)])
    app = FastAPI(title="Success attribution")
    app.state.services = AppServices(
        router=router, db_logger=mock_db_logger, log_store=mock_log_store
    )
    install_error_handlers(app)
    app.include_router(completions.router)
    # As in the application: the request id outermost, the request log inside it.
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(RequestIdMiddleware)
    return app


async def _request_lines(app: FastAPI, caplog, requests: list[tuple[str, str, dict | None]]):
    with caplog.at_level(logging.INFO, logger=_REQUEST_LOG):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            for method, path, body in requests:
                response = await client.request(method, path, json=body)
                await response.aread()
    return [
        r for r in caplog.records if r.name == _REQUEST_LOG and r.getMessage() == "http_request"
    ]


def _chat(stream: bool) -> tuple[str, str, dict]:
    body = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": stream}
    return ("POST", "/v1/chat/completions", body)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_a_successful_request_line_names_its_model_and_upstream(app, caplog, stream):
    [line] = await _request_lines(app, caplog, [_chat(stream)])

    assert line.status_code == 200
    assert line.model == MODEL
    assert line.provider == PROVIDER


@pytest.mark.asyncio
async def test_the_p95_rule_counts_successful_requests(app, caplog, monkeypatch):
    lines = await _request_lines(app, caplog, [_chat(False), _chat(True)] * 5)
    assert [line.status_code for line in lines] == [200] * 10

    breaches: list[dict[str, Any]] = []

    async def transition(**kwargs: Any) -> bool:
        if kwargs["breached"]:
            breaches.append(kwargs["context"]())
        return False

    monkeypatch.setattr(alert_rules, "alert_on_transition", transition)
    rule = alert_rules.P95LatencyRule(
        LatencyRule(enabled=True, window_sec=600, threshold_ms=1, min_samples=10, cooldown_sec=60)
    )
    for line in lines:
        await rule.on_record(line)

    # Every successful request counted, and slow success alone breaches.
    assert breaches
    assert breaches[-1]["provider"] == PROVIDER
    assert breaches[-1]["samples"] == 10


@pytest.mark.asyncio
async def test_the_next_request_on_the_task_does_not_inherit_the_served_model(app, caplog):
    routed, unrouted = await _request_lines(
        app, caplog, [_chat(False), ("GET", "/v1/no-such-path", None)]
    )

    assert routed.model == MODEL
    assert unrouted.status_code == 404
    assert unrouted.model is None
    assert unrouted.provider is None
