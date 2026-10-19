"""Tencent SES account mail: approved templates, escaping and no fallback."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from serving.config.settings import settings
from serving.config.site_identity import get_site_identity
from serving.integrations.tencent_cloud import api, ses
from serving.utils import email

EXAMPLE_TEMPLATES = Path(__file__).resolve().parents[3] / "config/examples/tencent-ses"


@pytest.fixture
def configured(monkeypatch):
    """Use only synthetic credentials and approved-template stand-ins."""
    environment = {
        "TENCENT_CLOUD_SECRET_ID": "test-id",
        "TENCENT_CLOUD_SECRET_KEY": "test-secret",
        "TENCENT_CLOUD_TOKEN": "test-session-token",
        "TENCENT_SES_FROM_EMAIL": "noreply@mail.example.org",
        "TENCENT_SES_FROM_NAME": "Test service",
        "TENCENT_SES_TEMPLATE_VERIFICATION": "101",
        "TENCENT_SES_TEMPLATE_PASSWORD_RESET": "102",
        "TENCENT_SES_TEMPLATE_APPROVAL": "103",
        "TENCENT_SES_TEMPLATE_REJECTION": "104",
        "TENCENT_SES_TEMPLATE_ADMIN_SIGNUP": "105",
        "TENCENT_SES_TEMPLATE_NOTICE": "106",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(settings, "email_provider", "tencent_ses")
    monkeypatch.setattr(settings, "frontend_url", "https://console.example.org")


@pytest.mark.parametrize(
    ("function", "args", "template_id", "variable"),
    [
        (
            email.send_verification_email,
            ("user@example.org", "test-token", "https://api.example.org"),
            101,
            "token",
        ),
        (
            email.send_password_reset_email,
            ("user@example.org", "test-token", "https://api.example.org"),
            102,
            "token",
        ),
        (email.send_approval_email, ("user@example.org",), 103, "site_name"),
        (email.send_rejection_email, ("user@example.org", "reason"), 104, "reason"),
        (
            email.send_new_registration_admin_email,
            ("admin@example.org", "user@example.org", "User", "user-id"),
            105,
            "identifier",
        ),
    ],
)
def test_account_mail_uses_approved_event_template(
    configured, monkeypatch, function, args, template_id, variable
):
    call = Mock(return_value={"MessageId": "accepted-id"})
    monkeypatch.setattr(ses, "call_tencent_cloud", call)
    assert function(*args)
    service, action, version, region, payload = call.call_args.args
    assert (service, action, version) == ("ses", "SendEmail", "2020-10-02")
    assert region == "ap-guangzhou"
    assert payload["Template"]["TemplateID"] == template_id
    data = json.loads(payload["Template"]["TemplateData"])
    assert variable in data
    assert "url" not in data
    assert payload["TriggerType"] == 1
    assert "Simple" not in payload
    assert payload["Destination"] == [args[0]]


@pytest.mark.parametrize(
    ("function", "template_file", "path"),
    [
        (email.send_verification_email, "verification.html", "/verify-email"),
        (email.send_password_reset_email, "password-reset.html", "/reset-password"),
    ],
)
def test_ses_account_links_keep_reviewed_domain_and_encode_token(
    configured, monkeypatch, function, template_file, path
):
    """SES review requires a fixed domain; variables cannot replace a whole URL."""
    call = Mock(return_value={"MessageId": "accepted-id"})
    monkeypatch.setattr(ses, "call_tencent_cloud", call)
    token = 'token+value/&next=https://other.example/?x="value"'
    assert function("user@example.org", token, "https://api.example.org")
    data = json.loads(call.call_args.args[-1]["Template"]["TemplateData"])
    template = (EXAMPLE_TEMPLATES / template_file).read_text()
    for key, value in data.items():
        template = template.replace("{{" + key + "}}", value)
    link = template.split('href="', 1)[1].split('"', 1)[0]
    parsed = urlsplit(link)
    assert parsed.scheme == "https"
    assert parsed.netloc == "console.example.org"
    assert parsed.path == path
    assert parse_qs(parsed.query) == {"token": [token]}
    assert "{{" not in template


def test_missing_template_fails_without_fallback(configured, monkeypatch):
    monkeypatch.setenv("TENCENT_SES_TEMPLATE_VERIFICATION", "")
    smtp = Mock()
    monkeypatch.setattr(email.smtplib, "SMTP", smtp)
    assert not email.is_email_enabled()
    assert not email.send_verification_email("user@example.org", "token", "https://api.example.org")
    smtp.assert_not_called()


def test_a_notice_without_template_data_sends_its_text_body(configured, monkeypatch):
    sent = []
    smtp = Mock()
    monkeypatch.setattr(ses, "send_template", lambda *args: sent.append(args) or True)
    monkeypatch.setattr(email.smtplib, "SMTP", smtp)

    assert email.send_email("user@example.org", "update", "<p>x</p>", "plain")
    assert not email.send_email("user@example.org", "update", "<p>x</p>", None)

    site_name = get_site_identity().name
    assert sent == [
        ("user@example.org", "update", "notice", {"site_name": site_name, "body": "plain"})
    ]
    smtp.assert_not_called()


def test_template_values_escape_html_and_enforce_parameter_limit(configured, monkeypatch):
    call = Mock(return_value={"MessageId": "accepted-id"})
    monkeypatch.setattr(ses, "call_tencent_cloud", call)
    assert ses.send_template(
        "user@example.org",
        "update",
        "rejection",
        {"site_name": "Test", "reason": '<img src=x onerror="alert(1)">'},
    )
    data = json.loads(call.call_args.args[-1]["Template"]["TemplateData"])
    assert "<img" not in data["reason"]
    call.reset_mock()
    assert not ses.send_template("user@example.org", "update", "notice", {"body": "测" * 300})
    call.assert_not_called()


def test_smtp_deployments_preserve_existing_delivery(monkeypatch):
    monkeypatch.setattr(settings, "email_provider", "smtp")
    monkeypatch.setattr(settings, "smtp_user", "test-user")
    monkeypatch.setattr(settings, "smtp_password", "test-password")
    smtp = MagicMock()
    server = smtp.return_value.__enter__.return_value
    monkeypatch.setattr(email.smtplib, "SMTP", smtp)
    assert email.send_email("user@example.org", "subject", "<p>body</p>", "body")
    server.starttls.assert_called_once()
    server.login.assert_called_once_with("test-user", "test-password")
    server.send_message.assert_called_once()


@pytest.mark.parametrize("recipient", [None, "", "   "])
def test_account_without_email_never_calls_ses(configured, monkeypatch, recipient):
    call = Mock()
    monkeypatch.setattr(ses, "call_tencent_cloud", call)
    assert not email.send_email(recipient, "subject", "<p>body</p>", "body")
    call.assert_not_called()


def test_ses_failure_never_falls_back_to_smtp(configured, monkeypatch):
    call = Mock(side_effect=api.TencentCloudError("TransportError"))
    smtp = Mock()
    monkeypatch.setattr(ses, "call_tencent_cloud", call)
    monkeypatch.setattr(email.smtplib, "SMTP", smtp)
    assert not email.send_verification_email("user@example.org", "token", "https://api.example.org")
    assert call.call_count == 1
    smtp.assert_not_called()
