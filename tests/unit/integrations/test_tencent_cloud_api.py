"""TC3 signing and the request contract of the Tencent Cloud API client."""

from __future__ import annotations

import json
from unittest.mock import Mock

import httpx
import pytest

from serving.integrations.tencent_cloud import api


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv("TENCENT_CLOUD_SECRET_ID", "test-id")
    monkeypatch.setenv("TENCENT_CLOUD_SECRET_KEY", "test-secret")
    monkeypatch.setenv("TENCENT_CLOUD_TOKEN", "test-session-token")


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
    headers = api.signed_headers(body=b'{"subject":"hello"}', **kwargs)
    assert "2019-02-25/ses/tc3_request" in headers["Authorization"]
    assert headers["X-TC-Timestamp"] == "1551113065"
    assert headers["X-TC-Token"] == "session-token"
    spaced = api.signed_headers(body=b'{"subject": "hello"}', **kwargs)
    assert headers["Authorization"] != spaced["Authorization"]


# Frozen vectors generated with Tencent's official SDK
# (``AbstractClient._get_tc3_signature`` and ``Sign.sign_tc3`` in
# TencentCloud/tencentcloud-sdk-python), not a second implementation of the
# algorithm. All credentials are synthetic.
@pytest.mark.parametrize(
    ("service", "action", "payload", "signature"),
    [
        (
            "sms",
            "SendSms",
            {
                "PhoneNumberSet": ["+8613800138000"],
                "SmsSdkAppId": "fake-app",
                "SignName": "测试签名",
                "TemplateId": "1",
                "TemplateParamSet": ["123456", "5"],
            },
            "ab36afd470b524e2c2777291fd21f26315a7190d7dc2a4328337cd97d8f04028",
        ),
        (
            "ses",
            "SendEmail",
            {
                "FromEmailAddress": "测试企业 <noreply@example.test>",
                "Destination": ["person@example.test"],
                "Subject": "Verify",
                "Template": {
                    "TemplateID": 2,
                    "TemplateData": (
                        '{"site_name":"测试","url":"https://example.test/verify?token=synthetic"}'
                    ),
                },
            },
            "04b1f86bbbc378340434bb8f1899b06bfbd3ee7173a37e687de649da1d198ca1",
        ),
    ],
)
def test_signed_headers_match_official_sdk_utf8_vectors(service, action, payload, signature):
    headers = api.signed_headers(
        service=service,
        action=action,
        version="test",
        region="ap-guangzhou",
        body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
        secret_id="AKID-synthetic",
        secret_key="synthetic-key-never-production",
        timestamp=1551113065,
    )
    assert headers["Authorization"] == (
        f"TC3-HMAC-SHA256 Credential=AKID-synthetic/2019-02-25/{service}/tc3_request, "
        f"SignedHeaders=content-type;host, Signature={signature}"
    )


def test_request_signs_transmitted_utf8_and_accepts_provider_response(credentials, monkeypatch):
    captured = {}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return httpx.Response(
            200,
            json={"Response": {"MessageId": "accepted-id"}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(api.httpx, "post", post)
    monkeypatch.setattr(api.time, "time", lambda: 1551113065)
    result = api.call_tencent_cloud(
        "ses", "SendEmail", "2020-10-02", "ap-guangzhou", {"Subject": "验证码"}
    )
    assert result["MessageId"] == "accepted-id"
    assert captured["url"] == "https://ses.tencentcloudapi.com/"
    assert captured["content"] == '{"Subject":"验证码"}'.encode()
    assert captured["headers"] == api.signed_headers(
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


def test_missing_credentials_never_reach_the_network(monkeypatch):
    monkeypatch.delenv("TENCENT_CLOUD_SECRET_ID", raising=False)
    monkeypatch.delenv("TENCENT_CLOUD_SECRET_KEY", raising=False)
    post = Mock()
    monkeypatch.setattr(api.httpx, "post", post)
    with pytest.raises(api.TencentCloudError, match="CredentialsMissing"):
        api.call_tencent_cloud("sms", "SendSms", "2021-01-11", "ap-guangzhou", {})
    post.assert_not_called()


def test_provider_error_does_not_expose_recipient_or_otp(credentials, monkeypatch):
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
    monkeypatch.setattr(api.httpx, "post", Mock(return_value=response))
    with pytest.raises(api.TencentCloudError, match=r"^AuthFailure\.SignatureFailure$"):
        api.call_tencent_cloud("sms", "SendSms", "2021-01-11", "ap-guangzhou", {})


def test_timeout_is_not_automatically_retried(credentials, monkeypatch):
    post = Mock(side_effect=httpx.ReadTimeout("unknown delivery outcome"))
    monkeypatch.setattr(api.httpx, "post", post)
    with pytest.raises(api.TencentCloudError, match="TransportError"):
        api.call_tencent_cloud("sms", "SendSms", "2021-01-11", "ap-guangzhou", {})
    assert post.call_count == 1
