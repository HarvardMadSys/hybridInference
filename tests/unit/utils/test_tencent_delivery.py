"""Contract and redaction checks for Tencent Cloud delivery adapters."""

from unittest.mock import Mock

import httpx
import pytest

from serving.config.settings import settings
from serving.utils import sms, tencent_cloud, tencent_sms


@pytest.fixture
def configured(monkeypatch):
    """Use only synthetic credentials and approved-template stand-ins."""
    values = {
        "tencent_cloud_secret_id": "test-id",
        "tencent_cloud_secret_key": "test-secret",
        "tencent_cloud_token": "test-session-token",
        "sms_auth_enabled": True,
        "sms_provider": "tencent",
        "sms_sdk_app_id": "1400000000",
        "sms_sign_name": "测试公司",
        "sms_template_id": "201",
        "sms_otp_expire_seconds": 300,
    }
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)


def test_signature_uses_utc_and_exact_payload_bytes():
    kwargs = {
        "service": "ses",
        "action": "SendEmail",
        "version": "2020-10-02",
        "region": "ap-guangzhou",
        "secret_id": "test-id",
        "secret_key": "test-secret",
        "timestamp": 1551113065,
        "token": "session-token",
    }
    headers = tencent_cloud.signed_headers(body=b'{"subject":"hello"}', **kwargs)
    assert "2019-02-25/ses/tc3_request" in headers["Authorization"]
    assert headers["X-TC-Timestamp"] == "1551113065"
    assert headers["X-TC-Token"] == "session-token"
    assert (
        headers["Authorization"]
        != tencent_cloud.signed_headers(body=b'{"subject": "hello"}', **kwargs)["Authorization"]
    )


def test_request_signs_transmitted_utf8_and_accepts_provider_response(configured, monkeypatch):
    captured = {}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return httpx.Response(
            200, json={"Response": {"MessageId": "accepted-id"}}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(tencent_cloud.httpx, "post", post)
    monkeypatch.setattr(tencent_cloud.time, "time", lambda: 1551113065)
    result = tencent_cloud.call_tencent_cloud(
        "ses", "SendEmail", "2020-10-02", "ap-guangzhou", {"Subject": "验证码"}
    )
    assert result["MessageId"] == "accepted-id"
    assert captured["url"] == "https://ses.tencentcloudapi.com/"
    assert captured["content"] == '{"Subject":"验证码"}'.encode()
    assert captured["headers"] == tencent_cloud.signed_headers(
        service="ses",
        action="SendEmail",
        version="2020-10-02",
        region="ap-guangzhou",
        body=captured["content"],
        secret_id="test-id",
        secret_key="test-secret",
        token="test-session-token",
        timestamp=1551113065,
    )


def test_provider_error_does_not_expose_recipient_or_otp(configured, monkeypatch):
    response = httpx.Response(
        200,
        json={
            "Response": {
                "Error": {
                    "Code": "AuthFailure.SignatureFailure",
                    "Message": "+8613800000000 code 123456 test-secret",
                }
            }
        },
        request=httpx.Request("POST", "https://sms.tencentcloudapi.com/"),
    )
    monkeypatch.setattr(tencent_cloud.httpx, "post", Mock(return_value=response))
    with pytest.raises(tencent_cloud.TencentCloudError, match=r"^AuthFailure\.SignatureFailure$"):
        tencent_cloud.call_tencent_cloud("sms", "SendSms", "2021-01-11", "ap-guangzhou", {})


def test_timeout_is_not_automatically_retried(configured, monkeypatch):
    post = Mock(side_effect=httpx.ReadTimeout("unknown delivery outcome"))
    monkeypatch.setattr(tencent_cloud.httpx, "post", post)
    with pytest.raises(tencent_cloud.TencentCloudError, match="TransportError"):
        tencent_cloud.call_tencent_cloud("sms", "SendSms", "2021-01-11", "ap-guangzhou", {})
    assert post.call_count == 1


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
    "phone,code",
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
