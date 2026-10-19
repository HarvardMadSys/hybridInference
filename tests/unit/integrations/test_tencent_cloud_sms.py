"""Tencent SMS verification codes: destination, template parameters and status."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from serving.config.settings import settings
from serving.integrations.tencent_cloud import sms as tencent_sms
from serving.utils import sms


@pytest.fixture
def configured(monkeypatch):
    """Use only synthetic credentials and approved-template stand-ins."""
    environment = {
        "TENCENT_CLOUD_SECRET_ID": "test-id",
        "TENCENT_CLOUD_SECRET_KEY": "test-secret",
        "TENCENT_CLOUD_TOKEN": "test-session-token",
        "SMS_SDK_APP_ID": "1400000000",
        "SMS_SIGN_NAME": "测试公司",
        "SMS_TEMPLATE_ID": "201",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(settings, "sms_auth_enabled", True)
    monkeypatch.setattr(settings, "sms_provider", "tencent")
    monkeypatch.setattr(settings, "sms_otp_expire_seconds", 300)


def test_sms_requires_individual_success_and_sends_two_parameters(configured, monkeypatch):
    call = Mock(return_value={"SendStatusSet": [{"Code": "Ok", "PhoneNumber": "+8613800000000"}]})
    monkeypatch.setattr(tencent_sms, "call_tencent_cloud", call)
    assert sms.send_verification_sms("+8613800000000", "123456")
    payload = call.call_args.args[-1]
    assert payload["TemplateParamSet"] == ["123456", "5"]
    assert payload["PhoneNumberSet"] == ["+8613800000000"]
    call.return_value = {
        "SendStatusSet": [
            {
                "Code": "FailedOperation.SignatureIncorrectOrUnapproved",
                "PhoneNumber": "+8613800000000",
            }
        ]
    }
    assert not sms.send_verification_sms("+8613800000000", "123456")


@pytest.mark.parametrize(
    ("phone", "code"),
    [("+12025550123", "123456"), ("+8613800000000", "00000"), ("+8613800000000", "1234567")],
)
def test_sms_rejects_invalid_domestic_destination_and_code(configured, monkeypatch, phone, code):
    call = Mock()
    monkeypatch.setattr(tencent_sms, "call_tencent_cloud", call)
    assert not sms.send_verification_sms(phone, code)
    call.assert_not_called()


def test_provider_must_be_selected_before_any_paid_delivery(configured, monkeypatch):
    monkeypatch.setattr(settings, "sms_provider", "none")
    send = Mock()
    monkeypatch.setattr(tencent_sms, "send_verification_sms", send)
    assert not sms.is_sms_enabled()
    assert not sms.send_verification_sms("+8613800000000", "123456")
    send.assert_not_called()
