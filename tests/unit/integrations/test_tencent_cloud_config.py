"""Tencent Cloud settings behave like built-in ones once the integration loads."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from serving.config import app_config, app_config_registry as registry
from serving.config.settings import get_settings
from serving.integrations.tencent_cloud import config, ses, sms as tencent_sms
from serving.utils import delivery, email, sms
from tests.fixtures.app_config_store import FakeAppConfigStore, row


async def boot(store):
    await app_config.load(store)
    app_config.attach(store)


def test_loading_offers_tencent_providers_and_lists_their_settings():
    assert delivery.email_provider_names() == ("smtp", "tencent_ses")
    assert delivery.sms_provider_names() == ("none", "tencent")
    keys = {entry.key for entry in registry.static_entries()}
    assert {entry.key for entry in config.ENTRIES} <= keys
    assert registry.static_entry("EMAIL_PROVIDER").choices == ("smtp", "tencent_ses")


@pytest.mark.asyncio
async def test_ses_existing_keys_masking_and_live_template_refresh(clean_config, monkeypatch):
    store = FakeAppConfigStore([row("EMAIL_PROVIDER", "tencent_ses")])
    await boot(store)
    missing = app_config.get_config_health().missing
    assert "SMTP_PASSWORD" not in missing
    assert "TENCENT_CLOUD_SECRET_KEY" in missing
    assert "TENCENT_SES_TEMPLATE_VERIFICATION" in missing
    assert not email.is_email_enabled()
    await app_config.update(
        {
            "TENCENT_CLOUD_SECRET_ID": "synthetic-id",
            "TENCENT_CLOUD_SECRET_KEY": "synthetic-secret",
            "TENCENT_CLOUD_TOKEN": "synthetic-session",
            "TENCENT_SES_FROM_EMAIL": "noreply@example.org",
            "TENCENT_SES_TEMPLATE_VERIFICATION": 101,
            "TENCENT_SES_TEMPLATE_PASSWORD_RESET": 102,
        },
        updated_by="admin",
    )
    assert email.is_email_enabled()
    description = str(app_config.describe())
    assert "synthetic-secret" not in description
    assert "synthetic-session" not in description
    call = Mock(return_value={"MessageId": "accepted"})
    monkeypatch.setattr(ses, "call_tencent_cloud", call)
    assert email.send_verification_email("user@example.org", "token", "https://api.example.org")
    assert call.call_args.args[-1]["Template"]["TemplateID"] == 101
    # Another worker updates the database; the regular refresh picks it up.
    store.rows["TENCENT_SES_TEMPLATE_VERIFICATION"] = row(
        "TENCENT_SES_TEMPLATE_VERIFICATION", "201"
    )
    await app_config.refresh()
    assert email.send_verification_email("user@example.org", "token", "https://api.example.org")
    assert call.call_args.args[-1]["Template"]["TemplateID"] == 201
    await app_config.delete("TENCENT_SES_TEMPLATE_VERIFICATION", updated_by="admin")
    assert not email.is_email_enabled()


@pytest.mark.asyncio
async def test_sms_database_switch_overrides_environment_and_survives_restart(
    clean_config, monkeypatch
):
    monkeypatch.setenv("SMS_AUTH_ENABLED", "true")
    monkeypatch.setenv("TENCENT_CLOUD_SECRET_KEY", "environment-secret")
    store = FakeAppConfigStore(
        [
            row("SMS_AUTH_ENABLED", "false"),
            row("SMS_PROVIDER", "tencent"),
            row("TENCENT_CLOUD_SECRET_ID", "stored-id"),
            row("TENCENT_CLOUD_SECRET_KEY", "stored-secret", secret=True),
            row("TENCENT_CLOUD_TOKEN", "stored-session", secret=True),
            row("SMS_SDK_APP_ID", "1400000000"),
            row("SMS_SIGN_NAME", "测试公司"),
            row("SMS_TEMPLATE_ID", "201"),
        ]
    )
    await boot(store)
    assert not sms.is_sms_enabled()
    assert config.settings.tencent_cloud_secret_key == "stored-secret"
    await app_config.update({"SMS_AUTH_ENABLED": True}, updated_by="admin")
    assert sms.is_sms_enabled()
    assert "stored-secret" not in str(app_config.describe())
    assert "stored-session" not in str(app_config.describe())
    call = Mock(return_value={"SendStatusSet": [{"Code": "Ok", "PhoneNumber": "+8613800000000"}]})
    monkeypatch.setattr(tencent_sms, "call_tencent_cloud", call)
    assert sms.send_verification_sms("+8613800000000", "123456")
    assert call.call_args.args[-1]["TemplateParamSet"] == ["123456", "5"]
    app_config.reset_state()
    await boot(store)
    assert sms.is_sms_enabled()
    assert config.settings.tencent_cloud_secret_key == "stored-secret"


@pytest.mark.parametrize(
    ("sms_enabled", "sms_provider", "email_provider", "expected_sms", "expected_credentials"),
    [
        ("false", "tencent", "smtp", False, False),
        ("true", "none", "smtp", False, False),
        ("true", "tencent", "smtp", True, True),
        ("false", "none", "tencent_ses", False, True),
    ],
)
def test_requirements_are_scoped_to_selected_tencent_transports(
    sms_enabled, sms_provider, email_provider, expected_sms, expected_credentials
):
    entries = {entry.key: entry for entry in config.ENTRIES}
    values = {
        "SMS_AUTH_ENABLED": sms_enabled,
        "SMS_PROVIDER": sms_provider,
        "EMAIL_PROVIDER": email_provider,
    }
    context = registry.RequirementContext(False, True, False, lambda key: values.get(key, ""))
    for key in ("SMS_REGION", "SMS_SDK_APP_ID", "SMS_SIGN_NAME", "SMS_TEMPLATE_ID"):
        assert entries[key].required(context) is expected_sms
    for key in ("TENCENT_CLOUD_SECRET_ID", "TENCENT_CLOUD_SECRET_KEY"):
        assert entries[key].required(context) is expected_credentials
    assert entries["TENCENT_CLOUD_TOKEN"].required is False


@pytest.mark.asyncio
async def test_ses_region_preserves_exact_provider_region_spelling(clean_config):
    await boot(FakeAppConfigStore())
    with pytest.raises(app_config.ConfigUpdateError):
        await app_config.update({"TENCENT_SES_REGION": "AP-GUANGZHOU"}, updated_by="admin")
    await app_config.update({"TENCENT_SES_REGION": "ap-hongkong"}, updated_by="admin")
    assert config.settings.tencent_ses_region == "ap-hongkong"


@pytest.mark.asyncio
@pytest.mark.parametrize("required_ids", [(0, 0), (101, 102)])
async def test_legacy_zero_template_values_boot_and_optional_templates_stay_disabled(
    clean_config, monkeypatch, required_ids
):
    values = {
        "EMAIL_PROVIDER": "tencent_ses",
        "TENCENT_CLOUD_SECRET_ID": "synthetic-id",
        "TENCENT_CLOUD_SECRET_KEY": "synthetic-secret",
        "TENCENT_SES_FROM_EMAIL": "noreply@example.org",
        "TENCENT_SES_TEMPLATE_VERIFICATION": str(required_ids[0]),
        "TENCENT_SES_TEMPLATE_PASSWORD_RESET": str(required_ids[1]),
        "TENCENT_SES_TEMPLATE_APPROVAL": "0",
        "TENCENT_SES_TEMPLATE_REJECTION": "0",
        "TENCENT_SES_TEMPLATE_ADMIN_SIGNUP": "0",
        "TENCENT_SES_TEMPLATE_NOTICE": "0",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    current = get_settings()
    monkeypatch.setattr(email, "settings", current)
    await boot(FakeAppConfigStore())
    app_config.require_valid_environment()
    assert email.is_email_enabled() is bool(required_ids[0] and required_ids[1])
    call = Mock(return_value={"MessageId": "accepted"})
    monkeypatch.setattr(ses, "call_tencent_cloud", call)
    assert not email.send_approval_email("user@example.org")
    assert not email.send_email("user@example.org", "notice", "<p>text</p>", "text")
    call.assert_not_called()
