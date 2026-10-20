"""Tencent Cloud SES email and SMS delivery, off unless a deployment loads it.

Add ``serving.integrations.tencent_cloud`` to ``BACKEND_EXTENSIONS`` to offer
``EMAIL_PROVIDER=tencent_ses`` and ``SMS_PROVIDER=tencent`` and list their
settings. A deployment that does not load it sees neither.
"""

from __future__ import annotations


def register() -> None:
    """Register the Tencent Cloud settings and delivery providers."""
    from serving.config.app_config_registry import register_config_entries
    from serving.integrations.tencent_cloud import ses, sms
    from serving.integrations.tencent_cloud.config import ENTRIES
    from serving.utils.delivery import register_email_provider, register_sms_provider

    register_config_entries(ENTRIES)
    register_email_provider("tencent_ses", is_configured=ses.is_configured, send=ses.send)
    register_sms_provider(
        "tencent", is_configured=sms.is_configured, send=sms.send_verification_sms
    )
