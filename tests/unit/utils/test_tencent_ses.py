"""Contract and redaction checks for Tencent Cloud delivery adapters."""

import json
from pathlib import Path
from unittest.mock import MagicMock, Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from serving.config.settings import settings
from serving.utils import email, tencent_cloud, tencent_ses


@pytest.fixture
def configured(monkeypatch):
    """Use only synthetic credentials and approved-template stand-ins."""
    values = {
        "email_provider": "tencent_ses",
        "tencent_cloud_secret_id": "test-id",
        "tencent_cloud_secret_key": "test-secret",
        "tencent_cloud_token": "test-session-token",
        "tencent_ses_from_email": "noreply@mail.example.org",
        "tencent_ses_from_name": "Test service",
        "tencent_ses_template_verification": 101,
        "tencent_ses_template_password_reset": 102,
        "tencent_ses_template_approval": 103,
        "tencent_ses_template_rejection": 104,
        "tencent_ses_template_admin_signup": 105,
        "tencent_ses_template_notice": 106,
        "frontend_url": "https://console.example.org",
    }
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)


def test_signature_uses_utc_and_exact_payload_bytes():
    kwargs = {
        "service": "ses",
        "action": "SendEmail",
        "version": "2020-10-02",
        "region": "ap-guangzhou",
        "secret_id": "test-id",
        "secret_key": "test-secret",
        "timestamp": 1551113065,
        "token": "session-token",
    }
    headers = tencent_cloud.signed_headers(body=b'{"subject":"hello"}', **kwargs)
    assert "2019-02-25/ses/tc3_request" in headers["Authorization"]
    assert headers["X-TC-Timestamp"] == "1551113065"
    assert headers["X-TC-Token"] == "session-token"
    assert (
        headers["Authorization"]
        != tencent_cloud.signed_headers(body=b'{"subject": "hello"}', **kwargs)["Authorization"]
    )


def test_request_signs_transmitted_utf8_and_accepts_provider_response(configured, monkeypatch):
    captured = {}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return httpx.Response(
            200, json={"Response": {"MessageId": "accepted-id"}}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(tencent_cloud.httpx, "post", post)
    monkeypatch.setattr(tencent_cloud.time, "time", lambda: 1551113065)
    result = tencent_cloud.call_tencent_cloud(
        "ses", "SendEmail", "2020-10-02", "ap-guangzhou", {"Subject": "验证码"}
    )
    assert result["MessageId"] == "accepted-id"
    assert captured["url"] == "https://ses.tencentcloudapi.com/"
    assert captured["content"] == '{"Subject":"验证码"}'.encode()
    assert captured["headers"] == tencent_cloud.signed_headers(
        service="ses",
        action="SendEmail",
        version="2020-10-02",
        region="ap-guangzhou",
        body=captured["content"],
        secret_id="test-id",
        secret_key="test-secret",
        token="test-session-token",
        timestamp=1551113065,
    )


def test_provider_error_does_not_expose_recipient_or_otp(configured, monkeypatch):
    response = httpx.Response(
        200,
        json={
            "Response": {
                "Error": {
                    "Code": "AuthFailure.SignatureFailure",
                    "Message": "+8613800000000 code 123456 test-secret",
                }
            }
        },
        request=httpx.Request("POST", "https://sms.tencentcloudapi.com/"),
    )
    monkeypatch.setattr(tencent_cloud.httpx, "post", Mock(return_value=response))
    with pytest.raises(tencent_cloud.TencentCloudError, match=r"^AuthFailure\.SignatureFailure$"):
        tencent_cloud.call_tencent_cloud("sms", "SendSms", "2021-01-11", "ap-guangzhou", {})


def test_timeout_is_not_automatically_retried(configured, monkeypatch):
    post = Mock(side_effect=httpx.ReadTimeout("unknown delivery outcome"))
    monkeypatch.setattr(tencent_cloud.httpx, "post", post)
    with pytest.raises(tencent_cloud.TencentCloudError, match="TransportError"):
        tencent_cloud.call_tencent_cloud("sms", "SendSms", "2021-01-11", "ap-guangzhou", {})
    assert post.call_count == 1


@pytest.mark.parametrize(
    "function,args,template_id,variable",
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
    monkeypatch.setattr(tencent_ses, "call_tencent_cloud", call)
    assert function(*args)
    service, action, version, region, payload = call.call_args.args
    assert (service, action, version) == ("ses", "SendEmail", "2020-10-02")
    assert region == settings.tencent_ses_region
    assert payload["Template"]["TemplateID"] == template_id
    assert variable in json.loads(payload["Template"]["TemplateData"])
    assert payload["TriggerType"] == 1
    assert "Simple" not in payload
    assert payload["Destination"] == [args[0]]
    assert "url" not in json.loads(payload["Template"]["TemplateData"])


@pytest.mark.parametrize(
    "function,template_file,path",
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
    monkeypatch.setattr(tencent_ses, "call_tencent_cloud", call)
    token = 'token+value/&next=https://other.example/?x="value"'
    assert function("user@example.org", token, "https://api.example.org")
    data = json.loads(call.call_args.args[-1]["Template"]["TemplateData"])
    template = (
        Path(__file__).resolve().parents[3] / "config/examples/tencent-ses" / template_file
    ).read_text()
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
    monkeypatch.setattr(settings, "tencent_ses_template_verification", 0)
    smtp = Mock()
    monkeypatch.setattr(email.smtplib, "SMTP", smtp)
    assert not email.is_email_enabled()
    assert not email.send_verification_email("user@example.org", "token", "https://api.example.org")
    smtp.assert_not_called()


def test_template_values_escape_html_and_enforce_parameter_limit(configured, monkeypatch):
    call = Mock(return_value={"MessageId": "accepted-id"})
    monkeypatch.setattr(tencent_ses, "call_tencent_cloud", call)
    assert tencent_ses.send_template(
        "user@example.org",
        "update",
        "rejection",
        {"site_name": "Test", "reason": '<img src=x onerror="alert(1)">'},
    )
    data = json.loads(call.call_args.args[-1]["Template"]["TemplateData"])
    assert "<img" not in data["reason"]
    call.reset_mock()
    assert not tencent_ses.send_template(
        "user@example.org", "update", "notice", {"body": "测" * 300}
    )
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
    monkeypatch.setattr(tencent_ses, "call_tencent_cloud", call)
    assert not email.send_email(recipient, "subject", "<p>body</p>", "body")
    call.assert_not_called()


def test_ses_failure_never_falls_back_to_smtp(configured, monkeypatch):
    call = Mock(side_effect=tencent_cloud.TencentCloudError("TransportError"))
    smtp = Mock()
    monkeypatch.setattr(tencent_ses, "call_tencent_cloud", call)
    monkeypatch.setattr(email.smtplib, "SMTP", smtp)
    assert not email.send_verification_email("user@example.org", "token", "https://api.example.org")
    assert call.call_count == 1
    smtp.assert_not_called()
