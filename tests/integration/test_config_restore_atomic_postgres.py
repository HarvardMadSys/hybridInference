"""A configuration restore commits every table or none of them (PostgreSQL).

The operational and batch tables used to be written on connections of their
own, from inside the ``app_config`` transaction: a failure part-way rolled
``app_config`` back and left provider keys, settings and routes replaced. They
are now written on the transaction's own connection.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from serving.config import app_config
from serving.config.app_config_store import AppConfigStore, ConfigRow
from serving.schemas_config import ConfigBackupPayload
from serving.servers.routers.admin import config_backup
from serving.storage.batch_store import BatchStore
from serving.storage.cache import CachedOperationalStore, InMemoryCache
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest, pytest.mark.asyncio]

OLD_KEY = "sk-itest-restore-old-0123456789abcdef"


@pytest_asyncio.fixture
async def stores(monkeypatch, tmp_path):
    from serving.config.app_config_registry import static_entries
    from serving.config.settings import get_settings

    for entry in static_entries():
        monkeypatch.delenv(entry.key, raising=False)
    for name in ("MODELS_CONFIG_PATH", "ROUTING_CONFIG_PATH", "ALERTS_CONFIG_PATH"):
        monkeypatch.setenv(name, str(tmp_path / name))
    get_settings.cache_clear()

    pool = await postgres.create_pool(min_size=1, max_size=4)

    async def wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM app_config WHERE key = 'SITE_NAME'")
            await conn.execute("DELETE FROM provider_api_keys WHERE provider = 'itest-restore'")
            await conn.execute("DELETE FROM site_settings WHERE key = 'auto_mode_enabled'")
            await conn.execute("DELETE FROM signup_allowed_domains WHERE domain LIKE 'itest-%'")
            await conn.execute("DELETE FROM batch_model_limits WHERE model_id = 'itest-model'")

    await wipe()
    config_store = AppConfigStore(pool)
    await config_store.write([ConfigRow("SITE_NAME", "Before Restore")])
    await app_config.load(config_store, bootstrap=False)
    app_config.attach(config_store)
    op_store = CachedOperationalStore(PostgresOperationalStore(pool), InMemoryCache())
    batch_store = BatchStore(pool)
    await op_store.add_provider_key(
        provider="itest-restore",
        api_key=OLD_KEY,
        label="before",
        created_by=None,
        key_id="itest-restore-key",
    )
    await op_store.set_setting("auto_mode_enabled", "false", "bool", "itest")
    try:
        yield pool, op_store, batch_store
    finally:
        await wipe()
        await pool.close()


def _payload(**overrides) -> ConfigBackupPayload:
    data = {
        "schema_version": 1,
        "app_config": [{"key": "SITE_NAME", "value": "After Restore", "source": "admin"}],
        "site_settings": [{"key": "auto_mode_enabled", "value": "true", "value_type": "bool"}],
        "provider_keys": [
            {
                "id": "itest-restore-key",
                "provider": "itest-restore",
                "api_key": "sk-itest-restore-new-0123456789abcdef",
                "label": "after",
            }
        ],
        "batch_model_limits": {"itest-model": 2},
        "signup_allowed_domains": [{"domain": "itest-restore.example", "is_wildcard": False}],
    }
    data.update(overrides)
    return ConfigBackupPayload(**data)


async def _restore(op_store, batch_store, payload, *, admin_user_id) -> app_config.RestoreSummary:
    async def apply_in_transaction(transaction) -> None:
        async with config_backup._stores_in(transaction, op_store, batch_store) as (
            tx_op,
            tx_batch,
        ):
            await config_backup._apply_operational_and_batch_stores(
                tx_op, tx_batch, payload, "itest-admin", admin_user_id
            )

    return await app_config.restore_rows(
        [row.model_dump() for row in payload.app_config],
        updated_by="itest-admin",
        before_commit=apply_in_transaction,
    )


async def _state(pool) -> dict:
    async with pool.acquire() as conn:
        return {
            "site_name": await conn.fetchval(
                "SELECT value FROM app_config WHERE key = 'SITE_NAME'"
            ),
            "key": await conn.fetchval(
                "SELECT api_key FROM provider_api_keys WHERE id = 'itest-restore-key'"
            ),
            "setting": await conn.fetchval(
                "SELECT value FROM site_settings WHERE key = 'auto_mode_enabled'"
            ),
            "limit": await conn.fetchval(
                "SELECT max_concurrency FROM batch_model_limits WHERE model_id = 'itest-model'"
            ),
            "domains": await conn.fetchval(
                "SELECT count(*) FROM signup_allowed_domains WHERE domain LIKE 'itest-%'"
            ),
        }


async def test_a_failure_in_the_last_table_leaves_every_table_as_it_was(stores) -> None:
    pool, op_store, batch_store = stores
    before = await _state(pool)
    assert before["key"] == OLD_KEY

    # The allowlist is written last; a creator that is no account breaks its
    # foreign key after every other table has been written.
    with pytest.raises(Exception, match="foreign key"):
        await _restore(op_store, batch_store, _payload(), admin_user_id="no-such-account")

    assert await _state(pool) == before


async def test_a_restore_that_succeeds_commits_every_table(stores) -> None:
    pool, op_store, batch_store = stores

    await _restore(op_store, batch_store, _payload(), admin_user_id=None)

    assert await _state(pool) == {
        "site_name": "After Restore",
        "key": "sk-itest-restore-new-0123456789abcdef",
        "setting": "true",
        "limit": 2,
        "domains": 1,
    }


async def test_a_withheld_provider_key_keeps_its_value(stores) -> None:
    pool, op_store, batch_store = stores
    payload = _payload(
        provider_keys=[
            {
                "id": "itest-restore-key",
                "provider": "itest-restore",
                "api_key": None,
                "label": "relabelled",
            }
        ]
    )

    await _restore(op_store, batch_store, payload, admin_user_id=None)

    state = await _state(pool)
    assert state["key"] == OLD_KEY
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT label FROM provider_api_keys WHERE id = 'itest-restore-key'"
            )
            == "relabelled"
        )
