"""A source blocked for auth failures still reaches the gateway with a proven internal key.

The per-IP block answers 429 before the presented key is looked up, so key
brute force costs no database work. The status monitor probes from a
Cloudflare Worker, whose address anyone else's failures can get blocked, and
every probe then read the gateway's own refusal. A key that has authenticated
an internal or administrator account in this process is still looked up from a
blocked address; any other key is refused as before, with no lookup.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from serving.config.settings import get_settings, settings
from serving.servers import auth
from serving.utils import auth_failure_blocklist as blocklist
from serving.utils.request_ip import ClientIpInfo

pytestmark = pytest.mark.asyncio

ADDRESS = "8.8.4.4"
# Low-entropy stand-ins, not credentials.
PROBER_KEY = "hyi-" + "prober" * 4
FREE_KEY = "hyi-" + "free" * 6


def _row(role: str) -> dict[str, Any]:
    return {"id": 1, "user_id": f"u-{role}", "role": role, "email": None, "email_verified": True}


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(get_settings(), "api_key_secret", "unit-test-api-key-secret")
    monkeypatch.setattr(settings, "auth_failure_block_enabled", True)
    monkeypatch.setattr(settings, "auth_failure_block_threshold", 3)
    monkeypatch.setattr(settings, "auth_failure_block_window_sec", 3600)
    monkeypatch.setattr(settings, "auth_failure_block_duration_sec", 3600)
    monkeypatch.setattr(auth, "_verification_pending", AsyncMock(return_value=False))
    monkeypatch.setattr(auth, "rejection_logging_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(auth, "queue_rejection_log", AsyncMock())
    monkeypatch.setattr(auth, "log_rejection", AsyncMock())
    blocklist.reset_auth_failure_block_state()
    auth._privileged_keys.clear()
    yield
    blocklist.reset_auth_failure_block_state()
    auth._privileged_keys.clear()


@pytest.fixture
def accounts(monkeypatch) -> dict[str, dict[str, Any] | None]:
    """Which key resolves to which account; the lookup is counted."""
    known = {
        auth.hash_api_key(PROBER_KEY): _row("internal"),
        auth.hash_api_key(FREE_KEY): _row("free"),
    }

    async def lookup(op_store, key_hash, *, want_spend):
        return known.get(key_hash), None

    monkeypatch.setattr(auth, "_auth_context_with_spend", AsyncMock(side_effect=lookup))
    return known


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
            "client": (ADDRESS, 1),
        }
    )


async def _authenticate(key: str):
    return await auth._authenticate_by_api_key(_request(), f"Bearer {key}", None, op_store=object())


async def _block_the_address() -> None:
    info = ClientIpInfo(
        client_ip=ADDRESS,
        peer_ip=ADDRESS,
        source="socket",
        trusted_proxy_headers=False,
        resolved=True,
    )
    for _ in range(settings.auth_failure_block_threshold):
        await blocklist.record_auth_failure(info)
    assert (await blocklist.is_ip_blocked(info))[0]


async def test_a_guess_from_a_blocked_address_is_refused_with_no_lookup(accounts):
    await _block_the_address()

    with pytest.raises(HTTPException) as refused:
        await _authenticate("hyi-" + "guess" * 5)

    assert refused.value.status_code == 429
    auth._auth_context_with_spend.assert_not_awaited()


async def test_a_proven_internal_key_still_authenticates_from_a_blocked_address(accounts):
    user, _key_hash, _spend = await _authenticate(PROBER_KEY)
    assert user["role"] == "internal"
    await _block_the_address()

    user, _key_hash, _spend = await _authenticate(PROBER_KEY)

    assert user["role"] == "internal"
    assert auth._auth_context_with_spend.await_count == 2


async def test_an_internal_key_this_process_never_saw_is_refused(accounts):
    await _block_the_address()

    with pytest.raises(HTTPException) as refused:
        await _authenticate(PROBER_KEY)

    assert refused.value.status_code == 429


async def test_an_ordinary_key_gets_no_pass(accounts):
    await _authenticate(FREE_KEY)
    await _block_the_address()

    with pytest.raises(HTTPException) as refused:
        await _authenticate(FREE_KEY)

    assert refused.value.status_code == 429


async def test_a_key_revoked_since_it_proved_itself_loses_its_pass(accounts):
    await _authenticate(PROBER_KEY)
    await _block_the_address()
    accounts[auth.hash_api_key(PROBER_KEY)] = None  # revoked

    with pytest.raises(HTTPException) as first:
        await _authenticate(PROBER_KEY)
    assert first.value.status_code == 401
    lookups = auth._auth_context_with_spend.await_count

    with pytest.raises(HTTPException) as second:
        await _authenticate(PROBER_KEY)
    assert second.value.status_code == 429
    assert auth._auth_context_with_spend.await_count == lookups


async def test_a_demoted_account_loses_its_pass(accounts):
    await _authenticate(PROBER_KEY)
    accounts[auth.hash_api_key(PROBER_KEY)] = _row("free")
    await _authenticate(PROBER_KEY)  # still valid, no longer privileged
    await _block_the_address()

    with pytest.raises(HTTPException) as refused:
        await _authenticate(PROBER_KEY)

    assert refused.value.status_code == 429


async def test_the_passes_are_bounded(accounts, monkeypatch):
    monkeypatch.setattr(auth, "_PRIVILEGED_KEYS_MAX", 3)
    for n in range(10):
        auth._remember_key_privilege(f"hash-{n}", "admin")

    assert list(auth._privileged_keys) == ["hash-7", "hash-8", "hash-9"]
