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
    "get",
    "mark_model_not_found",
    "push",
    "set",
    "update",
]
