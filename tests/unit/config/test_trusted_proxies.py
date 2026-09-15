"""Tests for trusted_proxies and trusted_cloudflare_networks configuration."""

from __future__ import annotations

import ipaddress

import pytest

from serving.config.settings import Settings


def test_trusted_proxies_valid_cidrs_from_string():
    """Comma-separated string is parsed into networks."""
    s = Settings(trusted_proxies="172.16.0.0/12,10.0.0.0/8,fd00::/8")
    assert len(s.trusted_proxies_parsed) == 3


def test_trusted_proxies_empty_by_default():
    s = Settings()
    assert s.trusted_proxies == ""
    assert s.trusted_proxies_parsed == ()
    assert s.trusted_direct_client_parsed == ()
    assert s.trusted_cloudflare_networks == ""
    assert s.trusted_cloudflare_parsed == ()
    assert s.trust_proxy_headers is False
    assert s.trust_cloudflare_headers is False
    assert s.trust_x_real_ip is False


def test_trusted_direct_client_networks_parse():
    s = Settings(trusted_direct_client_networks="10.42.0.0/16,fd00:42::/64")
    assert s.trusted_direct_client_parsed == (
        ipaddress.ip_network("10.42.0.0/16"),
        ipaddress.ip_network("fd00:42::/64"),
    )


def test_trusted_direct_client_networks_invalid_cidr_fails():
    with pytest.raises(ValueError, match="trusted_direct_client_networks entry"):
        Settings(trusted_direct_client_networks="not-a-cidr")


def test_forwarding_trust_flags_are_typed_settings():
    s = Settings(
        trust_proxy_headers=True,
        trust_cloudflare_headers=True,
        trusted_cloudflare_networks="172.16.0.0/12",
        trust_x_real_ip=True,
    )
    assert s.trust_proxy_headers is True
    assert s.trust_cloudflare_headers is True
    assert s.trust_x_real_ip is True


def test_cloudflare_trust_requires_master_switch():
    with pytest.raises(ValueError, match="TRUST_CLOUDFLARE_HEADERS requires TRUST_PROXY_HEADERS"):
        Settings(
            trust_cloudflare_headers=True,
            trusted_cloudflare_networks="172.16.0.0/12",
        )


def test_cloudflare_trust_requires_authorized_networks():
    with pytest.raises(
        ValueError, match="TRUST_CLOUDFLARE_HEADERS requires TRUSTED_CLOUDFLARE_NETWORKS"
    ):
        Settings(trust_proxy_headers=True, trust_cloudflare_headers=True)


def test_x_real_ip_trust_requires_master_switch():
    with pytest.raises(ValueError, match="TRUST_X_REAL_IP requires TRUST_PROXY_HEADERS"):
        Settings(trust_x_real_ip=True)


def test_trusted_proxies_invalid_cidr_fails():
    with pytest.raises(ValueError, match="trusted_proxies entry"):
        Settings(trusted_proxies="not-a-cidr")


def test_trusted_proxies_unaligned_network_fails():
    """A host address must not silently widen an authorized proxy range."""
    with pytest.raises(ValueError, match="trusted_proxies entry"):
        Settings(trusted_proxies="172.19.0.2/24")


def test_trusted_cloudflare_invalid_cidr_fails():
    with pytest.raises(ValueError, match="trusted_cloudflare_networks entry"):
        Settings(trusted_cloudflare_networks="not-a-cidr")


def test_trusted_cloudflare_unaligned_network_fails():
    """A host address must not silently widen Cloudflare authorization."""
    with pytest.raises(ValueError, match="trusted_cloudflare_networks entry"):
        Settings(trusted_cloudflare_networks="172.19.0.2/24")


def test_trusted_proxies_from_env_var(monkeypatch):
    """TRUSTED_PROXIES env var works with comma-separated values."""
    monkeypatch.setenv("TRUSTED_PROXIES", "172.16.0.0/12, 10.0.0.0/8")
    s = Settings()
    assert len(s.trusted_proxies_parsed) == 2


def test_trusted_proxies_empty_env_var(monkeypatch):
    """Empty TRUSTED_PROXIES env var produces no trusted networks."""
    monkeypatch.setenv("TRUSTED_PROXIES", "")
    s = Settings()
    assert s.trusted_proxies_parsed == ()


def test_trusted_proxies_env_var_with_whitespace(monkeypatch):
    """Whitespace is handled in env var parsing."""
    monkeypatch.setenv("TRUSTED_PROXIES", " 172.16.0.0/12 , 10.0.0.0/8 ")
    s = Settings()
    assert len(s.trusted_proxies_parsed) == 2


def test_trusted_cloudflare_from_env_var(monkeypatch):
    """TRUSTED_CLOUDFLARE_NETWORKS env var works."""
    monkeypatch.setenv("TRUSTED_CLOUDFLARE_NETWORKS", "172.16.0.0/12")
    s = Settings()
    assert len(s.trusted_cloudflare_parsed) == 1


def test_both_settings_from_env(monkeypatch):
    """Both trusted_proxies and trusted_cloudflare_networks from env."""
    monkeypatch.setenv("TRUSTED_PROXIES", "172.16.0.0/12")
    monkeypatch.setenv("TRUSTED_CLOUDFLARE_NETWORKS", "10.0.0.0/8")
    s = Settings()
    assert len(s.trusted_proxies_parsed) == 1
    assert len(s.trusted_cloudflare_parsed) == 1
