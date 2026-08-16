"""Request-scoped context using contextvars."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

_ctx: ContextVar[dict[str, Any] | None] = ContextVar("request_context", default=None)

# req_ctx key tagging the kind of gateway-generated, client-driven error a request
# failed with. Surfaced onto the request log record by RequestLogMiddleware so alert
# rules can exclude these user-driven failures from service-failure-rate signals.
CLIENT_ERROR_KIND = "client_error_kind"
# Value: the gateway could not find/authorize the requested model (a 404 we raised
# ourselves, not an upstream provider 404).
MODEL_NOT_FOUND = "model_not_found"

# req_ctx key holding the authenticated caller's role, published by the API-key
# auth dependency. Read by the multi-key pool to skip upstream provider keys
# reserved for a higher tier. Absent means "no user identity" — an internal
# caller such as a health probe or warmup, which is unrestricted; entitlement is
# withheld from lower *tiers*, not from the gateway's own machinery. Being in
# REQUEST_SCOPED_KEYS is what makes "absent" trustworthy.
USER_ROLE = "user_role"

# UTC datetime captured once at the HTTP request boundary. Scheduled pricing
# consumers use it so routing, logs, and quota charging cannot disagree when a
# long-running request crosses a price-window boundary.
PRICING_TIME = "pricing_time"

# req_ctx key holding the scheduling priority the router assigned this request,
# published around dispatch by FixedRouter and read by the OpenAI-compatible
# adapter for endpoints configured with ``priority_scheduling``. Pushed rather
# than updated durably: it describes one dispatch attempt, and a fallback to a
# second endpoint re-publishes its own.
UPSTREAM_PRIORITY = "upstream_priority"

# req_ctx key naming the upstream that served (or refused) this request.
PROVIDER = "provider"
#: Provider label meaning "no upstream was ever selected" — a pre-routing failure.
#: Never published as attribution: the consumers of ``PROVIDER`` read a present
#: label as "an upstream answered us", so the sentinel would misattribute the
#: failure *and* defeat that distinction.
ROUTER_PROVIDER_SENTINEL = "router"

#: Keys holding state about *one* request, which therefore have to be cleared when
#: the next request is seeded (see :func:`reset_request_scope`).
#:
#: The contextvar is not re-created per request: whenever an ASGI server or test
#: transport drives sequential scopes from a single task, request N+1 starts out
#: seeing every durable write request N made. Consumers that read "key present" as
#: a fact about the current request — RequestLogMiddleware, the failed-request and
#: circuit-breaker alert rules — are wrong by exactly one request when a key is
#: missing from this tuple. Anything written durably via :func:`update` (rather
#: than the self-unwinding :func:`push`) belongs here.
REQUEST_SCOPED_KEYS = (
    "client_user_agent",
    "user_id",
    "user_name",
    # Caller identity for multi-key rotation. Held here for the same reason as
    # USER_ROLE: the key pool reads "absent" as "no caller to keep sticky" and
    # shares one binding for it, so a leftover value would bind an internal
    # request (health probe, warmup) to the previous caller's upstream key.
    "auth_key_hash",
    "affinity_key",
    USER_ROLE,
    PRICING_TIME,
    CLIENT_ERROR_KIND,
    PROVIDER,
)


def get() -> dict[str, Any]:
    """Return the current request-scoped context dict (empty if unset)."""
    value = _ctx.get()
    return value if value is not None else {}


def set(values: dict[str, Any]) -> None:
    """Set the request-scoped context to the given dict."""
    _ctx.set(values)


def update(values: dict[str, Any]) -> None:
    """Merge the given dict into the current request-scoped context."""
    current_value = _ctx.get()
    current = dict(current_value) if current_value is not None else {}
    current.update(values)
    _ctx.set(current)


@contextmanager
def push(**values: Any) -> Iterator[None]:
    """Temporarily merge fields into the request context."""
    current_value = _ctx.get()
    current = current_value if current_value is not None else {}
    token = _ctx.set({**current, **values})
    try:
        yield
    finally:
        _ctx.reset(token)


def reset_request_scope(**seed: Any) -> None:
    """Drop every per-request key, then merge ``seed`` into the context.

    Called once per request by ``RequestIdMiddleware`` (the outermost middleware,
    so it runs before anything can populate the context), so that a request which
    does not populate a key cannot inherit the value left behind by a previous
    request handled in the same task.

    Keys in :data:`REQUEST_SCOPED_KEYS` are *removed* rather than set to ``None``,
    which matters because some readers pass a non-``None`` default —
    ``ctx.get(PROVIDER, ROUTER_PROVIDER_SENTINEL)`` in the error-path attribution
    and ``ctx.get("provider", "unknown")`` in the HTTP retry log. A present-but-
    ``None`` value silences those defaults and would relabel a pre-routing
    failure from ``"router"`` to ``None``. Anything ``seed`` supplies is written
    back afterwards, so a key the middleware always sets stays present.
    """
    current_value = _ctx.get()
    current = current_value if current_value is not None else {}
    values = {k: v for k, v in current.items() if k not in REQUEST_SCOPED_KEYS}
    values.update(seed)
    _ctx.set(values)


def publish_upstream_provider(provider: str | None) -> None:
    """Attribute the current request's failure to the upstream that produced it.

    Call this from any path that relays an upstream error to the client. The
    label lands on the request-log record and is what lets
    ``RequestLogMiddleware`` and ``FailedRequestRateRule`` tell a relayed
    upstream failure from one the gateway raised itself. That distinction is
    load-bearing for 401 in particular: a gateway-issued 401 is routine
    token-refresh churn, while an upstream 401 means the gateway's *own*
    configured credential was refused — an all-users outage. Without
    attribution the two are the same bare 401, which is how a local endpoint
    rejecting the gateway's key for an hour stayed below the log threshold and
    never reached the failure-rate rule.

    No-ops for a missing label or the :data:`ROUTER_PROVIDER_SENTINEL`, neither
    of which identifies an upstream that actually answered.
    """
    if provider and provider != ROUTER_PROVIDER_SENTINEL:
        update({PROVIDER: provider})


def mark_model_not_found() -> None:
    """Tag the current request as a gateway model-not-found (client-driven 404).

    A user asked for an unknown or unauthorized model, so the gateway raised the
    404 itself. Surfaced onto the request log record so the failed-request-rate
    alert can exclude these user-driven 404s — the same way the DB-query alerter
    excludes the model-not-found error strings. Genuine *upstream* provider 404s
    are left unmarked so they still count toward the alert.
    """
    update({CLIENT_ERROR_KIND: MODEL_NOT_FOUND})


__all__ = [
    "CLIENT_ERROR_KIND",
    "MODEL_NOT_FOUND",
    "PRICING_TIME",
    "PROVIDER",
    "REQUEST_SCOPED_KEYS",
    "ROUTER_PROVIDER_SENTINEL",
    "USER_ROLE",
    "get",
    "mark_model_not_found",
    "publish_upstream_provider",
    "push",
    "reset_request_scope",
    "set",
    "update",
]
