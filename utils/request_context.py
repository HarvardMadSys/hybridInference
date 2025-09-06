from __future__ import annotations

"""Lightweight request-scoped context using contextvars.

Exposes a dictionary-like context that middlewares and downstream code can
enrich with fields like request_id, tenant, model, provider, etc.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_ctx: ContextVar[dict[str, Any]] = ContextVar("request_context", default={})


def get() -> dict[str, Any]:
    return _ctx.get()


def set(values: dict[str, Any]) -> None:
    _ctx.set(values)


def update(values: dict[str, Any]) -> None:
    current = dict(_ctx.get())
    current.update(values)
    _ctx.set(current)


@contextmanager
def push(**values: Any) -> Iterator[None]:
    """Temporarily merge fields into the request context."""
    token = _ctx.set({**_ctx.get(), **values})
    try:
        yield
    finally:
        _ctx.reset(token)
