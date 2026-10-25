"""Small TC3-signed Tencent Cloud API client for transactional delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from serving.integrations.tencent_cloud.config import settings


class TencentCloudError(RuntimeError):
    """A delivery failure containing only a safe provider error code."""


def signed_headers(
    *,
    service: str,
    action: str,
    version: str,
    region: str,
    body: bytes,
    secret_id: str,
    secret_key: str,
    token: str = "",
    timestamp: int | None = None,
) -> dict[str, str]:
    """Sign the exact JSON bytes using Tencent Cloud's TC3-HMAC-SHA256."""
    timestamp = int(time.time()) if timestamp is None else timestamp
    date = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")
    host = f"{service}.tencentcloudapi.com"
    content_type = "application/json; charset=utf-8"
    signed = "content-type;host"
    canonical_headers = f"content-type:{content_type}\nhost:{host}\n"
    canonical_request = (
        f"POST\n/\n\n{canonical_headers}\n{signed}\n{hashlib.sha256(body).hexdigest()}"
    )
    scope = f"{date}/{service}/tc3_request"
    to_sign = (
        f"TC3-HMAC-SHA256\n{timestamp}\n{scope}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )
    date_key = hmac.new(f"TC3{secret_key}".encode(), date.encode(), hashlib.sha256).digest()
    service_key = hmac.new(date_key, service.encode(), hashlib.sha256).digest()
    request_key = hmac.new(service_key, b"tc3_request", hashlib.sha256).digest()
    signature = hmac.new(request_key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Authorization": (
            f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}"
        ),
        "Content-Type": content_type,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Version": version,
        "X-TC-Region": region,
        "X-TC-Timestamp": str(timestamp),
    }
    if token:
        headers["X-TC-Token"] = token
    return headers


def call_tencent_cloud(
    service: Literal["ses", "sms"],
    action: str,
    version: str,
    region: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Submit one signed request without automatically repeating a paid send.

    Provider messages and request bodies can contain personal data or OTPs, so
    failures expose only a bounded error code. A successful return means the
    request was accepted, not that the recipient received it.
    """
    if not settings.tencent_cloud_secret_id or not settings.tencent_cloud_secret_key:
        raise TencentCloudError("CredentialsMissing")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = signed_headers(
        service=service,
        action=action,
        version=version,
        region=region,
        body=body,
        secret_id=settings.tencent_cloud_secret_id,
        secret_key=settings.tencent_cloud_secret_key,
        token=settings.tencent_cloud_token,
    )
    try:
        response = httpx.post(
            f"https://{service}.tencentcloudapi.com/", content=body, headers=headers, timeout=10
        )
        response.raise_for_status()
        result = response.json().get("Response")
        if not isinstance(result, dict):
            raise TencentCloudError("InvalidResponse")
        if error := result.get("Error"):
            code = error.get("Code", "ProviderError") if isinstance(error, dict) else None
            safe_code = (
                code
                if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.]{1,128}", code)
                else "ProviderError"
            )
            raise TencentCloudError(safe_code)
        return result
    except TencentCloudError:
        raise
    except (httpx.HTTPError, ValueError, AttributeError) as error:
        raise TencentCloudError("TransportError") from error
