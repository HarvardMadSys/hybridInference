"""Tencent Cloud delivery settings, stored and resolved like the gateway's own."""

from __future__ import annotations

from serving.config.app_config import config_value
from serving.config.app_config_registry import TRUE_WORDS, ConfigEntry, RequirementContext
from serving.config.settings import settings as core_settings


def _sms_required(context: RequirementContext) -> bool:
    return (
        context.value("SMS_AUTH_ENABLED").strip().lower() in TRUE_WORDS
        and context.value("SMS_PROVIDER") == "tencent"
    )


def _credentials_required(context: RequirementContext) -> bool:
    return _sms_required(context) or context.value("EMAIL_PROVIDER") == "tencent_ses"


def _ses_required(context: RequirementContext) -> bool:
    return (
        context.value("EMAIL_PROVIDER") == "tencent_ses"
        and context.email_verification_needed
        and not context.email_enabled
    )


def _template(key: str, description: str, *, required: bool = False) -> ConfigEntry:
    # 0 has always meant "no template": stored zeros must keep booting.
    return ConfigEntry(
        key,
        "email",
        description,
        type="int",
        minimum=0,
        required=_ses_required if required else False,
    )


ENTRIES = (
    ConfigEntry(
        "TENCENT_CLOUD_SECRET_ID",
        "integrations",
        "Tencent Cloud CAM credential ID.",
        secret=True,
        required=_credentials_required,
    ),
    ConfigEntry(
        "TENCENT_CLOUD_SECRET_KEY",
        "integrations",
        "Tencent Cloud CAM credential secret.",
        secret=True,
        required=_credentials_required,
    ),
    ConfigEntry(
        "TENCENT_CLOUD_TOKEN",
        "integrations",
        "Optional temporary CAM session token.",
        secret=True,
    ),
    ConfigEntry(
        "TENCENT_SES_REGION",
        "email",
        "Tencent SES resource region.",
        default="ap-guangzhou",
        choices=("ap-guangzhou", "ap-hongkong"),
        case_sensitive=True,
    ),
    ConfigEntry(
        "TENCENT_SES_FROM_EMAIL",
        "email",
        "Approved Tencent SES sender address.",
        required=_ses_required,
    ),
    ConfigEntry("TENCENT_SES_FROM_NAME", "email", "Display name for Tencent SES mail."),
    _template(
        "TENCENT_SES_TEMPLATE_VERIFICATION", "Approved verification template ID.", required=True
    ),
    _template(
        "TENCENT_SES_TEMPLATE_PASSWORD_RESET",
        "Approved password reset template ID.",
        required=True,
    ),
    _template("TENCENT_SES_TEMPLATE_APPROVAL", "Approved account approval template ID."),
    _template("TENCENT_SES_TEMPLATE_REJECTION", "Approved account rejection template ID."),
    _template("TENCENT_SES_TEMPLATE_ADMIN_SIGNUP", "Approved admin signup notice template ID."),
    _template("TENCENT_SES_TEMPLATE_NOTICE", "Approved short plain-text notice template ID."),
    ConfigEntry(
        "SMS_REGION",
        "phone",
        "Tencent SMS resource region.",
        default="ap-guangzhou",
        required=_sms_required,
    ),
    ConfigEntry("SMS_SDK_APP_ID", "phone", "Tencent SMS application ID.", required=_sms_required),
    ConfigEntry(
        "SMS_SIGN_NAME", "phone", "Approved Tencent SMS signature text.", required=_sms_required
    ),
    ConfigEntry(
        "SMS_TEMPLATE_ID",
        "phone",
        "Approved Tencent SMS code and minutes template ID.",
        required=_sms_required,
    ),
)
_BY_FIELD = {entry.key.lower(): entry for entry in ENTRIES}


class DeliverySettings:
    """Resolve on every send, so a database change or credential rotation applies."""

    def __getattr__(self, name: str) -> str | int | None:
        if name == "sms_otp_expire_seconds":
            return core_settings.sms_otp_expire_seconds
        entry = _BY_FIELD.get(name)
        if entry is None:
            raise AttributeError(name)
        value = config_value(entry.key, entry.default)
        if entry.type == "int":
            return int(value) if (value or "").strip() else None
        return value or ""


settings = DeliverySettings()
