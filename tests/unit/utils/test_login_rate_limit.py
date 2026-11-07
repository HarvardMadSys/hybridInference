"""Sign-in windows: per device, per account's devices, per account per address, per account and per address."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from serving.config import app_config_registry as registry
from serving.config.settings import Settings, settings
from serving.utils import login_rate_limit
from serving.utils.device_cookie import RecognizedDevice
from serving.utils.login_rate_limit import check_and_record_login, reset_login_rate_limit_state

ATTACKER = "198.51.100.7"
OWNER = "192.0.2.44"
# What the backend sees for every browser behind a proxy it does not trust.
SHARED = "unknown"


@pytest.fixture(autouse=True)
def _default_limits(monkeypatch):
    """Run against the shipped defaults, whatever the environment sets."""
    monkeypatch.setattr(settings, "login_rate_limit_per_15min", 5)
    monkeypatch.setattr(settings, "login_account_rate_limit_per_15min", 50)
    monkeypatch.setattr(settings, "login_rate_limit_per_hour_per_ip", 20)
    reset_login_rate_limit_state()
    yield
    reset_login_rate_limit_state()


def test_defaults_and_registry_entry():
    fields = Settings.model_fields
    assert fields["login_rate_limit_per_15min"].default == 5
    assert fields["login_account_rate_limit_per_15min"].default == 50
    assert fields["login_rate_limit_per_hour_per_ip"].default == 20

    entry = registry.static_entry("LOGIN_ACCOUNT_RATE_LIMIT_PER_15MIN")
    assert entry is not None
    assert entry.field == "login_account_rate_limit_per_15min"
    assert (entry.category, entry.type, entry.default, entry.minimum) == (
        "security",
        "int",
        "50",
        1,
    )
    assert not entry.secret
    assert not entry.restart_required
    # Zero would refuse every sign-in to every account.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, login_account_rate_limit_per_15min=0)


async def test_one_address_is_limited_per_account():
    for identifier in ("admin", "Admin", "admin ", "ADMIN", "admin"):
        assert await check_and_record_login(identifier, ATTACKER) == (True, None)

    assert await check_and_record_login("admin", ATTACKER) == (False, "email")
    # The address's other accounts are untouched.
    assert await check_and_record_login("root@example.com", ATTACKER) == (True, None)


async def test_guessing_from_one_address_does_not_lock_out_another():
    """The attacker's address stays refused; the owner signs in from their own."""
    for _ in range(5):
        assert await check_and_record_login("admin", ATTACKER) == (True, None)

    refused = [await check_and_record_login("admin", ATTACKER) for _ in range(200)]

    # Refused per account from that address until the per-IP window (20 an
    # hour, counting every attempt) trips and outranks it.
    assert refused[:15] == [(False, "email")] * 15
    assert set(refused[15:]) == {(False, "ip")}
    assert await check_and_record_login("admin", OWNER) == (True, None)


async def test_refused_attempts_do_not_count_account_wide():
    """An address hammering an account adds only the attempts it was allowed."""
    for _ in range(1000):
        await check_and_record_login("admin", ATTACKER)

    # Five of the 1000 were let through, so 45 more fit from elsewhere.
    for n in range(45):
        assert await check_and_record_login("admin", f"203.0.113.{n + 1}") == (True, None)
    assert await check_and_record_login("admin", OWNER) == (False, "account")


async def test_account_window_trips_on_attempts_spread_over_many_addresses():
    for n in range(10):
        for _ in range(5):
            assert await check_and_record_login("admin", f"203.0.113.{n + 1}") == (True, None)

    assert await check_and_record_login("admin", OWNER) == (False, "account")
    assert await check_and_record_login("ADMIN", "203.0.113.200") == (False, "account")
    # Another account keeps its own allowance, from the same addresses.
    assert await check_and_record_login("root@example.com", OWNER) == (True, None)


async def test_per_ip_window_spans_accounts():
    for n in range(20):
        assert await check_and_record_login(f"user{n}@example.com", ATTACKER) == (True, None)

    assert await check_and_record_login("fresh@example.com", ATTACKER) == (False, "ip")
    assert await check_and_record_login("fresh@example.com", OWNER) == (True, None)


async def test_account_wide_limit_applies_live(monkeypatch):
    """Read on every attempt, so a stored change applies without a restart."""
    monkeypatch.setattr(settings, "login_account_rate_limit_per_15min", 3)
    for n in range(3):
        assert await check_and_record_login("admin", f"203.0.113.{n + 1}") == (True, None)

    assert await check_and_record_login("admin", OWNER) == (False, "account")


async def test_windows_slide(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(login_rate_limit, "_now", lambda: now[0])
    for n in range(50):
        await check_and_record_login("admin", f"203.0.113.{n + 1}")
    for _ in range(5):
        await check_and_record_login("root@example.com", ATTACKER)
    assert await check_and_record_login("admin", OWNER) == (False, "account")
    assert await check_and_record_login("root@example.com", ATTACKER) == (False, "email")

    now[0] += 15 * 60 + 1

    assert await check_and_record_login("admin", OWNER) == (True, None)
    assert await check_and_record_login("root@example.com", ATTACKER) == (True, None)


async def test_a_device_is_limited_on_its_own_window():
    for _ in range(5):
        assert await check_and_record_login("admin", SHARED, device="d1") == (True, None)

    assert await check_and_record_login("admin", SHARED, device="d1") == (False, "device")
    # The account's other browser, and the address's other clients, are untouched.
    assert await check_and_record_login("admin", SHARED, device="d2") == (True, None)
    assert await check_and_record_login("admin", SHARED) == (True, None)


async def test_a_device_signs_in_while_every_window_it_would_share_is_exhausted():
    """Behind a proxy that hides addresses, the attacker and the admin share one."""
    for _ in range(25):
        await check_and_record_login("admin", SHARED)
    for n in range(10):
        for _ in range(5):
            await check_and_record_login("admin", f"203.0.113.{n + 1}")
    assert await check_and_record_login("admin", SHARED) == (False, "ip")
    assert await check_and_record_login("admin", OWNER) == (False, "account")

    assert await check_and_record_login("admin", SHARED, device="d1") == (True, None)


async def test_fresh_device_entries_do_not_lift_the_account_limit():
    """Every sign-in issues a new entry; presenting the newest one is still bounded.

    Each successful sign-in gives the browser a new nonce, and so a new device
    window, which alone let an account's owner sign in without limit by
    presenting the latest entry every time. Past the browsers' shared window
    such an attempt is limited like any other client's.
    """
    for n in range(50):
        assert await check_and_record_login("admin", SHARED, device=f"entry-{n}") == (True, None)

    for n in range(50, 55):
        assert await check_and_record_login("admin", SHARED, device=f"entry-{n}") == (True, None)
    assert await check_and_record_login("admin", SHARED, device="entry-55") == (False, "email")
    assert len(login_rate_limit._account_address_attempts[("admin", SHARED)]) == 6
    # Another account's browsers are untouched.
    assert await check_and_record_login("root@example.com", SHARED, device="r1") == (True, None)


async def test_the_devices_window_follows_the_account_wide_setting(monkeypatch):
    monkeypatch.setattr(settings, "login_account_rate_limit_per_15min", 3)
    for n in range(3):
        assert await check_and_record_login("admin", SHARED, device=f"entry-{n}") == (True, None)

    assert await check_and_record_login("admin", SHARED, device="entry-3") == (True, None)
    # Counted where an attempt without an entry is.
    assert len(login_rate_limit._account_attempts["admin"]) == 1


async def test_one_browser_adds_at_most_its_own_allowance_to_the_devices_window():
    """A browser that keeps trying (a stolen entry, say) cannot lock out the others."""
    refused = [await check_and_record_login("admin", SHARED, device="stolen") for _ in range(500)]

    assert refused[:5] == [(True, None)] * 5
    assert set(refused[5:]) == {(False, "device")}
    assert len(login_rate_limit._account_device_attempts[("admin", "")]) == 5
    for n in range(45):
        assert await check_and_record_login("admin", SHARED, device=f"owner-{n}") == (True, None)


async def test_no_one_without_an_entry_uses_up_the_devices_window():
    """The owner's browsers keep signing in while every other window is exhausted."""
    for _ in range(25):
        await check_and_record_login("admin", SHARED)
    for n in range(10):
        for _ in range(5):
            await check_and_record_login("admin", f"203.0.113.{n + 1}")
    assert await check_and_record_login("admin", SHARED) == (False, "ip")
    assert await check_and_record_login("admin", OWNER) == (False, "account")

    assert ("admin", "") not in login_rate_limit._account_device_attempts
    for n in range(50):
        assert await check_and_record_login("admin", SHARED, device=f"owner-{n}") == (True, None)


async def test_the_devices_window_slides(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(login_rate_limit, "_now", lambda: now[0])
    for n in range(50):
        await check_and_record_login("admin", SHARED, device=f"entry-{n}")
    await check_and_record_login("admin", SHARED, device="late")
    assert ("admin", SHARED) in login_rate_limit._account_address_attempts  # full: fell back

    now[0] += 15 * 60 + 1

    assert await check_and_record_login("admin", SHARED, device="late") == (True, None)
    assert len(login_rate_limit._account_device_attempts[("admin", "")]) == 1


def _entry(key: str, epoch: str) -> RecognizedDevice:
    return RecognizedDevice(key=key, epoch=epoch)


async def test_entries_kept_from_before_a_password_change_cannot_lock_out_the_owner():
    """The reported case: stale entries, held 180 days past a reset, filled the window.

    Someone who once knew the password kept ten entries; after the owner
    resets it, they guess with all ten. The owner's browser, recognized under
    the new password, has a window they cannot reach.
    """
    for n in range(10):
        for _ in range(5):
            await check_and_record_login("admin", ATTACKER, device=_entry(f"stale-{n}", "e-old"))
    assert len(login_rate_limit._account_device_attempts[("admin", "e-old")]) == 50

    assert await check_and_record_login("admin", OWNER, device=_entry("mine", "e-new")) == (
        True,
        None,
    )


async def test_a_full_window_falls_back_to_the_address_windows_not_a_refusal():
    """The owner's browser still holding an old entry is limited like any browser."""
    for n in range(10):
        for _ in range(5):
            await check_and_record_login("admin", ATTACKER, device=_entry(f"stale-{n}", "e-old"))

    for _ in range(5):
        assert await check_and_record_login("admin", OWNER, device=_entry("mine", "e-old")) == (
            True,
            None,
        )
    # Its own device window is spent now, as it would be with room left.
    assert await check_and_record_login("admin", OWNER, device=_entry("mine", "e-old")) == (
        False,
        "device",
    )
    assert len(login_rate_limit._account_address_attempts[("admin", OWNER)]) == 5


async def test_device_attempts_do_not_count_in_the_other_windows():
    for _ in range(100):
        await check_and_record_login("admin", SHARED, device="d1")

    assert await check_and_record_login("admin", SHARED) == (True, None)
    assert len(login_rate_limit._account_attempts["admin"]) == 1
    assert len(login_rate_limit._ip_attempts[SHARED]) == 1


async def test_sweep_drops_expired_buckets_from_every_login_window(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(login_rate_limit, "_now", lambda: now[0])
    monkeypatch.setattr(login_rate_limit, "_SWEEP_EVERY", 3)
    await check_and_record_login("admin", ATTACKER)
    await check_and_record_login("admin", OWNER, device="d1")

    now[0] += 15 * 60 + 1
    await check_and_record_login("root@example.com", OWNER)  # the third call sweeps

    assert login_rate_limit._device_attempts == {}
    assert login_rate_limit._account_device_attempts == {}
    assert set(login_rate_limit._account_address_attempts) == {("root@example.com", OWNER)}
    assert set(login_rate_limit._account_attempts) == {"root@example.com"}
    # The per-IP window is an hour, so the attacker's address is still counted.
    assert set(login_rate_limit._ip_attempts) == {ATTACKER, OWNER}


async def test_reset_clears_every_login_window():
    for _ in range(25):
        await check_and_record_login("admin", ATTACKER)
        await check_and_record_login("admin", ATTACKER, device="d1")

    reset_login_rate_limit_state()

    assert await check_and_record_login("admin", ATTACKER) == (True, None)
    assert await check_and_record_login("admin", ATTACKER, device="d1") == (True, None)
    assert len(login_rate_limit._account_device_attempts[("admin", "")]) == 1
