"""Compatibility names moved out of the core in the 2026-10 delivery split.

Old app_config rows lack ownership metadata. Keep them stored for the owning
extension to reclaim, but never reinterpret these former built-ins as arbitrary
custom settings in distributions that have not loaded that extension. This is
migration metadata only: it registers no providers, values or configuration.
"""

RETIRED_EXTENSION_KEYS: frozenset[str] = frozenset(
    {
        "TENCENT_CLOUD_SECRET_ID",
        "TENCENT_CLOUD_SECRET_KEY",
        "TENCENT_CLOUD_TOKEN",
        "TENCENT_SES_REGION",
        "TENCENT_SES_FROM_EMAIL",
        "TENCENT_SES_FROM_NAME",
        "TENCENT_SES_TEMPLATE_VERIFICATION",
        "TENCENT_SES_TEMPLATE_PASSWORD_RESET",
        "TENCENT_SES_TEMPLATE_APPROVAL",
        "TENCENT_SES_TEMPLATE_REJECTION",
        "TENCENT_SES_TEMPLATE_ADMIN_SIGNUP",
        "TENCENT_SES_TEMPLATE_NOTICE",
        "SMS_REGION",
        "SMS_SDK_APP_ID",
        "SMS_SIGN_NAME",
        "SMS_TEMPLATE_ID",
    }
)
