"""Alert history records by default wherever the gateway has a database.

``ALERT_HISTORY_ENABLED`` is an opt-out: absent, capture is on; an explicit
false — stored or in the environment — keeps it off, and so does a blank
stored value or a blank or non-boolean environment value. A stored value the
registry refuses is passed over like any setting's. Without a database
nothing is captured.
"""

from __future__ import annotations

import json

import pytest

from serving.config import app_config
from serving.config.app_config_registry import static_entries, static_entry
from serving.config.settings import get_settings
from serving.observability.alert_history import hooks, runtime as runtime_mod
from serving.servers.routers import alert_history_auth as auth
from tests.fixtures.app_config_store import FakeAppConfigStore, row

SETTING = runtime_mod.ENABLED_SETTING
DB_CONFIG = {"host": "127.0.0.1", "port": 1, "database": "x", "user": "x", "password": "x"}


@pytest.fixture
def config_env(monkeypatch, tmp_path):
    for entry in static_entries():
        monkeypatch.delenv(entry.key, raising=False)
    monkeypatch.delenv("DISTRIBUTION_CONFIG_PATH", raising=False)
    (tmp_path / "models.yaml").write_text("models: []\n")
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(tmp_path / "models.yaml"))
    monkeypatch.setenv("ROUTING_CONFIG_PATH", str(tmp_path / "routing.yaml"))
    monkeypatch.setenv("ALERTS_CONFIG_PATH", str(tmp_path / "alerts.yaml"))
    app_config.reset_state()
    get_settings.cache_clear()
    auth._CACHE.clear()

    # The writer would otherwise try to reach the database it was given.
    async def no_database(self):
        raise runtime_mod.HistoryUnavailable("database_unavailable")

    monkeypatch.setattr(runtime_mod.AlertHistoryRuntime, "_get_pool", no_database)
    yield monkeypatch
    hooks.install(None)
    runtime_mod.set_runtime(None)
    auth._CACHE.clear()
    app_config.reset_state()
    get_settings.cache_clear()


async def _stored(*rows):
    await app_config.load(FakeAppConfigStore(rows))


def test_the_registry_shows_the_default_the_runtime_uses():
    entry = static_entry(SETTING)
    assert entry is not None and entry.restart_required
    assert entry.default == "true"
    assert runtime_mod.DEFAULT_ENABLED is True


def test_nothing_configured_means_capture_on(config_env):
    # config_value does not supply a static setting's registry default.
    assert app_config.config_value(SETTING) is None
    assert runtime_mod.history_enabled() is True


@pytest.mark.asyncio
async def test_nothing_stored_means_capture_on(config_env):
    await _stored(row("SITE_NAME", "Elsewhere"))
    assert runtime_mod.history_enabled() is True


@pytest.mark.parametrize(
    "value,expected",
    [("false", False), ("0", False), ("no", False), ("off", False), ("true", True)],
)
def test_the_environment_decides_when_nothing_is_stored(config_env, value, expected):
    config_env.setenv(SETTING, value)
    assert runtime_mod.history_enabled() is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored,environment,expected",
    [("false", "true", False), ("false", None, False), ("true", "false", True)],
)
async def test_a_stored_value_wins_over_the_environment(config_env, stored, environment, expected):
    if environment is not None:
        config_env.setenv(SETTING, environment)
    await _stored(row(SETTING, stored))
    assert runtime_mod.history_enabled() is expected


@pytest.mark.asyncio
async def test_a_blank_stored_row_keeps_capture_off(config_env):
    """A blank row is in force under the shared contract: not the default."""
    config_env.setenv(SETTING, "true")
    await _stored(row(SETTING, ""))
    assert app_config.config_value(SETTING) == ""
    assert runtime_mod.history_enabled() is False


@pytest.mark.parametrize("value", ["", "   ", "maybe", "2"])
def test_a_blank_or_invalid_environment_value_keeps_capture_off(config_env, value):
    config_env.setenv(SETTING, value)
    assert runtime_mod.history_enabled() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "environment,expected",
    [("false", False), ("true", True), (None, True)],
    ids=["env-false", "env-true", "no-env-default"],
)
async def test_an_invalid_stored_row_is_passed_over_like_any_setting(
    config_env, caplog, environment, expected
):
    """The config layer ignores a refused row and warns; the fallback then decides."""
    if environment is not None:
        config_env.setenv(SETTING, environment)
    with caplog.at_level("WARNING", logger="serving.config.app_config"):
        await _stored(row(SETTING, "maybe"))
    assert f"Ignoring stored {SETTING}" in caplog.text
    assert app_config.config_value(SETTING) == environment
    assert runtime_mod.history_enabled() is expected


@pytest.mark.asyncio
async def test_no_database_means_no_runtime_and_no_capture(config_env):
    assert runtime_mod.start_runtime(db_config=None, fence_secret=None) is None
    assert runtime_mod.get_runtime() is None
    assert hooks.active() is None


@pytest.mark.asyncio
async def test_startup_with_a_database_and_nothing_configured_captures(config_env):
    """No reader or writer credential, a non-default deployment id: still captured."""
    config_env.setenv(runtime_mod.DEPLOYMENT_SETTING, "Example Deployment")
    rt = runtime_mod.start_runtime(db_config=DB_CONFIG, fence_secret=None)
    try:
        assert rt is not None and runtime_mod.get_runtime() is rt
        assert rt.enabled and rt.capture is not None
        assert hooks.active() is rt.capture
        assert rt.identity.deployment_id == "example-deployment"
        assert rt.local_status()["capture_enabled"] is True
        assert auth.load_credentials().readers == ()
        assert auth.load_credentials().writers == ()
    finally:
        await runtime_mod.stop_runtime()
    assert hooks.active() is None
    assert runtime_mod.get_runtime() is None


@pytest.mark.asyncio
async def test_startup_with_an_explicit_false_keeps_reads_but_not_capture(config_env):
    await _stored(row(SETTING, "false"))
    rt = runtime_mod.start_runtime(db_config=DB_CONFIG, fence_secret=None)
    try:
        # The runtime stays for the read API; nothing is recorded.
        assert rt is not None and runtime_mod.get_runtime() is rt
        assert rt.has_database
        assert not rt.enabled and rt.capture is None and rt.writer is None
        assert hooks.active() is None
    finally:
        await runtime_mod.stop_runtime()


# -- ingestion stays credential-gated --------------------------------------------

WRITER_TOKEN = "ahw_synthetic_default_on_writer_0123456789"
READER_TOKEN = "ahr_synthetic_default_on_reader_0123456789"


def _sha(token):
    import hashlib

    return hashlib.sha256(token.encode()).hexdigest()


def _credentials(monkeypatch):
    monkeypatch.setenv(
        auth.READ_SETTING,
        json.dumps(
            [
                {
                    "id": "reader",
                    "sha256": _sha(READER_TOKEN),
                    "deployment_ids": ["test-deployment"],
                    "target_environments": ["test"],
                    "expires_at": "2099-01-01T00:00:00Z",
                }
            ]
        ),
    )
    monkeypatch.setenv(
        auth.WRITE_SETTING,
        json.dumps(
            [
                {
                    "id": "writer",
                    "sha256": _sha(WRITER_TOKEN),
                    "source": "status_monitor",
                    "source_namespace": "test-monitor",
                    "deployment_id": "test-deployment",
                    "target_environments": ["test"],
                    "expires_at": "2099-01-01T00:00:00Z",
                }
            ]
        ),
    )


def _status(call) -> int:
    with pytest.raises(Exception) as refused:
        call()
    return refused.value.status_code


@pytest.mark.asyncio
async def test_default_capture_does_not_open_ingestion_without_a_writer(config_env):
    runtime_mod.start_runtime(db_config=DB_CONFIG, fence_secret=None)
    try:
        assert _status(lambda: auth.require_history_writer(f"Bearer {WRITER_TOKEN}")) == 404
        assert _status(lambda: auth.require_history_writer(None)) == 404
    finally:
        await runtime_mod.stop_runtime()


@pytest.mark.asyncio
async def test_default_capture_admits_only_a_valid_writer(config_env):
    _credentials(config_env)
    runtime_mod.start_runtime(db_config=DB_CONFIG, fence_secret=None)
    try:
        writer = auth.require_history_writer(f"Bearer {WRITER_TOKEN}")
        assert writer.source_namespace == "test-monitor"
        assert _status(lambda: auth.require_history_writer(f"Bearer {READER_TOKEN}")) == 403
        assert _status(lambda: auth.require_history_writer("Bearer " + "z" * 40)) == 401
        assert _status(lambda: auth.require_history_writer(None)) == 401
    finally:
        await runtime_mod.stop_runtime()


@pytest.mark.asyncio
async def test_capture_turned_off_refuses_even_a_valid_writer(config_env):
    _credentials(config_env)
    config_env.setenv(SETTING, "false")
    runtime_mod.start_runtime(db_config=DB_CONFIG, fence_secret=None)
    try:
        assert _status(lambda: auth.require_history_writer(f"Bearer {WRITER_TOKEN}")) == 404
    finally:
        await runtime_mod.stop_runtime()
