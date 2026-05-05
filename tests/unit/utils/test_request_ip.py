"""Tests for proxied client IP extraction."""

from __future__ import annotations

from types import SimpleNamespace

from serving.utils.request_ip import get_client_ip, get_client_ip_info


def _request(headers: dict[str, str], peer_ip: str = "10.0.0.2"):
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=peer_ip))


def test_client_ip_ignores_forwarded_headers_by_default(monkeypatch):
    """Ignore spoofable forwarding headers unless proxy trust is explicitly enabled."""
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)

    request = _request({"x-forwarded-for": "203.0.113.9"}, peer_ip="172.19.0.8")

    info = get_client_ip_info(request)
    assert get_client_ip(request) == "172.19.0.8"
    assert info.client_ip == "172.19.0.8"
    assert info.peer_ip == "172.19.0.8"
    assert info.source == "socket"
    assert info.x_forwarded_for == "203.0.113.9"


def test_client_ip_uses_first_forwarded_for_when_trusted(monkeypatch):
    """Use the leftmost X-Forwarded-For address from trusted proxies."""
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")

    request = _request(
        {"x-forwarded-for": "203.0.113.9, 198.51.100.4", "x-real-ip": "198.51.100.7"},
        peer_ip="172.19.0.8",
    )

    info = get_client_ip_info(request)
    assert get_client_ip(request) == "203.0.113.9"
    assert info.client_ip == "203.0.113.9"
    assert info.peer_ip == "172.19.0.8"
    assert info.source == "x-forwarded-for"
    assert info.x_forwarded_for == "203.0.113.9, 198.51.100.4"
    assert info.x_real_ip == "198.51.100.7"


def test_client_ip_falls_back_to_x_real_ip_when_trusted(monkeypatch):
    """Use X-Real-IP when trusted and X-Forwarded-For is absent."""
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")

    request = _request({"x-real-ip": "198.51.100.7"}, peer_ip="172.19.0.8")

    info = get_client_ip_info(request)
    assert info.client_ip == "198.51.100.7"
    assert info.peer_ip == "172.19.0.8"
    assert info.source == "x-real-ip"
