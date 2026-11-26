"""What the gateway tells an engine's admission process about a request.

Four headers, read from the request context as the adapter sends: how long the
request has been here, the cloud's first-token time, the caller's session and
the gateway's id for the request. They go to a route that declared the process
and to no other, and the held time counts from the moment the outermost
middleware saw the request.
"""

from __future__ import annotations

import math
import time
from unittest.mock import MagicMock

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.engine_admission import (
    ELAPSED_HEADER,
    REMOTE_TTFT_HEADER,
    REQUEST_ID_HEADER,
    SESSION_HEADER,
    AdmissionTerms,
    admission_context,
    admission_headers,
)
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.servers.middleware.request_id import RequestIdMiddleware
from serving.utils import context as req_ctx


@pytest.fixture(autouse=True)
def _clean_context():
    req_ctx.set({})
    yield
    req_ctx.set({})


def _adapter(*, admission: str | None, **config) -> OpenAICompatAdapter:
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id="qwen-local",
            name="Qwen local",
            provider="sglang",
            base_url="http://mock.local/v1",
            provider_model_id="qwen-local",
            admission=admission,
            **config,
        )
    )
    adapter.http = MagicMock()
    return adapter


def test_a_dispatch_nobody_described_has_no_cloud_behind_it():
    # A pinned request, or a model on another strategy: nothing takes the
    # request to the cloud on a decline, so the process is told there is none.
    assert admission_headers() == {REMOTE_TTFT_HEADER: "inf"}


def test_the_held_time_counts_from_when_the_request_arrived():
    with req_ctx.push(**{req_ctx.REQUEST_RECEIVED: 100.0}):
        assert admission_headers(now=100.25)[ELAPSED_HEADER] == "250.000"
        # A clock that read earlier than the arrival is no negative duration.
        assert admission_headers(now=99.0)[ELAPSED_HEADER] == "0.000"


def test_the_terms_a_router_pushed_become_headers():
    terms = AdmissionTerms(remote_ttft_ms=412.3456, session_id="sess-1", request_id="req-9")
    with req_ctx.push(**admission_context(terms), **{req_ctx.REQUEST_RECEIVED: 10.0}):
        headers = admission_headers(now=10.004)

    assert headers == {
        ELAPSED_HEADER: "4.000",
        REMOTE_TTFT_HEADER: "412.346",
        SESSION_HEADER: "sess-1",
        REQUEST_ID_HEADER: "req-9",
    }


def test_no_cloud_route_is_said_as_inf():
    with req_ctx.push(**admission_context(AdmissionTerms(remote_ttft_ms=math.inf))):
        assert admission_headers() == {REMOTE_TTFT_HEADER: "inf"}


@pytest.mark.parametrize("value", ["", "   ", "line\nbreak", "café", "x" * 129, 7, None])
def test_an_id_a_header_cannot_carry_is_left_out(value):
    terms = AdmissionTerms(remote_ttft_ms=1.0, session_id=value, request_id=value)
    with req_ctx.push(**admission_context(terms)):
        assert admission_headers() == {REMOTE_TTFT_HEADER: "1.000"}


@pytest.mark.parametrize("remote_ttft_ms", [math.nan, -1.0])
def test_a_cloud_time_that_is_no_duration_is_left_out(remote_ttft_ms):
    with req_ctx.push(**admission_context(AdmissionTerms(remote_ttft_ms=remote_ttft_ms))):
        assert admission_headers() == {}


def test_only_a_route_that_declared_the_process_is_told():
    terms = AdmissionTerms(remote_ttft_ms=500.0, session_id="s", request_id="r")
    with req_ctx.push(**admission_context(terms), **{req_ctx.REQUEST_RECEIVED: time.monotonic()}):
        told = _adapter(admission="nimbus")._build_headers()
        untold = _adapter(admission=None)._build_headers()

    assert told[REMOTE_TTFT_HEADER] == "500.000"
    assert told[SESSION_HEADER] == "s"
    assert told[REQUEST_ID_HEADER] == "r"
    assert float(told[ELAPSED_HEADER]) >= 0.0
    for name in (REMOTE_TTFT_HEADER, ELAPSED_HEADER, SESSION_HEADER, REQUEST_ID_HEADER):
        assert name not in untold


def test_a_configured_header_cannot_stand_in_for_the_requests_own():
    adapter = _adapter(admission="nimbus", extra_headers={REMOTE_TTFT_HEADER: "0"})
    with req_ctx.push(**admission_context(AdmissionTerms(remote_ttft_ms=500.0))):
        assert adapter._build_headers()[REMOTE_TTFT_HEADER] == "500.000"


async def test_the_outermost_middleware_notes_when_a_request_arrived():
    seen: dict[str, float] = {}

    async def app(scope, receive, send):
        seen["received"] = req_ctx.get().get(req_ctx.REQUEST_RECEIVED)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        return None

    before = time.monotonic()
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    await RequestIdMiddleware(app)(scope, receive, send)

    assert before <= seen["received"] <= time.monotonic()
    # Cleared with the rest of a request's own keys, so a scope that seeds
    # nothing cannot report the previous request's arrival as its own.
    assert req_ctx.REQUEST_RECEIVED in req_ctx.REQUEST_SCOPED_KEYS
