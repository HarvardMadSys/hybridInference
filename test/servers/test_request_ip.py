"""Tests for trusted proxy client IP extraction."""

from __future__ import annotations

from types import SimpleNamespace

from serving.utils.request_ip import get_client_ip


def _request(
    headers: dict[str, str] | None = None,
    client_host: str | None = "127.0.0.1",
):
    """Build a minimal request-like object for IP extraction tests."""
    client = SimpleNamespace(host=client_host) if client_host is not None else None
    return SimpleNamespace(headers=headers or {}, client=client)


def test_get_client_ip_ignores_proxy_headers_by_default(monkeypatch):
    """Proxy headers are ignored unless explicitly trusted."""
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    request = _request(
        {
            "x-forwarded-for": "203.0.113.10",
            "x-real-ip": "203.0.113.11",
        },
        client_host="10.0.0.5",
    )

    assert get_client_ip(request) == "10.0.0.5"


def test_get_client_ip_uses_first_forwarded_for_when_trusted(monkeypatch):
    """Trusted proxies should overwrite X-Forwarded-For before it reaches the app."""
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    request = _request(
        {
            "x-forwarded-for": "203.0.113.10, 198.51.100.20",
            "x-real-ip": "203.0.113.11",
        },
        client_host="10.0.0.5",
    )

    assert get_client_ip(request) == "203.0.113.10"


def test_get_client_ip_falls_back_to_real_ip_when_trusted(monkeypatch):
    """X-Real-IP is used when the trusted proxy does not send X-Forwarded-For."""
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    request = _request({"x-real-ip": "203.0.113.11"}, client_host="10.0.0.5")

    assert get_client_ip(request) == "203.0.113.11"


def test_get_client_ip_falls_back_to_peer_without_headers(monkeypatch):
    """Peer IP remains the fallback when no trusted proxy header is present."""
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    request = _request({"x-forwarded-for": "   "}, client_host="10.0.0.5")

    assert get_client_ip(request) == "10.0.0.5"
