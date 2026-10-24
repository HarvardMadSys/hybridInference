"""Key-pool exhaustion surfaces as 429, not 500, on the OpenAI-compatible surfaces (#1440).

When every key an endpoint could use is muted (typically the tail of a
provider 429 storm) or reserved above the caller's tier, the adapter raises
``KeyPoolExhausted`` before anything is sent. The exception carries no HTTP
status, so both status-derivation helpers used to fall through to their 500
default — while the ``anthropic_messages`` surface answers the same condition
with a deliberate 429 so clients back off instead of hammering retries for the
whole mute window (``KeyPoolExhausted`` mutes clear in 20s; see key_pool).
These tests pin the 429 mapping on the remaining surfaces.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from routing.routers import AllCircuitsOpenError
from serving.adapters.key_pool import KeyPoolExhausted, KeyPoolRoleRestricted
from serving.servers.routers.completions_stream import _extract_exception_status_code
from serving.servers.routers.routing_info import _status_code_from_exception


def test_extract_exception_status_code_key_pool_exhausted_is_429():
    assert _extract_exception_status_code(KeyPoolExhausted("all keys muted")) == 429


def test_extract_exception_status_code_role_restricted_is_429():
    """``KeyPoolRoleRestricted`` subclasses ``KeyPoolExhausted`` and is a
    rate-limit-shaped answer for the caller it shuts out."""
    assert _extract_exception_status_code(KeyPoolRoleRestricted("reserved for pro")) == 429


def test_extract_exception_status_code_all_circuits_open_still_503():
    assert _extract_exception_status_code(AllCircuitsOpenError("all open")) == 503


def test_extract_exception_status_code_real_status_still_wins():
    """A genuine upstream status is relayed verbatim, not overwritten with 429."""

    class _Upstream429:
        status = 429

    class _Upstream502(Exception):
        status_code = 502

    assert _extract_exception_status_code(_Upstream502()) == 502
    assert _extract_exception_status_code(_Upstream429()) == 429


def test_status_code_from_exception_key_pool_exhausted_is_429():
    assert _status_code_from_exception(KeyPoolExhausted("all keys muted")) == 429
    assert _status_code_from_exception(KeyPoolRoleRestricted("reserved for pro")) == 429


def test_status_code_from_exception_default_still_500():
    assert _status_code_from_exception(RuntimeError("boom")) == 500


# ---------------------------------------------------------------------------
# Embeddings handler: the same exhaustion reaches /v1/embeddings through
# ``OpenAICompatAdapter.embeddings`` -> ``_post_with_pool`` and used to fall
# into the generic ``except Exception`` -> 500 path.
# ---------------------------------------------------------------------------


class _FakeAdapter:
    def __init__(self, *, raises: Exception | None = None):
        from types import SimpleNamespace

        self.config = SimpleNamespace(
            provider="fake-provider",
            pricing={"prompt": "0", "completion": "0"},
        )
        self._raises = raises

    async def embeddings(self, input_data, **params):
        if self._raises is not None:
            raise self._raises
        return {"data": [{"embedding": [0.0], "index": 0}], "usage": {}}


class _FakeRoute:
    """Enough of a routing entry for ``model_access_denial`` (see #1602).

    The embeddings tier gate resolves the model's access-gate record off the
    router's ``embedding_routes`` table before serving; a published free-tier
    record lets the request through to the adapter, where the exhaustion is
    raised. Inert on branches without the gate: the handler never reads it.
    """

    def __init__(self) -> None:
        from types import SimpleNamespace

        adapter = SimpleNamespace(config=SimpleNamespace(id="emb-model"))
        self.adapters = [(adapter, 1.0)]
        self.published = True
        self.required_role = "free"
        self.admin_only = False


class _FakeRouterExec:
    def __init__(self) -> None:
        self.embedding_routes = {"emb-model": _FakeRoute()}


class _CapturingLogger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def schedule_log(self, request_id: str, log_data: dict[str, Any]) -> None:
        self.calls.append((request_id, log_data))


class _FakeRuntimeSettings:
    def __init__(self, log_synthetic_probes: bool = False) -> None:
        self._log_synthetic_probes = log_synthetic_probes

    async def get_bool(self, key: str) -> bool:
        return self._log_synthetic_probes


def _build_app(adapter: _FakeAdapter, logger: _CapturingLogger) -> FastAPI:
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
    from serving.servers.routers import embeddings

    app = FastAPI()
    app.include_router(embeddings.router)
    app.dependency_overrides[verify_api_key] = lambda: {
        "user_id": "user-emb",
        "role": "internal",
        "authenticated": True,
    }
    app.dependency_overrides[enforce_user_concurrency] = lambda: None
    app.dependency_overrides[get_embedding_adapters] = lambda: {"emb-model": adapter}
    app.dependency_overrides[get_log_store] = lambda: object()  # truthy → logging enabled
    app.dependency_overrides[get_completions_logger] = lambda: logger
    app.dependency_overrides[get_operational_store] = lambda: None
    app.dependency_overrides[get_runtime_settings] = lambda: _FakeRuntimeSettings()
    # The tier gate (#1602) sources these from app.state.services; overriding
    # them keeps the app dependency-free without building that state. Both
    # overrides are simply unused where the gate does not exist yet.
    app.dependency_overrides[get_router] = lambda: _FakeRouterExec()
    app.dependency_overrides[get_model_visibility_resolver] = lambda: None
    return app


@pytest.mark.asyncio
async def test_embeddings_key_pool_exhausted_answers_429():
    import asyncio

    from httpx import ASGITransport, AsyncClient

    adapter = _FakeAdapter(
        raises=KeyPoolExhausted(
            "No usable API key for provider 'fake' (role=pro, 2 configured): "
            "every key the caller may use is muted or reserved for a higher tier"
        )
    )
    logger = _CapturingLogger()
    app = _build_app(adapter, logger)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/embeddings", json={"model": "emb-model", "input": "hello"}
        )
    await asyncio.sleep(0.05)

    assert response.status_code == 429
    # The api_logs row matches what the client saw, instead of filing the
    # burst as a 500 outage.
    assert len(logger.calls) == 1
    assert logger.calls[0][1]["status_code"] == 429
