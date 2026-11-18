"""Confirming a change by password from a client whose address could not be resolved.

``confirm_account_owner`` counts password attempts against the sign-in limits.
Behind a proxy the backend does not trust, every browser reads "unknown":
someone else's sign-in attempts must not refuse a signed-in owner here, while
guesses at the owner's own password stay bounded per account.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException, Request

from serving.config.settings import settings
from serving.servers.routers import reauth
from serving.utils import login_rate_limit, password as password_utils
from serving.utils.login_rate_limit import check_and_record_login

PASSWORD = "Str0ngPassword"


def _request(client_ip: str = "127.0.0.1") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/auth/delete-account",
            "headers": [],
            "client": (client_ip, 1),
            "server": ("test", 443),
        }
    )


def _user(**fields: Any) -> dict[str, Any]:
    return {"id": "u-1", "email": "alice@example.edu", "password_hash": "hash", **fields}


@pytest.fixture(autouse=True)
def _limits(monkeypatch):
    for name, value in (
        ("login_rate_limit_per_15min", 5),
        ("login_account_rate_limit_per_15min", 50),
        ("login_rate_limit_per_hour_per_ip", 20),
    ):
        monkeypatch.setattr(settings, name, value)

    async def _verify(plain: str, _hash: str | None, *, shed: bool = True) -> bool:
        return plain == PASSWORD

    monkeypatch.setattr(password_utils, "verify_password_async", _verify)
    login_rate_limit.reset_login_rate_limit_state()
    yield
    login_rate_limit.reset_login_rate_limit_state()


async def _confirm(password: str, request: Request | None = None, sid: str = "sid-1") -> None:
    await reauth.confirm_account_owner(
        request or _request(),
        None,
        _user(),
        password=password,
        phone_code=None,
        purpose="delete_account",
        sid=sid,
    )


async def test_anonymous_guesses_at_the_owners_name_do_not_refuse_the_owner():
    """Review regression: five guesses at the name from "unknown" no longer block re-authentication."""
    for n in range(30):
        await check_and_record_login(f"stranger-{n}@example.com", "unknown")
    for _ in range(5):
        await check_and_record_login("alice@example.edu", "unknown")
    assert await check_and_record_login("alice@example.edu", "unknown") == (False, "email")

    await _confirm(PASSWORD)


async def test_guesses_through_the_session_are_still_bounded():
    """Whoever holds a stolen session still gets five a window."""
    for _ in range(5):
        with pytest.raises(HTTPException) as wrong:
            await _confirm("guess")
        assert wrong.value.status_code == 400

    with pytest.raises(HTTPException) as limited:
        await _confirm(PASSWORD)
    assert (limited.value.status_code, limited.value.headers["Retry-After"]) == (429, "900")


async def test_a_resolved_client_keeps_its_hourly_window():
    for n in range(20):
        await check_and_record_login(f"stranger-{n}@example.com", "8.8.8.8")

    with pytest.raises(HTTPException) as limited:
        await _confirm(PASSWORD, _request("8.8.8.8"))
    assert (limited.value.status_code, limited.value.headers["Retry-After"]) == (429, "3600")
    await _confirm(PASSWORD, _request("8.8.4.4"))
