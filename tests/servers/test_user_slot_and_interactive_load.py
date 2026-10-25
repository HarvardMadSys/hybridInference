"""The per-user slot decision shared with the batch worker, and the interactive-load count.

``user_slot_for`` is the slot ``enforce_user_concurrency`` takes for an HTTP
request, for a caller that holds the slot itself (the batch worker); the
dependency also counts each request it admits as interactive load on its model,
for batch work to yield to.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from serving.batch.interactive_load import InteractiveLoad
from serving.servers import concurrency
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import (
    EXEMPT_MODEL_USER_CONCURRENCY_LIMIT,
    UserConcurrencyLimiter,
    canonical_model_id,
    enforce_user_concurrency,
    static_limits_provider,
    user_slot_for,
)
from serving.servers.deps import (
    get_embedding_adapters,
    get_model_concurrency_resolver,
    get_router,
    get_user_concurrency_limiter,
)

LIMITS = {"free": 1, "pro": 3, "internal": 10, "admin": 10}


def _route(canonical: str) -> Any:
    adapter = types.SimpleNamespace(config=types.SimpleNamespace(id=canonical))
    return types.SimpleNamespace(adapters=[(adapter, 1.0)])


class _Router:
    """Routes for the given names; an alias maps to its model's route."""

    def __init__(self, routes: dict[str, str]) -> None:
        self.routes = {name: _route(canonical) for name, canonical in routes.items()}


class _Resolver:
    def __init__(self, exempt: set[str], *, fail: bool = False) -> None:
        self.exempt = exempt
        self.fail = fail
        self.asked: list[str] = []

    async def is_exempt(self, model_id: str) -> bool:
        self.asked.append(model_id)
        if self.fail:
            raise RuntimeError("resolver down")
        return model_id in self.exempt


ROUTER = _Router(
    {"normal-model": "normal-model", "exempt-model": "exempt-model", "alias": "exempt-model"}
)


async def _slot(user: dict[str, Any], model: str | None, resolver: Any = None) -> tuple[str, Any]:
    return await user_slot_for(
        user,
        model,
        router=ROUTER,
        concurrency_resolver=resolver if resolver is not None else _Resolver({"exempt-model"}),
        embedding_adapters={},
    )


# -- user_slot_for ------------------------------------------------------------


async def test_normal_model_takes_the_users_own_slot_and_role_default():
    assert await _slot({"user_id": "u1"}, "normal-model") == ("u1", None)


async def test_normal_model_carries_the_users_override():
    user = {"user_id": "u1", "max_concurrent_requests": 5}

    assert await _slot(user, "normal-model") == ("u1", 5)


async def test_exempt_model_takes_the_separate_budget_capped_at_64():
    assert await _slot({"user_id": "u1"}, "exempt-model") == (
        "u1:exempt",
        EXEMPT_MODEL_USER_CONCURRENCY_LIMIT,
    )
    assert EXEMPT_MODEL_USER_CONCURRENCY_LIMIT == 64


async def test_exempt_model_honours_a_lower_override_and_caps_a_higher_one():
    assert await _slot({"user_id": "u1", "max_concurrent_requests": 10}, "exempt-model") == (
        "u1:exempt",
        10,
    )
    assert await _slot({"user_id": "u1", "max_concurrent_requests": 500}, "exempt-model") == (
        "u1:exempt",
        64,
    )


async def test_an_alias_is_judged_by_its_model():
    resolver = _Resolver({"exempt-model"})

    assert await _slot({"user_id": "u1"}, "alias", resolver) == ("u1:exempt", 64)
    assert resolver.asked == ["exempt-model"]


async def test_unknown_model_never_reaches_the_resolver():
    resolver = _Resolver({"made-up"})

    assert await _slot({"user_id": "u1"}, "made-up", resolver) == ("u1", None)
    assert resolver.asked == []


async def test_failing_exemption_check_falls_back_to_the_normal_budget():
    assert await _slot({"user_id": "u1"}, "exempt-model", _Resolver(set(), fail=True)) == (
        "u1",
        None,
    )


async def test_no_resolver_means_no_exemption():
    slot = await user_slot_for(
        {"user_id": "u1"},
        "exempt-model",
        router=ROUTER,
        concurrency_resolver=None,
        embedding_adapters=None,
    )

    assert slot == ("u1", None)


def test_canonical_model_id_falls_back_to_embedding_adapters():
    embeddings = {"bge-m3": types.SimpleNamespace(config=types.SimpleNamespace(id="bge-m3"))}

    assert canonical_model_id("alias", router=ROUTER, embedding_adapters=None) == "exempt-model"
    assert canonical_model_id("bge-m3", router=ROUTER, embedding_adapters=embeddings) == "bge-m3"
    assert canonical_model_id("nope", router=ROUTER, embedding_adapters=embeddings) is None
    assert canonical_model_id(None, router=ROUTER, embedding_adapters=None) is None
    assert canonical_model_id("alias", router=None, embedding_adapters=None) is None


# -- interactive load -----------------------------------------------------------


@pytest.fixture
def load(monkeypatch) -> InteractiveLoad:
    fresh = InteractiveLoad()
    monkeypatch.setattr(concurrency, "INTERACTIVE_LOAD", fresh)
    return fresh


def _app(user: dict[str, Any], limiter: UserConcurrencyLimiter) -> FastAPI:
    app = FastAPI()
    app.release = asyncio.Event()  # type: ignore[attr-defined]
    app.entered = asyncio.Event()  # type: ignore[attr-defined]

    async def fake_verify_api_key() -> dict[str, Any]:
        return user

    app.dependency_overrides[verify_api_key] = fake_verify_api_key
    app.dependency_overrides[get_user_concurrency_limiter] = lambda: limiter
    app.dependency_overrides[get_router] = lambda: ROUTER
    app.dependency_overrides[get_model_concurrency_resolver] = lambda: _Resolver({"exempt-model"})
    app.dependency_overrides[get_embedding_adapters] = lambda: {}

    @app.post("/probe", dependencies=[Depends(enforce_user_concurrency)])
    async def probe():
        app.entered.set()  # type: ignore[attr-defined]
        await app.release.wait()  # type: ignore[attr-defined]
        return {"ok": True}

    @app.post("/probe_raise", dependencies=[Depends(enforce_user_concurrency)])
    async def probe_raise():
        raise RuntimeError("boom")

    return app


async def test_request_counts_on_its_model_while_it_holds_the_slot(load):
    app = _app(
        {"user_id": "u1", "role": "internal"},
        UserConcurrencyLimiter(static_limits_provider(LIMITS)),
    )
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        task = asyncio.create_task(client.post("/probe", json={"model": "normal-model"}))
        await asyncio.wait_for(app.entered.wait(), timeout=5)  # type: ignore[attr-defined]

        assert load.in_flight("normal-model") == 1

        app.release.set()  # type: ignore[attr-defined]
        assert (await task).status_code == 200

    assert load.in_flight("normal-model") == 0


async def test_alias_and_exempt_requests_count_under_the_canonical_id(load):
    app = _app(
        {"user_id": "u1", "role": "internal"},
        UserConcurrencyLimiter(static_limits_provider(LIMITS)),
    )
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        task = asyncio.create_task(client.post("/probe", json={"model": "alias"}))
        await asyncio.wait_for(app.entered.wait(), timeout=5)  # type: ignore[attr-defined]

        assert load.in_flight("exempt-model") == 1
        assert load.in_flight("alias") == 0

        app.release.set()  # type: ignore[attr-defined]
        assert (await task).status_code == 200

    assert load.in_flight("exempt-model") == 0


async def test_failed_request_stops_counting(load):
    app = _app(
        {"user_id": "u1", "role": "internal"},
        UserConcurrencyLimiter(static_limits_provider(LIMITS)),
    )
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/probe_raise", json={"model": "normal-model"})

    assert response.status_code == 500
    assert load.in_flight("normal-model") == 0


async def test_rejected_and_unknown_model_requests_are_not_counted(load):
    limiter = UserConcurrencyLimiter(static_limits_provider(LIMITS))
    app = _app({"user_id": "u1", "role": "free"}, limiter)
    # The free user's only slot is taken: the request is refused before it counts.
    granted, _, _ = await limiter.try_acquire("u1", "free", False)
    assert granted
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        refused = await client.post("/probe", json={"model": "normal-model"})
        assert refused.status_code == 429
        assert load.in_flight("normal-model") == 0

        limiter.release("u1")
        app.release.set()  # type: ignore[attr-defined]
        unknown = await client.post("/probe", json={"model": "made-up"})

    assert unknown.status_code == 200
    assert load.in_flight("made-up") == 0
