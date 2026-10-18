"""Adversarial checks for unresolved API-key failure accounting."""

from __future__ import annotations

import asyncio

import pytest

from serving.config.settings import settings
from serving.utils import auth_failure_blocklist as blocklist
from serving.utils.request_ip import ClientIpInfo


def _unresolved(peer_ip: str = "10.0.0.2") -> ClientIpInfo:
    return ClientIpInfo(
        client_ip="unknown",
        peer_ip=peer_ip,
        source="unknown",
        trusted_proxy_headers=False,
        resolved=False,
    )


def _resolved(ip: str) -> ClientIpInfo:
    return ClientIpInfo(
        client_ip=ip,
        peer_ip=ip,
        source="socket",
        trusted_proxy_headers=False,
        resolved=True,
    )


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(settings, "auth_failure_block_enabled", True)
    monkeypatch.setattr(settings, "unresolved_auth_failure_block_threshold", 3)
    monkeypatch.setattr(settings, "unresolved_auth_failure_block_window_sec", 60)
    monkeypatch.setattr(settings, "unresolved_auth_failure_block_duration_sec", 30)
    blocklist.reset_auth_failure_block_state()
    yield
    blocklist.reset_auth_failure_block_state()


@pytest.mark.asyncio
async def test_unresolved_flood_has_bounded_accounting_and_shared_block():
    unresolved = _unresolved()

    assert await blocklist.record_auth_failure(unresolved) is False
    assert await blocklist.record_auth_failure(unresolved) is False
    assert len(blocklist._unresolved_failures) == 2
    assert await blocklist.record_auth_failure(unresolved) is True
    assert not blocklist._unresolved_failures

    # Every unresolved caller shares one short circuit breaker, independent of
    # the proxy peer, while a caller with resolved provenance stays isolated.
    assert await blocklist.is_ip_blocked(_unresolved("10.0.0.3")) == (True, 30)
    assert await blocklist.is_ip_blocked(_resolved("8.8.8.8")) == (False, 0)
    for _ in range(1_000):
        assert await blocklist.record_auth_failure(unresolved) is False
    assert await blocklist.is_ip_blocked(unresolved) == (True, 30)
    assert len(blocklist._unresolved_failures) == 0

    blocks = await blocklist.list_active_blocks()
    assert [item.ip_bucket for item in blocks] == ["unresolved-global"]


@pytest.mark.asyncio
async def test_concurrent_unresolved_failures_trigger_one_fixed_block(monkeypatch):
    now = [10_000.0]
    monkeypatch.setattr(blocklist, "_now", lambda: now[0])
    settings.unresolved_auth_failure_block_threshold = 20
    settings.unresolved_auth_failure_block_duration_sec = 40
    unresolved = _unresolved()

    transitions = await asyncio.gather(
        *(blocklist.record_auth_failure(unresolved) for _ in range(200))
    )
    assert sum(transitions) == 1
    assert await blocklist.is_ip_blocked(unresolved) == (True, 40)

    now[0] += 39
    for _ in range(100):
        assert await blocklist.record_auth_failure(unresolved) is False
    assert await blocklist.is_ip_blocked(unresolved) == (True, 1)

    now[0] += 2
    assert await blocklist.is_ip_blocked(unresolved) == (False, 0)
    assert await blocklist.record_auth_failure(unresolved) is False
    assert len(blocklist._unresolved_failures) == 1


@pytest.mark.asyncio
async def test_unresolved_failure_window_expires_and_can_recover(monkeypatch):
    now = [20_000.0]
    monkeypatch.setattr(blocklist, "_now", lambda: now[0])
    unresolved = _unresolved()

    assert await blocklist.record_auth_failure(unresolved) is False
    assert await blocklist.record_auth_failure(unresolved) is False
    now[0] += 61

    assert await blocklist.record_auth_failure(unresolved) is False
    assert len(blocklist._unresolved_failures) == 1
    assert await blocklist.is_ip_blocked(unresolved) == (False, 0)
