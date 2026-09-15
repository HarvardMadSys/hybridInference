from __future__ import annotations

from typing import Any

import pytest

from serving.utils import context as req_ctx
from serving.utils.request_ip import ClientIpInfo, derive_affinity_key


def _derive_affinity_key(
    auth_key_hash: str | None,
    client_ip: str,
    *,
    grant_id: str | None = None,
) -> str | None:
    """Thin wrapper over the shared helper every request surface derives its key with."""
    # For backward compat in tests, treat string IP as resolved (unless "unknown")
    ip_info = ClientIpInfo(
        client_ip=client_ip,
        peer_ip="unknown",
        source="test",
        trusted_proxy_headers=False,
        resolved=(client_ip != "unknown"),
    )
    return derive_affinity_key(auth_key_hash, ip_info, grant_id=grant_id)


@pytest.mark.unit
def test_authenticated_uses_auth_key_hash():
    assert _derive_affinity_key("abc123", "1.2.3.4") == "abc123"


@pytest.mark.unit
def test_grant_token_keys_on_the_grant_not_the_sandbox_ip():
    """An inference grant carries no key hash; without this every sandbox behind
    one NAT or relay address would share a binding."""
    assert _derive_affinity_key(None, "1.2.3.4", grant_id="grn_7") == "grant:grn_7"


@pytest.mark.unit
def test_grant_id_never_outranks_a_presented_key():
    assert _derive_affinity_key("abc123", "1.2.3.4", grant_id="grn_7") == "abc123"


@pytest.mark.unit
def test_two_sandboxes_on_one_ip_get_distinct_keys():
    keys = {_derive_affinity_key(None, "1.2.3.4", grant_id=g) for g in ("grn_a", "grn_b")}
    assert len(keys) == 2


@pytest.mark.unit
def test_anonymous_uses_ip_prefix():
    assert _derive_affinity_key(None, "1.2.3.4") == "ip:1.2.3.4"


@pytest.mark.unit
def test_anonymous_unknown_ip_falls_back():
    """Unknown client IP returns None (no shared proxy IP for affinity)."""
    ip_info = ClientIpInfo(
        client_ip="unknown",
        peer_ip="172.19.0.1",
        source="unknown",
        trusted_proxy_headers=False,
        resolved=False,
    )
    assert derive_affinity_key(None, ip_info) is None


@pytest.mark.unit
def test_anonymous_ipv6_uses_slash_64_prefix():
    """IPv6 anonymous clients key on the /64 rather than the exact address."""
    assert _derive_affinity_key(None, "2001:db8:abcd:1234::5") == "ip:2001:db8:abcd:1234::/64"


@pytest.mark.unit
def test_anonymous_ipv6_rotation_keeps_one_affinity_key():
    """Rotating privacy addresses within a /64 must not re-shard sticky routing."""
    keys = {
        _derive_affinity_key(None, ip)
        for ip in (
            "2001:db8:abcd:1234::1",
            "2001:db8:abcd:1234:9c2b:1f4e:aa01:7d3f",
        )
    }
    assert len(keys) == 1


@pytest.mark.unit
def test_handler_propagates_affinity_key_to_context_authenticated():
    """The handler writes ``affinity_key`` into ``req_ctx`` derived from auth_key_hash."""
    user_ctx: dict[str, Any] = {"user_id": "u-1", "auth_key_hash": "deadbeef"}
    ip_info = ClientIpInfo(
        client_ip="1.2.3.4",
        peer_ip="unknown",
        source="test",
        trusted_proxy_headers=False,
        resolved=True,
    )

    req_ctx.set({})
    affinity_key = derive_affinity_key(user_ctx.get("auth_key_hash"), ip_info)
    req_ctx.update(
        {
            "auth_key_hash": user_ctx.get("auth_key_hash") or "_anon",
            "affinity_key": affinity_key,
        }
    )

    assert req_ctx.get().get("affinity_key") == "deadbeef"


@pytest.mark.unit
def test_handler_propagates_affinity_key_to_context_anonymous():
    """Anonymous users get an ip:-prefixed affinity_key in ``req_ctx``."""
    user_ctx: dict[str, Any] = {"user_id": "u-2"}
    ip_info = ClientIpInfo(
        client_ip="10.0.0.1",
        peer_ip="unknown",
        source="test",
        trusted_proxy_headers=False,
        resolved=True,
    )

    from serving.utils.request_ip import derive_affinity_key

    req_ctx.set({})
    affinity_key = derive_affinity_key(user_ctx.get("auth_key_hash"), ip_info)
    req_ctx.update(
        {
            "auth_key_hash": user_ctx.get("auth_key_hash") or "_anon",
            "affinity_key": affinity_key,
        }
    )

    assert req_ctx.get().get("affinity_key") == "ip:10.0.0.1"


@pytest.mark.unit
def test_affinity_key_unresolved_returns_none():
    """When client provenance is unresolved, affinity key is None (non-sticky)."""
    ip_info = ClientIpInfo(
        client_ip="unknown",
        peer_ip="172.19.0.1",
        source="unknown",
        trusted_proxy_headers=False,
        resolved=False,
    )
    assert derive_affinity_key(None, ip_info) is None


@pytest.mark.unit
def test_affinity_key_unresolved_with_auth_uses_auth():
    """Auth key hash is preferred even when client IP is unresolved."""
    ip_info = ClientIpInfo(
        client_ip="unknown",
        peer_ip="172.19.0.1",
        source="unknown",
        trusted_proxy_headers=False,
        resolved=False,
    )
    assert derive_affinity_key("mykey", ip_info) == "mykey"
