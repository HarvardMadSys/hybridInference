"""Device cookies: signed entries that recognize a browser at its next sign-in."""

from __future__ import annotations

import base64

import pytest
from starlette.requests import Request
from starlette.responses import Response

from serving.config.settings import settings
from serving.utils import device_cookie
from serving.utils.device_cookie import (
    LIFETIME_SECONDS,
    MAX_ENTRIES,
    device_cookie_name,
    recognized_device,
    remember_device,
)

ADMIN = {"id": "u-admin", "email": None, "login_name": "admin"}
ALICE = {"id": "u-alice", "email": "Alice@Example.com", "login_name": None}
BOTH = {"id": "u-ops", "email": "ops@example.com", "login_name": "ops"}


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(device_cookie, "get_jwt_secret", lambda: "unit-test-jwt-secret")
    monkeypatch.setattr(settings, "refresh_token_cookie_name", "refresh_token")
    monkeypatch.setattr(settings, "cookie_secure", True)
    monkeypatch.setattr(settings, "cookie_samesite", "lax")
    monkeypatch.setattr(settings, "cookie_domain", None)


def _request(value: str | None) -> Request:
    headers = [] if value is None else [(b"cookie", f"{device_cookie_name()}={value}".encode())]
    return Request({"type": "http", "method": "POST", "path": "/auth/login", "headers": headers})


def _set_cookie(account, carried: str | None = None) -> str:
    """Sign *account* in on a browser carrying *carried*; return the Set-Cookie header."""
    response = Response()
    remember_device(response, account, _request(carried))
    headers = response.headers.getlist("set-cookie")
    assert len(headers) == 1
    assert headers[0].startswith(f"{device_cookie_name()}=")
    return headers[0]


def _issue(account, carried: str | None = None) -> str:
    """Return the cookie value a sign-in to *account* leaves the browser holding."""
    return _set_cookie(account, carried).split(";", 1)[0].split("=", 1)[1]


def _recognized(value: str | None, identifier: str) -> str | None:
    return recognized_device(_request(value), identifier)


def test_an_entry_recognizes_its_account_however_the_identifier_is_typed():
    cookie = _issue(ALICE)

    nonce = _recognized(cookie, "alice@example.com")
    assert nonce is not None and len(nonce) == 32
    assert _recognized(cookie, " ALICE@Example.com ") == nonce

    admin_cookie = _issue(ADMIN)
    assert _recognized(admin_cookie, "Admin") is not None


def test_an_account_with_both_identifiers_is_recognized_by_either():
    cookie = _issue(BOTH)

    by_email = _recognized(cookie, "ops@example.com")
    assert by_email is not None
    assert _recognized(cookie, "ops") == by_email


def test_no_exemption_for_another_account():
    cookie = _issue(ALICE)

    assert _recognized(cookie, "admin") is None
    assert _recognized(cookie, "bob@example.com") is None


def test_no_exemption_for_a_forged_entry():
    raw = bytearray(base64.urlsafe_b64decode(_issue(ADMIN) + "=="))

    tampered_tag = bytearray(raw)
    tampered_tag[-1] ^= 1
    later_expiry = bytearray(raw)
    later_expiry[19] ^= 1  # the last byte of the expiry, which the tag covers
    for forged in (tampered_tag, later_expiry):
        value = base64.urlsafe_b64encode(bytes(forged)).rstrip(b"=").decode()
        assert _recognized(value, "admin") is None


def test_no_exemption_for_an_entry_signed_under_another_secret(monkeypatch):
    monkeypatch.setattr(device_cookie, "get_jwt_secret", lambda: "some-other-deployment")
    cookie = _issue(ADMIN)
    monkeypatch.setattr(device_cookie, "get_jwt_secret", lambda: "unit-test-jwt-secret")

    assert _recognized(cookie, "admin") is None


def test_no_exemption_for_an_expired_entry(monkeypatch):
    issued_at = device_cookie._now()
    monkeypatch.setattr(device_cookie, "_now", lambda: issued_at)
    cookie = _issue(ADMIN)

    monkeypatch.setattr(device_cookie, "_now", lambda: issued_at + LIFETIME_SECONDS - 1)
    assert _recognized(cookie, "admin") is not None
    monkeypatch.setattr(device_cookie, "_now", lambda: issued_at + LIFETIME_SECONDS)
    assert _recognized(cookie, "admin") is None


@pytest.mark.parametrize(
    "value",
    ["", "!!!", "a.b.c", "A" * 10_000, "QUFB", ".".join(["QUFB"] * 50), "é" * 40],
)
def test_malformed_cookies_are_ignored(value):
    assert _recognized(value, "admin") is None


def test_only_the_first_entries_are_read():
    admin = _issue(ADMIN)
    padding = [_issue({"login_name": f"user{n}"}) for n in range(MAX_ENTRIES)]

    assert _recognized(".".join([*padding, admin]), "admin") is None


def test_a_sign_in_rotates_its_own_entry_and_keeps_the_others():
    first = _issue(ALICE)
    alice = _recognized(first, "alice@example.com")
    both = _issue(ADMIN, first)

    assert both.count(".") == 1
    assert _recognized(both, "alice@example.com") == alice
    admin = _recognized(both, "admin")
    assert admin is not None

    again = _issue(ALICE, both)

    assert again.count(".") == 1
    assert again.split(".")[1] == both.split(".")[0]  # the admin's entry, now second
    rotated = _recognized(again, "alice@example.com")
    assert rotated is not None and rotated != alice
    assert _recognized(again, "admin") == admin


def test_entries_are_capped_newest_first():
    cookie = None
    accounts = [{"login_name": f"user{n}"} for n in range(MAX_ENTRIES + 2)]
    for account in accounts:
        cookie = _issue(account, cookie)

    assert cookie is not None
    assert cookie.count(".") == MAX_ENTRIES - 1
    assert [_recognized(cookie, a["login_name"]) is not None for a in accounts] == [
        False,
        False,
        *[True] * MAX_ENTRIES,
    ]


def test_expired_entries_are_dropped_when_the_cookie_is_rewritten(monkeypatch):
    issued_at = device_cookie._now()
    monkeypatch.setattr(device_cookie, "_now", lambda: issued_at)
    old = _issue(ADMIN)

    monkeypatch.setattr(device_cookie, "_now", lambda: issued_at + LIFETIME_SECONDS)
    cookie = _issue(ALICE, old)

    assert "." not in cookie


def test_the_cookie_stays_small():
    cookie = None
    for n in range(MAX_ENTRIES):
        cookie = _issue({"email": f"someone{n}@example.com", "login_name": f"someone{n}"}, cookie)

    assert cookie is not None
    assert len(cookie) <= 600


def test_cookie_attributes():
    header = _set_cookie(ADMIN)

    name = header.split("=", 1)[0]
    attributes = {part.strip().lower() for part in header.split(";")[1:]}
    assert name == "refresh_token_device"
    assert {"httponly", "secure", "path=/auth", "samesite=lax"} <= attributes
    assert f"max-age={LIFETIME_SECONDS}" in attributes
    assert not any(attribute.startswith("domain=") for attribute in attributes)


def test_cookie_attributes_follow_the_session_cookie_settings(monkeypatch):
    monkeypatch.setattr(settings, "refresh_token_cookie_name", "stack2_refresh")
    monkeypatch.setattr(settings, "cookie_secure", False)
    monkeypatch.setattr(settings, "cookie_samesite", "strict")
    monkeypatch.setattr(settings, "cookie_domain", "example.org")

    header = _set_cookie(ADMIN)

    attributes = {part.strip().lower() for part in header.split(";")[1:]}
    assert header.startswith("stack2_refresh_device=")
    assert "secure" not in attributes
    assert {"httponly", "path=/auth", "samesite=strict", "domain=example.org"} <= attributes


def test_tags_are_compared_in_constant_time(monkeypatch):
    calls = []
    original = device_cookie.hmac.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return original(a, b)

    cookie = _issue(BOTH)
    monkeypatch.setattr(device_cookie.hmac, "compare_digest", _spy)

    assert _recognized(cookie, "ops") is not None
    assert _recognized(cookie, "admin") is None
    assert len(calls) == 4  # every tag of the entry, for each identifier tried


def test_nothing_is_issued_or_recognized_without_a_secret(monkeypatch):
    cookie = _issue(ADMIN)

    def _unset():
        raise ValueError("JWT_SECRET_KEY must be set in environment")

    monkeypatch.setattr(device_cookie, "get_jwt_secret", _unset)
    response = Response()
    remember_device(response, ADMIN, None)

    assert response.headers.getlist("set-cookie") == []
    assert _recognized(cookie, "admin") is None


def test_nothing_is_issued_for_an_account_without_a_sign_in_identifier():
    response = Response()
    remember_device(response, {"id": "u-phone", "email": None, "login_name": None}, None)

    assert response.headers.getlist("set-cookie") == []
