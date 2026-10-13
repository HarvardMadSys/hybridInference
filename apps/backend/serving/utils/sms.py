"""Select a verification-message provider without coupling account policy to it."""

from serving.config.settings import settings
from serving.utils import tencent_sms


def is_sms_enabled() -> bool:
    """Require an explicit opt-in and a configured delivery provider."""
    return bool(
        settings.sms_auth_enabled
        and settings.sms_provider == "tencent"
        and tencent_sms.is_configured()
    )


def send_verification_sms(phone: str, code: str) -> bool:
    """Dispatch one verification message using the selected provider."""
    if not is_sms_enabled():
        return False
    return tencent_sms.send_verification_sms(phone, code)
