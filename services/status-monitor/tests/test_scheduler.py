"""Tests for the scheduler's discovery and probe-cycle reconciliation."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from status_monitor.config import AppConfig, E2EModelOverride, GatewayConfig, RegistryConfig, Settings
from status_monitor.prober import ProbeResult
from status_monitor.scheduler import discover_targets, probe_once
from status_monitor.state import StatusStore

pytestmark = pytest.mark.asyncio


async def test_probe_once_prunes_when_no_targets(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = StatusStore(history_size=5, state_path=str(state_path))
    store.record(
        ProbeResult(model_id="gone", ok=True, checked_at="2026-06-13T00:00:00+00:00")
    )

    # Discovery disabled + no registry => zero targets; the stale model must be
    # pruned (and persisted) rather than lingering on the dashboard.
    config = AppConfig(
        settings=Settings(),
        gateway=GatewayConfig(discover_models=False),
        registry=RegistryConfig(path=None),
    )
    await probe_once(config, store)

    assert store.snapshot()["total"] == 0
    assert state_path.is_file()


async def test_discover_targets_from_gateway_catalog() -> None:
    # The gateway catalog is already role/visibility-filtered; we probe exactly
    # what it returns, detecting embedding models and applying overrides.
    catalog = {
        "data": [
            {"id": "glm-4.7", "output_modalities": ["text"]},
            {"id": "bge-m3", "output_modalities": ["embedding"]},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/models"
        assert request.headers["Authorization"] == "Bearer k"
        return httpx.Response(200, json=catalog)

    config = AppConfig(
        gateway=GatewayConfig(base_url="http://gw:8080", api_key="k"),
        e2e_models=[E2EModelOverride(model_id="bge-m3"), E2EModelOverride(model_id="extra")],
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        targets = await discover_targets(config, client)

    assert targets is not None
    by_id = {t.model_id: t for t in targets}
    assert set(by_id) == {"glm-4.7", "bge-m3", "extra"}
    assert by_id["bge-m3"].kind == "embedding"
    assert by_id["bge-m3"].streaming is False  # embeddings never stream
    assert by_id["extra"].kind == "chat"  # override-only model appended


async def test_discover_targets_falls_back_on_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    config = AppConfig(gateway=GatewayConfig(base_url="http://gw:8080", api_key="k"))
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        assert await discover_targets(config, client) is None  # signals static fallback


async def test_probe_once_skips_recording_on_uniform_401(tmp_path: Path) -> None:
    # Catalog loads (anonymous), but every probe 401s => credential failure, not
    # a dozen false outages. The store must not record the all-down results.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/models":
            return httpx.Response(200, json={"data": [{"id": "glm-4.7"}]})
        return httpx.Response(401, text="unauthorized")

    store = StatusStore(history_size=5, state_path=str(tmp_path / "state.json"))
    config = AppConfig(
        settings=Settings(),
        gateway=GatewayConfig(base_url="http://gw:8080", api_key="bad"),
    )
    transport = httpx.MockTransport(handler)
    # probe_once builds its own client, so patch discovery to use our transport
    # by routing through a monkeypatched AsyncClient is overkill; instead probe
    # via the public path and assert nothing was recorded.
    import status_monitor.scheduler as sched

    orig = httpx.AsyncClient

    def client_factory(*args, **kwargs):  # noqa: ANN002, ANN003
        kwargs.pop("timeout", None)
        return orig(transport=transport)

    sched.httpx.AsyncClient = client_factory  # type: ignore[assignment]
    try:
        await sched.probe_once(config, store)
    finally:
        sched.httpx.AsyncClient = orig  # type: ignore[assignment]

    assert store.snapshot()["total"] == 0


async def test_discover_targets_disabled_returns_none() -> None:
    config = AppConfig(gateway=GatewayConfig(discover_models=False))
    async with httpx.AsyncClient() as client:
        assert await discover_targets(config, client) is None
