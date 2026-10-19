"""Isolated extension registries for configuration boundary tests."""

from __future__ import annotations

import pytest

from serving.config import app_config, app_config_registry as registry
from serving.utils import delivery


@pytest.fixture
def extension_registry(monkeypatch):
    monkeypatch.setattr(registry, "_EXTENSION_ENTRIES", {})
    monkeypatch.setattr(delivery, "_email_providers", {})
    monkeypatch.setattr(delivery, "_sms_providers", {})
    registry.invalidate_config_registry()
    yield registry
    registry.invalidate_config_registry()


@pytest.fixture
def sample_delivery(extension_registry):
    def sms_required(context):
        return (
            context.value("SMS_AUTH_ENABLED").lower() in registry.TRUE_WORDS
            and context.value("SMS_PROVIDER") == "sample_sms"
        )

    def credentials_required(context):
        return sms_required(context) or context.value("EMAIL_PROVIDER") == "sample_email"

    def email_required(context):
        return (
            context.value("EMAIL_PROVIDER") == "sample_email"
            and context.email_verification_needed
            and not context.email_enabled
        )

    entries = [
        registry.ConfigEntry(
            key, "integrations", "Sample credential.", secret=True, required=credentials_required
        )
        for key in ("SAMPLE_DELIVERY_ID", "SAMPLE_DELIVERY_SECRET")
    ]
    entries += [
        registry.ConfigEntry(
            "SAMPLE_DELIVERY_TOKEN", "integrations", "Sample session.", secret=True
        ),
        registry.ConfigEntry(
            "SAMPLE_EMAIL_FROM", "email", "Sample sender.", required=email_required
        ),
        registry.ConfigEntry(
            "SAMPLE_EMAIL_VERIFICATION",
            "email",
            "Sample verification template.",
            type="int",
            minimum=1,
            required=email_required,
        ),
        registry.ConfigEntry(
            "SAMPLE_EMAIL_PASSWORD_RESET",
            "email",
            "Sample reset template.",
            type="int",
            minimum=1,
            required=email_required,
        ),
        registry.ConfigEntry(
            "SAMPLE_SMS_REGION",
            "phone",
            "Sample region.",
            default="sample-region",
            required=sms_required,
        ),
    ]
    entries += [
        registry.ConfigEntry(key, "phone", "Sample SMS field.", required=sms_required)
        for key in ("SAMPLE_SMS_APP_ID", "SAMPLE_SMS_SIGN_NAME", "SAMPLE_SMS_TEMPLATE_ID")
    ]
    registry.register_config_entries(entries)

    def has_values(keys):
        return all((app_config.config_value(key) or "").strip() for key in keys)

    credentials = ("SAMPLE_DELIVERY_ID", "SAMPLE_DELIVERY_SECRET")
    delivery.register_email_provider(
        "sample_email",
        is_configured=lambda: has_values(
            (
                *credentials,
                "SAMPLE_EMAIL_FROM",
                "SAMPLE_EMAIL_VERIFICATION",
                "SAMPLE_EMAIL_PASSWORD_RESET",
            )
        ),
        send=lambda message: True,
    )
    delivery.register_sms_provider(
        "sample_sms",
        is_configured=lambda: has_values(
            (
                *credentials,
                "SAMPLE_SMS_REGION",
                "SAMPLE_SMS_APP_ID",
                "SAMPLE_SMS_SIGN_NAME",
                "SAMPLE_SMS_TEMPLATE_ID",
            )
        ),
        send=lambda phone, code: True,
    )
