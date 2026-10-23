"""Adversarial checks for admitted-login and refusal accounting."""

from __future__ import annotations

import asyncio

import pytest

from serving.config.settings import settings
from serving.utils import login_rate_limit as limiter
from serving.utils.request_ip import ClientIpInfo

ATTACKER = "203.0.113.7"
OWNER = "198.51.100.9"


def _unresolved_provenance() -> ClientIpInfo:
    return ClientIpInfo(
        client_ip="unknown",
        peer_ip="10.0.0.2",
        source="unknown",
        trusted_proxy_headers=False,
        resolved=False,
    )


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(settings, "login_rate_limit_per_15min", 2)
    monkeypatch.setattr(settings, "login_account_rate_limit_per_15min", 50)
    monkeypatch.setattr(settings, "login_rate_limit_per_hour_per_ip", 10)
    monkeypatch.setattr(limiter, "_MAX_ACTIVE_BUCKETS", 20_000)
    limiter.reset_login_rate_limit_state()
    yield
    limiter.reset_login_rate_limit_state()


@pytest.mark.asyncio
async def test_sustained_address_flood_does_not_grow_admission_buckets(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(limiter, "_now", lambda: now[0])

    for _ in range(2):
        assert await limiter.check_and_record_login("admin", ATTACKER) == (True, None)

    lengths = (
        len(limiter._account_address_attempts[("admin", ATTACKER)]),
        len(limiter._account_attempts["admin"]),
        len(limiter._ip_attempts[ATTACKER]),
    )
    first_refusal = await limiter.check_and_record_login("admin", ATTACKER)
    assert first_refusal == (False, "email")
    pressure_started_at = limiter._ip_refusal_pressure[ATTACKER].started_at

    for _ in range(500):
        allowed, _ = await limiter.check_and_record_login("admin", ATTACKER)
        assert not allowed

    assert lengths == (
        len(limiter._account_address_attempts[("admin", ATTACKER)]),
        len(limiter._account_attempts["admin"]),
        len(limiter._ip_attempts[ATTACKER]),
    )
    # The separate abuse signal is a bounded scalar and has a fixed expiry.
    pressure = limiter._ip_refusal_pressure[ATTACKER]
    assert pressure.count == settings.login_rate_limit_per_hour_per_ip - 2
    assert pressure.started_at == pressure_started_at == now[0]


@pytest.mark.asyncio
async def test_account_limit_refusals_eventually_trip_source_ip_pressure(monkeypatch):
    now = [1_500.0]
    monkeypatch.setattr(limiter, "_now", lambda: now[0])
    monkeypatch.setattr(settings, "login_account_rate_limit_per_15min", 6)
    monkeypatch.setattr(settings, "login_rate_limit_per_hour_per_ip", 20)

    for suffix in range(3):
        ip = f"203.0.113.{suffix + 1}"
        for _ in range(2):
            assert await limiter.check_and_record_login("admin", ip) == (True, None)

    new_source = "198.51.100.44"
    for _ in range(20):
        assert await limiter.check_and_record_login("admin", new_source) == (
            False,
            "account",
        )
    assert len(limiter._account_attempts["admin"]) == 6

    # Refusals do not enter the admitted-login window, but repeated polling
    # reaches the separate hourly pressure threshold for this source IP.
    assert new_source not in limiter._ip_attempts
    assert limiter._ip_refusal_pressure[new_source].count == 20
    assert await limiter.check_and_record_login("admin", new_source) == (False, "ip")
    assert await limiter.check_and_record_login("other@example.com", new_source) == (False, "ip")

    now[0] += 15 * 60 + 1
    # The account window expires, but the IP's refusal pressure keeps its
    # fixed hourly expiry. A different source can use the recovered account.
    assert await limiter.check_and_record_login("admin", new_source) == (False, "ip")
    assert await limiter.check_and_record_login("admin", OWNER) == (True, None)

    now[0] += 45 * 60
    assert await limiter.check_and_record_login("admin", new_source) == (True, None)


@pytest.mark.asyncio
async def test_address_refusal_escalates_to_ip_and_exposes_shared_ip_collateral(monkeypatch):
    monkeypatch.setattr(settings, "login_rate_limit_per_15min", 1)
    monkeypatch.setattr(settings, "login_rate_limit_per_hour_per_ip", 3)

    assert await limiter.check_and_record_login("admin", ATTACKER) == (True, None)
    assert await limiter.check_and_record_login("admin", ATTACKER) == (False, "email")
    # The second refusal reaches the IP threshold but is still reported at the
    # account/address layer; the next request sees the IP-wide refusal.
    assert await limiter.check_and_record_login("admin", ATTACKER) == (False, "email")
    assert await limiter.check_and_record_login("other@example.com", ATTACKER) == (
        False,
        "ip",
    )
    assert await limiter.check_and_record_login("admin", OWNER) == (True, None)

    # A recognized browser retains the existing independent recovery path.
    assert await limiter.check_and_record_login("admin", ATTACKER, device="trusted-device") == (
        True,
        None,
    )


@pytest.mark.asyncio
async def test_refusal_pressure_expires_from_first_refusal_and_login_recovers(monkeypatch):
    now = [2_000.0]
    monkeypatch.setattr(limiter, "_now", lambda: now[0])
    monkeypatch.setattr(settings, "login_rate_limit_per_15min", 2)
    monkeypatch.setattr(settings, "login_rate_limit_per_hour_per_ip", 4)

    for _ in range(2):
        assert await limiter.check_and_record_login("admin", ATTACKER) == (True, None)
    assert await limiter.check_and_record_login("admin", ATTACKER) == (False, "email")
    assert await limiter.check_and_record_login("admin", ATTACKER) == (False, "email")
    assert await limiter.check_and_record_login("admin", ATTACKER) == (False, "ip")
    started_at = limiter._ip_refusal_pressure[ATTACKER].started_at

    now[0] += 3_599
    for _ in range(100):
        assert await limiter.check_and_record_login("admin", ATTACKER) == (False, "ip")
    assert limiter._ip_refusal_pressure[ATTACKER].started_at == started_at

    now[0] += 2
    assert await limiter.check_and_record_login("admin", ATTACKER) == (True, None)
    assert ATTACKER not in limiter._ip_refusal_pressure


@pytest.mark.asyncio
async def test_login_bucket_key_cardinality_is_bounded_and_new_keys_still_admit(
    monkeypatch,
):
    # A full attacker-populated map must not fail closed: map saturation is
    # not a global login denial of service. New identities use bounded
    # overflow accounting, so a live identity is never evicted to make room.
    now = [3_000.0]
    monkeypatch.setattr(limiter, "_now", lambda: now[0])
    monkeypatch.setattr(limiter, "_MAX_ACTIVE_BUCKETS", 2)

    assert await limiter.check_and_record_login("first", "198.51.100.1") == (True, None)
    assert await limiter.check_and_record_login("second", "198.51.100.2") == (True, None)
    # Third identity: the maps use bounded overflow instead of refusing.
    assert await limiter.check_and_record_login("third", "198.51.100.3") == (True, None)
    assert len(limiter._account_address_attempts) <= 2
    assert len(limiter._account_attempts) <= 2
    assert len(limiter._ip_attempts) <= 2

    # Expired keys are swept before eviction is needed, and recovery works.
    now[0] += 3_601
    assert await limiter.check_and_record_login("fourth", "198.51.100.4") == (True, None)
    assert len(limiter._account_address_attempts) <= 2

    # After its old window expires, "first" is admitted with a fresh window.
    now[0] += 3_601
    assert await limiter.check_and_record_login("first", "198.51.100.1") == (True, None)


@pytest.mark.asyncio
async def test_primary_map_overflow_collisions_are_conservative_and_bounded(monkeypatch):
    """Distinct saturated keys sharing a hash slot remain accounted."""
    monkeypatch.setattr(settings, "login_rate_limit_per_15min", 1)
    monkeypatch.setattr(limiter, "_MAX_ACTIVE_BUCKETS", 1)
    monkeypatch.setattr(limiter, "_OVERFLOW_BUCKET_COUNT", 1)

    assert await limiter.check_and_record_login("resident", OWNER) == (True, None)
    assert await limiter.check_and_record_login("overflow-a", ATTACKER) == (True, None)

    # Every overflow identity hashes to the same bounded slot. It is
    # conservatively rate limited after the first identity used that slot.
    assert await limiter.check_and_record_login("overflow-b", "192.0.2.18") == (
        False,
        "email",
    )
    assert len(limiter._account_address_attempts) == 1
    assert len(limiter._account_attempts) == 1
    assert len(limiter._ip_attempts) == 1
    assert len(limiter._overflow_attempts) <= 3


@pytest.mark.asyncio
async def test_refusal_pressure_map_is_bounded(monkeypatch):
    monkeypatch.setattr(settings, "login_rate_limit_per_15min", 1)
    monkeypatch.setattr(settings, "login_rate_limit_per_hour_per_ip", 10)

    ips = ["198.51.100.1", "198.51.100.2", "198.51.100.3"]
    for index, ip in enumerate(ips):
        assert await limiter.check_and_record_login(f"user{index}", ip) == (True, None)

    monkeypatch.setattr(limiter, "_MAX_ACTIVE_BUCKETS", 2)

    # These attempts are against existing account/address keys, but begin a
    # separate refusal record for each source IP. The third IP uses bounded
    # refusal overflow accounting instead of making map saturation a global
    # IP refusal.
    assert await limiter.check_and_record_login("user0", ips[0]) == (False, "email")
    assert await limiter.check_and_record_login("user1", ips[1]) == (False, "email")
    assert await limiter.check_and_record_login("user2", ips[2]) == (False, "email")
    assert len(limiter._ip_refusal_pressure) <= 2
    assert len(limiter._ip_refusal_overflow) <= 1


@pytest.mark.asyncio
async def test_concurrent_requests_cannot_exceed_admission_limit(monkeypatch):
    monkeypatch.setattr(settings, "login_rate_limit_per_15min", 5)
    monkeypatch.setattr(settings, "login_account_rate_limit_per_15min", 100)
    monkeypatch.setattr(settings, "login_rate_limit_per_hour_per_ip", 1_000)

    results = await asyncio.gather(
        *(limiter.check_and_record_login("admin", ATTACKER) for _ in range(100))
    )

    assert sum(allowed for allowed, _ in results) == 5
    assert sum(reason == "email" for _, reason in results) == 95
    assert len(limiter._account_address_attempts[("admin", ATTACKER)]) == 5
    assert len(limiter._account_attempts["admin"]) == 5
    assert len(limiter._ip_attempts[ATTACKER]) == 5
    assert limiter._ip_refusal_pressure[ATTACKER].count == 95


@pytest.mark.asyncio
async def test_unresolved_login_budget_is_global_bounded_and_recovers(monkeypatch):
    now = [4_000.0]
    monkeypatch.setattr(limiter, "_now", lambda: now[0])
    monkeypatch.setattr(settings, "unresolved_login_rate_limit_per_hour", 2)

    assert await limiter.check_and_record_login("first", _unresolved_provenance()) == (
        True,
        None,
    )
    assert await limiter.check_and_record_login("second", _unresolved_provenance()) == (
        True,
        None,
    )
    for _ in range(100):
        assert await limiter.check_and_record_login("third", _unresolved_provenance()) == (
            False,
            "unresolved",
        )

    assert len(limiter._unresolved_attempts) == 2
    assert not limiter._ip_attempts
    # Unresolved attempts share the "unresolved" account/address bucket; they
    # are not assigned a synthetic per-client address.
    assert ("third", "unresolved") not in limiter._account_address_attempts

    # A resolved caller has its own address and can still use the resolved
    # protections; unresolved traffic is not assigned the proxy's address.
    assert await limiter.check_and_record_login("third", OWNER) == (True, None)

    now[0] += 3_601
    assert await limiter.check_and_record_login("third", _unresolved_provenance()) == (
        True,
        None,
    )
    assert len(limiter._unresolved_attempts) == 1


@pytest.mark.asyncio
async def test_unresolved_account_and_recognized_device_protections_remain(monkeypatch):
    monkeypatch.setattr(settings, "login_account_rate_limit_per_15min", 1)
    monkeypatch.setattr(settings, "unresolved_login_rate_limit_per_hour", 10)
    unknown = _unresolved_provenance()

    assert await limiter.check_and_record_login("owner", unknown) == (True, None)
    assert await limiter.check_and_record_login("owner", unknown) == (False, "account")

    # A recognized-device budget remains independent of unresolved traffic and
    # account-wide admission history, as in the upstream limiter contract.
    assert await limiter.check_and_record_login("owner", unknown, device="browser") == (
        True,
        None,
    )


@pytest.mark.asyncio
async def test_full_bucket_maps_do_not_deny_unrelated_users(monkeypatch):
    # security: filling every admission map with distinct attacker identities
    # must not lock out a new legitimate user (the old fail-closed behaviour).
    now = [5_000.0]
    monkeypatch.setattr(limiter, "_now", lambda: now[0])
    monkeypatch.setattr(limiter, "_MAX_ACTIVE_BUCKETS", 3)

    for i in range(6):
        ip = f"198.51.100.{i + 10}"
        assert await limiter.check_and_record_login(f"flood-{i}@example.com", ip) == (
            True,
            None,
        )

    # A brand-new user from a brand-new address still gets the ordinary
    # per-account allowance; map saturation is not a global login DoS.
    assert await limiter.check_and_record_login("alice@example.com", OWNER) == (
        True,
        None,
    )
    assert await limiter.check_and_record_login("alice@example.com", OWNER) == (
        True,
        None,
    )
    # Saturated maps use overflow accounting, so her third attempt is refused
    # at the ordinary account/address limit rather than becoming untracked.
    assert await limiter.check_and_record_login("alice@example.com", OWNER) == (
        False,
        "email",
    )
    assert ("alice@example.com", OWNER) not in limiter._account_address_attempts
    now[0] += 15 * 60 + 1
    assert await limiter.check_and_record_login("alice@example.com", OWNER) == (True, None)


@pytest.mark.asyncio
async def test_one_account_cannot_exhaust_the_device_escape_hatch(monkeypatch):
    # security: one account cannot create independent live limiter windows
    # without bound or disable an existing administrator device.
    now = [6_000.0]
    monkeypatch.setattr(limiter, "_now", lambda: now[0])
    monkeypatch.setattr(limiter, "_MAX_ACTIVE_BUCKETS", 3)
    monkeypatch.setattr(limiter, "_MAX_DEVICES_PER_ACCOUNT", 2)

    # One account floods device sign-ins from its own address; its IP window
    # stays at zero because the device path never touches the IP buckets.
    for i in range(2):
        assert await limiter.check_and_record_login(
            "attacker@example.com", ATTACKER, device=f"nonce-{i}"
        ) == (True, None)
    assert await limiter.check_and_record_login(
        "admin@example.com", OWNER, device="admin-browser"
    ) == (True, None)
    for i in range(2, 4):
        assert await limiter.check_and_record_login(
            "attacker@example.com", ATTACKER, device=f"nonce-{i}"
        ) == (True, None)
    assert await limiter.check_and_record_login(
        "attacker@example.com", ATTACKER, device="nonce-4"
    ) == (False, "device")
    assert ATTACKER not in limiter._ip_attempts
    # The account cap leaves the admin's existing key intact.
    assert len(limiter._device_attempts) == 3
    assert {account_key for account_key, _device_key in limiter._device_attempts} == {
        "attacker@example.com",
        "admin@example.com",
    }

    # The existing administrator browser retains its independent window even
    # though the attacker saturated the shared device map.
    assert await limiter.check_and_record_login(
        "admin@example.com", OWNER, device="admin-browser"
    ) == (True, None)


@pytest.mark.asyncio
async def test_unresolved_caller_keeps_per_account_address_cap(monkeypatch):
    # security: unresolved provenance keeps the per-account/address window
    # instead of being able to spend the whole coarse hourly budget on one
    # account in a burst.
    monkeypatch.setattr(settings, "login_rate_limit_per_15min", 2)
    monkeypatch.setattr(settings, "unresolved_login_rate_limit_per_hour", 20)
    unknown = _unresolved_provenance()

    # Only two guesses per 15 minutes against one account, exactly as for a
    # resolved address.
    assert await limiter.check_and_record_login("victim@example.com", unknown) == (
        True,
        None,
    )
    assert await limiter.check_and_record_login("victim@example.com", unknown) == (
        True,
        None,
    )
    assert await limiter.check_and_record_login("victim@example.com", unknown) == (
        False,
        "email",
    )

    # The account/address window is keyed per account, so a different account
    # is not blocked by the victim's window.
    assert await limiter.check_and_record_login("other@example.com", unknown) == (
        True,
        None,
    )


@pytest.mark.asyncio
async def test_saturation_does_not_evict_live_state_and_bypass_is_bounded(monkeypatch):
    """Pin bounded overflow accounting without live-key eviction."""
    now = [7_000.0]
    monkeypatch.setattr(limiter, "_now", lambda: now[0])
    monkeypatch.setattr(settings, "login_rate_limit_per_15min", 2)
    monkeypatch.setattr(limiter, "_MAX_ACTIVE_BUCKETS", 2)

    # A protected account is actively near its per-address limit; every
    # admitted attempt refreshes its key's recorded state.
    assert await limiter.check_and_record_login("victim", "198.51.100.50") == (True, None)
    now[0] += 1
    assert await limiter.check_and_record_login("victim", "198.51.100.50") == (True, None)
    assert await limiter.check_and_record_login("victim", "198.51.100.50") == (False, "email")
    now[0] += 1

    # The attacker saturates the map with fresh identities.
    assert await limiter.check_and_record_login("atk1", "198.51.100.1") == (True, None)
    now[0] += 1
    assert await limiter.check_and_record_login("atk2", "198.51.100.2") == (True, None)
    assert len(limiter._account_address_attempts) == 2
    now[0] += 1

    # A third attacker identity uses bounded overflow counters: map memory
    # remains fixed, no live entry is dropped, and its attempts still count.
    assert await limiter.check_and_record_login("atk3", "198.51.100.3") == (True, None)
    assert len(limiter._account_address_attempts) == 2
    assert ("atk3", "198.51.100.3") not in limiter._account_address_attempts
    # The victim's recorded window survived the flood.
    assert ("victim", "198.51.100.50") in limiter._account_address_attempts
    assert await limiter.check_and_record_login("victim", "198.51.100.50") == (False, "email")

    # The same overflow identity stays tracked and cannot reset its window.
    now[0] += 1
    assert await limiter.check_and_record_login("atk3", "198.51.100.3") == (True, None)
    assert await limiter.check_and_record_login("atk3", "198.51.100.3") == (False, "email")
    assert len(limiter._account_address_attempts) == 2


@pytest.mark.asyncio
async def test_capacity_pressure_logs_and_diagnostic_counts_are_bounded(monkeypatch, caplog):
    now = [8_000.0]
    monkeypatch.setattr(limiter, "_now", lambda: now[0])
    monkeypatch.setattr(limiter, "_MAX_ACTIVE_BUCKETS", 1)
    monkeypatch.setattr(limiter, "_MAX_CAPACITY_PRESSURE_COUNT", 3)

    assert await limiter.check_and_record_login("first", "198.51.100.1") == (True, None)
    assert await limiter.check_and_record_login("second", "198.51.100.2") == (True, None)
    assert await limiter.check_and_record_login("second", "198.51.100.2") == (True, None)
    assert await limiter.check_and_record_login("second", "198.51.100.2") == (False, "email")

    counts = limiter.get_login_rate_limit_capacity_pressure_counts()
    assert set(counts) == {"device", "account_address", "account", "ip", "refusal"}
    assert counts["account_address"] == 3
    assert counts["account"] == 3
    assert counts["ip"] == 3
    assert all(count <= 3 for count in counts.values())
    warnings = [
        record
        for record in caplog.records
        if "login rate-limit bucket capacity reached" in record.getMessage()
    ]
    assert {record.capacity_scope for record in warnings} >= {
        "account_address",
        "account",
        "ip",
    }


@pytest.mark.asyncio
async def test_refusal_pressure_overflow_does_not_block_unrelated_addresses(monkeypatch):
    now = [9_000.0]
    monkeypatch.setattr(limiter, "_now", lambda: now[0])
    monkeypatch.setattr(limiter, "_MAX_ACTIVE_BUCKETS", 1)
    monkeypatch.setattr(settings, "login_rate_limit_per_15min", 1)
    monkeypatch.setattr(settings, "login_rate_limit_per_hour_per_ip", 3)

    assert await limiter.check_and_record_login("first", "198.51.100.1") == (True, None)
    assert await limiter.check_and_record_login("first", "198.51.100.1") == (False, "email")
    assert await limiter.check_and_record_login("second", "198.51.100.2") == (True, None)
    assert await limiter.check_and_record_login("second", "198.51.100.2") == (False, "email")
    assert await limiter.check_and_record_login("third", "198.51.100.3") == (True, None)
    assert await limiter.check_and_record_login("third", "198.51.100.3") == (False, "email")
    assert limiter.get_login_rate_limit_capacity_pressure_counts()["refusal"] > 0
