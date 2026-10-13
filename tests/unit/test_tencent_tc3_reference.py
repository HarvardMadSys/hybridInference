"""Frozen TC3 vectors independently generated with Tencent's official SDK.

The reference used ``AbstractClient._get_tc3_signature`` and ``Sign.sign_tc3``
from TencentCloud/tencentcloud-sdk-python; it is not a second implementation
of the signing algorithm in this repository. All credentials are synthetic.

Reference sources:
https://github.com/TencentCloud/tencentcloud-sdk-python/blob/master/tencentcloud/common/abstract_client.py
https://github.com/TencentCloud/tencentcloud-sdk-python/blob/master/tencentcloud/common/sign.py
"""

import json

import pytest

from serving.utils.tencent_cloud import signed_headers


@pytest.mark.parametrize(
    "service,action,payload,signature",
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
                    "TemplateData": '{"site_name":"测试","url":"https://example.test/verify?token=synthetic"}',
                },
            },
            "04b1f86bbbc378340434bb8f1899b06bfbd3ee7173a37e687de649da1d198ca1",
        ),
    ],
)
def test_signed_headers_match_official_sdk_utf8_vectors(service, action, payload, signature):
    headers = signed_headers(
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
