"""Contract for the public /site-config endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config.distribution import get_distribution_config
from serving.config.settings import get_settings
from serving.servers.routers import site_config

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BRANDING_EXAMPLE = _REPO_ROOT / "config" / "examples" / "branding.example.yaml"

MANIFEST = """\
schema_version: 1
distribution:
  id: example-site
  display_name: Example Site
  release: 2026.07.1
site:
  public_base_url: https://gateway.example.com
  support_email: admin@example.com
  terms_document: ./content/terms.md
features:
  routers: [fixed, routewise]
  public_signup: true
"""


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("DISTRIBUTION_CONFIG_PATH", raising=False)
    monkeypatch.delenv("DISTRIBUTION_CONFIG_MODE", raising=False)
    monkeypatch.delenv("SITE_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("SITE_DOCS_URL", raising=False)
    monkeypatch.delenv("SITE_SUPPORT_EMAIL", raising=False)
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
    assert body["schema_version"] == 1
    assert body["distribution"]["id"] == "neutral"
    assert body["site"] == {"public_base_url": "", "support_email": ""}
    assert body["branding"] is None
    assert set(body) == {"schema_version", "distribution", "site", "features", "branding"}


@pytest.mark.asyncio
async def test_serves_manifest_site_identity(client, monkeypatch, tmp_path):
    manifest = tmp_path / "distribution.yaml"
    manifest.write_text(MANIFEST)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "active")
    get_settings.cache_clear()
    get_distribution_config.cache_clear()

    resp = await client.get("/site-config")
    body = resp.json()
    assert body["distribution"] == {
        "id": "example-site",
        "display_name": "Example Site",
        "release": "2026.07.1",
    }
    assert body["site"]["public_base_url"] == "https://gateway.example.com"
    assert body["site"]["support_email"] == "admin@example.com"
    assert body["features"]["routers"] == ["fixed", "routewise"]
    assert body["branding"] is None


@pytest.mark.asyncio
async def test_site_identity_env_overrides_manifest_site_fields(client, monkeypatch, tmp_path):
    manifest = tmp_path / "distribution.yaml"
    manifest.write_text(MANIFEST)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "active")
    monkeypatch.setenv("SITE_PUBLIC_BASE_URL", "http://localhost:13001")
    monkeypatch.setenv("SITE_SUPPORT_EMAIL", "support@local.dev")
    get_settings.cache_clear()
    get_distribution_config.cache_clear()

    body = (await client.get("/site-config")).json()

    assert body["site"] == {
        "public_base_url": "http://localhost:13001",
        "support_email": "support@local.dev",
    }
    assert body["distribution"] == {
        "id": "example-site",
        "display_name": "Example Site",
        "release": "2026.07.1",
    }
    assert body["features"]["public_signup"] is True


@pytest.mark.asyncio
async def test_dark_mode_does_not_publish_manifest_identity(client, monkeypatch, tmp_path):
    manifest = tmp_path / "distribution.yaml"
    manifest.write_text(MANIFEST)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "dark")
    get_settings.cache_clear()
    get_distribution_config.cache_clear()

    body = (await client.get("/site-config")).json()
    assert body["distribution"]["id"] == "neutral"
    assert body["site"] == {"public_base_url": "", "support_email": ""}
    assert body["branding"] is None


@pytest.mark.asyncio
async def test_serves_validated_public_branding_with_docs_override(client, monkeypatch, tmp_path):
    branding = tmp_path / "branding" / "site.yaml"
    branding.parent.mkdir()
    branding.write_text(_BRANDING_EXAMPLE.read_text())
    manifest = tmp_path / "distribution.yaml"
    manifest.write_text(
        MANIFEST.replace(
            "features:\n",
            "  branding: ./branding/site.yaml\nfeatures:\n",
        )
    )
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "active")
    monkeypatch.setenv("SITE_DOCS_URL", "https://override-docs.example.com")
    get_settings.cache_clear()
    get_distribution_config.cache_clear()

    body = (await client.get("/site-config")).json()

    assert body["schema_version"] == 1
    public_branding = body["branding"]
    assert set(public_branding) == {
        "app_description",
        "site_host",
        "organization",
        "links",
        "example",
        "analytics",
        "signup",
        "storage_key_prefix",
        "data_policy_notice",
        "assets",
        "team",
        "sponsors",
    }
    assert public_branding["links"] == {
        "docs_url": "https://override-docs.example.com",
        "status_url": "https://status.example.com",
        "github_url": "https://github.com/example/hybridInference",
    }
    assert public_branding["signup"] == {
        "turnstile_site_key": "",
        "fast_track_domain": "",
        "fast_track_org": "",
    }
    assert public_branding["example"]["api_key_env_var"] == "EXAMPLE_INFERENCE_API_KEY"
    assert "schema_version" not in public_branding
    assert str(tmp_path) not in str(body)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_docs_url",
    [
        "http://insecure.example.com",
        "https://docs.example.com?language=en",
        "https://docs.example.com#install",
    ],
)
async def test_invalid_docs_override_cannot_invalidate_runtime_branding(
    client, monkeypatch, tmp_path, invalid_docs_url
):
    branding = tmp_path / "branding" / "site.yaml"
    branding.parent.mkdir()
    branding.write_text(_BRANDING_EXAMPLE.read_text())
    manifest = tmp_path / "distribution.yaml"
    manifest.write_text(
        MANIFEST.replace(
            "features:\n",
            "  branding: ./branding/site.yaml\nfeatures:\n",
        )
    )
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "active")
    monkeypatch.setenv("SITE_DOCS_URL", invalid_docs_url)
    get_settings.cache_clear()
    get_distribution_config.cache_clear()

    body = (await client.get("/site-config")).json()

    assert body["branding"]["links"]["docs_url"] == "https://docs.example.com"
    assert body["branding"]["site_host"] == "inference.example.com"


@pytest.mark.asyncio
async def test_never_leaks_server_paths(client, monkeypatch, tmp_path):
    manifest = tmp_path / "distribution.yaml"
    manifest.write_text(MANIFEST)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "active")
    get_settings.cache_clear()
    get_distribution_config.cache_clear()

    body = (await client.get("/site-config")).json()
    text = str(body)
    assert "terms.md" not in text
    assert str(tmp_path) not in text
    assert set(body["site"]) == {"public_base_url", "support_email"}
