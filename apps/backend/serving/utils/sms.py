"""Select a verification-message provider without coupling account policy to it."""

from serving.config.settings import settings
from serving.utils.delivery import get_sms_provider


def is_sms_enabled() -> bool:
    """Require an explicit opt-in and a configured delivery provider."""
    if not settings.sms_auth_enabled:
        return False
    provider = get_sms_provider(settings.sms_provider)
    return provider is not None and provider.is_configured()


def send_verification_sms(phone: str, code: str) -> bool:
    """Dispatch one verification message using the selected provider."""
    if not is_sms_enabled():
        return False
    provider = get_sms_provider(settings.sms_provider)
    return provider is not None and provider.send(phone, code)
