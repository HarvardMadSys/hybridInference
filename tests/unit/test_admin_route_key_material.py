"""Which key a route install uses, and what an admin sees when verification fails.

``_resolve_key_material`` picks the key material for every route, candidate and
model install: all of the provider's active keys, one settings ("env") key by
id, or one row from the Keys tab. A disabled key must not be selectable, a row
belonging to another provider must not be borrowed, and a store failure must
be a 503 rather than an install with fewer keys than the admin thinks.

``_verify_provider_route`` sends one tiny request before a target change is
accepted; its failure detail is what the admin reads, so it is checked too.
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import pytest
from fastapi import HTTPException

from serving.adapters import dynamic_keys
from serving.servers.routers.admin import provider_routes

pytestmark = pytest.mark.asyncio

ENV_A = "sk-env-aaaaaaaaaaaaaaaa"
ENV_B = "sk-env-bbbbbbbbbbbbbbbb"
POOL_P = "sk-pool-pppppppppppppppp"
DB_C = "sk-db-cccccccccccccccccc"


class Store:
    def __init__(self, *, disabled=(), db_keys=(), rows=None, fail=()):
        self.disabled = [dynamic_keys.env_key_hash(key) for key in disabled]
        self.db_keys = list(db_keys)
        self.rows = rows or {}
        self.fail = set(fail)

    async def list_disabled_provider_env_key_hashes(self, provider):
        if "disabled" in self.fail:
            raise RuntimeError("disabled list down")
        return self.disabled

    async def list_provider_keys_full(self, provider):
        if "db" in self.fail:
            raise RuntimeError("keys table down")
        return list(self.db_keys)

    async def get_provider_key_full(self, key_id):
        return self.rows.get(key_id)


@pytest.fixture
def keys(monkeypatch):
    """The provider's pools, pool-less static and settings keys, and DB-source bookkeeping."""
    state: dict[str, Any] = {
        "pool": [],
        "static": [],
        "settings": [],
        "marked": [],
        "declared": [],
    }

    class Pool:
        def snapshot_keys(self):
            return list(state["pool"])

    monkeypatch.setattr(dynamic_keys, "get_pools_for_provider", lambda provider: [Pool()])
    # As in dynamic_keys: the pool keys, then statics of adapters that have no pool.
    monkeypatch.setattr(
        dynamic_keys,
        "list_candidate_env_keys",
        lambda provider: _unique(state["pool"] + state["static"]),
    )
    monkeypatch.setattr(
        dynamic_keys, "configured_env_keys_for_provider", lambda provider: list(state["settings"])
    )
    monkeypatch.setattr(dynamic_keys, "is_env_key_disabled", lambda provider, key_hash: False)
    monkeypatch.setattr(
        dynamic_keys,
        "mark_db_key_for_provider",
        lambda provider, key: state["marked"].append((provider, key)),
    )

    async def declare(store, provider):
        state["declared"].append(provider)

    monkeypatch.setattr(dynamic_keys, "load_min_role_declarations_for_provider", declare)
    return state


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


async def _resolve(store, api_key_id=None, provider="zai"):
    return await provider_routes._resolve_key_material(
        store, key_provider=provider, api_key_id=api_key_id
    )


async def _status(store, api_key_id=None) -> tuple[int, str]:
    with pytest.raises(HTTPException) as caught:
        await _resolve(store, api_key_id)
    return caught.value.status_code, caught.value.detail


# --- every active key --------------------------------------------------------------


async def test_without_a_key_id_every_active_key_is_used_once(keys):
    keys["pool"] = [POOL_P, ENV_A, " "]
    keys["settings"] = [ENV_A, ENV_B]
    store = Store(disabled=[ENV_B], db_keys=[DB_C, ENV_A, ""])

    assert await _resolve(store) == (None, [POOL_P, ENV_A, DB_C])
    assert keys["declared"] == ["zai"]  # tiers are known before the adapter registers
    assert ("zai", DB_C) in keys["marked"]


async def test_a_single_key_adapter_without_a_pool_supplies_its_key(keys):
    keys["static"] = [ENV_A]

    assert await _resolve(Store()) == (None, [ENV_A])
    assert await _resolve(Store(), dynamic_keys.env_key_id(ENV_A)) == (None, [ENV_A])


async def test_pool_less_static_keys_come_after_the_pool_and_settings_keys(keys):
    keys["pool"] = [POOL_P]
    keys["static"] = [ENV_B, POOL_P]
    keys["settings"] = [ENV_A]

    assert await _resolve(Store()) == (None, [POOL_P, ENV_A, ENV_B])


async def test_a_key_disabled_in_the_live_cache_is_left_out(keys, monkeypatch):
    keys["settings"] = [ENV_A, ENV_B]
    hash_b = dynamic_keys.env_key_hash(ENV_B)
    monkeypatch.setattr(
        dynamic_keys, "is_env_key_disabled", lambda provider, key_hash: key_hash == hash_b
    )

    assert await _resolve(Store()) == (None, [ENV_A])


async def test_a_provider_with_no_usable_key_is_refused(keys):
    keys["settings"] = [ENV_A]

    assert await _status(Store(disabled=[ENV_A], db_keys=["  "])) == (
        400,
        "No API keys configured for provider 'zai'",
    )


@pytest.mark.parametrize(
    ("fail", "detail"),
    [
        ("disabled", "Failed to load disabled env keys for zai: disabled list down"),
        ("db", "Failed to load provider keys for zai: keys table down"),
    ],
)
async def test_an_unreadable_store_is_a_503_not_a_smaller_key_set(keys, fail, detail):
    keys["settings"] = [ENV_A]

    assert await _status(Store(fail={fail})) == (503, detail)


# --- one settings key by id ---------------------------------------------------------


async def test_a_settings_key_is_chosen_by_its_id(keys):
    keys["settings"] = [ENV_A, ENV_B]

    assert await _resolve(Store(), dynamic_keys.env_key_id(ENV_B)) == (None, [ENV_B])
    assert keys["marked"] == []  # a settings key is not DB-sourced


async def test_a_disabled_settings_key_cannot_be_chosen_by_id(keys):
    keys["settings"] = [ENV_A]

    status = await _status(Store(disabled=[ENV_A]), dynamic_keys.env_key_id(ENV_A))

    assert status == (404, "Env provider key not found")


# --- one Keys-tab row ------------------------------------------------------------------


async def test_a_key_row_is_used_alone_and_marked_as_db_sourced(keys):
    store = Store(rows={"row-1": ("zai", DB_C)})

    assert await _resolve(store, "row-1") == (None, [DB_C])
    assert keys["marked"] == [("zai", DB_C)]


async def test_another_providers_key_row_is_not_borrowed(keys):
    store = Store(rows={"row-1": ("deepseek", DB_C)})

    assert await _status(store, "row-1") == (
        400,
        "Provider key belongs to 'deepseek', expected 'zai'",
    )
    assert keys["marked"] == []


async def test_an_unknown_key_row_is_a_404(keys):
    assert await _status(Store(), "row-missing") == (404, "Provider key not found")


# --- verification ------------------------------------------------------------------------


class Adapter:
    def __init__(self, outcome: BaseException | None = None, delay: float = 0.0):
        self.outcome = outcome
        self.delay = delay
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    async def chat_completion(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        await asyncio.sleep(self.delay)
        if self.outcome is not None:
            raise self.outcome
        return {"choices": []}


def _upstream_error(status: int, body: str) -> aiohttp.ClientResponseError:
    exc = aiohttp.ClientResponseError(request_info=None, history=(), status=status)
    exc.error_body = body
    return exc


async def _verify(adapter: Adapter) -> HTTPException | None:
    update = type("Update", (), {"adapter": adapter})()
    try:
        await provider_routes._verify_provider_route(update)
    except HTTPException as exc:
        return exc
    return None


async def test_verification_sends_one_minimal_request():
    adapter = Adapter()

    assert await _verify(adapter) is None
    assert adapter.calls == [
        ([{"role": "user", "content": "ping"}], {"max_tokens": 1, "temperature": 0})
    ]


@pytest.mark.parametrize(
    ("outcome", "detail"),
    [
        (
            _upstream_error(401, '  {"error": "bad key"}  '),
            'Provider verification failed with HTTP 401: {"error": "bad key"}',
        ),
        (_upstream_error(502, ""), "Provider verification failed with HTTP 502"),
        (ConnectionResetError("peer reset"), "Provider verification failed: peer reset"),
        (ValueError(), "Provider verification failed: ValueError"),
    ],
    ids=["http-with-body", "http-without-body", "error", "error-without-message"],
)
async def test_a_failed_verification_tells_the_admin_why(outcome, detail):
    exc = await _verify(Adapter(outcome))

    assert exc is not None
    assert (exc.status_code, exc.detail) == (400, detail)


async def test_a_long_upstream_body_is_cut_short():
    exc = await _verify(Adapter(_upstream_error(400, "x" * 2000)))

    assert exc is not None
    assert exc.detail == f"Provider verification failed with HTTP 400: {'x' * 500}..."


async def test_a_verification_that_hangs_is_cut_off(monkeypatch):
    monkeypatch.setattr(provider_routes, "VERIFY_TIMEOUT_SEC", 0.05)

    exc = await _verify(Adapter(delay=5))

    assert exc is not None
    assert (exc.status_code, exc.detail) == (400, "Provider verification timed out after 0s")
