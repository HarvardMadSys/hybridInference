"""The security notices an account's owner relies on, and the sender under them.

"Your password was changed", "your sign-in email was changed" and "your
account was deleted" are how an owner learns someone else used their session.
None had a test. What matters: each goes to the right address (an email
change notifies the address that *lost* the account), says what happened and
whom to contact, and quotes user-controlled text escaped in the HTML part.
``send_email`` itself is checked against a stand-in SMTP server.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, ClassVar

import pytest

from serving.utils import email as email_mod

if TYPE_CHECKING:
    from email.message import Message


class FakeSMTP:
    """Records what ``smtplib.SMTP`` was asked to do."""

    sent: ClassVar[list[Message]] = []
    calls: ClassVar[list[str]] = []
    fail_with: ClassVar[Exception | None] = None

    def __init__(self, host: str, port: int, timeout: float) -> None:
        type(self).calls.append(f"connect {host}:{port}")

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self) -> None:
        type(self).calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        type(self).calls.append(f"login {user}")

    def send_message(self, msg: Message) -> None:
        if type(self).fail_with is not None:
            raise type(self).fail_with
        type(self).sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.calls, FakeSMTP.fail_with = [], [], None
    monkeypatch.setattr(email_mod.smtplib, "SMTP", FakeSMTP)
    for name, value in {
        "email_provider": "smtp",
        "smtp_host": "smtp.test",
        "smtp_port": 587,
        "smtp_user": "mailer",
        "smtp_password": "pw",
        "smtp_from_email": "noreply@acme.test",
        "smtp_from_name": "Acme",
        "frontend_url": "https://console.acme.test",
    }.items():
        monkeypatch.setattr(email_mod.settings, name, value, raising=False)
    monkeypatch.setattr(
        email_mod,
        "get_site_identity",
        lambda: SimpleNamespace(name="Acme", support_email="help@acme.test"),
    )
    return FakeSMTP


def _parts(msg: Message) -> dict[str, str]:
    return {
        part.get_content_type(): part.get_payload(decode=True).decode()
        for part in msg.walk()
        if not part.is_multipart()
    }


# --- send_email ----------------------------------------------------------------


def test_a_message_is_sent_over_tls_with_both_parts(smtp):
    assert email_mod.send_email("owner@example.com", "Hi", "<p>html</p>", "text") is True

    assert smtp.calls == ["connect smtp.test:587", "starttls", "login mailer"]
    [msg] = smtp.sent
    assert (msg["To"], msg["Subject"], msg["From"]) == (
        "owner@example.com",
        "Hi",
        "Acme <noreply@acme.test>",
    )
    assert _parts(msg) == {"text/plain": "text", "text/html": "<p>html</p>"}


@pytest.mark.parametrize("recipient", [None, "", "   "])
def test_an_account_without_an_address_sends_nothing(smtp, recipient):
    assert email_mod.send_email(recipient, "Hi", "<p>x</p>", "x") is False
    assert smtp.calls == []


def test_unconfigured_smtp_sends_nothing(smtp, monkeypatch):
    monkeypatch.setattr(email_mod.settings, "smtp_password", "")

    assert email_mod.send_email("owner@example.com", "Hi", "<p>x</p>", "x") is False
    assert smtp.calls == []


def test_a_delivery_failure_is_reported_not_raised(smtp):
    smtp.fail_with = OSError("connection reset")

    assert email_mod.send_email("owner@example.com", "Hi", "<p>x</p>", "x") is False


# --- the notices -----------------------------------------------------------------

HOSTILE = 'evil"<script>alert(1)</script>@example.com'


def _only_message(smtp) -> tuple[Message, dict[str, str]]:
    [msg] = smtp.sent
    return msg, _parts(msg)


def test_an_email_change_notifies_the_address_that_lost_the_account(smtp):
    assert email_mod.send_email_changed_email("old@example.com", "new@example.com") is True

    msg, parts = _only_message(smtp)
    assert msg["To"] == "old@example.com"
    assert msg["Subject"] == "Your Acme sign-in email was changed"
    for body in parts.values():
        assert "old@example.com" in body and "new@example.com" in body
        assert "help@acme.test" in body


@pytest.mark.parametrize(
    ("first", "verb"), [(False, "changed"), (True, "set")], ids=["changed", "first-set"]
)
def test_a_password_change_says_what_happened_and_how_to_recover(smtp, first, verb):
    assert email_mod.send_password_changed_email("owner@example.com", first_password=first)

    msg, parts = _only_message(smtp)
    assert msg["To"] == "owner@example.com"
    assert msg["Subject"] == f"Your Acme password was {verb}"
    assert 'href="https://console.acme.test/forgot-password"' in parts["text/html"]
    assert "https://console.acme.test/forgot-password" in parts["text/plain"]
    assert "signed out" in parts["text/plain"]


def test_a_deletion_tells_the_deleted_address(smtp):
    assert email_mod.send_account_deleted_email("owner@example.com") is True

    msg, parts = _only_message(smtp)
    assert msg["To"] == "owner@example.com"
    assert msg["Subject"] == "Your Acme account has been deleted"
    assert "revoked" in parts["text/plain"] and "help@acme.test" in parts["text/plain"]


def test_without_a_support_address_the_notice_points_at_the_administrator(smtp, monkeypatch):
    monkeypatch.setattr(
        email_mod, "get_site_identity", lambda: SimpleNamespace(name="Acme", support_email="")
    )

    email_mod.send_account_deleted_email("owner@example.com")

    _, parts = _only_message(smtp)
    assert "contact the administrator" in parts["text/plain"]


@pytest.mark.parametrize(
    "send",
    [
        lambda: email_mod.send_email_changed_email("old@example.com", HOSTILE),
        lambda: email_mod.send_email_changed_email(HOSTILE, "new@example.com"),
        lambda: email_mod.send_account_deleted_email(HOSTILE),
    ],
    ids=["new-address", "old-address", "deleted-address"],
)
def test_an_address_is_escaped_in_the_html(smtp, send):
    send()

    _, parts = _only_message(smtp)
    assert "<script>" not in parts["text/html"]
    assert "&lt;script&gt;" in parts["text/html"]
