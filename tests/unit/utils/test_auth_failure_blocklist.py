"""Auto-block a source IP after repeated auth failures.

Covers the in-memory blocklist logic (threshold, counting window, block
expiry, IPv6 /64 bucketing, disable switch) and its enforcement at the
API-key auth layer (``_authenticate_by_api_key`` returns 429 for a blocked IP).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from serving.config.settings import Settings, settings
from serving.utils import auth_failure_blocklist as afb
from serving.utils.auth_failure_blocklist import (
    is_ip_blocked,
    record_auth_failure,
    reset_auth_failure_block_state,
)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_auth_failure_block_state()
    yield
    reset_auth_failure_block_state()


@pytest.fixture
def clock(monkeypatch):
    """A controllable wall clock for the blocklist module."""
    state = {"t": 1_000_000.0}
    monkeypatch.setattr(afb, "_now", lambda: state["t"])
    return state


@pytest.fixture
def small_limits(monkeypatch):
    """Tiny threshold/window so tests need a handful of calls, not 200."""
    monkeypatch.setattr(settings, "auth_failure_block_enabled", True)
    monkeypatch.setattr(settings, "auth_failure_block_threshold", 3)
    monkeypatch.setattr(settings, "auth_failure_block_window_sec", 100)
    monkeypatch.setattr(settings, "auth_failure_block_duration_sec", 1000)


def test_default_config_encodes_the_spec():
    """Defaults are '200 auth failures in a day → blocked for a day'."""
    fields = Settings.model_fields
    assert fields["auth_failure_block_enabled"].default is True
    assert fields["auth_failure_block_threshold"].default == 200
    assert fields["auth_failure_block_window_sec"].default == 86400
    assert fields["auth_failure_block_duration_sec"].default == 86400
    # No source is exempt unless an operator lists it.
    assert fields["auth_failure_block_exempt_ips"].default == ""


@pytest.mark.asyncio
async def test_blocks_on_the_threshold_failure(small_limits, clock):
    ip = "203.0.113.7"
    # The first threshold-1 failures accrue without blocking.
    assert await record_auth_failure(ip) is False
    assert await record_auth_failure(ip) is False
    assert await is_ip_blocked(ip) == (False, 0)

    # The threshold-th failure trips the block; the return marks the transition.
    assert await record_auth_failure(ip) is True
    blocked, retry_after = await is_ip_blocked(ip)
    assert blocked is True
    assert retry_after == settings.auth_failure_block_duration_sec


@pytest.mark.asyncio
async def test_record_signals_the_transition_only_once(small_limits, clock):
    ip = "203.0.113.8"
    results = [await record_auth_failure(ip) for _ in range(6)]
    # Exactly the 3rd call (the threshold) reports True; before and after
    # (already blocked) are all False.
    assert results == [False, False, True, False, False, False]


@pytest.mark.asyncio
async def test_below_threshold_never_blocks(small_limits, clock):
    ip = "203.0.113.9"
    await record_auth_failure(ip)
    await record_auth_failure(ip)
    assert await is_ip_blocked(ip) == (False, 0)


@pytest.mark.asyncio
async def test_ipv6_rotation_within_a_64_shares_one_bucket(small_limits, clock):
    """RFC 4941 rotation inside a delegated /64 cannot dodge the block."""
    prefix = "2001:db8:abcd:1234::"
    await record_auth_failure(prefix + "1")
    await record_auth_failure(prefix + "2")
    assert await record_auth_failure(prefix + "3") is True

    # Any other address in the same /64 is now blocked.
    blocked, _ = await is_ip_blocked(prefix + "dead")
    assert blocked is True

    # A different /64 is a separate bucket — no collateral block.
    assert await is_ip_blocked("2001:db8:abcd:9999::1") == (False, 0)


@pytest.mark.asyncio
async def test_ipv4_addresses_bucket_individually(small_limits, clock):
    for _ in range(3):
        await record_auth_failure("203.0.113.20")
    assert (await is_ip_blocked("203.0.113.20"))[0] is True
    # The neighbouring address is untouched.
    assert await is_ip_blocked("203.0.113.21") == (False, 0)


@pytest.mark.asyncio
async def test_failures_outside_the_window_are_not_counted(small_limits, clock):
    ip = "203.0.113.30"
    await record_auth_failure(ip)
    await record_auth_failure(ip)

    # Age the two prior failures out of the counting window.
    clock["t"] += settings.auth_failure_block_window_sec + 1

    # Fresh failures start from zero, so it takes a full threshold again.
    assert await record_auth_failure(ip) is False
    assert await record_auth_failure(ip) is False
    assert await is_ip_blocked(ip) == (False, 0)
    assert await record_auth_failure(ip) is True


@pytest.mark.asyncio
async def test_block_lapses_after_its_duration(small_limits, clock):
    ip = "203.0.113.40"
    for _ in range(3):
        await record_auth_failure(ip)
    assert (await is_ip_blocked(ip))[0] is True

    # One second before expiry: still blocked.
    clock["t"] += settings.auth_failure_block_duration_sec - 1
    assert (await is_ip_blocked(ip))[0] is True

    # Past expiry: cleared lazily on read.
    clock["t"] += 2
    assert await is_ip_blocked(ip) == (False, 0)


@pytest.mark.asyncio
async def test_disabled_is_a_noop(monkeypatch, clock):
    monkeypatch.setattr(settings, "auth_failure_block_enabled", False)
    monkeypatch.setattr(settings, "auth_failure_block_threshold", 1)
    ip = "203.0.113.50"
    # Even at threshold 1, a disabled feature never blocks.
    assert await record_auth_failure(ip) is False
    assert await is_ip_blocked(ip) == (False, 0)


@pytest.fixture
def exempt(monkeypatch):
    """Set the exemption list for a test."""

    def _set(value: str) -> None:
        monkeypatch.setattr(settings, "auth_failure_block_exempt_ips", value)

    return _set


@pytest.mark.asyncio
async def test_exempt_ip_never_blocks(small_limits, clock, exempt):
    exempt("203.0.113.60")
    ip = "203.0.113.60"
    # Far past the threshold: failures are not even counted, so no transition.
    for _ in range(10):
        assert await record_auth_failure(ip) is False
    assert await is_ip_blocked(ip) == (False, 0)


@pytest.mark.asyncio
async def test_exempt_cidr_covers_the_range_but_nothing_else(small_limits, clock, exempt):
    exempt("203.0.113.0/24")
    for _ in range(5):
        assert await record_auth_failure("203.0.113.61") is False
    assert await is_ip_blocked("203.0.113.61") == (False, 0)

    # A source outside the exempted range still blocks at the threshold.
    outsider = "198.51.100.9"
    await record_auth_failure(outsider)
    await record_auth_failure(outsider)
    assert await record_auth_failure(outsider) is True
    assert (await is_ip_blocked(outsider))[0] is True


@pytest.mark.asyncio
async def test_exemption_overrides_an_existing_block(small_limits, clock, exempt):
    """Adding an exemption unblocks the source on the next read."""
    ip = "203.0.113.62"
    for _ in range(3):
        await record_auth_failure(ip)
    assert (await is_ip_blocked(ip))[0] is True

    exempt(ip)
    assert await is_ip_blocked(ip) == (False, 0)


@pytest.mark.asyncio
async def test_exempt_host_survives_its_blocked_ipv6_bucket(small_limits, clock, exempt):
    """A /128 exemption outranks a block on the surrounding /64 bucket."""
    prefix = "2001:db8:abcd:1234::"
    exempt(prefix + "5")
    # Non-exempt rotation within the /64 blocks the shared bucket...
    await record_auth_failure(prefix + "1")
    await record_auth_failure(prefix + "2")
    assert await record_auth_failure(prefix + "3") is True
    assert (await is_ip_blocked(prefix + "dead"))[0] is True
    # ...but the exempted address inside it stays reachable.
    assert await is_ip_blocked(prefix + "5") == (False, 0)


@pytest.mark.asyncio
async def test_ipv4_mapped_literal_matches_an_ipv4_entry(small_limits, clock, exempt):
    """A dual-stack listener's ::ffff: form is exempt via its embedded IPv4."""
    exempt("203.0.113.70")
    mapped = "::ffff:203.0.113.70"
    for _ in range(5):
        assert await record_auth_failure(mapped) is False
    assert await is_ip_blocked(mapped) == (False, 0)


@pytest.mark.asyncio
async def test_invalid_exempt_entries_are_skipped(small_limits, clock, exempt):
    """A malformed entry is ignored; the valid ones still apply."""
    exempt("not-an-ip, ,203.0.113.80")
    for _ in range(5):
        assert await record_auth_failure("203.0.113.80") is False
    assert await is_ip_blocked("203.0.113.80") == (False, 0)

    # The malformed entry exempts nothing: other sources still block.
    other = "198.51.100.10"
    await record_auth_failure(other)
    await record_auth_failure(other)
    assert await record_auth_failure(other) is True


def _make_request(client_ip: str) -> Request:
    """A minimal ASGI request with a direct socket peer of *client_ip*."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [],
        "client": (client_ip, 40000),
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_authenticate_rejects_a_blocked_ip_with_429(monkeypatch, clock):
    """A blocked source is refused before any key extraction or DB lookup."""
    from serving.servers import auth as auth_mod

    monkeypatch.setattr(settings, "auth_failure_block_enabled", True)
    monkeypatch.setattr(settings, "auth_failure_block_threshold", 2)
    monkeypatch.setattr(settings, "auth_failure_block_window_sec", 100)
    monkeypatch.setattr(settings, "auth_failure_block_duration_sec", 1000)

    async def _noop_log_rejection(**_kwargs):
        return None

    monkeypatch.setattr(auth_mod, "log_rejection", _noop_log_rejection)

    ip = "198.51.100.5"
    await record_auth_failure(ip)
    await record_auth_failure(ip)  # second failure trips the block

    request = _make_request(ip)
    with pytest.raises(HTTPException) as excinfo:
        # op_store is a bare sentinel: nothing touches it. The block decision
        # needs no lookup, and the rejection-log enrichment that *would* resolve
        # the caller's identity is inert here — this scope carries no app, so
        # there is no log store to write the enriched row to. Enrichment with
        # the log on is covered in tests/servers/test_auth_rejection_log.py.
        await auth_mod._authenticate_by_api_key(request, None, None, object())

    assert excinfo.value.status_code == 429
    assert excinfo.value.headers["Retry-After"] == str(settings.auth_failure_block_duration_sec)
