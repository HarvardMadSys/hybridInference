"""Contract for OpenInfra's routes: the opt-in public reads, the token-gated ingest, the export."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config.distribution import get_distribution_config
from serving.config.settings import get_settings
from serving.openinfra import config, dashboard
from serving.servers.deps import get_db_logger, get_operational_store, verify_admin_access
from serving.servers.routers import openinfra, site_config
from serving.servers.routers.admin import openinfra as admin_openinfra
from tests.fixtures.application_documents import seed_site

MANIFEST = """\
schema_version: 1
distribution:
  id: example-site
  display_name: Example Site
  release: 2026.11.1
features:
  routers: [fixed]
{extra}"""

TOKEN = "test-ingest-token-0123456789abcdef"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("DISTRIBUTION_CONFIG_PATH", raising=False)
    monkeypatch.delenv("DISTRIBUTION_CONFIG_MODE", raising=False)
    get_settings.cache_clear()
    get_distribution_config.cache_clear()
    openinfra._DASHBOARDS.clear()
    openinfra._HISTORIES.clear()
    yield
    get_settings.cache_clear()
    get_distribution_config.cache_clear()
    openinfra._DASHBOARDS.clear()
    openinfra._HISTORIES.clear()


def _publish(monkeypatch, tmp_path, extra: str = "  openinfra: true\n") -> None:
    manifest = tmp_path / "distribution.yaml"
    manifest.write_text(MANIFEST.format(extra=extra))
    seed_site(monkeypatch, manifest)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "active")
    get_settings.cache_clear()
    get_distribution_config.cache_clear()


def _settings(monkeypatch, **values: str) -> None:
    monkeypatch.setattr(config, "config_value", lambda key, default=None: values.get(key, default))


class FakeConn:
    """Records statements; transactions are no-ops."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    @asynccontextmanager
    async def _transaction(self):
        yield

    def transaction(self, **_kwargs):
        return self._transaction()

    async def execute(self, sql, *args):
        self.statements.append(sql)

    async def executemany(self, sql, rows):
        self.statements.append(sql)


def _db(conn: FakeConn | None = None):
    conn = conn or FakeConn()

    @asynccontextmanager
    async def acquire():
        yield conn

    db = MagicMock()
    db.pool.acquire = acquire
    return db


def _app(db) -> FastAPI:
    app = FastAPI()
    app.include_router(openinfra.router)
    app.include_router(site_config.router)
    app.include_router(admin_openinfra.router)
    app.dependency_overrides[get_db_logger] = lambda: db
    app.dependency_overrides[get_operational_store] = lambda: None
    return app


async def _call(db, method: str, path: str, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


@pytest.fixture
def builds(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_dashboard(conn, **kwargs):
        calls.append(("dashboard", kwargs))
        return {"generated_at": 1, "fleet": "healthy", "sglang": []}

    async def fake_history(conn, hours, **kwargs):
        calls.append(("history", {"hours": hours, **kwargs}))
        return {"generated_at": 1, "grain_ms": 60_000, "gpu": [], "sglang": [], "cpu": []}

    monkeypatch.setattr(dashboard, "build_dashboard", fake_dashboard)
    monkeypatch.setattr(dashboard, "build_history", fake_history)
    return calls


# --- public reads ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/public-openinfra", "/public-openinfra/history"])
async def test_hidden_without_a_manifest(builds, path):
    assert (await _call(_db(), "GET", path)).status_code == 404
    assert builds == []


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", ["", "  openinfra: false\n"])
async def test_hidden_unless_the_distribution_opts_in(monkeypatch, tmp_path, builds, extra):
    _publish(monkeypatch, tmp_path, extra)
    assert (await _call(_db(), "GET", "/public-openinfra")).status_code == 404


@pytest.mark.asyncio
async def test_serves_the_dashboard_with_cache_headers(monkeypatch, tmp_path, builds):
    _publish(monkeypatch, tmp_path)
    _settings(monkeypatch, OPENINFRA_ENGINE_ORDER="b, a ,b", OPENINFRA_RETIRED_ENGINES="old,,older")
    resp = await _call(_db(), "GET", "/public-openinfra")
    assert resp.status_code == 200
    assert resp.json() == {"generated_at": 1, "fleet": "healthy", "sglang": []}
    assert resp.headers["cache-control"] == "public, max-age=10"
    assert builds == [("dashboard", {"retired": ("old", "older"), "order": ("b", "a")})]


@pytest.mark.asyncio
async def test_a_burst_of_readers_costs_one_build(monkeypatch, tmp_path, builds):
    _publish(monkeypatch, tmp_path)
    db = _db()
    async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url="http://test") as client:
        for _ in range(5):
            assert (await client.get("/public-openinfra")).status_code == 200
            assert (await client.get("/public-openinfra/history?hours=24")).status_code == 200
    assert [kind for kind, _ in builds] == ["dashboard", "history"]


@pytest.mark.asyncio
async def test_history_passes_the_normalized_window_and_node(monkeypatch, tmp_path, builds):
    _publish(monkeypatch, tmp_path)
    resp = await _call(_db(), "GET", "/public-openinfra/history?hours=1.3&node=node-a")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=30"
    assert builds == [("history", {"hours": 1.25, "node": "node-a", "retired": ()})]


@pytest.mark.parametrize(
    ("raw", "hours"),
    [
        (None, 2.0),
        ("abc", 2.0),
        ("nan", 2.0),
        ("inf", 2.0),
        ("0", 0.25),
        ("0.1", 0.25),
        ("24", 24.0),
        ("1000", 168.0),
        ("6.9", 7.0),
    ],
)
def test_history_hours(raw, hours):
    assert openinfra.history_hours(raw) == hours


@pytest.mark.asyncio
async def test_a_very_long_node_is_refused(monkeypatch, tmp_path, builds):
    _publish(monkeypatch, tmp_path)
    resp = await _call(_db(), "GET", f"/public-openinfra/history?node={'x' * 201}")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_no_database_is_a_503(monkeypatch, tmp_path, builds):
    _publish(monkeypatch, tmp_path)
    db = MagicMock()
    db.pool = None
    assert (await _call(db, "GET", "/public-openinfra")).status_code == 503


@pytest.mark.asyncio
async def test_a_failed_read_is_a_503_and_is_not_cached(monkeypatch, tmp_path):
    _publish(monkeypatch, tmp_path)
    attempts = []

    async def failing(conn, **kwargs):
        attempts.append(1)
        raise RuntimeError("down")

    monkeypatch.setattr(dashboard, "build_dashboard", failing)
    db = _db()
    assert (await _call(db, "GET", "/public-openinfra")).status_code == 503
    assert (await _call(db, "GET", "/public-openinfra")).status_code == 503
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_site_config_omits_the_flag_while_it_is_off(monkeypatch, tmp_path):
    _publish(monkeypatch, tmp_path, "")
    features = (await _call(_db(), "GET", "/site-config")).json()["features"]
    assert "openinfra" not in features


@pytest.mark.asyncio
async def test_site_config_reports_the_flag_when_enabled(monkeypatch, tmp_path):
    _publish(monkeypatch, tmp_path)
    features = (await _call(_db(), "GET", "/site-config")).json()["features"]
    assert features["openinfra"] is True


# --- ingest ------------------------------------------------------------------

SNAPSHOT = {
    "node": {"id": "node-a", "cpu": {"cpu_util_pct": 5}},
    "gpus": [{"index": 0, "utilization_pct": 10}, {"index": 1}],
    "sglang": [{"server": {"id": "chat-a"}, "metrics": {"num_running_reqs": 2}}],
}


async def _ingest(conn: FakeConn | None = None, *, body=None, headers=None, **kwargs):
    content = json.dumps(SNAPSHOT if body is None else body)
    return await _call(
        _db(conn),
        "POST",
        "/internal/openinfra/ingest",
        content=content,
        headers={"Content-Type": "application/json", **(headers or {})},
        **kwargs,
    )


@pytest.mark.asyncio
async def test_ingest_is_absent_without_a_token(monkeypatch):
    _settings(monkeypatch)
    resp = await _ingest(headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["type"] == "openinfra_ingest_not_configured"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}, {"x-ingest-token": "no"}],
)
async def test_ingest_refuses_a_missing_or_wrong_token(monkeypatch, headers):
    _settings(monkeypatch, OPENINFRA_INGEST_TOKEN=TOKEN)
    conn = FakeConn()
    resp = await _ingest(conn, headers=headers)
    assert resp.status_code == 401
    assert conn.statements == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers", [{"Authorization": f"Bearer {TOKEN}"}, {"x-ingest-token": TOKEN}]
)
async def test_ingest_stores_a_snapshot(monkeypatch, headers):
    _settings(monkeypatch, OPENINFRA_INGEST_TOKEN=f"  {TOKEN}  ")
    conn = FakeConn()
    resp = await _ingest(conn, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "gpus": 2, "sglang": 1}
    written = " ".join(conn.statements)
    for table in ("openinfra_nodes", "openinfra_gpus", "openinfra_engines", "cpu_samples"):
        assert table in written


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "message"),
    [
        ([], "body must be an object"),
        ({}, "body must include node, gpus, or sglang"),
        ({"gpus": [{"index": 0}]}, "node is required when ingesting gpus"),
        ({"node": {"hostname": "x"}}, "node.id is required"),
        ({"node": {"id": "a"}, "gpus": [{"index": 1.5}]}, "gpu.index must be"),
        ({"sglang": {"server": {"id": "e"}}}, "sglang requires metrics or prometheus text"),
    ],
)
async def test_ingest_refuses_a_body_of_the_wrong_shape(monkeypatch, body, message):
    _settings(monkeypatch, OPENINFRA_INGEST_TOKEN=TOKEN)
    conn = FakeConn()
    resp = await _ingest(conn, body=body, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 400
    assert message in resp.json()["detail"]["error"]["message"]
    assert conn.statements == []


@pytest.mark.asyncio
async def test_ingest_refuses_text_that_is_not_json(monkeypatch):
    _settings(monkeypatch, OPENINFRA_INGEST_TOKEN=TOKEN)
    resp = await _call(
        _db(),
        "POST",
        "/internal/openinfra/ingest",
        content=b"{not json",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_ingest_refuses_an_oversized_body(monkeypatch):
    _settings(monkeypatch, OPENINFRA_INGEST_TOKEN=TOKEN)
    big = {"node": {"id": "a", "metadata": "x" * (openinfra.MAX_BODY_BYTES + 1)}}
    resp = await _ingest(body=big, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_ingest_without_a_database_is_a_503(monkeypatch):
    _settings(monkeypatch, OPENINFRA_INGEST_TOKEN=TOKEN)
    db = MagicMock()
    db.pool = None
    resp = await _call(
        db,
        "POST",
        "/internal/openinfra/ingest",
        content=json.dumps(SNAPSHOT),
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code == 503


# --- export ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_needs_an_administrator():
    resp = await _call(_db(), "GET", "/admin/openinfra/export")
    assert resp.status_code == 401
    assert "content-disposition" not in resp.headers


@pytest.mark.asyncio
async def test_export_is_an_attachment(monkeypatch):
    seen = {}

    async def fake_export(conn, days, **kwargs):
        seen["days"] = days
        return {"exported_at": 1_800_000_000_000, "days": 14, "servers": []}

    monkeypatch.setattr(dashboard, "build_export", fake_export)
    app = _app(_db())
    app.dependency_overrides[verify_admin_access] = lambda: "admin-id"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/admin/openinfra/export?days=14")
        too_many = await client.get("/admin/openinfra/export?days=31")
    assert resp.status_code == 200
    assert seen == {"days": 14.0}
    assert resp.headers["content-disposition"] == (
        'attachment; filename="openinfra-export-14d-2027-01-15.json"'
    )
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json()["servers"] == []
    assert too_many.status_code == 422
