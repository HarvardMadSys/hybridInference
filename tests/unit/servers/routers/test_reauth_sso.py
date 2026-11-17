"""Confirming a sensitive action by a fresh sign-in at the account's identity provider."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request

from serving.servers.routers import reauth


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/auth/delete-account",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("test", 443),
        }
    )


def _user(**fields: Any) -> dict[str, Any]:
    return {
        "id": "u-1",
        "email": "alice@example.edu",
        "password_hash": None,
        "phone": None,
        "phone_verified": False,
        **fields,
    }


class Store:
    def __init__(self, *, proof: bool, identities: int = 1) -> None:
        self.consume_sso_reauth_proof = AsyncMock(return_value=proof)
        self.list_user_identities = AsyncMock(return_value=[{"id": "i"}] * identities)


async def _confirm(store: Store, user: dict[str, Any], *, sid: str | None = "sid-1", **proof: Any):
    await reauth.confirm_account_owner(
        _request(),
        store,
        user,
        password=proof.get("password"),
        phone_code=proof.get("phone_code"),
        purpose="delete_account",
        sid=sid,
    )


async def test_a_fresh_sign_in_at_the_provider_confirms_an_account_with_nothing_else():
    store = Store(proof=True)

    await _confirm(store, _user())

    store.consume_sso_reauth_proof.assert_awaited_once_with(
        user_id="u-1", sid="sid-1", purpose="delete_account", max_age_seconds=300
    )


async def test_without_a_recent_sign_in_the_account_is_told_to_confirm_at_its_provider():
    store = Store(proof=False)

    with pytest.raises(HTTPException) as excinfo:
        await _confirm(store, _user())

    assert excinfo.value.status_code == 400
    assert "organization" in excinfo.value.detail


async def test_a_confirmation_needs_the_callers_session():
    store = Store(proof=True)

    with pytest.raises(HTTPException) as excinfo:
        await _confirm(store, _user(), sid=None)

    assert excinfo.value.status_code == 400
    store.consume_sso_reauth_proof.assert_not_awaited()


async def test_an_account_with_no_identity_still_has_no_way_to_confirm():
    store = Store(proof=False, identities=0)

    with pytest.raises(HTTPException) as excinfo:
        await _confirm(store, _user())

    assert excinfo.value.status_code == 400
    assert "no password or verified phone" in excinfo.value.detail


async def test_an_account_with_a_password_is_never_confirmed_at_its_provider(monkeypatch):
    # The password is asked for even when a sign-in at the provider is on record.
    monkeypatch.setattr(reauth, "check_and_record_login", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(
        reauth.password_utils, "verify_password_async", AsyncMock(return_value=False)
    )
    store = Store(proof=True)

    with pytest.raises(HTTPException) as excinfo:
        await _confirm(store, _user(password_hash="hash"), password="wrong")

    assert excinfo.value.detail == "Current password is incorrect."
    store.consume_sso_reauth_proof.assert_not_awaited()
