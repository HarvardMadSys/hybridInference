"""The configuration command line: argument handling and value parsing, no database."""

from __future__ import annotations

import contextlib

import pytest

from serving.config import app_config, manage
from serving.config.app_config_registry import static_entries
from serving.config.settings import get_settings
from tests.fixtures.app_config_store import FakeAppConfigStore, row


@pytest.fixture
def store(tmp_path, monkeypatch) -> FakeAppConfigStore:
    """A clean environment and an in-memory table standing in for the database."""
    for entry in static_entries():
        monkeypatch.delenv(entry.key, raising=False)
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(tmp_path / "models.yaml"))
    monkeypatch.setenv("ROUTING_CONFIG_PATH", str(tmp_path / "routing.yaml"))
    get_settings.cache_clear()
    fake = FakeAppConfigStore(
        [
            row("JWT_SECRET_KEY", "stored-jwt-secret", secret=True),
            row("API_KEY_SECRET", "stored-api-secret", secret=True),
        ]
    )

    @contextlib.asynccontextmanager
    async def session():
        await app_config.load(fake, bootstrap=False)
        app_config.attach(fake)
        try:
            yield
        finally:
            app_config._STATE.store = None

    monkeypatch.setattr(app_config, "command_line_session", session)
    monkeypatch.setattr(manage, "_database_enabled", lambda: True)
    return fake


def test_set_stores_a_value_parsed_by_its_type(store, capsys) -> None:
    assert manage.main(["set", "SMTP_PORT", "2525"]) == 0

    stored = store.rows["SMTP_PORT"]
    assert stored.value == "2525"
    assert stored.updated_by == "cli"
    out = capsys.readouterr().out
    assert "SMTP_PORT=2525" in out
    assert "within 10 seconds" in out


def test_set_writes_a_switch_in_its_stored_form(store) -> None:
    assert manage.main(["set", "ROUTING_AFFINITY_ENABLED", "false"]) == 0
    assert store.rows["ROUTING_AFFINITY_ENABLED"].value == "0"

    assert manage.main(["set", "COOKIE_SECURE", "no"]) == 0
    assert store.rows["COOKIE_SECURE"].value == "false"


def test_set_applies_the_consoles_validation(store, capsys) -> None:
    assert manage.main(["set", "JWT_ALGORITHM", "HS265"]) == 1

    err = capsys.readouterr().err
    assert "JWT_ALGORITHM: must be one of HS256, HS384, HS512" in err
    assert "JWT_ALGORITHM" not in store.rows


def test_set_refuses_an_immutable_secret(store, capsys) -> None:
    assert manage.main(["set", "API_KEY_SECRET", "replacement"]) == 1

    err = capsys.readouterr().err
    assert "API_KEY_SECRET is already set" in err
    assert store.rows["API_KEY_SECRET"].value == "stored-api-secret"


def test_set_restores_a_lost_secret_where_startup_refuses(store, capsys) -> None:
    """The escape hatch must work in the state that keeps the gateway down."""
    del store.rows["API_KEY_SECRET"]
    store.api_keys = True  # boot would refuse to generate a new one

    assert manage.main(["set", "API_KEY_SECRET", "the-original-secret"]) == 0

    assert store.rows["API_KEY_SECRET"].value == "the-original-secret"
    assert store.rows["API_KEY_SECRET"].secret
    assert "the-original-secret" not in capsys.readouterr().out


def test_set_adds_a_custom_variable_secret_on_request(store) -> None:
    assert manage.main(["set", "MY_BOX_TOKEN", "t0k3n", "--secret"]) == 0
    assert store.rows["MY_BOX_TOKEN"].secret


def test_reset_falls_back_to_the_default(store, capsys) -> None:
    store.rows["JWT_ALGORITHM"] = row("JWT_ALGORITHM", "HS384")

    assert manage.main(["reset", "JWT_ALGORITHM"]) == 0

    assert "JWT_ALGORITHM" not in store.rows
    out = capsys.readouterr().out
    assert "JWT_ALGORITHM=HS256" in out
    assert "source: default" in out


def test_reset_of_a_custom_variable_removes_it(store, capsys) -> None:
    store.rows["MY_BOX_REGION"] = row("MY_BOX_REGION", "east")

    assert manage.main(["reset", "MY_BOX_REGION"]) == 0

    assert capsys.readouterr().out.strip() == "MY_BOX_REGION removed."


def test_get_and_list_never_print_a_secret(store, capsys) -> None:
    store.rows["SMTP_PASSWORD"] = row("SMTP_PASSWORD", "smtp-hunter2", secret=True)

    assert manage.main(["get", "SMTP_PASSWORD"]) == 0
    assert manage.main(["list"]) == 0

    out = capsys.readouterr().out
    assert "SMTP_PASSWORD=(secret, set)" in out
    assert "SMTP_PASSWORD=(set)  [database]" in out
    assert "JWT_SECRET_KEY=(set)" in out
    for secret in ("smtp-hunter2", "stored-jwt-secret", "stored-api-secret"):
        assert secret not in out


def test_get_explains_names_it_cannot_show(store, capsys) -> None:
    assert manage.main(["get", "DB_PASSWORD"]) == 1
    assert manage.main(["get", "USER_AUTH_ENABLED"]) == 1
    assert manage.main(["get", "NOT_A_SETTING"]) == 1

    err = capsys.readouterr().err
    assert "DB_PASSWORD stays in the environment" in err
    assert "USER_AUTH_ENABLED is a runtime setting" in err
    assert "NOT_A_SETTING is not a known setting" in err


@pytest.mark.parametrize(
    "argv",
    [[], ["set", "SMTP_PORT"], ["get"], ["frobnicate"], ["reset", "A", "B"]],
)
def test_malformed_commands_are_usage_errors(store, argv) -> None:
    with pytest.raises(SystemExit) as excinfo:
        manage.main(argv)
    assert excinfo.value.code == 2


def test_a_database_free_deployment_has_nothing_to_manage(monkeypatch, capsys) -> None:
    monkeypatch.setattr(manage, "_database_enabled", lambda: False)

    assert manage.main(["list"]) == 1
    assert "DB_ENABLED=false" in capsys.readouterr().err


def test_an_unreachable_database_is_reported(monkeypatch, capsys) -> None:
    @contextlib.asynccontextmanager
    async def unreachable():
        raise ConnectionRefusedError("connection refused")
        yield  # pragma: no cover

    monkeypatch.setattr(app_config, "command_line_session", unreachable)
    monkeypatch.setattr(manage, "_database_enabled", lambda: True)

    assert manage.main(["list"]) == 1
    assert "Could not use the database" in capsys.readouterr().err


@pytest.mark.parametrize(
    "key,text,expected",
    [
        ("COOKIE_SECURE", "true", True),
        ("COOKIE_SECURE", "OFF", False),
        ("ROUTING_AFFINITY_ENABLED", "0", False),
        ("SMTP_PORT", " 2525 ", 2525),
        ("CIRCUIT_COOLDOWN_SECONDS", "12.5", 12.5),
        ("SITE_NAME", "  spaced  ", "  spaced  "),
        ("MINIMAX_API_KEY3", "k3", "k3"),
        ("NEVER_HEARD_OF_IT", "x", "x"),
    ],
)
def test_parse_text_value(store, key, text, expected) -> None:
    app_config.use_environment(database_enabled=True)
    assert app_config.parse_text_value(key, text) == expected


@pytest.mark.parametrize(
    "key,text,detail",
    [
        ("COOKIE_SECURE", "maybe", "COOKIE_SECURE: must be true or false."),
        ("SMTP_PORT", "25.5", "SMTP_PORT: must be an integer."),
        ("CIRCUIT_COOLDOWN_SECONDS", "soon", "CIRCUIT_COOLDOWN_SECONDS: must be a number."),
    ],
)
def test_parse_text_value_refuses_the_wrong_type(store, key, text, detail) -> None:
    app_config.use_environment(database_enabled=True)
    with pytest.raises(app_config.ConfigUpdateError) as excinfo:
        app_config.parse_text_value(key, text)
    assert excinfo.value.detail == detail


# --- loading for other command-line tools ------------------------------------


def test_load_stored_configuration_reads_the_database(store, monkeypatch) -> None:
    monkeypatch.delenv("RAG_GATEWAY_API_KEY", raising=False)
    store.rows["RAG_GATEWAY_API_KEY"] = row("RAG_GATEWAY_API_KEY", "stored-gw-key", secret=True)

    assert manage.load_stored_configuration() is True

    assert app_config.config_value("RAG_GATEWAY_API_KEY") == "stored-gw-key"


def test_load_stored_configuration_without_a_database(monkeypatch) -> None:
    monkeypatch.setattr(manage, "_database_enabled", lambda: False)
    assert manage.load_stored_configuration() is False


def test_load_stored_configuration_keeps_the_environment_when_unreachable(
    monkeypatch, capsys
) -> None:
    @contextlib.asynccontextmanager
    async def unreachable():
        raise OSError("no route to host")
        yield  # pragma: no cover

    monkeypatch.setattr(app_config, "command_line_session", unreachable)
    monkeypatch.setattr(manage, "_database_enabled", lambda: True)

    assert manage.load_stored_configuration() is False
    assert "using the environment's" in capsys.readouterr().err


def test_the_ingest_cli_loads_stored_settings_except_for_check(monkeypatch, tmp_path) -> None:
    from serving.rag import ingest

    calls: list[str] = []
    monkeypatch.setattr(ingest, "load_stored_configuration", lambda: calls.append("load"))
    monkeypatch.setattr(ingest, "check_index", lambda settings: 0)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("# A\n\nText.\n")

    assert ingest.main(["--check"]) == 0
    assert calls == []

    out = tmp_path / "index.json"
    assert ingest.main(["--corpus", str(corpus), "--out", str(out), "--embedder", "hash"]) == 0
    assert calls == ["load"]
