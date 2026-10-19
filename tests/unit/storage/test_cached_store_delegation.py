"""Every OperationalStore method reaches the wrapped store, unchanged.

``CachedOperationalStore`` re-declares the whole ``OperationalStore`` contract,
mostly as one-line pass-throughs. A pass-through that calls a sibling method,
drops a keyword argument or discards the result would type-check and pass every
test that does not happen to touch it -- and about eighty of them were touched
by none. This walks the abstract contract instead of a hand-kept list, so a
method added later is covered the day it lands.

Caching is checked elsewhere (``test_cached_store.py``,
``test_cache_invalidation.py``); here every call is a cache miss, which is
exactly when the wrapped store must see the call as made.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.base import OperationalStore
from serving.storage.cache import CachedOperationalStore, InMemoryCache

pytestmark = pytest.mark.asyncio


# The cache's own operation: it clears this layer and has nothing to forward.
_CACHE_LOCAL = {"invalidate_auth_caches"}


def _contract() -> list[str]:
    return sorted(
        name
        for name, member in inspect.getmembers(OperationalStore, inspect.isfunction)
        if not name.startswith("_")
        and inspect.iscoroutinefunction(member)
        and name not in _CACHE_LOCAL
    )


def _returns_nothing(name: str) -> bool:
    return inspect.signature(getattr(OperationalStore, name)).return_annotation in (None, "None")


def _arguments(name: str) -> tuple[list[Any], dict[str, Any]]:
    """A distinct sentinel for every parameter, so a swapped or dropped one shows."""
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    params = list(inspect.signature(getattr(OperationalStore, name)).parameters.values())[1:]
    named = {param.name for param in params}
    for param in params:
        value = f"<{name}.{param.name}>"
        if param.kind is param.VAR_KEYWORD:
            # ``**fields`` writers name the account in it, as their callers do.
            if "user_id" not in named:
                kwargs["user_id"] = f"<{name}.user_id>"
            kwargs["extra_field"] = value
        elif param.kind is param.VAR_POSITIONAL:
            continue
        elif param.kind is param.KEYWORD_ONLY:
            kwargs[param.name] = value
        else:
            args.append(value)
    return args, kwargs


def test_the_contract_is_not_empty():
    assert len(_contract()) > 100


@pytest.mark.parametrize("name", _contract())
async def test_the_call_reaches_the_wrapped_store_as_made(name):
    inner = MagicMock()
    result = MagicMock(name=f"{name}-result")
    setattr(inner, name, AsyncMock(return_value=result))
    cached = CachedOperationalStore(inner, InMemoryCache())
    args, kwargs = _arguments(name)

    returned = await getattr(cached, name)(*args, **kwargs)

    method = getattr(inner, name)
    method.assert_awaited_once()
    call = method.await_args
    # Positional and keyword spellings of the same argument are equivalent.
    bound_sent = inspect.signature(getattr(OperationalStore, name)).bind(None, *args, **kwargs)
    bound_seen = inspect.signature(getattr(OperationalStore, name)).bind(
        None, *call.args, **call.kwargs
    )
    assert bound_seen.arguments == bound_sent.arguments
    if not _returns_nothing(name):
        assert returned is result
