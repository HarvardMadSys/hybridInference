"""Batch webhooks refuse IPv4 carried in IPv6 exactly as they refuse the IPv4 address.

``::169.254.169.254``, ``64:ff9b::10.0.0.5`` (NAT64) and their kin are "global"
to ``ipaddress`` and have no ``ipv4_mapped``. Each of the three checks -- the
URL when it is saved, its resolution, every redirect hop and every socket --
must still see the address they reach. Names resolve through a stand-in and no
socket is opened for a refused address.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from serving.batch import webhooks

#: IPv6 spellings of an internal or metadata IPv4 address.
EMBEDDED_INSIDE = [
    "::169.254.169.254",  # IPv4-compatible, to metadata
    "::10.0.0.5",
    "::ffff:0:10.0.0.5",  # IPv4-translated
    "64:ff9b::169.254.169.254",  # NAT64 well-known prefix
    "64:ff9b::169.254.0.23",
    "64:ff9b::10.0.0.5",
    "64:ff9b:1::a00:5",  # RFC 8215 local-use NAT64
    "2002:a9fe:a9fe::1",  # 6to4 of 169.254.169.254
    "2002:a00:5::1",  # 6to4 of 10.0.0.5
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
]

PUBLIC_IPV6 = ["2606:4700::6810:84e5", "64:ff9b::808:808", "2002:808:808::1"]


def _addr_info(address: str) -> tuple:
    if ":" in address:
        return (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 443, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))


@pytest.mark.parametrize("address", EMBEDDED_INSIDE)
def test_an_embedded_internal_literal_is_refused_when_saved(address):
    with pytest.raises(webhooks.WebhookTargetRefused):
        webhooks.check_url(f"https://[{address}]/hook", allow_private=False)


@pytest.mark.parametrize("address", ["::169.254.169.254", "64:ff9b::169.254.169.254"])
def test_embedded_metadata_stays_refused_with_private_targets_allowed(address):
    with pytest.raises(webhooks.WebhookTargetRefused):
        webhooks.check_url(f"http://[{address}]/hook", allow_private=True)


@pytest.mark.parametrize("address", PUBLIC_IPV6)
def test_a_public_ipv6_literal_is_accepted(address):
    assert webhooks.check_url(f"https://[{address}]/hook", allow_private=False)


def test_a_metadata_host_name_is_refused():
    with pytest.raises(webhooks.WebhookTargetRefused):
        webhooks.check_url("https://metadata.tencentyun.com/latest", allow_private=False)


@pytest.mark.parametrize("address", EMBEDDED_INSIDE)
async def test_a_name_resolving_to_an_embedded_internal_address_is_refused(monkeypatch, address):
    loop = asyncio.get_running_loop()

    async def resolve(host, port, **kwargs):
        return [_addr_info(address)]

    monkeypatch.setattr(loop, "getaddrinfo", resolve)
    with pytest.raises(webhooks.WebhookTargetRefused):
        await webhooks.check_resolves_public("https://inside.example.com/h", allow_private=False)


@pytest.mark.parametrize("address", EMBEDDED_INSIDE)
def test_the_socket_guard_refuses_an_embedded_internal_address(monkeypatch, address):
    def no_socket(*args, **kwargs):
        raise AssertionError("a socket must not be created for a refused address")

    monkeypatch.setattr(socket, "socket", no_socket)
    guard = webhooks.guarded_socket(allow_private=False)
    with pytest.raises(OSError):
        guard(_addr_info(address))


@pytest.mark.parametrize("address", ["::169.254.169.254", "64:ff9b::a9fe:a9fe", "::ffff:0.0.0.0"])
def test_the_socket_guard_refuses_metadata_and_unspecified_with_private_allowed(
    monkeypatch, address
):
    def no_socket(*args, **kwargs):
        raise AssertionError("a socket must not be created for a refused address")

    monkeypatch.setattr(socket, "socket", no_socket)
    with pytest.raises(OSError):
        webhooks.guarded_socket(allow_private=True)(_addr_info(address))


@pytest.mark.parametrize("address", PUBLIC_IPV6)
def test_the_socket_guard_lets_a_public_ipv6_address_through(address):
    sock = webhooks.guarded_socket(allow_private=False)(_addr_info(address))
    sock.close()


class _Response:
    def __init__(self, status: int, location: str | None = None) -> None:
        self.status = status
        self.headers = {"location": location} if location else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, answers: list[_Response], sent: list[str]) -> None:
        self._answers = answers
        self._sent = sent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, *, data, headers, allow_redirects):
        assert allow_redirects is False
        self._sent.append(url)
        return self._answers.pop(0)


@pytest.mark.parametrize("address", ["::169.254.169.254", "64:ff9b::10.0.0.5"])
async def test_a_redirect_to_an_embedded_internal_address_is_not_followed(address):
    sent: list[str] = []
    answers = [_Response(302, f"http://[{address}]/steal")]
    delivered, status, error = await webhooks.deliver(
        url="https://hooks.example.com/h",
        secret=webhooks.new_secret(),
        event_id="evt_1",
        payload={"id": "evt_1"},
        allow_private=False,
        session_factory=lambda **kwargs: _Session(answers, sent),
    )
    assert (delivered, status) == (False, 302)
    assert "redirect refused" in error
    assert sent == ["https://hooks.example.com/h"]
