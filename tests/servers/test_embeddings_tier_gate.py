"""Embedding models honor the model-level access gate (required_role/admin_only).

``docs/developer/adding-models.md`` documents ``required_role`` as "the lowest
user role that can see and call the model" — a model-level field with no
chat-only qualifier. But embedding models never enter the weighted route table
where the chat surface reads that field: the registry's embedding branch
registered only the adapters, so the declaration was dead config and
/v1/embeddings served any authenticated caller regardless of the declared
tier. These tests pin the fix: the registry records the same fail-closed gate
it parses for chat models, and /v1/embeddings enforces it through the shared
``model_access_denial`` check (uniform 404, aliases held to the model's rules,
runtime visibility overrides applied).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.config.runtime_settings import get_runtime_settings
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import (
    get_completions_logger,
    get_embedding_adapters,
    get_log_store,
    get_model_visibility_resolver,
    get_operational_store,
    get_router,
)
from serving.servers.registry import register_from_models_yaml
from serving.servers.routers import embeddings

# ---------------------------------------------------------------------------
# Registry: the embedding branch records the gate the chat branch parses.
# ---------------------------------------------------------------------------


def _write_models_yaml(tmp_path, body: str):
    p = tmp_path / "models.yaml"
    p.write_text(body)
    return p


def _embedding_yaml(*, role: str | None = "pro", route_extra: str = "") -> str:
    gate = f"    required_role: {role}\n" if role is not None else ""
    return f"""
models:
  - id: emb-x
    name: Embedding X
    provider: openai_compat
    model_type: embedding
{gate}    aliases: [emb-x-alias]
    route:
      - kind: openai_compat
        base_url: https://gateway.example/api/v2/
        api_keys: [pool-key]
        embeddings_path: /embeddings{route_extra}
"""


@pytest.mark.unit
def test_embedding_model_records_required_role_gate(tmp_path):
    p = _write_models_yaml(tmp_path, _embedding_yaml(role="pro"))

    exe = FixedRouter()
    count, _infos = register_from_models_yaml(exe, p, {})

    assert count == 2  # model + alias
    gate = exe.embedding_routes["emb-x"]
    assert gate.required_role == "pro"
    # The alias is held to the model's rules: same object, same gate.
    assert exe.embedding_routes["emb-x-alias"] is gate
    # And the model must NOT appear in the weighted chat table, where an
    # embedding adapter would be dispatchable as a chat route.
    assert "emb-x" not in exe.routes
    assert "emb-x-alias" not in exe.routes


@pytest.mark.unit
def test_embedding_model_without_role_shares_the_chat_cost_default(tmp_path):
    """An omitted required_role reads as the same default on both surfaces.

    The cross-PR consistency requirement: the chat branch derives the tier
    from cost (a paid or unknown-cost model publishes as pro), and the
    embedding branch must not silently keep those models free-tier. The
    fixture's route is a remote openai_compat endpoint with no pricing
    metadata — the exact shape that must not fall through to free.
    """
    p = _write_models_yaml(tmp_path, _embedding_yaml(role=None))

    exe = FixedRouter()
    register_from_models_yaml(exe, p, {})

    assert exe.embedding_routes["emb-x"].required_role == "pro"


@pytest.mark.unit
def test_embedding_admin_only_without_role_defaults_to_admin(tmp_path):
    body = _embedding_yaml(role=None).replace(
        "    model_type: embedding\n", "    model_type: embedding\n    admin_only: true\n"
    )
    p = _write_models_yaml(tmp_path, body)

    exe = FixedRouter()
    register_from_models_yaml(exe, p, {})

    gate = exe.embedding_routes["emb-x"]
    assert gate.required_role == "admin"


def _admin_only_embedding_yaml(role: str) -> str:
    return _embedding_yaml(role=role).replace(
        "    model_type: embedding\n", "    model_type: embedding\n    admin_only: true\n"
    )


@pytest.mark.unit
def test_embedding_admin_only_with_explicit_free_role_stays_admin(tmp_path):
    """The legacy contradictory pair stays admin-only, as on the chat surface."""
    p = _write_models_yaml(tmp_path, _admin_only_embedding_yaml("free"))

    exe = FixedRouter()
    register_from_models_yaml(exe, p, {})

    gate = exe.embedding_routes["emb-x"]
    assert gate.required_role == "admin"
    assert gate.admin_only is True


@pytest.mark.unit
def test_embedding_admin_only_with_explicit_pro_role_is_pro(tmp_path):
    p = _write_models_yaml(tmp_path, _admin_only_embedding_yaml("pro"))

    exe = FixedRouter()
    register_from_models_yaml(exe, p, {})

    gate = exe.embedding_routes["emb-x"]
    assert gate.required_role == "pro"
    assert gate.admin_only is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("provider", "base_url", "expected"),
    [
        ("openai_compat", "https://gateway.example/api/v2/", "pro"),
        ("sglang", "http://localhost:12003/v1", "free"),
    ],
)
def test_embedding_with_an_empty_route_list_defaults_like_an_omitted_one(
    tmp_path, provider, base_url, expected
):
    # route: [] synthesizes the model-level route in the registry, so the
    # cost default must classify that route: an unpriced remote endpoint is
    # unknown-cost (pro), a local engine stays free.
    body = f"""
models:
  - id: emb-x
    name: Embedding X
    provider: {provider}
    base_url: {base_url}
    model_type: embedding
    route: []
"""
    p = _write_models_yaml(tmp_path, body)

    exe = FixedRouter()
    adapters: dict = {}
    count, _infos = register_from_models_yaml(exe, p, adapters)

    assert count == 1
    assert "emb-x" in adapters
    assert exe.embedding_routes["emb-x"].required_role == expected


@pytest.mark.unit
def test_embedding_with_only_unconfigured_optional_routes_is_skipped(tmp_path, monkeypatch):
    body = _embedding_yaml(role=None).replace(
        "      - kind: openai_compat\n        base_url: https://gateway.example/api/v2/",
        "      - kind: openai_compat\n        optional: true\n"
        "        base_url: ${OPTIONAL_EMBEDDING_BASE_URL}",
    )
    monkeypatch.delenv("OPTIONAL_EMBEDDING_BASE_URL", raising=False)
    p = _write_models_yaml(tmp_path, body)

    exe = FixedRouter()
    adapters: dict = {}
    count, _infos = register_from_models_yaml(exe, p, adapters)

    assert count == 0
    assert adapters == {}
    assert exe.embedding_routes == {}


@pytest.mark.unit
def test_embedding_model_on_a_local_engine_keeps_the_free_default(tmp_path):
    # The cost-derived default reaches embeddings too: a locally-served
    # engine route (the one free-by-construction shape) with no role and no
    # pricing stays free, same as chat would classify it.
    body = _embedding_yaml(role=None).replace(
        "      - kind: openai_compat\n        base_url: https://gateway.example/api/v2/",
        "      - kind: sglang\n        base_url: http://localhost:12003/v1",
    )
    p = _write_models_yaml(tmp_path, body)

    exe = FixedRouter()
    register_from_models_yaml(exe, p, {})

    assert exe.embedding_routes["emb-x"].required_role == "free"


@pytest.mark.unit
def test_embedding_model_with_zero_user_pricing_keeps_remote_cost_gate(tmp_path):
    # Zero user-facing prices do not prove that a remote provider is free.
    body = _embedding_yaml(role=None).replace(
        "    route:",
        '    pricing:\n      prompt: "0"\n      completion: "0"\n    route:',
    )
    p = _write_models_yaml(tmp_path, body)

    exe = FixedRouter()
    register_from_models_yaml(exe, p, {})

    assert exe.embedding_routes["emb-x"].required_role == "pro"


@pytest.mark.unit
def test_embedding_model_invalid_role_fails_closed(tmp_path):
    p = _write_models_yaml(tmp_path, _embedding_yaml(role="god"))

    exe = FixedRouter()
    register_from_models_yaml(exe, p, {})

    assert exe.embedding_routes["emb-x"].required_role == "admin"


@pytest.mark.unit
def test_embedding_model_zero_weight_route_still_gated(tmp_path):
    """A zero-weight route must not skip the gate record.

    The weight table plays no role in embedding dispatch (the surface walks
    its adapter chain directly), so unlike register_route there is nothing
    to protect by ignoring zero-weight routes — only a gate to lose.
    """
    p = _write_models_yaml(tmp_path, _embedding_yaml(role="pro", route_extra="\n        weight: 0"))

    exe = FixedRouter()
    register_from_models_yaml(exe, p, {})

    gate = exe.embedding_routes["emb-x"]
    assert gate.required_role == "pro"
    assert "emb-x" not in exe.routes


# ---------------------------------------------------------------------------
# Surface: /v1/embeddings enforces the gate the way chat enforces its route's.
# ---------------------------------------------------------------------------


class _FakeAdapter:
    def __init__(self):
        self.config = SimpleNamespace(
            id="emb-pro",
            provider="fake-provider",
            pricing={"prompt": "1", "completion": "0"},
        )

    async def embeddings(self, input_data, **params):
        return {
            "object": "list",
            "model": "emb-pro",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }


class _CapturingLogger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def schedule_log(self, request_id: str, log_data: dict[str, Any]) -> None:
        self.calls.append((request_id, log_data))


class _FakeRuntimeSettings:
    async def get_bool(self, key: str) -> bool:
        return False


class _StubVisibilityResolver:
    """Runtime override standing in for the admin visibility table."""

    def __init__(self, required: str) -> None:
        self._required = required

    async def get_effective_required_role(self, canonical: str, required: str) -> str:
        return self._required


def _gated_router(required_role: str = "pro") -> FixedRouter:
    """A router whose embedding_routes gates emb-pro (and its alias)."""
    exe = FixedRouter()
    exe.register_embedding_route(
        "emb-pro",
        [(_FakeAdapter(), 1.0)],
        aliases=["emb-pro-alias"],
        required_role=required_role,
    )
    return exe


def _build_app(
    router_exec,
    logger: _CapturingLogger,
    *,
    user_role: str = "free",
    visibility_resolver=None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(embeddings.router)
    app.dependency_overrides[verify_api_key] = lambda: {
        "user_id": "user-emb",
        "role": user_role,
        "authenticated": True,
    }
    app.dependency_overrides[enforce_user_concurrency] = lambda: None
    app.dependency_overrides[get_router] = lambda: router_exec
    app.dependency_overrides[get_embedding_adapters] = lambda: {
        "emb-pro": _FakeAdapter(),
        "emb-pro-alias": _FakeAdapter(),
    }
    app.dependency_overrides[get_model_visibility_resolver] = lambda: visibility_resolver
    app.dependency_overrides[get_log_store] = lambda: object()  # truthy → logging enabled
    app.dependency_overrides[get_completions_logger] = lambda: logger
    app.dependency_overrides[get_operational_store] = lambda: None
    app.dependency_overrides[get_runtime_settings] = lambda: _FakeRuntimeSettings()
    return app


async def _post(app: FastAPI, model: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/embeddings", json={"model": model, "input": "hi"})
    await asyncio.sleep(0.05)
    return resp


@pytest.mark.asyncio
async def test_required_role_denies_free_caller():
    logger = _CapturingLogger()
    app = _build_app(_gated_router("pro"), logger, user_role="free")

    resp = await _post(app, "emb-pro")

    assert resp.status_code == 404
    # The denial is logged as a 404 row, the same shape as an unknown model.
    assert len(logger.calls) == 1
    assert logger.calls[0][1]["status_code"] == 404
    assert logger.calls[0][1]["error"] == "Embedding model 'emb-pro' not found"


@pytest.mark.asyncio
async def test_required_role_admits_sufficient_role():
    logger = _CapturingLogger()
    app = _build_app(_gated_router("pro"), logger, user_role="pro")

    resp = await _post(app, "emb-pro")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_alias_held_to_model_rules():
    app = _build_app(_gated_router("pro"), _CapturingLogger(), user_role="free")

    resp = await _post(app, "emb-pro-alias")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_only_gate_denies_free_caller():
    exe = FixedRouter()
    exe.register_embedding_route("emb-pro", [(_FakeAdapter(), 1.0)], admin_only=True)
    app = _build_app(exe, _CapturingLogger(), user_role="free")

    resp = await _post(app, "emb-pro")

    assert resp.status_code == 404


@pytest.mark.parametrize(("user_role", "status"), [("free", 404), ("pro", 404), ("admin", 200)])
@pytest.mark.asyncio
async def test_yaml_admin_only_with_free_role_is_served_to_admins_only(tmp_path, user_role, status):
    """admin_only: true + required_role: free, registered from YAML, stays admin-only."""

    class _CountingAdapter(_FakeAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.config.id = "emb-x"
            self.calls = 0

        async def embeddings(self, input_data, **params):
            self.calls += 1
            return await super().embeddings(input_data, **params)

    p = _write_models_yaml(tmp_path, _admin_only_embedding_yaml("free"))
    exe = FixedRouter()
    register_from_models_yaml(exe, p, {})
    adapter = _CountingAdapter()
    app = _build_app(exe, _CapturingLogger(), user_role=user_role)
    # Dispatch goes to a local fake; the gate is the one the YAML registered.
    app.dependency_overrides[get_embedding_adapters] = lambda: {"emb-x": adapter}

    resp = await _post(app, "emb-x")

    assert resp.status_code == status
    assert adapter.calls == (1 if status == 200 else 0)


@pytest.mark.asyncio
async def test_visibility_override_wins_over_baseline():
    app = _build_app(
        _gated_router("pro"),
        _CapturingLogger(),
        user_role="free",
        visibility_resolver=_StubVisibilityResolver("free"),
    )

    resp = await _post(app, "emb-pro")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_router_without_gate_fails_closed():
    """A router with no embedding_routes table at all refuses the request.

    The gate is mandatory, not best-effort: an adapter that dispatches
    without a gate record would skip tier/visibility/grant enforcement
    entirely, so the surface refuses — exactly like an unknown model —
    rather than serving through the gap.
    """

    class _LegacyRouter:
        def __init__(self) -> None:
            self.routes = {}

    logger = _CapturingLogger()
    app = _build_app(_LegacyRouter(), logger, user_role="free")

    resp = await _post(app, "emb-pro")

    assert resp.status_code == 404
    # The refusal is loud internally, silent to the caller.
    assert len(logger.calls) == 1
    assert logger.calls[0][1]["status_code"] == 404


@pytest.mark.asyncio
async def test_adapter_without_gate_record_fails_closed_and_skips_dispatch():
    """Adapter registered, access policy missing → 404, no adapter call.

    This is the fail-closed invariant: normal YAML registration populates
    both tables, so the mismatch can only come from dynamic registration,
    a partial rebuild, or a future refactor — the one state the surface
    must never serve through.
    """

    class _CountingAdapter(_FakeAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def embeddings(self, input_data, **params):
            self.calls += 1
            return await super().embeddings(input_data, **params)

    adapter = _CountingAdapter()
    exe = FixedRouter()  # no embedding_routes entry for emb-pro
    app = _build_app(exe, _CapturingLogger(), user_role="pro")
    app.dependency_overrides[get_embedding_adapters] = lambda: {"emb-pro": adapter}

    resp = await _post(app, "emb-pro")

    assert resp.status_code == 404
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_alias_without_gate_record_fails_closed():
    """An alias present in the adapter table but not the gate table refuses."""

    class _CountingAdapter(_FakeAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def embeddings(self, input_data, **params):
            self.calls += 1
            return await super().embeddings(input_data, **params)

    adapter = _CountingAdapter()
    exe = FixedRouter()
    # Gate record exists for the canonical model only — the alias was added
    # to the adapter table without one (e.g. dynamic registration).
    exe.register_embedding_route("emb-pro", [(_FakeAdapter(), 1.0)], required_role="pro")
    app = _build_app(exe, _CapturingLogger(), user_role="pro")
    app.dependency_overrides[get_embedding_adapters] = lambda: {
        "emb-pro": _FakeAdapter(),
        "emb-pro-alias": adapter,
    }

    resp = await _post(app, "emb-pro-alias")

    assert resp.status_code == 404
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_visibility_override_can_raise_required_role():
    """A runtime override may only tighten the gate: pro caller, model
    raised to admin → refused."""

    class _RaisingResolver:
        async def get_effective_required_role(self, canonical: str, required: str) -> str:
            return "admin"

    app = _build_app(
        _gated_router("pro"),
        _CapturingLogger(),
        user_role="pro",
        visibility_resolver=_RaisingResolver(),
    )

    resp = await _post(app, "emb-pro")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_grant_excluding_embedding_model_denied():
    """An inference grant whose model list omits the model → 404."""
    app = _build_app(_gated_router("pro"), _CapturingLogger(), user_role="pro")
    # Auth dependency returns a grant ctx whose allowed list names another model.
    app.dependency_overrides[verify_api_key] = lambda: {
        "user_id": "grant-user",
        "role": "pro",
        "authenticated": True,
        "agent_allowed_models": ["some-other-model"],
    }

    resp = await _post(app, "emb-pro")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_grant_including_embedding_model_admitted():
    app = _build_app(_gated_router("pro"), _CapturingLogger(), user_role="pro")
    app.dependency_overrides[verify_api_key] = lambda: {
        "user_id": "grant-user",
        "role": "pro",
        "authenticated": True,
        "agent_allowed_models": ["emb-pro"],
    }

    resp = await _post(app, "emb-pro")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_missing_gate_error_names_model_internally(caplog):
    """The internal error names the gap and the model id; the caller's 404
    body never mentions authorization details."""
    import logging

    caplog.set_level(logging.ERROR, logger="serving.servers.routers.embeddings")
    app = _build_app(FixedRouter(), _CapturingLogger(), user_role="free")

    resp = await _post(app, "emb-pro")

    assert resp.status_code == 404
    assert any("embedding_access_policy_missing" in r.message for r in caplog.records)
    assert "embedding" not in resp.text.lower() or "not found" in resp.text.lower()
    # The external detail is the standard unavailable-model message, same as
    # the chat surface's denial — no authorization wording leaks.
    assert "authorization" not in resp.text.lower()
    assert "policy" not in resp.text.lower()
