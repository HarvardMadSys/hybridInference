"""Cloudflare Email Sending: the built-in transport behind EMAIL_PROVIDER=cloudflare."""

from __future__ import annotations

import logging
from unittest.mock import Mock

import httpx
import pytest

from serving.config.settings import settings
from serving.utils import cloudflare_email, delivery, email
from serving.utils.delivery import EmailMessage

ACCOUNT_ID = "0123456789abcdef0123456789abcdef"
TOKEN = "synthetic-cloudflare-token"
SEND_URL = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/email/sending/send"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "email_provider", "cloudflare")
    monkeypatch.setattr(settings, "cloudflare_account_id", ACCOUNT_ID)
    monkeypatch.setattr(settings, "cloudflare_email_api_token", TOKEN)
    monkeypatch.setattr(settings, "smtp_from_email", "noreply@example.org")
    monkeypatch.setattr(settings, "smtp_from_name", "Example")


def _answer(status: int = 200, **envelope) -> httpx.Response:
    return httpx.Response(status, json=envelope, request=httpx.Request("POST", SEND_URL))


def _delivered(address: str = "user@example.org") -> httpx.Response:
    return _answer(
        success=True,
        errors=[],
        messages=[],
        result={
            "message_id": "<id@example.org>",
            "delivered": [address],
            "queued": [],
            "permanent_bounces": [],
            "suppressed_recipients": [],
        },
    )


@pytest.fixture
def post(monkeypatch):
    post = Mock(return_value=_delivered())
    monkeypatch.setattr(cloudflare_email.httpx, "post", post)
    return post


def _message(**fields) -> EmailMessage:
    values = {
        "to_email": "user@example.org",
        "subject": "Subject",
        "html_body": "<p>Body</p>",
        "text_body": "Body",
    }
    values.update(fields)
    return EmailMessage(**values)


def test_a_message_is_one_authenticated_request_to_the_account(configured, post):
    assert cloudflare_email.send(_message())

    post.assert_called_once()
    assert post.call_args.args == (SEND_URL,)
    assert post.call_args.kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    assert post.call_args.kwargs["timeout"] == cloudflare_email.TIMEOUT_SECONDS
    assert post.call_args.kwargs["json"] == {
        "from": {"address": "noreply@example.org", "name": "Example"},
        "to": "user@example.org",
        "subject": "Subject",
        "html": "<p>Body</p>",
        "text": "Body",
    }


def test_the_sender_name_and_text_body_are_left_out_when_empty(configured, post, monkeypatch):
    monkeypatch.setattr(settings, "smtp_from_name", "")

    assert cloudflare_email.send(_message(text_body=None))

    body = post.call_args.kwargs["json"]
    assert body["from"] == {"address": "noreply@example.org"}
    assert "text" not in body


def test_a_queued_message_counts_as_sent(configured, post):
    post.return_value = _answer(
        success=True, errors=[], messages=[], result={"delivered": [], "queued": ["u@x.org"]}
    )

    assert cloudflare_email.send(_message())


@pytest.mark.parametrize(
    "result,reason",
    [
        ({"delivered": [], "queued": [], "permanent_bounces": ["user@example.org"]}, "bounced"),
        (
            {"delivered": [], "queued": [], "suppressed_recipients": ["user@example.org"]},
            "suppression",
        ),
        ({}, "neither delivery nor queueing"),
        (None, "neither delivery nor queueing"),
    ],
)
def test_an_accepted_request_that_delivers_nothing_fails(configured, post, caplog, result, reason):
    post.return_value = _answer(success=True, errors=[], messages=[], result=result)

    with caplog.at_level(logging.ERROR):
        assert not cloudflare_email.send(_message())

    assert reason in caplog.text


def test_a_refusal_fails_and_logs_only_its_error_codes(configured, post, caplog):
    post.return_value = _answer(
        401,
        success=False,
        errors=[
            {"code": 10101, "message": "email.sending.error.authentication.unauthorized"},
            {"code": 10001, "message": "echoed <p>Body</p> and the token " + TOKEN},
        ],
        messages=[],
        result=None,
    )

    with caplog.at_level(logging.ERROR):
        assert not cloudflare_email.send(_message())

    assert "HTTP 401" in caplog.text
    assert "10101 email.sending.error.authentication.unauthorized" in caplog.text
    assert "10001" in caplog.text
    assert "echoed" not in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            502, text="<html>Bad gateway</html>", request=httpx.Request("POST", SEND_URL)
        ),
        httpx.Response(
            200, json=["not", "an", "envelope"], request=httpx.Request("POST", SEND_URL)
        ),
        httpx.Response(
            200,
            json={"result": {"delivered": ["u@x.org"]}},
            request=httpx.Request("POST", SEND_URL),
        ),
    ],
)
def test_an_answer_that_is_not_a_success_envelope_fails(configured, post, response):
    post.return_value = response

    assert not cloudflare_email.send(_message())


def test_a_failed_connection_fails_without_raising(configured, post, caplog):
    post.side_effect = httpx.ConnectTimeout("timed out")

    with caplog.at_level(logging.ERROR):
        assert not cloudflare_email.send(_message())

    assert "ConnectTimeout" in caplog.text


@pytest.mark.parametrize(
    "account_id,token", [("", TOKEN), (ACCOUNT_ID, ""), ("  ", TOKEN), (ACCOUNT_ID, "   ")]
)
def test_without_both_credentials_nothing_is_sent(configured, post, monkeypatch, account_id, token):
    monkeypatch.setattr(settings, "cloudflare_account_id", account_id)
    monkeypatch.setattr(settings, "cloudflare_email_api_token", token)

    assert not cloudflare_email.is_configured()
    assert not cloudflare_email.send(_message())
    post.assert_not_called()


def test_credentials_are_read_on_every_send(configured, post, monkeypatch):
    assert cloudflare_email.send(_message())
    monkeypatch.setattr(settings, "cloudflare_email_api_token", "rotated-token")

    assert cloudflare_email.send(_message())

    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer rotated-token"}


# --- selected through EMAIL_PROVIDER ------------------------------------------


@pytest.fixture
def smtp(monkeypatch):
    # Configured, so a fallback to SMTP would be possible -- and must not happen.
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.org")
    monkeypatch.setattr(settings, "smtp_user", "test-user")
    monkeypatch.setattr(settings, "smtp_password", "test-password")
    smtp = Mock()
    monkeypatch.setattr(email.smtplib, "SMTP", smtp)
    monkeypatch.setattr(email.smtplib, "SMTP_SSL", smtp)
    return smtp


def test_cloudflare_is_a_builtin_choice_no_extension_can_take():
    assert "cloudflare" in delivery.email_provider_names()
    with pytest.raises(ValueError, match="reserved"):
        delivery.register_email_provider("cloudflare", is_configured=lambda: True, send=Mock())


def test_the_selected_cloudflare_transport_carries_account_mail(configured, post, smtp):
    assert email.is_email_enabled()
    assert email.send_verification_email("user@example.org", "token", "https://api.example.org")

    smtp.assert_not_called()
    body = post.call_args.kwargs["json"]
    assert body["to"] == "user@example.org"
    # The rendered bodies, not a template: the link is in them.
    assert "token" in body["html"]
    assert "token" in body["text"]


def test_an_unconfigured_cloudflare_transport_never_falls_back_to_smtp(
    configured, post, smtp, monkeypatch
):
    monkeypatch.setattr(settings, "cloudflare_email_api_token", "")

    assert not email.is_email_enabled()
    assert not email.send_email("user@example.org", "subject", "<p>body</p>", "body")

    post.assert_not_called()
    smtp.assert_not_called()


def test_a_cloudflare_refusal_never_falls_back_to_smtp(configured, post, smtp):
    post.return_value = _answer(429, success=False, errors=[{"code": 10004}], result=None)

    assert not email.send_email("user@example.org", "subject", "<p>body</p>", "body")

    smtp.assert_not_called()


def test_smtp_credentials_do_not_enable_cloudflare_mail(configured, smtp, monkeypatch):
    monkeypatch.setattr(settings, "cloudflare_account_id", "")

    assert not email.is_email_enabled()


# --- what a failed send tells the administrator ---------------------------------


def test_a_delivered_message_has_no_reason(configured, post):
    assert cloudflare_email.deliver(_message()) == delivery.EmailDelivery(True)


@pytest.mark.parametrize(
    "status,code,advice",
    [
        (401, 10101, "did not accept the API token"),
        (403, 10102, "Email Sending: Edit"),
        (403, 10105, "not entitled"),
        (403, 10208, "verified destination addresses"),
        (404, 10000, "Check the account ID"),
        (429, 10004, "rate-limiting"),
        (400, 10202, "onboarded under Email Service > Email Sending"),
    ],
)
def test_a_documented_refusal_says_what_to_fix(configured, post, status, code, advice):
    post.return_value = _answer(
        status, success=False, errors=[{"code": code, "message": "x.y"}], result=None
    )

    sent = cloudflare_email.deliver(_message())

    assert sent.sent is False
    assert advice in sent.reason
    assert f"HTTP {status}" in sent.reason
    assert str(code) in sent.reason


@pytest.mark.parametrize(
    "status,advice",
    [
        (401, "did not accept the API token"),
        (403, "Email Sending: Edit"),
        (418, "Check the sender address"),
        (503, "Try again in a few minutes"),
    ],
)
def test_an_undocumented_refusal_is_explained_by_its_status(configured, post, status, advice):
    post.return_value = _answer(status, success=False, errors=[{"code": 19999}], result=None)

    sent = cloudflare_email.deliver(_message())

    assert advice in sent.reason
    assert "19999" in sent.reason


def test_a_refusal_never_repeats_the_token_even_as_a_bare_identifier(configured, post, caplog):
    # TOKEN looks like an identifier, so only the withholding keeps it out.
    post.return_value = _answer(
        401, success=False, errors=[{"code": 10101, "message": TOKEN}], result=None
    )

    with caplog.at_level(logging.ERROR):
        sent = cloudflare_email.deliver(_message())

    assert TOKEN not in sent.reason
    assert TOKEN not in caplog.text
    assert "10101" in sent.reason


def test_an_unreachable_api_is_reported_with_the_host_to_allow(configured, post):
    post.side_effect = httpx.ConnectTimeout("timed out")

    sent = cloudflare_email.deliver(_message())

    assert sent.sent is False
    assert "ConnectTimeout" in sent.reason
    assert "api.cloudflare.com" in sent.reason


def test_a_bounced_recipient_is_reported(configured, post):
    post.return_value = _answer(
        success=True,
        errors=[],
        messages=[],
        result={"delivered": [], "queued": [], "permanent_bounces": ["user@example.org"]},
    )

    assert "bounced permanently" in cloudflare_email.deliver(_message()).reason


def test_deliver_email_reports_cloudflares_reason(configured, post, smtp):
    post.return_value = _answer(403, success=False, errors=[{"code": 10102}], result=None)

    sent = email.deliver_email("user@example.org", "subject", "<p>body</p>", "body")

    assert sent.sent is False
    assert "Email Sending: Edit" in sent.reason
    smtp.assert_not_called()
