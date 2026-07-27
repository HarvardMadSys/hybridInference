"""Per-attempt capability tokens for agent-sandbox workers (issue #1041).

Workers run inside the sandbox and must report events back, but must never
hold a long-lived or cross-task credential. Per the adjudicated credential
principle: *long-lived, high-privilege or cross-task credentials never enter
the agent environment; only short-lived, single-task, capped, instantly
revocable capability tokens do.*

This module mints exactly that: a stateless HMAC token bound to one
``(job_id, attempt_id, lease_generation)`` triple. It is:

- **write-only in scope** — it authorizes the worker endpoints for its own
  attempt and nothing else;
- **self-revoking** — the token carries the fencing triple, so the moment the
  reaper supersedes that attempt every store write it can attempt is rejected.
  Revocation needs no token blacklist: the fence in ``AgentJobStore`` is the
  authority.

Signed with ``API_KEY_SECRET`` (already required for the gateway to run) under
a distinct domain-separation prefix, so an agent token can never be confused
with, or replayed as, a user API key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from serving.config.settings import get_settings

# Domain separation: a signature over this prefix is meaningless anywhere else.
_TOKEN_PREFIX = "ajt"
_SIGNING_CONTEXT = b"agent-job-worker-token:v1"


class InvalidAgentToken(Exception):
    """Raised when a worker token is malformed, mis-signed, or truncated."""


def _signing_key() -> bytes:
    """Return the HMAC key derived from ``API_KEY_SECRET``."""
    secret = get_settings().api_key_secret.encode()
    if not secret:
        raise ValueError("API_KEY_SECRET must be set to mint agent worker tokens")
    return hashlib.sha256(_SIGNING_CONTEXT + secret).digest()


def _b64encode(raw: bytes) -> str:
    """URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    """Decode URL-safe base64 that had its padding stripped."""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def mint_worker_token(*, job_id: str, attempt_id: int, lease_generation: int) -> str:
    """Mint a capability token bound to one attempt's fencing triple."""
    payload = json.dumps(
        {"j": job_id, "a": attempt_id, "g": lease_generation},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    signature = hmac.new(_signing_key(), payload, hashlib.sha256).digest()
    return f"{_TOKEN_PREFIX}.{_b64encode(payload)}.{_b64encode(signature)}"


def parse_worker_token(token: str) -> dict[str, Any]:
    """Verify a worker token and return its fencing triple.

    Returns ``{"job_id", "attempt_id", "lease_generation"}``. Raises
    :class:`InvalidAgentToken` on any structural or signature problem — the
    caller maps that to 401.
    """
    if not token:
        raise InvalidAgentToken("empty token")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_PREFIX:
        raise InvalidAgentToken("malformed token")
    try:
        payload = _b64decode(parts[1])
        signature = _b64decode(parts[2])
    except (ValueError, TypeError) as exc:
        raise InvalidAgentToken("undecodable token") from exc

    expected = hmac.new(_signing_key(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise InvalidAgentToken("bad signature")

    try:
        claims = json.loads(payload)
        return {
            "job_id": str(claims["j"]),
            "attempt_id": int(claims["a"]),
            "lease_generation": int(claims["g"]),
        }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise InvalidAgentToken("bad claims") from exc
