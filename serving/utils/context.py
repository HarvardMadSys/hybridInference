"""Request-scoped context using contextvars."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

_ctx: ContextVar[dict[str, Any] | None] = ContextVar("request_context", default=None)


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


__all__ = ["get", "push", "set", "update"]
