"""An admin-supplied provider Base URL can never point the gateway inward.

``_validate_base_url`` is the SSRF guard for every Base URL an admin types (a
provider route, a custom provider definition); the gateway then sends real
requests, with real keys, to whatever it accepts. Many of its refusals had no
test -- among them every IP-literal form, so nothing showed that
``https://169.254.169.254`` (cloud metadata) or ``https://127.0.0.1`` is refused.

Numeric hosts resolve without the network; names are resolved by a stand-in.
"""

from __future__ import annotations

import socket

import pytest
from fastapi import HTTPException

from serving.servers.routers.admin import provider_routes

pytestmark = pytest.mark.asyncio


def _resolves_to(*addresses: str):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port or 443)) for a in addresses]

    return fake_getaddrinfo


async def _refused(url: str) -> str:
    with pytest.raises(HTTPException) as caught:
        await provider_routes._validate_base_url(url)
    assert caught.value.status_code == 422
    return caught.value.detail


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/v1",
        "https://169.254.169.254/latest/meta-data",  # cloud metadata
        "https://10.0.0.5/v1",
        "https://172.16.3.4/v1",
        "https://192.168.1.10/v1",
        "https://100.64.0.1/v1",  # carrier-grade NAT
        "https://0.0.0.0/v1",
        "https://224.0.0.1/v1",
        "https://[::1]/v1",
        "https://[fe80::1]/v1",
        "https://[fd00::1]/v1",
        "https://[::ffff:127.0.0.1]/v1",  # IPv4-mapped loopback
        # Other spellings of 127.0.0.1 that the resolver accepts.
        "https://2130706433/v1",
        "https://0x7f.1/v1",
        "https://127.1/v1",
    ],
)
async def test_an_internal_address_is_refused_in_every_spelling(url):
    assert await _refused(url) == "base_url host is not allowed"


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/v1",
        "https://LOCALHOST./v1",
        "https://api.localhost/v1",
        "https://host.docker.internal/v1",
        "https://metadata.google.internal/computeMetadata/v1",
    ],
)
async def test_a_reserved_host_name_is_refused_before_any_lookup(url, monkeypatch):
    def no_lookup(*args, **kwargs):
        raise AssertionError("a reserved name must not be resolved")

    monkeypatch.setattr(provider_routes.socket, "getaddrinfo", no_lookup)

    assert await _refused(url) == "base_url host is not allowed"


async def test_a_name_with_any_internal_address_is_refused(monkeypatch):
    """Every address it resolves to must be public, not just the first."""
    monkeypatch.setattr(
        provider_routes.socket, "getaddrinfo", _resolves_to("93.184.216.34", "10.1.2.3")
    )

    assert await _refused("https://mixed.example.com/v1") == "base_url host is not allowed"


async def test_a_public_name_or_address_is_accepted(monkeypatch):
    monkeypatch.setattr(provider_routes.socket, "getaddrinfo", _resolves_to("93.184.216.34"))

    assert (
        await provider_routes._validate_base_url("  api.example.com/v1 ")
        == "https://api.example.com/v1"
    )
    assert await provider_routes._validate_base_url("https://8.8.8.8/v1") == "https://8.8.8.8/v1"


@pytest.mark.parametrize(
    ("url", "detail"),
    [
        ("   ", "base_url must not be blank"),
        ("http://api.example.com/v1", "base_url must use https"),
        ("ftp://api.example.com/v1", "base_url must use https"),
        ("https:///v1", "base_url must include a host"),
        ("https://user:pass@api.example.com/v1", "base_url must not include credentials"),
        ("https://api.example.com/v1?key=x", "base_url must not include query or fragment"),
        ("https://api.example.com/v1#frag", "base_url must not include query or fragment"),
    ],
)
async def test_a_malformed_base_url_is_refused(url, detail):
    assert await _refused(url) == detail


async def test_a_name_that_does_not_resolve_is_refused(monkeypatch):
    def unknown(*args, **kwargs):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(provider_routes.socket, "getaddrinfo", unknown)

    assert await _refused("https://nowhere.invalid/v1") == "base_url host could not be resolved"


async def test_a_lookup_that_hangs_is_refused(monkeypatch):
    import time

    def slow(*args, **kwargs):
        time.sleep(0.5)
        return []

    monkeypatch.setattr(provider_routes.socket, "getaddrinfo", slow)
    monkeypatch.setattr(provider_routes, "BASE_URL_DNS_TIMEOUT_SEC", 0.05)

    assert await _refused("https://slow.example.com/v1") == "base_url host resolution timed out"


async def test_a_resolver_answer_that_is_not_an_address_is_refused(monkeypatch):
    monkeypatch.setattr(provider_routes.socket, "getaddrinfo", _resolves_to("not-an-ip"))

    assert (
        await _refused("https://odd.example.com/v1")
        == "base_url host resolved to an invalid address"
    )
