"""Tencent Cloud delivery of mainland China phone verification codes."""

from __future__ import annotations

import re

from serving.integrations.tencent_cloud.api import TencentCloudError, call_tencent_cloud
from serving.integrations.tencent_cloud.config import settings
from serving.utils.logging import get_logger

logger = get_logger(__name__)

_MAINLAND_MOBILE_RE = re.compile(r"\+861[3-9][0-9]{9}")
_CODE_RE = re.compile(r"[0-9]{6}")


def is_configured() -> bool:
    """Return whether phone authentication has a complete delivery configuration."""
    return bool(
        settings.tencent_cloud_secret_id
        and settings.tencent_cloud_secret_key
        and settings.sms_sdk_app_id
        and settings.sms_sign_name
        and settings.sms_template_id
        and settings.sms_region
    )


def send_verification_sms(phone: str, code: str) -> bool:
    """Send a code using the approved template's code and minutes parameters."""
    if not is_configured():
        return False
    if not _MAINLAND_MOBILE_RE.fullmatch(phone) or not _CODE_RE.fullmatch(code):
        return False
    try:
        result = call_tencent_cloud(
            "sms",
            "SendSms",
            "2021-01-11",
            settings.sms_region,
            {
                "PhoneNumberSet": [phone],
                "SmsSdkAppId": settings.sms_sdk_app_id,
                "SignName": settings.sms_sign_name,
                "TemplateId": settings.sms_template_id,
                "TemplateParamSet": [code, str((settings.sms_otp_expire_seconds + 59) // 60)],
            },
        )
    except TencentCloudError as error:
        logger.warning("Tencent SMS request failed: %s", error)
        return False
    statuses = result.get("SendStatusSet")
    accepted = (
        isinstance(statuses, list)
        and len(statuses) == 1
        and isinstance(statuses[0], dict)
        and statuses[0].get("Code") == "Ok"
        and statuses[0].get("PhoneNumber") == phone
    )
    if not accepted:
        logger.warning("Tencent SMS did not accept the verification message")
    return accepted
