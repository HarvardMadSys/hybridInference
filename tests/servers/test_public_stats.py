"""Contract for the opt-in public usage stats endpoint and its site-config flag."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config.distribution import get_distribution_config
from serving.config.settings import get_settings
from serving.servers.deps import get_db_logger
from serving.servers.routers import public_stats, site_config
from tests.fixtures.application_documents import seed_site

MANIFEST = """\
schema_version: 1
distribution:
  id: example-site
  display_name: Example Site
  release: 2026.10.1
features:
  routers: [fixed]
{extra}"""

SNAPSHOT = {"schema_version": 1, "totals": {"tokens": 42}}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("DISTRIBUTION_CONFIG_PATH", raising=False)
    monkeypatch.delenv("DISTRIBUTION_CONFIG_MODE", raising=False)
    get_settings.cache_clear()
    get_distribution_config.cache_clear()
    yield
    get_settings.cache_clear()
    get_distribution_config.cache_clear()


def _activate(monkeypatch, tmp_path, extra: str = "") -> None:
    manifest = tmp_path / "distribution.yaml"
    manifest.write_text(MANIFEST.format(extra=extra))
    seed_site(monkeypatch, manifest)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "active")
    get_settings.cache_clear()
    get_distribution_config.cache_clear()


def _db(row=None, *, fail: bool = False):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("down") if fail else None, return_value=row)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    db = MagicMock()
    db.pool.acquire = MagicMock(return_value=acquire)
    return db


async def _get(db, path: str = "/public-stats"):
    app = FastAPI()
    app.include_router(public_stats.router)
    app.include_router(site_config.router)
    app.dependency_overrides[get_db_logger] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_hidden_without_a_manifest():
    resp = await _get(_db({"payload": json.dumps(SNAPSHOT)}))
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", ["", "  public_stats: false\n"])
async def test_hidden_unless_the_distribution_opts_in(monkeypatch, tmp_path, extra):
    _activate(monkeypatch, tmp_path, extra)
    resp = await _get(_db({"payload": json.dumps(SNAPSHOT)}))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_serves_the_newest_snapshot_with_cache_headers(monkeypatch, tmp_path):
    _activate(monkeypatch, tmp_path, "  public_stats: true\n")
    resp = await _get(_db({"payload": json.dumps(SNAPSHOT)}))
    assert resp.status_code == 200
    assert resp.json() == SNAPSHOT
    assert resp.headers["cache-control"] == "public, max-age=600"


@pytest.mark.asyncio
async def test_decoded_jsonb_payload_is_served_as_is(monkeypatch, tmp_path):
    _activate(monkeypatch, tmp_path, "  public_stats: true\n")
    resp = await _get(_db({"payload": SNAPSHOT}))
    assert resp.json() == SNAPSHOT


@pytest.mark.asyncio
async def test_not_found_before_the_first_snapshot(monkeypatch, tmp_path):
    _activate(monkeypatch, tmp_path, "  public_stats: true\n")
    resp = await _get(_db(None))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_database_failure_is_a_503(monkeypatch, tmp_path):
    _activate(monkeypatch, tmp_path, "  public_stats: true\n")
    resp = await _get(_db(fail=True))
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_site_config_omits_the_flag_while_it_is_off(monkeypatch, tmp_path):
    # Consoles parse `features` strictly; one that predates the flag must keep
    # loading the document on every deployment that has not turned it on.
    _activate(monkeypatch, tmp_path)
    features = (await _get(_db(), "/site-config")).json()["features"]
    assert "public_stats" not in features


@pytest.mark.asyncio
async def test_site_config_reports_the_flag_when_enabled(monkeypatch, tmp_path):
    _activate(monkeypatch, tmp_path, "  public_stats: true\n")
    features = (await _get(_db(), "/site-config")).json()["features"]
    assert features["public_stats"] is True


async def _get_many(db, count: int, path: str = "/public-stats"):
    app = FastAPI()
    app.include_router(public_stats.router)
    app.dependency_overrides[get_db_logger] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return [await client.get(path) for _ in range(count)]


def _reads(db) -> int:
    return db.pool.acquire.return_value.__aenter__.return_value.fetchrow.await_count


@pytest.mark.asyncio
async def test_anonymous_callers_share_one_read_of_the_snapshot(monkeypatch, tmp_path):
    """Anyone may ask, as often as they like: the database sees one read a window."""
    _activate(monkeypatch, tmp_path, "  public_stats: true\n")
    db = _db({"payload": json.dumps(SNAPSHOT)})

    responses = await _get_many(db, 5)

    assert {r.status_code for r in responses} == {200}
    assert all(r.json() == SNAPSHOT for r in responses)
    assert all(r.headers["cache-control"] == "public, max-age=600" for r in responses)
    assert _reads(db) == 1


@pytest.mark.asyncio
async def test_no_snapshot_yet_is_kept_too_and_a_failure_is_not(monkeypatch, tmp_path):
    _activate(monkeypatch, tmp_path, "  public_stats: true\n")
    missing = _db(None)
    assert [r.status_code for r in await _get_many(missing, 3)] == [404, 404, 404]
    assert _reads(missing) == 1

    failing = _db(fail=True)
    assert [r.status_code for r in await _get_many(failing, 2)] == [503, 503]
    assert _reads(failing) == 2


@pytest.mark.asyncio
async def test_turning_the_flag_off_hides_a_kept_snapshot_at_once(monkeypatch, tmp_path):
    _activate(monkeypatch, tmp_path, "  public_stats: true\n")
    db = _db({"payload": json.dumps(SNAPSHOT)})
    assert (await _get_many(db, 1))[0].status_code == 200

    _activate(monkeypatch, tmp_path, "  public_stats: false\n")

    assert (await _get_many(db, 1))[0].status_code == 404
