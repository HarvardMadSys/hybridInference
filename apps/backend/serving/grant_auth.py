"""Authenticating a sandbox's model traffic with its inference grant.

This was ``model_auth.py`` in the agent package until the cloud agent moved to
its own repository; ``git log --follow`` has the rest. Everything that resolved
the old per-attempt worker token against a job fence went with it — the fence
lives in the control plane now, and this gateway holds no attempt state to
check one against. MCP went too, registry and proxy and all: a grant authorizes
models, and the cloud agent's own relay serves tools.


The sandbox needs to call models, which normally means an API key. Issuing one
would put a second credential inside the sandbox and create something that has
to be explicitly revoked — and a revocation that is forgotten, or that races a
cached auth lookup, silently reopens the hole.

Instead the sandbox holds an ``agr`` grant: a short-lived capability row the
control plane mints through ``/internal/agent-grants`` and renews while the
attempt runs. This module resolves that token into the identity its model
calls run as. Two properties come from the grant row rather than from
bookkeeping:

- **A grant dies on its own.** ``grants.is_live`` requires the row to be
  unrevoked and unexpired, and the TTL is minutes: the control plane renews a
  running attempt's grant and revokes it when the attempt settles, but a
  revoke that is forgotten or lost is a token that stops working at its next
  expiry — not a standing credential.
- **Spend is metered as the owner.** A grant carries no budget of its own.
  The account's daily quota — the same ceiling the direct path enforces — is
  checked on every call, and spend is measured from the billing ledger, not
  from anything the agent reports about itself.

Requests bill the grant's *owner*, so a job's usage shows up in that user's
account exactly like any other traffic, attributed via
``api_logs.agent_job_id``.
"""

from __future__ import annotations

from typing import Any

from serving import grants, quota
from serving.model_access import get_disabled_models_from_preferences
from serving.utils.logging import get_logger

logger = get_logger(__name__)


class AgentModelAuthError(Exception):
    """Raised when a grant token may not be used for model traffic.

    ``status_code`` mirrors what the HTTP layer should return: 401 for a token
    that is invalid, revoked or expired; 403 for a subject that may not call
    (suspended account, no applicable spending limit); 429 when the owner's
    daily quota is exhausted; 503 when the answer cannot be determined — a
    grant that cannot be verified refuses rather than proceeding unmetered.
    """

    def __init__(self, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class AgentQuotaExceeded(AgentModelAuthError):
    """The account behind a grant has spent its daily quota.

    Distinct from the base error because the HTTP layer must answer with the
    *same* body and ``X-RateLimit-*`` headers the direct path returns — a
    client should not be able to tell which door it came through — and that
    needs the numbers, not a rendered message.
    """

    def __init__(self, *, quota_usd: float, spent_usd: float) -> None:
        super().__init__("Daily cost quota exceeded", status_code=429)
        self.quota_usd = quota_usd
        self.spent_usd = spent_usd


async def _resolve_grant(
    api_key: str,
    *,
    op_store: Any | None,
) -> dict[str, Any]:
    """Load and validate the grant behind an ``agr`` token.

    Token shape, row lookup and liveness only; the subject and quota checks
    belong to the caller. ``authenticate_grant_model_call`` is the sole
    consumer since MCP moved out with the cloud agent — the seam stays in
    case a second grant-authorized capability ever returns.

    Raises:
        AgentModelAuthError: For any unusable token or grant.
    """
    if op_store is None:
        raise AgentModelAuthError("Inference grants require a configured database.")

    try:
        grant_id = grants.parse_grant_token(api_key)
    except grants.InvalidGrantToken as exc:
        raise AgentModelAuthError(f"Invalid inference grant: {exc}") from exc

    try:
        row = await op_store.get_agent_grant(grant_id)
    except Exception as exc:
        # A store read that errors must refuse the call rather than let it
        # proceed unmetered. This is the one place where a permissive default
        # is unbounded spend.
        logger.warning(
            "agent_grant_lookup_failed",
            exc_info=True,
            extra={"event": "agent_grant_lookup_failed", "grant_id": grant_id},
        )
        raise AgentModelAuthError(
            "Inference grants cannot be verified right now.", status_code=503
        ) from exc

    # Unknown, revoked and expired get one answer: a sandbox learns nothing
    # about a job's state from a rejection.
    if row is None or not grants.is_live(row):
        raise AgentModelAuthError("This inference grant is no longer valid.")
    return row


async def _quota_context(op_store: Any, user_id: str) -> tuple[float, float]:
    """Return ``(limit, spent)`` for the account behind a grant.

    The limit lives on the user's API key and the spend on the user, so this
    is two reads that the direct path gets in one — a grant carries no key to
    join through.

    Raises:
        AgentModelAuthError: If the account has no active key, if it somehow
            has more than one, or if either read fails. Every branch refuses;
            none defaults to "no ceiling".
    """
    try:
        keys = await op_store.get_quota_context_for_user(user_id)
    except Exception as exc:
        logger.warning(
            "agent_grant_quota_lookup_failed",
            exc_info=True,
            extra={"event": "agent_grant_quota_lookup_failed", "user_id": user_id},
        )
        raise AgentModelAuthError(
            "Spending limits cannot be verified right now.", status_code=503
        ) from exc

    if not keys:
        # No key means no configured limit, and absence of a limit must never
        # be read as absence of a ceiling.
        raise AgentModelAuthError(
            "This account has no active API key, so its spending limit cannot be applied.",
            status_code=403,
        )
    if len(keys) > 1:
        # UNIQUE (user_id) WHERE status='active' forbids this. Reaching it
        # means the index is gone; picking a winner would settle a spending
        # question by papering over a schema failure.
        logger.error(
            "agent_grant_duplicate_active_keys",
            extra={
                "event": "agent_grant_duplicate_active_keys",
                "user_id": user_id,
                "count": len(keys),
            },
        )
        raise AgentModelAuthError(
            "This account's spending limit is ambiguous and cannot be applied.",
            status_code=403,
        )

    try:
        spent = await op_store.get_user_cost_today(user_id)
    except Exception as exc:
        logger.warning(
            "agent_grant_spend_lookup_failed",
            exc_info=True,
            extra={"event": "agent_grant_spend_lookup_failed", "user_id": user_id},
        )
        raise AgentModelAuthError(
            "Spending cannot be measured right now.", status_code=503
        ) from exc

    return quota.resolve_quota(keys[0].get("quota_daily_cost_usd")), float(spent or 0.0)


async def authenticate_grant_model_call(
    api_key: str,
    *,
    op_store: Any | None,
) -> dict[str, Any]:
    """Resolve an inference grant into a user context for a model request.

    This is where the per-task budget went. A grant says *what* may be called;
    the account's daily quota — the same one the direct path enforces — says
    how much. Before grants existed, an agent token returned from
    ``verify_api_key`` above the quota gate and reached inference having been
    authenticated and never metered; that bypass closes here.

    Raises:
        AgentModelAuthError: For an unusable grant or an exhausted quota.
    """
    row = await _resolve_grant(api_key, op_store=op_store)

    user = await _active_subject(op_store, row["user_id"])
    quota_usd, spent_usd = await _quota_context(op_store, row["user_id"])
    try:
        quota.check(quota_usd=quota_usd, spent_usd=spent_usd)
    except quota.QuotaExceeded as exc:
        raise AgentQuotaExceeded(quota_usd=exc.quota_usd, spent_usd=exc.spent_usd) from exc

    return {
        "user_id": row["user_id"],
        # The owner's role: a grant's models were already clamped to it at mint
        # time, so this only has to agree with that decision.
        "role": user.get("role") or "free",
        "authenticated": True,
        "is_admin": False,
        # The same two the direct path puts here. A grant is the owner calling
        # through a sandbox, so anything the owner set for themselves has to
        # apply: without these, disabling a model in the dashboard or capping
        # an account's concurrency stopped applying the moment the call arrived
        # from an agent — a per-user control with a hole in it, and one nobody
        # would think to test for.
        "disabled_models": get_disabled_models_from_preferences(user.get("preferences")),
        "max_concurrent_requests": user.get("max_concurrent_requests"),
        # Attribution, unchanged: api_logs.agent_job_id is what the owner's
        # cost report reads, and it keeps working across the token change.
        "agent_job_id": row["external_job_id"],
        "agent_grant_id": row["grant_id"],
        "agent_allowed_models": row.get("allowed_models") or [],
    }


async def _active_subject(op_store: Any, user_id: str) -> dict[str, Any]:
    """Load the grant's owner, refusing anyone who may no longer sign in.

    Checked on every call, not only at mint: a grant lives minutes, and a
    suspension inside that window has to take effect immediately.

    Raises:
        AgentModelAuthError: If the account is unknown or not active.
    """
    try:
        user = await op_store.get_user_by_id(user_id)
    except Exception as exc:
        raise AgentModelAuthError(
            "This account cannot be verified right now.", status_code=503
        ) from exc
    if user is None or user.get("status") != "active":
        raise AgentModelAuthError("This account may not call models.", status_code=403)
    return user
