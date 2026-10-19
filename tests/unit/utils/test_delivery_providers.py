"""Transport boundaries keep extension-owned delivery out of base deployments."""

from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock, Mock
from urllib.parse import quote

import pytest

from serving.config.settings import settings
from serving.utils import delivery, email, sms


@pytest.fixture(autouse=True)
def isolated_delivery_providers(monkeypatch):
    monkeypatch.setattr(delivery, "_email_providers", {})
    monkeypatch.setattr(delivery, "_sms_providers", {})
    delivery._invalidate_config_choices()
    yield
    delivery._invalidate_config_choices()


def test_base_deployment_has_only_builtin_provider_choices():
    assert delivery.email_provider_names() == ("smtp",)
    assert delivery.sms_provider_names() == ("none",)


@pytest.mark.parametrize("register", [delivery.register_email_provider, delivery.register_sms_provider])
@pytest.mark.parametrize("name", ["smtp", "none", "", "Acme", "acme-provider", "acme.provider"])
def test_provider_registration_rejects_reserved_and_invalid_names(register, name):
    with pytest.raises(ValueError):
        register(name, is_configured=lambda: True, send=Mock(return_value=True))


@pytest.mark.parametrize("register", [delivery.register_email_provider, delivery.register_sms_provider])
def test_provider_registration_cannot_replace_existing_transport(register):
    register("example", is_configured=lambda: True, send=Mock(return_value=True))
    with pytest.raises(ValueError, match="already registered"):
        register("example", is_configured=lambda: True, send=Mock(return_value=True))


@pytest.mark.parametrize("register", [delivery.register_email_provider, delivery.register_sms_provider])
@pytest.mark.parametrize("field", ["is_configured", "send"])
def test_provider_registration_rejects_async_callbacks(register, field):
    async def asynchronous(*args):
        return True

    callbacks = {"is_configured": lambda: True, "send": Mock(return_value=True)}
    callbacks[field] = asynchronous
    with pytest.raises(TypeError, match="synchronous"):
        register("example", **callbacks)


def test_provider_registration_refreshes_cached_configuration_choices():
    from serving.config.app_config_registry import static_entries

    def choices(key):
        return next(entry.choices for entry in static_entries() if entry.key == key)

    assert choices("EMAIL_PROVIDER") == ("smtp",)
    assert choices("SMS_PROVIDER") == ("none",)
    delivery.register_email_provider("example_mail", is_configured=lambda: True, send=Mock())
    delivery.register_sms_provider("example_sms", is_configured=lambda: True, send=Mock())
    assert choices("EMAIL_PROVIDER") == ("smtp", "example_mail")
    assert choices("SMS_PROVIDER") == ("none", "example_sms")


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
    message = server.send_message.call_args.args[0]
    assert message["To"] == "user@example.org"
    assert message["Subject"] == "subject"
    assert [part.get_content_type() for part in message.get_payload()] == ["text/plain", "text/html"]


@pytest.mark.parametrize("provider", ["smtp", "missing_extension"])
def test_unconfigured_email_never_attempts_delivery(monkeypatch, provider):
    monkeypatch.setattr(settings, "email_provider", provider)
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "smtp_password", "")
    smtp = Mock()
    monkeypatch.setattr(email.smtplib, "SMTP", smtp)

    assert not email.is_email_enabled()
    assert not email.send_email("user@example.org", "subject", "<p>body</p>", "body")
    smtp.assert_not_called()


@pytest.mark.parametrize("registered,configured", [(False, False), (True, False), (True, True)])
def test_email_extension_failure_never_falls_back_to_smtp(monkeypatch, registered, configured):
    monkeypatch.setattr(settings, "email_provider", "example_mail")
    monkeypatch.setattr(settings, "smtp_user", "test-user")
    monkeypatch.setattr(settings, "smtp_password", "test-password")
    smtp = Mock()
    send = Mock(return_value=False)
    monkeypatch.setattr(email.smtplib, "SMTP", smtp)
    if registered:
        delivery.register_email_provider("example_mail", is_configured=lambda: configured, send=send)

    assert not email.send_email("user@example.org", "subject", "<p>body</p>", "body")
    assert send.call_count == int(registered and configured)
    smtp.assert_not_called()


@pytest.mark.parametrize("recipient", [None, "", "   "])
def test_missing_address_never_reaches_extension(monkeypatch, recipient):
    monkeypatch.setattr(settings, "email_provider", "example_mail")
    send = Mock(return_value=True)
    delivery.register_email_provider("example_mail", is_configured=lambda: True, send=send)

    assert not email.send_email(recipient, "subject", "<p>body</p>", "body")
    send.assert_not_called()


@pytest.mark.parametrize(
    "function,args,template,expected_data",
    [
        (
            email.send_verification_email,
            ("user@example.org", "token+/&", "https://api.example.org"),
            "verification",
            {"token": quote("token+/&", safe="")},
        ),
        (
            email.send_password_reset_email,
            ("user@example.org", "token+/&", "https://api.example.org"),
            "password_reset",
            {"token": quote("token+/&", safe="")},
        ),
        (email.send_approval_email, ("user@example.org",), "approval", {}),
        (email.send_rejection_email, ("user@example.org", "reason"), "rejection", {"reason": "reason"}),
        (
            email.send_new_registration_admin_email,
            ("admin@example.org", "user@example.org", "User", "user-id"),
            "admin_signup",
            {"identifier": "user@example.org"},
        ),
    ],
)
def test_account_events_preserve_rendered_bodies_and_template_data(
    monkeypatch, function, args, template, expected_data
):
    monkeypatch.setattr(settings, "email_provider", "example_mail")
    monkeypatch.setattr(settings, "frontend_url", "https://console.example.org")
    send = Mock(return_value=True)
    delivery.register_email_provider("example_mail", is_configured=lambda: True, send=send)

    assert function(*args)
    message = send.call_args.args[0]
    assert isinstance(message, delivery.EmailMessage)
    assert message.to_email == args[0]
    assert message.html_body and message.text_body
    assert message.template == template
    assert message.template_data["site_name"]
    for key, value in expected_data.items():
        assert message.template_data[key] == value


@pytest.mark.parametrize(
    "function,args",
    [
        (email.send_account_deleted_email, ("user@example.org",)),
        (email.send_email_changed_email, ("old@example.org", "new@example.org")),
        (email.send_password_changed_email, ("user@example.org",)),
    ],
)
def test_notices_pass_rendered_body_to_extension(monkeypatch, function, args):
    monkeypatch.setattr(settings, "email_provider", "example_mail")
    send = Mock(return_value=True)
    delivery.register_email_provider("example_mail", is_configured=lambda: True, send=send)

    assert function(*args)
    message = send.call_args.args[0]
    assert message.to_email == args[0]
    assert message.template == "notice"
    assert message.template_data is None
    assert message.html_body and message.text_body


def test_email_message_copies_and_freezes_event_metadata():
    data = {"token": "original"}
    message = delivery.EmailMessage("user@example.org", "subject", "<p>body</p>", template_data=data)
    data["token"] = "changed"
    assert message.template_data["token"] == "original"
    with pytest.raises(TypeError):
        message.template_data["token"] = "changed"
    with pytest.raises(FrozenInstanceError):
        message.subject = "changed"


@pytest.mark.parametrize(
    "enabled,provider,registered,configured,expected",
    [
        (False, "example_sms", True, True, False),
        (True, "none", True, True, False),
        (True, "example_sms", False, True, False),
        (True, "example_sms", True, False, False),
        (True, "example_sms", True, True, True),
    ],
)
def test_sms_requires_opt_in_and_selected_configured_extension(
    monkeypatch, enabled, provider, registered, configured, expected
):
    monkeypatch.setattr(settings, "sms_auth_enabled", enabled)
    monkeypatch.setattr(settings, "sms_provider", provider)
    send = Mock(return_value=True)
    if registered:
        delivery.register_sms_provider("example_sms", is_configured=lambda: configured, send=send)

    assert sms.is_sms_enabled() is expected
    assert sms.send_verification_sms("+12025550123", "123456") is expected
    if expected:
        send.assert_called_once_with("+12025550123", "123456")
    else:
        send.assert_not_called()


def test_sms_provider_failure_is_returned_without_retry(monkeypatch):
    monkeypatch.setattr(settings, "sms_auth_enabled", True)
    monkeypatch.setattr(settings, "sms_provider", "example_sms")
    send = Mock(return_value=False)
    delivery.register_sms_provider("example_sms", is_configured=lambda: True, send=send)

    assert not sms.send_verification_sms("+12025550123", "123456")
    send.assert_called_once_with("+12025550123", "123456")
