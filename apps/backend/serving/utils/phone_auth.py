"""Canonical phone identifiers and secret-bound verification code digests."""

import hashlib
import hmac
import re

from serving.config.settings import settings
from serving.utils.jwt import get_jwt_secret


def normalize_phone(value: str) -> str:
    """Normalize international phone syntax and apply the deployment's send policy."""
    value = value.strip()
    if not value.startswith("+") and settings.sms_default_country_code:
        value = "+" + settings.sms_default_country_code + value
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", value):
        raise ValueError("Enter a phone number with its country code, such as +12025550123.")
    prefixes = [item.strip() for item in settings.sms_allowed_prefixes.split(",") if item.strip()]
    if prefixes and not any(value.startswith(prefix) for prefix in prefixes):
        raise ValueError("Phone numbers in this region are not enabled for this deployment.")
    return value


def hash_phone_code(phone: str, purpose: str, user_id: str | None, code: str) -> str:
    """Domain-separated HMAC prevents offline enumeration of six-digit codes."""
    payload = "\x00".join(("phone-otp-v1", phone, purpose, user_id or "", code))
    return hmac.new(get_jwt_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()


def phone_identity_unverified(user: dict) -> bool:
    """Phone-only accounts must prove ownership even if email verification is disabled."""
    return (
        not user.get("email")
        and not user.get("login_name")
        and not (user.get("phone") and user.get("phone_verified"))
    )
