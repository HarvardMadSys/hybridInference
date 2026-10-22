"""Cloud metadata addresses are refused at the moment a socket would be opened.

``_validate_base_url`` resolves a Base URL's host once, when the admin saves it.
Every later connection resolves it again, so the check that has to hold is the
one on the address actually being connected: ``refuse_metadata_socket``, the
``socket_factory`` of the adapters' shared HTTP session.

Names are resolved by a stand-in ``socket.getaddrinfo``; nothing here reaches a
real metadata endpoint, because the refusal comes before the socket exists.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from serving.http import AsyncHTTPClient
from serving.utils.metadata_addresses import is_metadata_address, refuse_metadata_socket

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",  # AWS, GCP, Azure
        "169.254.0.23",  # Tencent Cloud
        "169.254.170.2",  # AWS ECS task credentials
        "169.254.170.23",  # AWS EKS Pod Identity
        "100.100.100.200",  # Alibaba Cloud
        "fd00:ec2::254",  # AWS IPv6 instance metadata
        "fd00:ec2::23",  # AWS IPv6 EKS Pod Identity
        "fd00:ec2::254%0",
        "fd00:ec2::254%eth0",
        "fe80::1",
        "fe80::1%eth0",
        "::ffff:169.254.169.254",
    ],
)
def test_a_metadata_address_is_recognised_in_every_spelling(address):
    assert is_metadata_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",
        "192.168.1.10",
        "100.64.0.1",  # carrier-grade NAT
        "100.100.100.201",
        "127.0.0.1",
        "::1",
        "fd00::1",
        "fd00:ec3::254",
        "8.8.8.8",
        "2606:4700::6810:84e5",
    ],
)
def test_an_upstream_address_is_not_a_metadata_address(address):
    assert not is_metadata_address(address)


def _addr_info(address: str) -> tuple:
    if ":" in address:
        return (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 80, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))


@pytest.mark.parametrize("address", ["169.254.169.254", "fd00:ec2::254%0", "not-an-address"])
def test_the_socket_factory_refuses_before_creating_a_socket(address, monkeypatch):
    def no_socket(*args, **kwargs):
        raise AssertionError("a socket must not be created for a refused address")

    monkeypatch.setattr(socket, "socket", no_socket)

    with pytest.raises(OSError, match="cloud metadata address"):
        refuse_metadata_socket(_addr_info(address))


@pytest.mark.parametrize("address", ["10.1.2.3", "::1"])
def test_the_socket_factory_opens_an_ordinary_socket_otherwise(address):
    family, type_, proto, _, _ = _addr_info(address)

    with refuse_metadata_socket(_addr_info(address)) as sock:
        assert (sock.family, sock.type, sock.proto) == (family, type_, proto)


@pytest_asyncio.fixture
async def local_upstream() -> AsyncIterator[int]:
    async def chat(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    finally:
        await runner.cleanup()


def _resolve_every_name_to(address: str, monkeypatch) -> None:
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.mark.asyncio
async def test_the_shared_adapter_session_refuses_a_name_that_now_resolves_to_metadata(
    monkeypatch,
):
    _resolve_every_name_to("169.254.169.254", monkeypatch)
    client = AsyncHTTPClient()
    try:
        with pytest.raises(aiohttp.ClientConnectorError, match="cloud metadata address"):
            await client.json_post(
                "http://rebind.example/v1/chat/completions",
                json={},
                timeout=aiohttp.ClientTimeout(total=2),
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_shared_adapter_session_refuses_a_metadata_ip_literal():
    """aiohttp skips the resolver for an IP literal; the socket factory still runs."""
    client = AsyncHTTPClient()
    try:
        with pytest.raises(aiohttp.ClientConnectorError, match="cloud metadata address"):
            await client.json_post(
                "http://169.254.169.254/latest/meta-data",
                json={},
                timeout=aiohttp.ClientTimeout(total=2),
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_shared_adapter_session_still_reaches_a_private_upstream(
    local_upstream, monkeypatch
):
    _resolve_every_name_to("127.0.0.1", monkeypatch)
    client = AsyncHTTPClient()
    try:
        answer = await client.json_post(
            f"http://upstream.internal.example:{local_upstream}/v1/chat/completions", json={}
        )
    finally:
        await client.close()

    assert answer == {"ok": True}
