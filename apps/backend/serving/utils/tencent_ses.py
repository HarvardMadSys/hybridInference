"""Approved Tencent SES templates for transactional account emails."""

import html
import json
import re

from serving.config.settings import settings
from serving.utils.logging import get_logger
from serving.utils.tencent_cloud import TencentCloudError, call_tencent_cloud

logger = get_logger(__name__)


def is_ses_enabled() -> bool:
    """Require credentials, an approved sender and the two account templates."""
    return bool(
        settings.tencent_cloud_secret_id
        and settings.tencent_cloud_secret_key
        and re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", settings.tencent_ses_from_email)
        and settings.tencent_ses_template_verification
        and settings.tencent_ses_template_password_reset
    )


def send_template(to_email: str, subject: str, kind: str, data: dict[str, str]) -> bool:
    """Send scalar template data; never submit arbitrary HTML as a variable."""
    template_ids = {
        "verification": settings.tencent_ses_template_verification,
        "password_reset": settings.tencent_ses_template_password_reset,
        "approval": settings.tencent_ses_template_approval,
        "rejection": settings.tencent_ses_template_rejection,
        "admin_signup": settings.tencent_ses_template_admin_signup,
        "notice": settings.tencent_ses_template_notice,
    }
    template_id = template_ids.get(kind)
    if not is_ses_enabled() or not template_id:
        logger.warning("Tencent SES template is unavailable: %s", kind)
        return False
    # Templates are HTML. Escape user-supplied text before interpolation, and
    # keep the serialized variables within the provider's documented bound.
    template_data = json.dumps(
        {key: html.escape(value, quote=True) for key, value in data.items()},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(template_data.encode("utf-8")) > 800:
        logger.warning("Tencent SES template parameters exceed the size limit")
        return False
    sender_name = settings.tencent_ses_from_name
    if any(char in sender_name for char in "\r\n:<>"):
        logger.warning("Tencent SES sender name is invalid")
        return False
    sender = settings.tencent_ses_from_email
    if sender_name:
        sender = f"{sender_name} <{sender}>"
    try:
        result = call_tencent_cloud(
            "ses",
            "SendEmail",
            "2020-10-02",
            settings.tencent_ses_region,
            {
                "FromEmailAddress": sender,
                "Destination": [to_email],
                "Subject": subject,
                "Template": {"TemplateID": template_id, "TemplateData": template_data},
                "TriggerType": 1,
                "Unsubscribe": "0",
            },
        )
        return bool(result.get("MessageId"))
    except TencentCloudError as error:
        logger.warning("Tencent SES request failed: %s", error)
        return False
