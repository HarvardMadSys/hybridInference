"""API key authentication and quota enforcement."""

import asyncio
import hashlib
import hmac
import os
import secrets
from base64 import urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet
from fastapi import Depends, Header, HTTPException, Request

from serving import grants, quota
from serving.agent_jobs.model_auth import (
    AgentModelAuthError,
    AgentQuotaExceeded,
    authenticate_agent_model_call,
    authenticate_grant_model_call,
    looks_like_agent_token,
)
from serving.config.settings import get_settings
from serving.config.site_identity import get_site_identity
from serving.model_access import get_disabled_models_from_preferences
from serving.observability.rejection_log import log_rejection
from serving.servers.deps import (
    auth_database_detail,
    get_agent_job_store,
    get_db_logger,
    get_log_store,
    get_operational_store,
)
from serving.utils.auth_failure_blocklist import is_ip_blocked, record_auth_failure
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip, get_client_ip_info

logger = get_logger(__name__)


def _quota_contact() -> str:
    """Support address for quota messages, or empty when none is configured."""
    return get_site_identity().support_email


def is_user_auth_enabled() -> bool:
    """Return whether API-key user auth is enabled.

    Fail-closed by default: auth is enabled unless ``USER_AUTH_ENABLED``
    is explicitly set to a falsy value (parsed by Pydantic).
    """
    try:
        from serving.config.runtime_settings import get_runtime_settings_instance

        rs = get_runtime_settings_instance()
        found, value = rs.get_cached("user_auth_enabled")
        if found:
            return bool(value)
    except (RuntimeError, KeyError):
        pass
    return get_settings().user_auth_enabled


def generate_api_key() -> str:
    """Generate a new API key with format: hyi-{32 random bytes}."""
    random_part = secrets.token_urlsafe(32)
    return f"hyi-{random_part}"


def hash_api_key(plaintext_key: str) -> str:
    """Hash an API key using HMAC-SHA256 keyed by ``API_KEY_SECRET``."""
    secret = get_settings().api_key_secret.encode()
    if not secret:
        raise ValueError("API_KEY_SECRET must be set in environment")
    return hmac.new(secret, plaintext_key.encode(), hashlib.sha256).hexdigest()


def _api_key_cipher() -> Fernet:
    secret = get_settings().api_key_secret.encode()
    if not secret:
        raise ValueError("API_KEY_SECRET must be set in environment")
    key = urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def encrypt_api_key(plaintext_key: str) -> str:
    """Encrypt an API key for user-facing display later."""
    return _api_key_cipher().encrypt(plaintext_key.encode()).decode()


def decrypt_api_key(encrypted_key: str | None) -> str | None:
    """Decrypt a stored API key, returning None for legacy rows."""
    if not encrypted_key:
        return None
    return _api_key_cipher().decrypt(encrypted_key.encode()).decode()


def constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    return hmac.compare_digest(a, b)


def _extract_api_key(authorization: str | None, x_api_key: str | None) -> str | None:
    """Return the presented credential from either accepted header."""
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:]
    return x_api_key or None


async def _authenticate_by_api_key(
    request: Request,
    authorization: str | None,
    x_api_key: str | None,
    op_store: Any,
) -> tuple[dict[str, Any], str]:
    """Resolve and validate the caller's API key against the database.

    Shared by :func:`verify_api_key` and :func:`verify_api_key_for_balance` --
    both need the same identity/email-verification checks, but only
    ``verify_api_key`` additionally enforces the daily cost quota gate.

    Returns ``(user_row, key_hash)``. Raises ``HTTPException(401)`` for a
    missing/invalid key and ``HTTPException(403)`` for an unverified email.
    """
    # Refuse sources already blocked for repeated auth failures, before any key
    # extraction or DB lookup so a flood is shed cheaply. ``ip_info`` is computed
    # once here and reused by the failure logs below.
    ip_info = get_client_ip_info(request)
    blocked, retry_after = await is_ip_blocked(ip_info.client_ip)
    if blocked:
        asyncio.create_task(  # noqa: RUF006 — fire-and-forget rejection log
            log_rejection(
                request=request,
                status_code=429,
                error_code="ip_blocked",
                reason="auth_failures_exceeded",
                user=None,
            )
        )
        raise HTTPException(
            status_code=429,
            detail="Too many authentication failures from this IP. Temporarily blocked.",
            headers={"Retry-After": str(retry_after)},
        )

    # Extract API key from headers
    api_key = _extract_api_key(authorization, x_api_key)

    if not api_key:
        logger.warning(
            "auth_failure",
            extra={
                "event": "auth_failure",
                "remote_ip": ip_info.client_ip,
                "peer_ip": ip_info.peer_ip,
                "ip_source": ip_info.source,
                "key_prefix": None,
                "reason": "missing_api_key",
            },
        )
        await record_auth_failure(ip_info.client_ip)
        asyncio.create_task(  # noqa: RUF006 — fire-and-forget rejection log
            log_rejection(
                request=request,
                status_code=401,
                error_code="auth_missing",
                reason="missing_api_key",
                user=None,
            )
        )
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Use 'Authorization: Bearer hyi-xxx' or 'X-API-Key: hyi-xxx'",
        )

    # Validate key against database
    if not op_store:
        # A configuration state, not a server fault: 503 tells the caller the
        # deployment cannot authenticate anyone right now, and the detail says
        # which of the two supported setups is missing.
        raise HTTPException(status_code=503, detail=auth_database_detail())

    key_hash = hash_api_key(api_key)

    user = await op_store.get_auth_context_by_key_hash(key_hash)

    if not user:
        logger.warning(
            "auth_failure",
            extra={
                "event": "auth_failure",
                "remote_ip": ip_info.client_ip,
                "peer_ip": ip_info.peer_ip,
                "ip_source": ip_info.source,
                "key_prefix": api_key[:6] if api_key else None,
                "reason": "invalid_api_key",
            },
        )
        await record_auth_failure(ip_info.client_ip)
        asyncio.create_task(  # noqa: RUF006 — fire-and-forget rejection log
            log_rejection(
                request=request,
                status_code=401,
                error_code="auth_invalid",
                reason=f"key_prefix={api_key[:6] if api_key else None}",
                user=None,
            )
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired API key",
        )

    require_verification = get_settings().signup_require_email_verification
    try:
        from serving.config.runtime_settings import get_runtime_settings_instance

        rs = get_runtime_settings_instance()
        require_verification = await rs.get_bool("signup_require_email_verification")
    except Exception as exc:
        logger.warning(
            "RuntimeSettings lookup for signup_require_email_verification failed; "
            f"falling back to env: {exc}"
        )
    if require_verification and user.get("email") and not user.get("email_verified"):
        raise HTTPException(
            status_code=403,
            detail="Email not verified. Please verify your email to continue.",
        )

    return user, key_hash


def _impersonation_service_key_hashes() -> frozenset[str]:
    """Key hashes permitted to act on behalf of another user via ``X-On-Behalf-Of``.

    Currently only the RAG doc-assistant service account (``RAG_API_KEY``): the
    ``/v1/rag/chat`` handler authenticates the real end user by JWT, then embeds
    the query and generates the answer via gateway self-calls under this single
    key. Honoring the header for that key lets those self-calls attribute logs,
    cost, quota, and per-user concurrency to the real end user instead of the
    shared service account.

    Fails closed: an unset/blank ``RAG_API_KEY`` (or an unset ``API_KEY_SECRET``)
    yields an empty set, disabling impersonation entirely. Recomputed per call (a
    cheap HMAC) so key rotation / env changes take effect without a restart.
    """
    rag_key = os.getenv("RAG_API_KEY", "").strip()
    if not rag_key:
        return frozenset()
    try:
        return frozenset({hash_api_key(rag_key)})
    except ValueError:
        return frozenset()


async def _resolve_effective_identity(
    caller: dict[str, Any],
    caller_key_hash: str,
    on_behalf_of: str | None,
    op_store: Any,
) -> dict[str, Any]:
    """Return the identity a request is attributed to (caller, or an impersonated user).

    Normally the caller. When a *trusted* service account (see
    :func:`_impersonation_service_key_hashes`) sends
    ``X-On-Behalf-Of: <user_id>`` for an existing active user, returns that
    user's identity instead, so the request attributes to the real end user.

    Fails safe to the caller for every off-nominal case — an untrusted caller,
    a missing/blank target, an unknown or non-active user, or a store miss — so
    the header can never misattribute a request to a user the caller could not
    otherwise name, and a bad header never turns auth into a 500.

    The returned dict is shaped like the ``get_auth_context_by_key_hash`` row
    that :func:`verify_api_key` consumes downstream: ``user_id``, ``user_name``,
    ``role``, ``quota_daily_cost_usd``, ``preferences``,
    ``max_concurrent_requests``.
    """
    if not on_behalf_of:
        return caller
    if caller_key_hash not in _impersonation_service_key_hashes():
        logger.warning(
            "Ignoring X-On-Behalf-Of from non-service caller (user_id=%s)",
            caller.get("user_id"),
        )
        return caller
    target = await op_store.get_user_by_id(on_behalf_of) if op_store else None
    if not target or target.get("status") != "active":
        logger.warning(
            "X-On-Behalf-Of target %r not found or inactive; "
            "attributing request to the service account",
            on_behalf_of,
        )
        return caller
    # The daily quota column lives on ``api_keys``; an on-behalf-of user is
    # addressed by id (no specific key), so leave quota None to take the same
    # default the NULL-quota path uses. The per-user cost counter is keyed by
    # user_id, so the end user's own daily spend still gates their RAG usage.
    return {
        "user_id": target["id"],
        "user_name": target.get("user_name"),
        "role": target.get("role"),
        "quota_daily_cost_usd": None,
        "preferences": target.get("preferences"),
        "max_concurrent_requests": target.get("max_concurrent_requests"),
    }


# The only routes an agent-job token may reach. An allowlist rather than a
# denylist: a new control-plane route must not silently become reachable by a
# sandbox credential just because nobody remembered to exclude it.
_AGENT_TOKEN_PATH_PREFIXES = (
    "/v1/chat/completions",
    "/v1/messages",
    "/v1/embeddings",
    "/v1/completions",
    "/v1/responses",
    "/anthropic/v1/messages",
)


def _is_inference_path(request: Request) -> bool:
    """Return whether this request targets a billed inference endpoint."""
    path = request.url.path.rstrip("/")
    return any(
        path == prefix or path.startswith(prefix + "/") for prefix in _AGENT_TOKEN_PATH_PREFIXES
    )


async def verify_api_key(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    x_on_behalf_of: str | None = Header(None, alias="X-On-Behalf-Of"),
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
    agent_job_store=Depends(get_agent_job_store),
) -> dict[str, Any]:
    """Verify API key and enforce quotas.

    Returns user context dict with user_id, role, etc.
    Raises HTTPException(401/403/429) on auth/quota failures.

    A trusted service account may present ``X-On-Behalf-Of: <user_id>`` to
    attribute the request (logs, cost, quota, concurrency) to that end user
    instead of to itself; see :func:`_resolve_effective_identity`. The presented
    key is still what is authenticated and rate-limited at the transport layer.
    """
    # Agent-sandbox capability tokens (issue #1041) are a distinct credential
    # namespace (``ajt.`` vs ``hyi-``) resolved against the job fence rather
    # than the api_keys table, so the sandbox never needs a second credential
    # and revocation is automatic. Checked before the auth-disabled shortcut:
    # a job's budget and cost attribution are cost controls, not authn, and
    # must hold in every deployment. Ordinary keys pay one prefix comparison.
    presented_key = _extract_api_key(authorization, x_api_key)
    if looks_like_agent_token(presented_key):
        # An agent token buys inference and nothing else. This dependency is
        # shared with the owner-facing control plane (/v1/agent/jobs), so
        # resolving one here as its owner's normal context would let a sandbox
        # enumerate, cancel, or create that owner's other jobs — the exact
        # authority the model scope exists to withhold.
        if not _is_inference_path(request):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": {
                        "type": "insufficient_scope",
                        "message": ("This credential may only be used for model inference."),
                    }
                },
            )
        try:
            if grants.looks_like_grant_token(presented_key):
                # The grant path meters against the account's daily quota,
                # which the legacy branch below this return never reached.
                return await authenticate_grant_model_call(presented_key, op_store=op_store)
            return await authenticate_agent_model_call(
                presented_key,
                job_store=agent_job_store,
                log_store=log_store,
            )
        except AgentQuotaExceeded as exc:
            # Same body and headers as the direct path's 429, from the same
            # builder: a caller must not be able to tell which door it used.
            body, headers = quota.exceeded_payload(
                quota_usd=exc.quota_usd,
                spent_usd=exc.spent_usd,
                contact_email=_quota_contact(),
            )
            raise HTTPException(status_code=429, detail=body, headers=headers) from exc
        except AgentModelAuthError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"error": {"type": "agent_job_auth", "message": exc.message}},
            ) from exc

    # Check if auth is enabled
    if not is_user_auth_enabled():
        # Auth disabled - allow all, mark as anonymous
        return {
            "user_id": "anonymous",
            "role": "admin",
            "authenticated": False,
            "is_admin": True,
        }

    caller, key_hash = await _authenticate_by_api_key(request, authorization, x_api_key, op_store)
    # The presented key is always the one whose last_used we touch, even when the
    # request is attributed to another user via X-On-Behalf-Of.
    caller_key_id = caller["id"]

    # Resolve who this request is attributed to: normally the caller, or an end
    # user when a trusted service account (the RAG doc-assistant) acts on their
    # behalf. Everything below — quota gate, cost counter, returned context —
    # keys off this identity, so log attribution and per-user concurrency follow.
    identity = await _resolve_effective_identity(caller, key_hash, x_on_behalf_of, op_store)
    effective_user_id = identity["user_id"]

    # Pre-check daily cost quota via operational store counter table
    cost_spent = 0.0
    if op_store:
        cost_spent = await op_store.get_user_cost_today(effective_user_id)

    # Estimate cost for this request
    estimated_cost = 0.01

    # Get quota with fallback for NULL (old rows from migration)
    quota_daily_cost_usd = identity.get("quota_daily_cost_usd")
    quota_daily_cost_usd = 1000.0 if quota_daily_cost_usd is None else float(quota_daily_cost_usd)

    # Check cost quota
    if cost_spent + estimated_cost > quota_daily_cost_usd:
        seconds_until_midnight_utc = _seconds_until_utc_midnight()
        quota_reset_at = _next_utc_midnight()
        asyncio.create_task(  # noqa: RUF006 — fire-and-forget rejection log
            log_rejection(
                request=request,
                status_code=429,
                error_code="quota_exceeded",
                reason=(f"quota_usd={quota_daily_cost_usd:.4f} spent_usd={cost_spent:.4f}"),
                user={
                    "user_id": effective_user_id,
                    "role": identity.get("role") or "free",
                },
            )
        )
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Daily cost quota exceeded",
                "quota_usd": quota_daily_cost_usd,
                "spent_usd": cost_spent,
                "remaining_usd": max(0, quota_daily_cost_usd - cost_spent),
                "reset_at": quota_reset_at.isoformat(),
                "contact_email": _quota_contact(),
                "message": (
                    f"Need more quota? Email {_quota_contact()} and explain your use case."
                    if _quota_contact()
                    else "Daily quota exhausted. Contact the operator of this deployment."
                ),
                "retry_after": seconds_until_midnight_utc,
            },
            headers={
                "Retry-After": str(seconds_until_midnight_utc),
                "X-RateLimit-Limit-Cost": str(quota_daily_cost_usd),
                "X-RateLimit-Remaining-Cost": str(max(0, quota_daily_cost_usd - cost_spent)),
                "X-RateLimit-Reset": str(int(quota_reset_at.timestamp())),
            },
        )

    # Update last_used timestamp (fire and forget) on the presented key.
    await op_store.update_key_last_used(caller_key_id)

    # Return user context for the attributed identity.
    user_role = identity.get("role") or "free"
    return {
        "user_id": effective_user_id,
        "user_name": identity.get("user_name"),
        "role": user_role,
        "authenticated": True,
        "quota_daily_cost_usd": quota_daily_cost_usd,
        "spent_today_usd": cost_spent,
        "quota_remaining_cost_usd": quota_daily_cost_usd - cost_spent,
        "is_admin": user_role == "admin",
        "disabled_models": get_disabled_models_from_preferences(identity.get("preferences")),
        "max_concurrent_requests": identity.get("max_concurrent_requests"),
        # key_hash identifies the specific hyi-xxx key in use (a user may
        # have multiple). Used as the affinity key for multi-key API rotation;
        # stays the presented key even when acting on behalf of another user.
        "auth_key_hash": key_hash,
    }


async def verify_api_key_for_balance(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    op_store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Authenticate an API key for a balance check, without the quota gate.

    Unlike :func:`verify_api_key`, this never raises 429 for an exhausted
    quota -- checking remaining balance must keep working exactly when the
    balance is low or zero. Read-only: does not update ``last_used_at``.
    """
    if not is_user_auth_enabled():
        return {"user_id": "anonymous", "authenticated": False}

    user, _key_hash = await _authenticate_by_api_key(request, authorization, x_api_key, op_store)

    cost_spent = 0.0
    if op_store:
        cost_spent = await op_store.get_user_cost_today(user["user_id"])

    quota_daily_cost_usd = user.get("quota_daily_cost_usd")
    quota_daily_cost_usd = 1000.0 if quota_daily_cost_usd is None else float(quota_daily_cost_usd)

    return {
        "user_id": user["user_id"],
        "authenticated": True,
        "quota_daily_cost_usd": quota_daily_cost_usd,
        "spent_today_usd": cost_spent,
    }


async def optional_verify_api_key(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    op_store=Depends(get_operational_store),
) -> dict[str, Any] | None:
    """Lightweight identity lookup — no quota check, no last_used_at write.

    Returns a minimal user context (with ``is_admin``) when a valid API key is
    present, or ``None`` when the key is missing/invalid.  Designed for
    read-only endpoints like ``/v1/models`` that need admin visibility without
    side-effects.
    """
    # Auth disabled — treat caller as anonymous admin
    if not is_user_auth_enabled():
        return {"user_id": "anonymous", "role": "admin", "authenticated": False, "is_admin": True}

    # Extract API key from headers
    api_key = None
    if authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:]
    elif x_api_key:
        api_key = x_api_key

    if not api_key:
        return None  # No key supplied — anonymous

    if not op_store:
        logger.warning("optional_verify_api_key: DB unavailable, cannot resolve identity")
        raise HTTPException(status_code=503, detail=auth_database_detail())

    try:
        key_hash = hash_api_key(api_key)
    except ValueError as exc:
        logger.error("optional_verify_api_key: API_KEY_SECRET not set")
        raise HTTPException(
            status_code=500, detail="Server authentication misconfiguration"
        ) from exc

    try:
        row = await op_store.get_auth_context_lightweight(key_hash)
    except Exception as exc:
        logger.exception("optional_verify_api_key: DB query failed")
        raise HTTPException(status_code=500, detail="Database error during authentication") from exc

    if not row:
        return None  # Key invalid or expired — treat as anonymous

    require_verification = get_settings().signup_require_email_verification
    try:
        from serving.config.runtime_settings import get_runtime_settings_instance

        rs = get_runtime_settings_instance()
        require_verification = await rs.get_bool("signup_require_email_verification")
    except Exception as exc:
        logger.warning(
            "RuntimeSettings lookup for signup_require_email_verification failed; "
            f"falling back to env: {exc}"
        )
    if require_verification and row["email"] and not row["email_verified"]:
        return None

    user_role = row["role"] or "free"
    return {
        "user_id": row["user_id"],
        "role": user_role,
        "authenticated": True,
        "is_admin": user_role == "admin",
        "disabled_models": get_disabled_models_from_preferences(row.get("preferences")),
        "max_concurrent_requests": row.get("max_concurrent_requests"),
        # key_hash identifies the specific hyi-xxx key in use (a user may
        # have multiple). Used as the affinity key for multi-key API rotation.
        "auth_key_hash": key_hash,
    }


def _seconds_until_utc_midnight() -> int:
    """Calculate seconds until next UTC midnight."""
    return int((_next_utc_midnight() - datetime.now(timezone.utc)).total_seconds())


def _next_utc_midnight() -> datetime:
    """Return the next UTC midnight timestamp."""
    now = datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


async def verify_admin_token(
    request: Request,
    authorization: str | None = Header(None),
    db_logger=Depends(get_db_logger),
) -> str:
    """Verify admin token from Authorization header.

    Returns admin IP address for audit logging.
    Raises HTTPException(401) if invalid or missing token.

    All /admin/* routes should use this dependency to protect access.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing admin token. Use 'Authorization: Bearer {ADMIN_TOKEN}' header.",
        )

    token = authorization[7:]
    admin_token = get_settings().admin_token

    if not admin_token:
        raise HTTPException(
            status_code=500,
            detail="Server misconfiguration: ADMIN_TOKEN environment variable not set",
        )

    # Constant-time comparison to prevent timing attacks
    if not constant_time_compare(token, admin_token):
        raise HTTPException(
            status_code=401,
            detail="Invalid admin token",
        )

    # Extract admin IP for audit logging
    admin_ip = get_client_ip(request)
    return admin_ip


async def log_admin_action(
    db_logger,
    admin_ip: str,
    action: str,
    target_user_id: str | None = None,
    details: dict[str, Any] | None = None,
    success: bool = True,
) -> None:
    """Log admin action to audit trail.

    Accepts either an OperationalStore or a legacy DatabaseLogger. Callers
    are migrating to pass the store directly; during transition both are
    supported.

    Args:
        db_logger: OperationalStore or DatabaseLogger instance
        admin_ip: IP address of admin performing the action
        action: Action type (e.g., 'create_key', 'revoke_key')
        target_user_id: User ID affected by the action (if applicable)
        details: Additional context (will be stored as JSONB)
        success: Whether the action succeeded
    """
    if not db_logger:
        return  # Silently skip if logging not configured

    # Use store method if available (new path)
    if hasattr(db_logger, "log_admin_action"):
        await db_logger.log_admin_action(
            admin_ip=admin_ip,
            action=action,
            target_user_id=target_user_id,
            details=details,
            success=success,
        )
        return

    # Legacy path: raw pool access (will be removed after full migration)
    if not getattr(db_logger, "pool", None):
        return

    import json

    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO admin_audit_log (admin_ip, action, target_user_id, details, success)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            """,
            admin_ip,
            action,
            target_user_id,
            json.dumps(details) if details else None,
            success,
        )
