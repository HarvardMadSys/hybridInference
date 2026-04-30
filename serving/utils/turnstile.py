"""Cloudflare Turnstile server-side verification."""

from __future__ import annotations

import os

import httpx

from serving.utils.logging import get_logger

logger = get_logger(__name__)

_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def _get_secret_key() -> str:
    return os.getenv("TURNSTILE_SECRET_KEY", "").strip()


async def verify_turnstile_token(token: str | None, remote_ip: str) -> bool:
    secret_key = _get_secret_key()
    if not secret_key:
        return True

    if not token:
        return False

    data = {"secret": secret_key, "response": token, "remoteip": remote_ip}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(_VERIFY_URL, data=data)
    except httpx.HTTPError as exc:
        logger.warning(f"Turnstile verification request failed: {exc}")
        return False

    if resp.status_code != 200:
        logger.warning(f"Turnstile verification non-200: {resp.status_code}")
        return False

    try:
        payload = resp.json()
    except ValueError:
        return False

    success = bool(payload.get("success"))
    if not success:
        logger.info(f"Turnstile rejected token: {payload.get('error-codes')}")
    return success
