"""Webhook target validation: HTTPS shape and public-address resolution."""

from __future__ import annotations

import socket

import pytest

from serving.utils.webhook_target import (
    UnsafeWebhookTarget,
    resolve_public_webhook,
    webhook_url_problem,
)


def _answers(*addresses: str, port: int = 443):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)) for address in addresses]


@pytest.mark.parametrize(
    "url",
    [
        "https://hooks.example.com/batch",
        "https://hooks.example.com:8443/batch?token=abc",
    ],
)
def test_an_https_url_with_a_host_is_usable(url):
    assert webhook_url_problem(url) is None


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("", "must not be empty"),
        ("http://hooks.example.com/batch", "must use https"),
        ("https://hooks.example.com/batch#frag", "must not have a fragment"),
        ("https://user:pass@hooks.example.com/batch", "must not include credentials"),
        ("https:///batch", "must include a host"),
        ("https://hooks.example.com/bat ch", "must not contain whitespace or control characters"),
        ("https://hooks.example.com:0/batch", "has an invalid port"),
        ("https://hooks.example.com/" + "a" * 4096, "must be at most 4096 characters"),
    ],
)
def test_a_webhook_url_shape_that_is_refused(url, problem):
    assert webhook_url_problem(url) == problem


@pytest.mark.asyncio
async def test_a_public_address_resolves(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: _answers("93.184.216.34"))

    await resolve_public_webhook("https://hooks.example.com/batch")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "fc00::1",
        "::ffff:10.0.0.1",
    ],
)
async def test_a_non_public_address_is_refused(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: _answers(address))

    with pytest.raises(UnsafeWebhookTarget, match="non-public"):
        await resolve_public_webhook("https://hooks.example.com/batch")


@pytest.mark.asyncio
async def test_one_private_answer_among_public_ones_is_refused(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kwargs: _answers("93.184.216.34", "10.0.0.7")
    )

    with pytest.raises(UnsafeWebhookTarget, match="non-public"):
        await resolve_public_webhook("https://hooks.example.com/batch")


@pytest.mark.asyncio
async def test_an_unresolvable_host_is_refused(monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fail)

    with pytest.raises(UnsafeWebhookTarget, match="could not be resolved"):
        await resolve_public_webhook("https://hooks.example.com/batch")
