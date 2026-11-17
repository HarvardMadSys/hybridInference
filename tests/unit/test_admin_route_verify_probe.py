"""A route's verification probe connects only where its Base URL was allowed to.

``_verify_provider_route`` sends one chat completion through the route's real
adapter before an admin's route change is accepted. It used to go through the
shared session that serves inference: a pooled connector guarded only against
cloud metadata, which follows redirects. So a name that passed
``_validate_base_url`` could answer a private address when the probe
connected (DNS rebinding), or a validated public host could 307 the probe into
the network, even with ``ADMIN_UPSTREAM_ALLOW_PRIVATE_NETWORKS=0``.

The upstream here is a local aiohttp server; names resolve through a stand-in
for the event loop's ``getaddrinfo``. Nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from aiohttp import web
from fastapi import HTTPException

from serving import http as serving_http
from serving.adapters import ModelConfig, OpenAICompatAdapter
from serving.http import AsyncHTTPClient
from serving.servers.routers.admin import provider_routes

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.asyncio

COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "model": "probe-model",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


class Upstream:
    """A local server whose chat endpoint the test scripts."""

    def __init__(self) -> None:
        self.port = 0
        self.chat_hits = 0
        self.internal_hits = 0
        self.redirect_to: str | None = None

    async def chat(self, request: web.Request) -> web.StreamResponse:
        self.chat_hits += 1
        if self.redirect_to:
            raise web.HTTPTemporaryRedirect(location=self.redirect_to)
        return web.json_response(COMPLETION)

    async def internal(self, request: web.Request) -> web.StreamResponse:
        self.internal_hits += 1
        return web.json_response(COMPLETION)


@pytest_asyncio.fixture
async def upstream() -> AsyncIterator[Upstream]:
    fake = Upstream()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", fake.chat)
    app.router.add_route("*", "/internal/chat/completions", fake.internal)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    fake.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield fake
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def http_client(monkeypatch) -> AsyncIterator[AsyncHTTPClient]:
    """A shared client of the test's own, as every adapter would use."""
    client = AsyncHTTPClient()
    monkeypatch.setattr(AsyncHTTPClient, "_shared", client)
    try:
        yield client
    finally:
        await client.close()


def _resolve_to(monkeypatch, address: str) -> list[str]:
    """Make every name the connector resolves answer *address*; return the names asked."""
    loop = asyncio.get_running_loop()
    asked: list[str] = []

    async def fake_getaddrinfo(host, port, *args, **kwargs):
        asked.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    return asked


def _update(base_url: str) -> Any:
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id="probe-model",
            name="Probe model",
            provider="openai",
            base_url=base_url,
            api_key="sk-probe-key",
        )
    )
    return SimpleNamespace(adapter=adapter)


async def _refused(update: Any) -> HTTPException:
    with pytest.raises(HTTPException) as caught:
        await provider_routes._verify_provider_route(update)
    assert caught.value.status_code == 400
    return caught.value


async def test_a_private_upstream_still_verifies_by_default(upstream, http_client):
    """The default keeps self-hosted engines: loopback is allowed."""
    await provider_routes._verify_provider_route(_update(f"http://127.0.0.1:{upstream.port}/v1"))

    assert upstream.chat_hits == 1


async def test_a_name_that_rebinds_to_loopback_is_refused_when_private_upstreams_are_off(
    upstream, http_client, monkeypatch
):
    """Validated while it answered a public address; connected while it answers 127.0.0.1."""
    monkeypatch.setenv("ADMIN_UPSTREAM_ALLOW_PRIVATE_NETWORKS", "0")
    asked = _resolve_to(monkeypatch, "127.0.0.1")

    refused = await _refused(_update(f"http://rebind.example:{upstream.port}/v1"))

    assert asked == ["rebind.example"]
    assert upstream.chat_hits == 0
    assert refused.detail == (
        "Provider verification failed: this deployment does not allow connections to the "
        "provider's address."
    )
    assert "127.0.0.1" not in refused.detail


async def test_a_name_that_rebinds_to_cloud_metadata_is_refused_by_default(
    http_client, monkeypatch
):
    _resolve_to(monkeypatch, "169.254.169.254")

    refused = await _refused(_update("http://rebind.example:8080/v1"))

    assert "does not allow connections" in refused.detail


async def test_a_redirect_into_the_network_is_never_followed(upstream, http_client):
    """A host that passed validation must not 307 the probe somewhere that was never checked."""
    upstream.redirect_to = f"http://127.0.0.1:{upstream.port}/internal/chat/completions"

    refused = await _refused(_update(f"http://127.0.0.1:{upstream.port}/v1"))

    assert upstream.chat_hits == 1
    assert upstream.internal_hits == 0
    assert refused.detail.startswith("Provider answered with a redirect (HTTP 307)")
    assert "/internal" not in refused.detail


async def test_a_public_name_redirecting_to_a_private_address_is_refused_with_private_off(
    upstream, http_client, monkeypatch
):
    monkeypatch.setenv("ADMIN_UPSTREAM_ALLOW_PRIVATE_NETWORKS", "0")
    upstream.redirect_to = f"http://10.0.0.5:{upstream.port}/internal/chat/completions"
    # The public name is the local server, standing in for a public host.
    monkeypatch.setattr(provider_routes, "probe_session", _probe_session_allowing(upstream.port))
    _resolve_to(monkeypatch, "127.0.0.1")

    refused = await _refused(_update(f"http://public.example:{upstream.port}/v1"))

    assert upstream.chat_hits == 1
    assert upstream.internal_hits == 0
    assert refused.detail.startswith("Provider answered with a redirect (HTTP 307)")
    assert "10.0.0.5" not in refused.detail


def _probe_session_allowing(port: int):
    """The real probe session, its socket factory letting the stand-in public host through.

    Loopback on *port* plays the public host here; every other address goes
    through the real admin upstream policy.
    """
    from serving.utils.admin_upstreams import admin_upstream_socket

    def factory(addr_info: tuple[Any, ...]) -> socket.socket:
        if addr_info[4][:2] == ("127.0.0.1", port):
            family, type_, proto, _, _ = addr_info
            return socket.socket(family=family, type=type_, proto=proto)
        return admin_upstream_socket(addr_info)

    return lambda _socket_factory: serving_http.probe_session(factory)


async def test_inference_keeps_the_shared_session_and_its_pool(upstream, http_client):
    """Only the probe's own task uses the probe session; saved routes are untouched."""
    shared = await http_client._ensure_session()
    seen: list[Any] = []

    async def record_inside() -> None:
        seen.append(await http_client._ensure_session())

    async with serving_http.probe_session(lambda info: socket.socket(*info[:3])):
        await record_inside()
        # A request already running elsewhere -- inference -- is not in the probe.
        outside = await asyncio.create_task(_session_outside(http_client), context=_empty())

    assert seen[0] is not shared
    assert seen[0].closed
    assert outside is shared
    assert await http_client._ensure_session() is shared
    assert serving_http._PROBE_SESSION.get() is None


async def _session_outside(client: AsyncHTTPClient) -> Any:
    return await client._ensure_session()


def _empty():
    import contextvars

    return contextvars.Context()
