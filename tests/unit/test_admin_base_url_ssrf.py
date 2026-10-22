"""An admin-supplied provider Base URL may point anywhere except cloud metadata.

``_validate_base_url`` checks every Base URL an admin types (a provider route,
a custom provider definition); the gateway then sends real requests, with real
keys, to whatever it accepts. Admins are trusted to reach plain-http and
private upstreams -- self-hosted engines and on-prem gateways live there -- but
never a cloud instance metadata endpoint, in any spelling.

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
        "https://169.254.169.254/latest/meta-data",  # AWS, GCP, Azure
        "http://169.254.0.23/meta-data",  # Tencent Cloud
        "http://100.100.100.200/latest/meta-data",  # Alibaba Cloud
        "http://[fd00:ec2::254]/latest/meta-data",  # AWS IPv6
        "http://[fd00:ec2::23]/v1/credentials",  # AWS EKS Pod Identity, IPv6
        "http://[fd00:ec2::254%0]/latest",  # a zone does not change the address
        "http://[fd00:ec2::254%25eth0]/latest",
        "http://[fe80::1]/v1",
        "http://[::ffff:169.254.169.254]/latest",  # IPv4-mapped
        "http://2852039166/latest",  # 169.254.169.254 as one integer
    ],
)
async def test_a_metadata_address_is_refused_in_every_spelling(url):
    assert await _refused(url) == "base_url host is not allowed"


async def test_a_metadata_host_name_is_refused_before_any_lookup(monkeypatch):
    def no_lookup(*args, **kwargs):
        raise AssertionError("a reserved name must not be resolved")

    monkeypatch.setattr(provider_routes.socket, "getaddrinfo", no_lookup)

    assert (
        await _refused("http://METADATA.google.internal./computeMetadata/v1")
        == "base_url host is not allowed"
    )


async def test_a_name_resolving_to_aws_ipv6_metadata_is_refused(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00:ec2::254", port or 443, 0, 0))]

    monkeypatch.setattr(provider_routes.socket, "getaddrinfo", fake_getaddrinfo)

    assert await _refused("http://aws6.example/v1") == "base_url host is not allowed"


async def test_a_name_with_any_metadata_address_is_refused(monkeypatch):
    """Every address it resolves to is checked, not just the first."""
    monkeypatch.setattr(
        provider_routes.socket, "getaddrinfo", _resolves_to("10.1.2.3", "169.254.169.254")
    )

    assert await _refused("http://mixed.internal.example/v1") == "base_url host is not allowed"


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:8000/v1",
        "http://172.16.3.4/v1",
        "http://192.168.1.10/v1",
        "http://100.64.0.1/v1",  # carrier-grade NAT
        "http://127.0.0.1:8001/v1",
        "http://[::1]:8001/v1",
        "http://[fd00::1]/v1",
    ],
)
async def test_a_private_or_loopback_address_is_accepted_over_http(url):
    assert await provider_routes._validate_base_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8001/v1",
        "http://host.docker.internal:8001/v1",
        "http://gateway.internal.example/llmproxy",
    ],
)
async def test_an_internal_name_is_accepted(url, monkeypatch):
    monkeypatch.setattr(provider_routes.socket, "getaddrinfo", _resolves_to("10.20.30.40"))

    assert await provider_routes._validate_base_url(url) == url


async def test_a_public_name_or_address_is_accepted(monkeypatch):
    monkeypatch.setattr(provider_routes.socket, "getaddrinfo", _resolves_to("93.184.216.34"))

    assert (
        await provider_routes._validate_base_url("  api.example.com/v1 ")
        == "https://api.example.com/v1"
    )
    assert (
        await provider_routes._validate_base_url("http://api.example.com/v1")
        == "http://api.example.com/v1"
    )
    assert await provider_routes._validate_base_url("https://8.8.8.8/v1") == "https://8.8.8.8/v1"


@pytest.mark.parametrize(
    ("url", "detail"),
    [
        ("   ", "base_url must not be blank"),
        ("ftp://api.example.com/v1", "base_url must use http or https"),
        ("file:///etc/passwd", "base_url must include a host"),
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
