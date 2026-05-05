from __future__ import annotations

import pytest

from serving.utils import context as req_ctx  # noqa: F401


def _derive_affinity_key(auth_key_hash: str | None, client_ip: str) -> str:
    """Mirror of the production helper. If completions.py exports one, import it instead."""
    from serving.servers.routers.completions import derive_affinity_key

    return derive_affinity_key(auth_key_hash, client_ip)


@pytest.mark.unit
def test_authenticated_uses_auth_key_hash():
    assert _derive_affinity_key("abc123", "1.2.3.4") == "abc123"


@pytest.mark.unit
def test_anonymous_uses_ip_prefix():
    assert _derive_affinity_key(None, "1.2.3.4") == "ip:1.2.3.4"


@pytest.mark.unit
def test_anonymous_unknown_ip_falls_back():
    # When IP is "unknown" (the get_client_ip sentinel), still produce a stable key.
    assert _derive_affinity_key(None, "unknown") == "ip:unknown"
