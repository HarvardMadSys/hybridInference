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

from serving.agent_jobs.tokens import InvalidAgentToken, parse_worker_token
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


def looks_like_agent_token(api_key: str | None) -> bool:
    """Return whether a credential should be resolved as a worker token."""
    return bool(api_key) and api_key.startswith(AGENT_TOKEN_PREFIX)


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
        "role": "free",
        "authenticated": True,
        "is_admin": False,
        # Attribution: propagated into api_logs.agent_job_id, which is both the
        # cost report the owner sees and the ledger the budget check reads.
        "agent_job_id": identity["job_id"],
        "agent_job_budget_usd": budget,
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
