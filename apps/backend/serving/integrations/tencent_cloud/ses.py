"""Approved Tencent SES templates for transactional account emails."""

from __future__ import annotations

import html
import json
import re
from typing import TYPE_CHECKING

from serving.config.site_identity import get_site_identity
from serving.integrations.tencent_cloud.api import TencentCloudError, call_tencent_cloud
from serving.integrations.tencent_cloud.config import settings
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from serving.utils.delivery import EmailMessage

logger = get_logger(__name__)

#: Tencent SES documents this bound on the serialized template variables.
TEMPLATE_DATA_LIMIT_BYTES = 800


def is_configured() -> bool:
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
    if not is_configured() or not template_id:
        logger.warning("Tencent SES template is unavailable: %s", kind)
        return False
    # Templates are HTML: escape user-supplied text before interpolation.
    template_data = json.dumps(
        {key: html.escape(value, quote=True) for key, value in data.items()},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(template_data.encode("utf-8")) > TEMPLATE_DATA_LIMIT_BYTES:
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


def send(message: EmailMessage) -> bool:
    """Adapt generic account events to reviewed Tencent SES templates."""
    data = message.template_data
    if data is None:
        if message.text_body is None:
            logger.warning("Tencent SES notices require a plain text body")
            return False
        data = {"site_name": get_site_identity().name, "body": message.text_body}
    return send_template(message.to_email, message.subject, message.template, dict(data))
