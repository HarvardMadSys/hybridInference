"""Static deployment wiring tests for DB-IP Country Lite data."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_compose_mounts_country_database_and_declares_provider() -> None:
    compose = (REPO_ROOT / "deploy/docker/docker-compose.yml").read_text()

    assert "GEOIP_COUNTRY_DB: /app/var/data/geoip/dbip-country-lite.mmdb" in compose
    assert "GEOIP_COUNTRY_PROVIDER: dbip-lite" in compose
    assert "../../var/data:/app/var/data" in compose


def test_deploy_scripts_refresh_country_database_best_effort() -> None:
    for name in ("deploy_staging.sh", "deploy_production.sh"):
        script = (REPO_ROOT / "ops/deploy" / name).read_text()
        assert "if ! ops/setup/update_dbip_country_lite.sh; then" in script
        assert "retaining the last good database" in script
