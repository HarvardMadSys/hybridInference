"""Contract for the public /site-config endpoint."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config.distribution import get_distribution_config
from serving.config.settings import get_settings
from serving.servers.routers import site_config

MANIFEST = """\
schema_version: 1
distribution:
  id: freeinference
  display_name: FreeInference
  release: 2026.07.1
site:
  public_base_url: https://freeinference.org
  support_email: admin@freeinference.org
  terms_document: ./content/terms.md
features:
  routers: [fixed, routewise]
  public_signup: true
"""


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("DISTRIBUTION_CONFIG_PATH", raising=False)
    monkeypatch.delenv("DISTRIBUTION_CONFIG_MODE", raising=False)
    get_settings.cache_clear()
    get_distribution_config.cache_clear()
    yield
    get_settings.cache_clear()
    get_distribution_config.cache_clear()


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(site_config.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_neutral_fallback_without_manifest(client):
    resp = await client.get("/site-config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["distribution"]["id"] == "neutral"
    assert body["site"] == {"public_base_url": "", "support_email": ""}
    assert set(body) == {"distribution", "site", "features"}


@pytest.mark.asyncio
async def test_serves_manifest_site_identity(client, monkeypatch, tmp_path):
    manifest = tmp_path / "distribution.yaml"
    manifest.write_text(MANIFEST)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    get_settings.cache_clear()
    get_distribution_config.cache_clear()

    resp = await client.get("/site-config")
    body = resp.json()
    assert body["distribution"] == {
        "id": "freeinference",
        "display_name": "FreeInference",
        "release": "2026.07.1",
    }
    assert body["site"]["public_base_url"] == "https://freeinference.org"
    assert body["site"]["support_email"] == "admin@freeinference.org"
    assert body["features"]["routers"] == ["fixed", "routewise"]


@pytest.mark.asyncio
async def test_never_leaks_server_paths(client, monkeypatch, tmp_path):
    manifest = tmp_path / "distribution.yaml"
    manifest.write_text(MANIFEST)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    get_settings.cache_clear()
    get_distribution_config.cache_clear()

    body = (await client.get("/site-config")).json()
    text = str(body)
    assert "terms.md" not in text
    assert str(tmp_path) not in text
    assert set(body["site"]) == {"public_base_url", "support_email"}
