"""Inference grants: the capability a detached service asks this gateway for.

The cloud agent runs in its own repository and its own database. It cannot
authorize a model call — it has no user table, no model registry, and no MCP
credentials. So it *asks*, and the gateway decides.

**The gateway decides, the control plane asks.** A request names a user and the
scope one attempt needs. The gateway looks that user up, narrows the requested
scope against state only it owns, and returns the **effective** grant. A caller
cannot widen anything by asking louder.

| Field | Who decides |
|---|---|
| ``user_id`` | Caller names it; we look it up and refuse unless active |
| ``allowed_models`` | **Clamped** to what the role may reach |
| ``ttl_seconds`` | ``min(requested, MAX_GRANT_TTL_S)`` |

**Models are the whole of it.** A grant authorizes model inference and nothing
else; it carried an MCP scope until the ownership amendment moved the MCP
registry, its credentials and its proxy to the cloud agent, which is where the
job, its requested tools and the attempt fence already live. A sandbox reaches
tools with a separate credential this gateway neither mints nor accepts, so a
leaked grant buys models and no tools, and a leaked MCP token buys tools and no
models.

**No budget.** A grant scopes *what* may be called, never *how much*. Inference
spend stays subject to the gateway's existing per-user quota, which is the one
place that already knows what an account has spent. A second budget system here
would be a second answer to the same question.

**Short TTL with renewal, not long TTL with revocation.** Revocation over the
network can fail, and a failed revoke on a long-lived grant leaves an abandoned
attempt able to call models. With a bounded TTL the grant dies on its own if the
control plane stops renewing — for any reason, including the control plane being
gone. Revoke stays as an *acceleration* of something that happens anyway, which
is the only kind of revoke that needs no durable outbox behind it.

The fence lives where the data is. After the split this gateway holds no attempt
or lease state at all, so nothing here consults one: the control plane stops
renewing when its own store says the attempt was superseded.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ulid import ULID

from serving.config.settings import get_settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

#: Ceiling on a grant's life, whatever the caller asks for. Long enough for a
#: renewal cycle to be comfortable, short enough that a grant nobody renews is
#: harmless within minutes.
MAX_GRANT_TTL_S = 900
DEFAULT_GRANT_TTL_S = 300

_TOKEN_PREFIX = "agr"
_SIGNING_CONTEXT = b"agent-inference-grant:v1"


class GrantError(Exception):
    """Base for grant failures that map to a 4xx."""


class GrantRequestInvalid(GrantError):
    """The request cannot be satisfied as asked — 400."""


class GrantSubjectUnavailable(GrantError):
    """The named user does not exist or may not use the platform — 403."""


class InvalidGrantToken(GrantError):
    """A presented grant token is malformed or mis-signed — 401."""


def _signing_key() -> bytes:
    """Derive the grant-signing key.

    Separate context from the worker token's, so a signature over one is
    meaningless as the other even though both derive from the same secret.
    """
    secret = get_settings().api_key_secret.encode()
    if not secret:
        raise ValueError("API_KEY_SECRET must be set to mint inference grants")
    return hashlib.sha256(_SIGNING_CONTEXT + secret).digest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def new_grant_id() -> str:
    """Return an identifier for a new grant."""
    return f"agr_{ULID()}"


def mint_grant_token(grant_id: str) -> str:
    """Mint the bearer token for a grant.

    The token carries the grant id and nothing else. Scope, expiry and
    revocation all live in the row, so a token cannot outlive or out-scope what
    the database says — there is no cached claim to go stale.
    """
    payload = json.dumps({"g": grant_id}, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(_signing_key(), payload, hashlib.sha256).digest()
    return f"{_TOKEN_PREFIX}.{_b64encode(payload)}.{_b64encode(signature)}"


def looks_like_grant_token(token: str) -> bool:
    """Whether this is shaped like a grant token, before verifying it."""
    return token.startswith(f"{_TOKEN_PREFIX}.")


def parse_grant_token(token: str) -> str:
    """Verify a grant token's signature and return its grant id.

    Raises:
        InvalidGrantToken: On any structural or signature problem.
    """
    if not token:
        raise InvalidGrantToken("empty token")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_PREFIX:
        raise InvalidGrantToken("malformed token")
    try:
        payload = _b64decode(parts[1])
        signature = _b64decode(parts[2])
    except (ValueError, TypeError) as exc:
        raise InvalidGrantToken("undecodable token") from exc

    expected = hmac.new(_signing_key(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise InvalidGrantToken("bad signature")

    try:
        grant_id = str(json.loads(payload)["g"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise InvalidGrantToken("bad claims") from exc
    if not grant_id:
        raise InvalidGrantToken("bad claims")
    return grant_id


def clamp_ttl(requested: int | None) -> int:
    """Return the lifetime a grant will actually get.

    A caller asking for longer gets the ceiling rather than an error: the
    request is satisfiable, just not on the caller's terms, and the response
    reports the effective value.
    """
    if requested is None or requested <= 0:
        return DEFAULT_GRANT_TTL_S
    return min(int(requested), MAX_GRANT_TTL_S)


def clamp_models(requested: Sequence[str] | None, *, visible: Sequence[str]) -> list[str]:
    """Narrow a requested model list to what the user's role may reach.

    Args:
        requested: What the control plane asked for; ``None`` means "everything
            this user can reach".
        visible: The canonical ids the role resolves, from the gateway's own
            registry.

    Returns:
        The effective list, in registry order so the result is stable.
    """
    if requested is None:
        return list(visible)
    wanted = set(requested)
    return [model for model in visible if model in wanted]


def expiry_from(ttl_seconds: int, *, now: datetime | None = None) -> datetime:
    """Return the absolute expiry for a grant minted now."""
    return (now or datetime.now(UTC)) + timedelta(seconds=ttl_seconds)


def is_live(row: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Whether a grant row may still authorize anything."""
    if row.get("revoked_at") is not None:
        return False
    expires_at = row.get("expires_at")
    if expires_at is None:
        return False
    return expires_at > (now or datetime.now(UTC))


def model_allowed(row: dict[str, Any], model: str) -> bool:
    """Whether this grant's effective model scope covers ``model``."""
    return model in (row.get("allowed_models") or [])


#: DDL for the gateway-owned grants table.
#:
#: Deliberately not in ``agent_job_store``: that module is frozen and leaves
#: with the cloud agent. This table stays, because minting and verifying a
#: capability for *this* gateway's models is this gateway's job.
#:
#: The unique constraint is what makes minting idempotent — a control plane that
#: retries after a timeout gets its grant back rather than a second capability
#: nobody is tracking.
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS agent_grants (
    grant_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    external_job_id TEXT NOT NULL,
    external_attempt_id TEXT NOT NULL,
    allowed_models JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    UNIQUE (external_job_id, external_attempt_id)
)
"""

CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_agent_grants_expires ON agent_grants(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_agent_grants_user ON agent_grants(user_id)",
)
