"""The model and routing files are imported once, then the database wins."""

from __future__ import annotations

import textwrap
from typing import Any

import pytest

from routing.config import load_routing_config_text
from routing.routers import FixedRouter
from serving.config.app_config_registry import discover_references
from serving.config.route_catalog import (
    import_missing,
    install,
    installed_body,
    reset_installed,
)
from serving.config.settings import get_settings
from serving.servers import bootstrap
from serving.servers.registry import register_from_models_text, register_from_models_yaml
from tests.fixtures.app_config_store import FakeAppConfigStore


def _model(model_id: str) -> str:
    return textwrap.dedent(
        f"""
        models:
          - id: {model_id}
            name: {model_id}
            provider: vllm
            base_url: http://127.0.0.1:9/v1
            route:
              - kind: vllm
                weight: 1.0
                base_url: http://127.0.0.1:9/v1
                api_key: test-key
        """
    )


class CatalogConn:
    """Enough of an asyncpg connection for the catalog import."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}
        self.inserts: list[str] = []

    async def execute(self, sql: str, *args: Any) -> None:
        if sql.lstrip().startswith("INSERT INTO route_catalog") and args[0] not in self.rows:
            self.rows[args[0]] = args[1]
            self.inserts.append(args[0])

    async def fetch(self, sql: str) -> list[dict[str, str]]:
        return [{"kind": kind, "body": body} for kind, body in self.rows.items()]

    def transaction(self) -> CatalogConn:
        return self

    async def __aenter__(self) -> CatalogConn:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def close(self) -> None:
        return None


@pytest.fixture
def clean_catalog():
    reset_installed()
    yield
    reset_installed()


@pytest.fixture
def boot_env(monkeypatch, tmp_path):
    """The environment ``_load_app_config`` needs, with the catalog files unset."""
    monkeypatch.setenv("DB_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET_KEY", "env-jwt")
    monkeypatch.setenv("API_KEY_SECRET", "env-api")
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(tmp_path / "models.yaml"))
    monkeypatch.setenv("ROUTING_CONFIG_PATH", str(tmp_path / "routing.yaml"))
    monkeypatch.delenv("DB_STORE_FULL_CONTENT", raising=False)
    monkeypatch.setattr(bootstrap, "_CONFIG_LOAD_RETRY_DELAY", 0)
    get_settings.cache_clear()
    return tmp_path


async def test_import_stores_a_missing_document_and_leaves_an_existing_one() -> None:
    conn = CatalogConn()
    conn.rows["models"] = "models: []\n"

    stored = await import_missing(
        conn,
        {"models": "models:\n  - id: replacement\n", "routing": "default_router: fixed\n"},
    )

    assert stored["models"] == "models: []\n"
    assert stored["routing"] == "default_router: fixed\n"
    assert conn.inserts == ["routing"]

    again = await import_missing(conn, {"routing": "default_router: routewise\n"})
    assert again["routing"] == "default_router: fixed\n"
    assert conn.inserts == ["routing"]


async def test_import_skips_a_blank_seed() -> None:
    conn = CatalogConn()

    stored = await import_missing(conn, {"models": "   \n", "routing": ""})

    assert stored == {}
    assert conn.inserts == []


def test_discovery_reads_the_stored_document_and_not_the_file(tmp_path, clean_catalog) -> None:
    path = tmp_path / "models.yaml"
    path.write_text("models:\n  - id: file-model\n    base_url: ${FILE_ONLY_URL}\n")

    found = discover_references(
        path,
        None,
        None,
        models_text="models:\n  - id: db-model\n    base_url: ${DB_ONLY_URL}\n",
    )
    assert "DB_ONLY_URL" in found
    assert "FILE_ONLY_URL" not in found

    empty = discover_references(path, None, None, models_text="")
    assert "FILE_ONLY_URL" not in empty


def test_a_stored_models_document_registers_the_same_routes_as_the_file(tmp_path) -> None:
    text = _model("same-model")
    path = tmp_path / "models.yaml"
    path.write_text(text)
    from_file = FixedRouter()
    from_text = FixedRouter()

    register_from_models_yaml(from_file, path)
    register_from_models_text(from_text, text, origin=str(path))

    assert list(from_text.routes) == list(from_file.routes) == ["same-model"]


def test_routing_text_expands_and_drops_a_blank_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("MISSING_ROUTE_URL", raising=False)
    config = load_routing_config_text(
        textwrap.dedent(
            """
            default_router: fixed
            health_check: 0
            local_deployment:
              - endpoint: ${MISSING_ROUTE_URL}
                models: [gone]
              - endpoint: http://127.0.0.1:9/v1
                models: [kept]
            """
        )
    )

    assert config.default_router == "fixed"
    assert [item.endpoint for item in config.local_deployment] == ["http://127.0.0.1:9/v1"]


async def test_stored_models_win_over_the_file(tmp_path, monkeypatch, clean_catalog) -> None:
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(tmp_path / "models.yaml"))
    (tmp_path / "models.yaml").write_text(_model("from-file"))
    get_settings.cache_clear()
    install({"models": _model("from-db")})

    router = FixedRouter()
    _adapters, infos = await bootstrap._init_router_and_models(router)

    assert [info.model_id for info in infos] == ["from-db"]
    assert "from-file" not in router.routes


async def test_the_models_file_is_used_when_nothing_is_stored(
    tmp_path, monkeypatch, clean_catalog
) -> None:
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(tmp_path / "models.yaml"))
    (tmp_path / "models.yaml").write_text(_model("from-file"))
    get_settings.cache_clear()

    router = FixedRouter()
    _adapters, infos = await bootstrap._init_router_and_models(router)

    assert [info.model_id for info in infos] == ["from-file"]


def test_stored_routing_wins_and_an_invalid_row_does_not_fall_back(
    tmp_path, monkeypatch, clean_catalog
) -> None:
    monkeypatch.setenv("ROUTING_CONFIG_PATH", str(tmp_path / "routing.yaml"))
    (tmp_path / "routing.yaml").write_text("default_router: fixed\nhealth_check: 0\n")
    get_settings.cache_clear()
    install({"routing": "default_router: routewise\nhealth_check: 0\n"})

    manager = bootstrap._apply_routing_manager(FixedRouter())

    assert manager is not None
    assert manager.conf is not None
    assert manager.conf.default_router == "routewise"

    install({"routing": "- not-a-mapping\n"})
    assert bootstrap._apply_routing_manager(FixedRouter()) is None


def test_auto_mode_reads_the_stored_registry(monkeypatch, clean_catalog) -> None:
    seen: dict[str, str] = {}

    def _from_text(text: str, routes: object) -> None:
        seen["text"] = text
        return None

    monkeypatch.setattr(bootstrap, "load_auto_mode_from_text", _from_text)
    monkeypatch.setattr(
        bootstrap,
        "load_auto_mode",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("file was read")),
    )
    install({"models": "models: []\n"})

    assert bootstrap._load_auto_mode(FixedRouter()) is None
    assert seen["text"] == "models: []\n"


async def test_boot_imports_each_file_once(boot_env, monkeypatch, tmp_path, clean_catalog) -> None:
    from unittest.mock import AsyncMock

    models = _model("catalog-model")
    routing = "default_router: fixed\nhealth_check: 0\n"
    (tmp_path / "models.yaml").write_text(models)
    (tmp_path / "routing.yaml").write_text(routing)
    conn = CatalogConn()
    monkeypatch.setattr(bootstrap, "connect_app_config", AsyncMock(return_value=conn))
    monkeypatch.setattr(bootstrap, "AppConfigStore", lambda _conn: FakeAppConfigStore())

    await bootstrap._load_app_config()

    assert installed_body("models") == models
    assert installed_body("routing") == routing
    assert conn.inserts == ["models", "routing"]

    (tmp_path / "models.yaml").write_text(_model("edited-after-import"))
    await bootstrap._load_app_config()

    assert installed_body("models") == models
    assert conn.inserts == ["models", "routing"]


async def test_a_database_free_boot_forgets_a_stored_catalog(
    boot_env, monkeypatch, clean_catalog
) -> None:
    install({"models": "models: []\n"})
    monkeypatch.setenv("DB_ENABLED", "false")
    get_settings.cache_clear()

    await bootstrap._load_app_config()

    assert installed_body("models") is None
