"""The provider-definition probe against a real (local) HTTP upstream.

Before an admin saves a custom provider, ``_probe_openai_compat`` streams one
short chat completion from it. The route tests replace the probe wholesale, so
none of this had run: how it reads the stream, what it reports for each kind
of failure, and that it never follows a redirect -- a validated public host
must not be able to 302 the probe to an internal address, past
``_validate_base_url``'s IP checks.

The base-URL validator refuses loopback, so it is replaced here by one that
accepts this test's local server only.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web
from fastapi import HTTPException

from serving.servers.routers.admin import provider_definitions

pytestmark = pytest.mark.asyncio

Handler = Callable[[web.Request], Any]


def _sse(*events: dict[str, Any] | str) -> bytes:
    lines = []
    for event in events:
        data = event if isinstance(event, str) else json.dumps(event)
        lines.append(f"data: {data}\n\n")
    return "".join(lines).encode()


def _delta(content: str) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {"content": content}}]}


class Upstream:
    """A local server whose /v1/chat/completions the test scripts."""

    def __init__(self) -> None:
        self.handler: Handler = lambda request: web.Response(status=500)
        self.requests: list[dict[str, Any]] = []
        self.elsewhere_hits = 0
        self.base_url = ""

    async def _chat(self, request: web.Request) -> Any:
        self.requests.append({"headers": dict(request.headers), "body": await request.json()})
        return (
            await self.handler(request)
            if asyncio.iscoroutinefunction(self.handler)
            else self.handler(request)
        )

    async def _elsewhere(self, request: web.Request) -> web.Response:
        self.elsewhere_hits += 1
        return web.Response(body=_sse(_delta("internal secret"), "[DONE]"))


@pytest_asyncio.fixture
async def upstream(monkeypatch) -> AsyncIterator[Upstream]:
    fake = Upstream()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", fake._chat)
    app.router.add_route("*", "/internal/chat/completions", fake._elsewhere)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    fake.base_url = f"http://127.0.0.1:{port}/v1"

    async def _accept_only_this_server(base_url: str) -> str:
        assert base_url.startswith(f"http://127.0.0.1:{port}"), base_url
        return base_url.rstrip("/")

    monkeypatch.setattr(provider_definitions, "_validate_base_url", _accept_only_this_server)
    try:
        yield fake
    finally:
        await runner.cleanup()


async def _probe(upstream: Upstream, **overrides: Any):
    return await provider_definitions._probe_openai_compat(
        **{
            "default_base_url": upstream.base_url,
            "api_key": " sk-test-key ",
            "probe_model_id": " probe-model ",
            **overrides,
        }
    )


async def _refused(upstream: Upstream, **overrides: Any) -> HTTPException:
    with pytest.raises(HTTPException) as caught:
        await _probe(upstream, **overrides)
    return caught.value


async def test_a_streaming_answer_reports_its_latency_and_preview(upstream):
    upstream.handler = lambda request: web.Response(
        body=_sse({"choices": []}, _delta("Hello"), _delta(" there"), "[DONE]"),
        content_type="text/event-stream",
    )

    result = await _probe(upstream)

    assert result.preview == "Hello there"
    assert result.first_event_ttft_ms is not None
    assert result.first_content_ttft_ms is not None
    assert result.first_event_ttft_ms <= result.first_content_ttft_ms
    [sent] = upstream.requests
    assert sent["headers"]["Authorization"] == "Bearer sk-test-key"  # stripped
    assert sent["body"]["model"] == "probe-model"
    assert sent["body"]["stream"] is True


async def test_the_preview_is_one_line_of_at_most_160_characters(upstream):
    upstream.handler = lambda request: web.Response(
        body=_sse(_delta("a\x00b\n\n  c " * 10), _delta("x" * 400), "[DONE]")
    )

    result = await _probe(upstream)

    assert result.preview is not None
    assert len(result.preview) <= 160
    assert "\x00" not in result.preview and "\n" not in result.preview
    assert result.preview.startswith("ab c ab c")


async def test_reasoning_content_counts_as_an_answer(upstream):
    upstream.handler = lambda request: web.Response(
        body=_sse({"choices": [{"index": 0, "delta": {"reasoning_content": "thinking"}}]}, "[DONE]")
    )

    assert (await _probe(upstream)).preview == "thinking"


@pytest.mark.parametrize(
    ("status", "says"),
    [
        (401, "Authentication failed. Check the API key."),
        (403, "Authentication failed. Check the API key."),
        (404, "Endpoint not found. Check that Base URL excludes /chat/completions."),
        (500, "Provider probe failed with HTTP 500"),
    ],
)
async def test_an_error_status_says_what_to_fix(upstream, status, says):
    upstream.handler = lambda request: web.Response(
        status=status, text='{"error": "upstream said no"}'
    )

    refused = await _refused(upstream)

    assert refused.status_code == 400
    assert refused.detail.startswith(says)
    assert "upstream said no" in refused.detail


async def test_an_error_body_is_quoted_short(upstream):
    upstream.handler = lambda request: web.Response(status=502, text="boom " * 200)

    refused = await _refused(upstream)

    assert len(refused.detail) <= len("Provider probe failed with HTTP 502 ") + 160


async def test_a_redirect_is_never_followed(upstream):
    """A public host must not be able to send the probe to an internal address."""
    upstream.handler = lambda request: web.HTTPFound(
        location=upstream.base_url.replace("/v1", "/internal") + "/chat/completions"
    )

    refused = await _refused(upstream)

    assert upstream.elsewhere_hits == 0
    assert refused.detail == "Provider did not return a streaming response."


async def test_a_non_streaming_answer_is_refused(upstream):
    upstream.handler = lambda request: web.json_response(
        {"choices": [{"message": {"content": "not a stream"}}]}
    )

    refused = await _refused(upstream)

    assert refused.detail == "Provider did not return a streaming response."


async def test_a_stream_that_ends_without_done_still_reports(upstream):
    upstream.handler = lambda request: web.Response(body=_sse(_delta("partial")))

    assert (await _probe(upstream)).preview == "partial"


async def test_a_slow_provider_times_out(upstream, monkeypatch):
    monkeypatch.setattr(provider_definitions, "PROBE_TIMEOUT_SEC", 0.2)

    async def _slow(request: web.Request) -> web.Response:
        await asyncio.sleep(2)
        return web.Response(body=_sse("[DONE]"))

    upstream.handler = _slow

    refused = await _refused(upstream)

    assert refused.detail == "Probe timed out. Check provider availability or model id."


async def test_an_unreachable_provider_is_a_400(upstream, monkeypatch):
    async def _closed_port(base_url: str) -> str:
        return "http://127.0.0.1:1/v1"

    monkeypatch.setattr(provider_definitions, "_validate_base_url", _closed_port)

    refused = await _refused(upstream)

    assert refused.status_code == 400
    assert refused.detail.startswith("Provider probe failed:")


@pytest.mark.parametrize(
    ("overrides", "says"),
    [
        ({"api_key": "   "}, "api_key must not be blank"),
        ({"probe_model_id": " "}, "probe_model_id must not be blank"),
    ],
)
async def test_blank_inputs_are_refused_before_any_request(upstream, overrides, says):
    refused = await _refused(upstream, **overrides)

    assert (refused.status_code, refused.detail) == (422, says)
    assert upstream.requests == []


async def test_a_base_url_that_already_names_the_endpoint_is_refused(upstream):
    refused = await _refused(upstream, default_base_url=upstream.base_url + "/chat/completions/")

    assert refused.status_code == 422
    assert upstream.requests == []
