"""Retired settings: a value an earlier version stored is kept, unlisted and inert.

``BATCH_EXPANSION_ENABLED`` once held the newer batch kinds back. Nothing reads
it now, but a database row or environment value an operator left behind must
neither break a boot nor be lost: a rollback to the version that reads it reads
it again.
"""

from __future__ import annotations

import pytest

from serving.config import app_config
from serving.config.app_config_registry import RETIRED_SETTINGS, static_entries
from serving.config.app_config_store import RESET_SOURCE
from serving.config.settings import get_settings
from tests.fixtures.app_config_store import FakeAppConfigStore, row

RETIRED = "BATCH_EXPANSION_ENABLED"


@pytest.fixture
def config_env(tmp_path, monkeypatch):
    for entry in static_entries():
        monkeypatch.delenv(entry.key, raising=False)
    for key in ("ENVIRONMENT", "DISTRIBUTION_CONFIG_PATH", RETIRED):
        monkeypatch.delenv(key, raising=False)
    models = tmp_path / "models.yaml"
    models.write_text("models: []\n")
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(models))
    monkeypatch.setenv("ROUTING_CONFIG_PATH", str(tmp_path / "routing.yaml"))
    monkeypatch.setenv("ALERTS_CONFIG_PATH", str(tmp_path / "alerts.yaml"))
    get_settings.cache_clear()
    return tmp_path


async def _boot(store: FakeAppConfigStore) -> None:
    await app_config.load(store)
    app_config.attach(store)


def _listed() -> set[str]:
    return {entry["key"] for entry in app_config.describe()["entries"]}


def test_the_expansion_flag_is_retired_not_registered() -> None:
    assert RETIRED in RETIRED_SETTINGS
    assert RETIRED not in {entry.key for entry in static_entries()}


@pytest.mark.asyncio
async def test_a_stored_and_an_environment_value_are_kept_unlisted_and_warned_about(
    config_env, monkeypatch, caplog
) -> None:
    monkeypatch.setenv(RETIRED, "1")
    store = FakeAppConfigStore([row(RETIRED, "0", source="env_import")])

    with caplog.at_level("WARNING", logger="serving.config.app_config"):
        await _boot(store)

    assert store.rows[RETIRED].value == "0", "a boot must not rewrite or drop the old row"
    assert RETIRED not in _listed(), "listed, it would read as a provider variable"
    assert f"{RETIRED} is set in the database and the environment" in caplog.text
    assert "no longer controls anything" in caplog.text


@pytest.mark.asyncio
async def test_an_environment_value_is_not_imported(config_env, monkeypatch, caplog) -> None:
    monkeypatch.setenv(RETIRED, "1")
    store = FakeAppConfigStore()

    with caplog.at_level("WARNING", logger="serving.config.app_config"):
        await _boot(store)

    assert RETIRED not in store.rows
    assert f"{RETIRED} is set in the environment" in caplog.text


@pytest.mark.asyncio
async def test_a_boot_without_it_says_nothing(config_env, caplog) -> None:
    with caplog.at_level("WARNING", logger="serving.config.app_config"):
        await _boot(FakeAppConfigStore())

    assert RETIRED not in caplog.text


@pytest.mark.asyncio
async def test_it_cannot_be_set(config_env) -> None:
    store = FakeAppConfigStore()
    await _boot(store)

    with pytest.raises(app_config.ConfigUpdateError) as excinfo:
        await app_config.update({RETIRED: True}, updated_by="admin")

    assert excinfo.value.status_code == 400
    assert "no longer controls anything" in excinfo.value.detail
    assert store.writes == []


@pytest.mark.asyncio
async def test_an_administrator_may_delete_the_old_row(config_env) -> None:
    store = FakeAppConfigStore([row(RETIRED, "1")])
    await _boot(store)

    change = await app_config.delete(RETIRED, updated_by="admin")

    assert RETIRED not in store.rows
    assert change.audit_details() == {"key": RETIRED, "old_value": "1", "new_value": None}
    with pytest.raises(app_config.ConfigUpdateError) as excinfo:
        await app_config.delete(RETIRED, updated_by="admin")
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_deleting_a_secret_marked_row_audits_no_value(config_env) -> None:
    store = FakeAppConfigStore([row(RETIRED, "synthetic-retired-secret", secret=True)])
    await _boot(store)

    change = await app_config.delete(RETIRED, updated_by="admin")

    assert RETIRED not in store.rows
    assert change.secret is True
    assert change.audit_details() == {"key": RETIRED, "changed": True}
    assert "synthetic-retired-secret" not in repr(change)


@pytest.mark.asyncio
async def test_deleting_a_secret_reset_row_audits_no_value(config_env) -> None:
    reset = row(RETIRED, "", secret=True, source=RESET_SOURCE)
    store = FakeAppConfigStore([reset])
    await _boot(store)

    change = await app_config.delete(RETIRED, updated_by="admin")

    assert RETIRED not in store.rows
    assert change.audit_details() == {"key": RETIRED, "changed": True}


@pytest.mark.asyncio
async def test_a_reference_in_the_model_registry_does_not_list_it_again(config_env) -> None:
    """A YAML ``${BATCH_EXPANSION_ENABLED}`` is not a reason to offer the retired setting."""
    (config_env / "models.yaml").write_text(
        "models:\n"
        "  - id: m\n"
        "    name: m\n"
        "    provider: openai_compat\n"
        "    route:\n"
        "      - kind: openai_compat\n"
        "        base_url: https://upstream.example.invalid/v1\n"
        "        api_key: ${BATCH_EXPANSION_ENABLED}\n"
    )
    store = FakeAppConfigStore([row(RETIRED, "synthetic-retired-secret", secret=True)])
    await _boot(store)

    description = app_config.describe()
    assert RETIRED not in {entry["key"] for entry in description["entries"]}
    assert RETIRED not in description["missing"]
    assert "synthetic-retired-secret" not in str(description)
    assert store.rows[RETIRED].value == "synthetic-retired-secret"


@pytest.mark.asyncio
async def test_a_restore_keeps_the_deployments_row_and_ignores_the_backups(config_env) -> None:
    store = FakeAppConfigStore([row(RETIRED, "1"), row("SITE_NAME", "Before")])
    await _boot(store)

    await app_config.restore_rows(
        [
            {"key": "SITE_NAME", "value": "After", "source": "admin"},
            {"key": RETIRED, "value": "0", "source": "admin"},
        ],
        updated_by="admin",
    )

    assert store.rows["SITE_NAME"].value == "After"
    assert store.rows[RETIRED].value == "1"
