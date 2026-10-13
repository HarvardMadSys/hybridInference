"""Phone identity policy stays independent of the SMS delivery service."""

import pytest

from serving.config.settings import settings
from serving.utils.phone_auth import normalize_phone, phone_identity_unverified


@pytest.mark.parametrize("phone", ["+12025550123", "+442079460123", "+8613800138000"])
def test_international_identifiers_do_not_require_tencent(phone, monkeypatch):
    monkeypatch.setattr(settings, "sms_default_country_code", "")
    monkeypatch.setattr(settings, "sms_allowed_prefixes", "")
    assert normalize_phone(phone) == phone


def test_deployment_can_restrict_destinations_and_accept_local_input(monkeypatch):
    monkeypatch.setattr(settings, "sms_default_country_code", "86")
    monkeypatch.setattr(settings, "sms_allowed_prefixes", "+86")
    assert normalize_phone("13800138000") == "+8613800138000"
    with pytest.raises(ValueError, match="region"):
        normalize_phone("+12025550123")


@pytest.mark.parametrize(
    "identity,blocked",
    [
        ({"email": None, "login_name": "admin"}, False),
        ({"email": "user@example.test"}, False),
        ({"email": None, "phone": "+12025550123", "phone_verified": True}, False),
        ({"email": None, "phone": "+12025550123", "phone_verified": False}, True),
        ({"email": None}, True),
    ],
)
def test_username_admin_is_not_mistaken_for_unverified_phone(identity, blocked):
    assert phone_identity_unverified(identity) is blocked
