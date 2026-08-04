"""Authenticating a sandbox's model traffic with its own capability token.

The sandbox needs to call models, which normally means an API key. Issuing one
would put a second credential inside the sandbox and create something that has
to be explicitly revoked — and a revocation that is forgotten, or that races a
cached auth lookup, silently reopens the hole.

Instead the sandbox reuses the one credential it already holds: the per-attempt
capability token. This module resolves such a token into the identity its model
calls run as. Two properties follow directly from the store's fence rather than
from bookkeeping:

- **Revocation is automatic.** The lookup requires the attempt to still be the
  job's current one, still ``running``, with an unexpired lease, and the job to
  be in a live state. The instant the reaper supersedes the attempt, the owner
  cancels, or the job finishes, the same token stops buying inference. There is
  no key to remember to revoke and no cache to invalidate.
- **The budget is enforced from the billing ledger**, not from anything the
  agent reports about itself: spend is summed from ``api_logs.agent_job_id``.

Requests bill the job's *owner*, so a job's usage shows up in that user's
account exactly like any other traffic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from serving import grants, quota
from serving.agent_jobs.tokens import InvalidAgentToken, parse_worker_token
from serving.model_access import get_disabled_models_from_preferences
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from serving.storage.agent_job_store import AgentJobStore

logger = get_logger(__name__)

# Tokens are prefixed ``ajt.`` (see tokens.py). User API keys are ``hyi-``, so
# the two namespaces cannot collide and the dispatch below is unambiguous.
AGENT_TOKEN_PREFIX = "ajt."

# Headroom required before admitting another call. Sized so one ordinary
# request cannot cross the cap on its own; it is not a reservation, and the
# comment at the check explains what that does and does not guarantee.
REQUEST_HEADROOM_USD = 0.25


class AgentModelAuthError(Exception):
    """Raised when a worker token may not be used for model traffic.

    ``status_code`` mirrors what the HTTP layer should return: 401 for a token
    that is invalid or whose fence has moved on, 429 when the job's budget is
    exhausted.
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


def looks_like_agent_token(api_key: str | None) -> bool:
    """Return whether a credential should be resolved as an agent credential.

    Covers both kinds during the transition: the legacy per-attempt worker
    token (``ajt.``) and the inference grant that replaces it (``agr.``). The
    HTTP layer routes on this one predicate, so both must answer here or a
    grant would fall through to the API-key path and be rejected as garbage.
    """
    if not api_key:
        return False
    return api_key.startswith(AGENT_TOKEN_PREFIX) or grants.looks_like_grant_token(api_key)


async def _resolve_grant(
    api_key: str,
    *,
    op_store: Any | None,
) -> dict[str, Any]:
    """Load and validate the grant behind an ``agr`` token.

    Shared by the model and tool paths: signature, row, liveness and subject
    are the same questions for both. What differs is what each does next —
    only the model path meters.

    Raises:
        AgentModelAuthError: For any unusable token, grant, or subject.
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


async def authenticate_grant_tool_call(
    api_key: str,
    *,
    op_store: Any | None,
) -> dict[str, Any]:
    """Resolve an inference grant for an MCP proxy call.

    The same signature, row, liveness and subject checks as a model call, and
    deliberately no quota: a tool call invokes no inference provider, so it
    spends nothing there is a limit on.

    Raises:
        AgentModelAuthError: For an unusable grant or subject.
    """
    row = await _resolve_grant(api_key, op_store=op_store)
    await _active_subject(op_store, row["user_id"])
    return {
        "user_id": row["user_id"],
        "agent_job_id": row["external_job_id"],
        "agent_grant_id": row["grant_id"],
        "agent_allowed_mcp": row.get("allowed_mcp") or [],
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


async def authenticate_agent_model_call(
    api_key: str,
    *,
    job_store: AgentJobStore | None,
    log_store: Any | None,
) -> dict[str, Any]:
    """Resolve a worker token into a user context for a model request.

    Raises :class:`AgentModelAuthError` when the token is unusable. On success
    the returned context carries ``agent_job_id`` so the request is attributed
    to the job in ``api_logs`` — which is also what the budget is measured
    from on the next call.
    """
    if job_store is None:
        raise AgentModelAuthError("Agent job tokens require a configured database.")

    try:
        claims = parse_worker_token(api_key)
    except InvalidAgentToken as exc:
        raise AgentModelAuthError(f"Invalid agent job token: {exc}") from exc

    identity = await job_store.resolve_model_credential(
        job_id=claims["job_id"],
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
    )
    if identity is None:
        # Superseded, cancelled, finished, or lease expired — all the same
        # answer, and deliberately not distinguished in the message so the
        # sandbox learns nothing about the job's state from a rejection.
        raise AgentModelAuthError(
            "This agent job token is no longer valid for model calls.",
        )

    # Fail closed on a missing budget. "No budget configured" must never mean
    # "spend without limit": the create schema now always supplies one, so a
    # None here is a job from before that or a direct DB write, and neither is
    # a reason to hand out uncapped inference.
    budget = identity["budget_usd"]
    if budget is None:
        raise AgentModelAuthError(
            "This agent job has no spending limit configured.", status_code=403
        )
    if log_store is None:
        # Without the ledger the budget cannot be measured, so it cannot be
        # enforced — refuse rather than run uncapped.
        raise AgentModelAuthError(
            "Agent job spending cannot be verified right now.", status_code=503
        )

    spent = await _job_spend(log_store, identity["job_id"])
    # Require headroom for one more request rather than merely "not yet over":
    # admitting a call that starts a cent below the cap lets a single large
    # completion blow through it. This bounds one request's overshoot; it does
    # NOT make the cap hard under concurrency, where several in-flight calls
    # all observe the same spend before any is logged. Overshoot is bounded by
    # (in-flight requests x REQUEST_HEADROOM_USD); a genuinely hard cap needs a
    # reservation counter rather than a read of the ledger.
    if spent + REQUEST_HEADROOM_USD > budget:
        raise AgentModelAuthError(
            f"Agent job budget exhausted (${spent:.4f} of ${budget:.2f}).",
            status_code=429,
        )

    return {
        "user_id": identity["user_id"],
        # The owner's role, so the sandbox can call exactly the models its
        # owner can call directly — they are billed to the owner either way,
        # and a narrower role here made the composer offer models whose first
        # call then failed with 404 (staging: 15 listed, 2 resolvable). The
        # blast radius of a leaked token is bounded by the budget, not by the
        # model tier. Falls back to `free` for a store that cannot report it.
        "role": identity.get("role") or "free",
        "authenticated": True,
        "is_admin": False,
        # Attribution: propagated into api_logs.agent_job_id, which is both the
        # cost report the owner sees and the ledger the budget check reads.
        "agent_job_id": identity["job_id"],
        "agent_job_budget_usd": budget,
    }


async def authenticate_agent_tool_call(
    api_key: str,
    *,
    job_store: AgentJobStore | None,
) -> dict[str, Any]:
    """Resolve a worker token for an MCP proxy call.

    The same fence as :func:`authenticate_agent_model_call`, and therefore the
    same automatic revocation: a cancelled, superseded or finished job stops
    reaching MCP servers at the same instant it stops buying inference.

    It deliberately does **not** apply the budget check. The budget caps model
    spend, measured from ``api_logs``; a tool call buys no inference and
    contributes nothing to that ledger, so refusing one with a 429 would report
    an overspend the call did not cause — to an agent whose only sensible
    response is to retry. A job that has exhausted its budget already cannot
    take another turn, which is the control that actually stops it.

    Returns the identity plus ``mcp_servers``: the servers this specific job was
    created with. The proxy checks membership against that list rather than
    against the deployment registry, so a token cannot reach a server its job
    never asked for by naming it in the URL.
    """
    if job_store is None:
        raise AgentModelAuthError("Agent job tokens require a configured database.")

    try:
        claims = parse_worker_token(api_key)
    except InvalidAgentToken as exc:
        raise AgentModelAuthError(f"Invalid agent job token: {exc}") from exc

    identity = await job_store.resolve_model_credential(
        job_id=claims["job_id"],
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
    )
    if identity is None:
        raise AgentModelAuthError("This agent job token is no longer valid.")

    return {
        "user_id": identity["user_id"],
        "job_id": identity["job_id"],
        "mcp_servers": list(identity.get("mcp_servers") or []),
    }


async def _job_spend(log_store: Any, job_id: str) -> float:
    """Return a job's spend so far, tolerating a store without the query."""
    getter = getattr(log_store, "get_agent_job_cost", None)
    if getter is None:
        # A log store that cannot report agent spend must not silently grant
        # unlimited budget; treat it as unavailable rather than as zero spend.
        raise AgentModelAuthError(
            "Agent job budgets require a log store that reports per-job cost.",
            status_code=503,
        )
    try:
        return float(await getter(job_id))
    except AgentModelAuthError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("agent job spend lookup failed", exc_info=True)
        raise AgentModelAuthError(
            "Could not verify the agent job budget.", status_code=503
        ) from exc
