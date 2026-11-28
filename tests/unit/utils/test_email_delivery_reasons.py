"""What ``deliver_email`` says when mail cannot go out, before and after it tries.

Configuration first: the selected transport names the settings it still needs,
in agreement with ``is_email_enabled``. Then the SMTP conversation: each kind
of failure is explained by what the gateway was doing, never with the server's
own text or a credential.
"""

from __future__ import annotations

import smtplib
import socket
import ssl
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from serving.config.settings import settings
from serving.utils import delivery, email


@pytest.fixture
def unconfigured(monkeypatch):
    for name, value in {
        "email_provider": "smtp",
        "smtp_host": "",
        "smtp_user": "",
        "smtp_password": "",
        "cloudflare_account_id": "",
        "cloudflare_email_api_token": "",
    }.items():
        monkeypatch.setattr(settings, name, value)


@pytest.fixture
def transports(monkeypatch):
    """No transport may be reached: a configuration problem stops the send first."""
    smtp = Mock()
    monkeypatch.setattr(email.smtplib, "SMTP", smtp)
    monkeypatch.setattr(email.smtplib, "SMTP_SSL", smtp)
    post = Mock()
    monkeypatch.setattr(email.cloudflare_email.httpx, "post", post)
    return smtp, post


def test_unconfigured_smtp_names_every_missing_setting(unconfigured):
    assert email.email_configuration_problem() == (
        "SMTP needs SMTP_HOST, SMTP_USER and SMTP_PASSWORD."
    )


def test_partly_configured_smtp_names_only_what_is_missing(unconfigured, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.org")
    monkeypatch.setattr(settings, "smtp_user", "mailer")

    assert email.email_configuration_problem() == "SMTP needs SMTP_PASSWORD."


def test_cloudflare_names_its_own_settings_and_ignores_blank_ones(unconfigured, monkeypatch):
    monkeypatch.setattr(settings, "email_provider", "cloudflare")
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.org")  # not Cloudflare's
    monkeypatch.setattr(settings, "cloudflare_email_api_token", "token")
    monkeypatch.setattr(settings, "cloudflare_account_id", "   ")

    assert email.email_configuration_problem() == (
        "Cloudflare Email Sending needs CLOUDFLARE_ACCOUNT_ID."
    )


def test_a_configured_transport_has_no_problem(unconfigured, monkeypatch):
    monkeypatch.setattr(settings, "email_provider", "cloudflare")
    monkeypatch.setattr(settings, "cloudflare_account_id", "0123456789abcdef0123456789abcdef")
    monkeypatch.setattr(settings, "cloudflare_email_api_token", "token")

    assert email.is_email_enabled()
    assert email.email_configuration_problem() is None


def test_a_provider_no_extension_registered_is_named(unconfigured, monkeypatch):
    monkeypatch.setattr(settings, "email_provider", "no_such_provider")

    assert "no loaded extension provides it" in email.email_configuration_problem()


def test_an_unconfigured_transport_reports_its_problem_and_sends_nothing(unconfigured, transports):
    sent = email.deliver_email("user@example.org", "subject", "<p>body</p>", "body")

    assert sent == delivery.EmailDelivery(
        False, "SMTP needs SMTP_HOST, SMTP_USER and SMTP_PASSWORD."
    )
    smtp, post = transports
    smtp.assert_not_called()
    post.assert_not_called()


def test_a_missing_recipient_is_reported(unconfigured, transports):
    assert email.deliver_email("  ", "subject", "<p>body</p>").reason == (
        "There is no address to send the message to."
    )


def test_send_email_still_answers_with_a_bool(unconfigured, transports):
    assert email.send_email("user@example.org", "subject", "<p>body</p>", "body") is False


def test_an_extension_that_does_not_send_is_reported_without_guessing(monkeypatch):
    provider = delivery.EmailProvider(is_configured=lambda: True, send=Mock(return_value=False))
    monkeypatch.setattr(settings, "email_provider", "acme_mail")
    monkeypatch.setattr(email, "get_email_provider", lambda name: provider)

    sent = email.deliver_email("user@example.org", "subject", "<p>body</p>", "body")

    assert sent.sent is False
    assert sent.reason == (
        "The acme_mail email provider did not send the message. The backend log says why."
    )


WHERE = "smtp.example.org:587"


@pytest.mark.parametrize(
    "error,stage,expected",
    [
        (smtplib.SMTPAuthenticationError(535, b"no"), "login", "rejected the username or password"),
        (smtplib.SMTPNotSupportedError("no"), "starttls", "does not offer STARTTLS"),
        (smtplib.SMTPNotSupportedError("no"), "login", "does not accept a login"),
        (
            smtplib.SMTPSenderRefused(553, b"no", "noreply@localhost"),
            "send",
            "refused the sender address noreply@localhost (SMTP 553)",
        ),
        (smtplib.SMTPRecipientsRefused({}), "send", "refused the recipient address"),
        (
            smtplib.SMTPServerDisconnected("gone"),
            "connect",
            "closed the connection while connecting",
        ),
        (smtplib.SMTPDataError(554, b"no"), "send", "answered SMTP 554 while sending the message"),
        (ssl.SSLError(1, "wrong version number"), "starttls", "TLS handshake"),
        (socket.gaierror(-2, "Name or service not known"), "connect", "could not be found"),
        (TimeoutError("timed out"), "connect", "did not answer within 10 seconds"),
        (ConnectionRefusedError(111, "refused"), "connect", "refused the connection"),
        (OSError(101, "unreachable"), "connect", "Could not reach the SMTP server"),
        (ValueError("odd"), "send", "could not be sent through SMTP while sending the message"),
    ],
)
def test_each_smtp_failure_is_explained_by_what_failed(error, stage, expected):
    reason = email._smtp_failure(error, stage, WHERE, "noreply@localhost")

    assert expected in reason
    # The server's own words never reach the reason.
    for words in ("no", "gone", "odd", "Name or service not known", "wrong version number"):
        assert f"({words})" not in reason and f"b'{words}'" not in reason


def test_the_test_email_names_the_transport_and_escapes_the_site_name(monkeypatch):
    site = SimpleNamespace(name="A&B <Gateway>")
    monkeypatch.setattr(email, "get_site_identity", lambda: site)

    subject, html_body, text_body = email.render_test_email("cloudflare")

    assert subject == "Test email from A&B <Gateway>"
    assert "A&amp;B &lt;Gateway&gt;" in html_body
    assert "<Gateway>" not in html_body
    assert "through Cloudflare Email Sending" in text_body
    # An extension's transport is named as configured.
    assert "through tencent_ses" in email.render_test_email("tencent_ses")[2]
