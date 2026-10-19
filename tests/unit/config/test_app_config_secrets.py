"""Secrecy of database-backed settings: what makes one secret, and that it stays so.

A secret's value never leaves the backend: not in ``GET /admin/config``, not in
the audit log, not on the command line. These tests cover the ways one could
leak: a credential under a name the heuristic does not recognize, a flag the
administrator asked for but did not get, and a flag lost to a reset.
"""

from __future__ import annotations

import contextlib
import textwrap

import pytest

from serving.config import app_config, manage
from serving.config.app_config_registry import static_entries
from serving.config.settings import get_settings
from tests.fixtures.app_config_store import FakeAppConfigStore, row

#: Credentials under names the secret-name heuristic once missed. Assembled, so
#: a secret scanner does not read the test as a credential assignment.
_CREDENTIAL_VALUES = {
    "ACME_CREDENTIALS": "sk-live-" + "acme-0001",
    "GLM_PASS": "glm-" + "pass-0002",
    "GLM_AUTH": "glm-" + "auth-0003",
    # The PEM label is assembled: the detect-private-key pre-commit hook
    # matches it spelled out, and there is no key here either way.
    "VERTEX_SA_JSON": '{"private_key": "'
    + "-" * 5
    + "BEGIN "
    + "PRIVATE"
    + " KEY"
    + "-" * 5
    + '"}',
}

#: Neither its name nor its field make this variable secret: an endpoint whose
#: URL carries a token, say. Only an administrator's flag does.
_PLAIN_NAME = "ACME_GATEWAY_URL"


@pytest.fixture
def config_env(tmp_path, monkeypatch):
    """A clean environment: no registered setting set, and config files under tmp_path."""
    for entry in static_entries():
        monkeypatch.delenv(entry.key, raising=False)
    for key in ("ENVIRONMENT", "DISTRIBUTION_CONFIG_PATH", *_CREDENTIAL_VALUES, _PLAIN_NAME):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "models.yaml").write_text("models: []\n")
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(tmp_path / "models.yaml"))
    monkeypatch.setenv("ROUTING_CONFIG_PATH", str(tmp_path / "routing.yaml"))
    monkeypatch.setenv("ALERTS_CONFIG_PATH", str(tmp_path / "alerts.yaml"))
    get_settings.cache_clear()
    return tmp_path


def _write_models(path, text: str) -> None:
    (path / "models.yaml").write_text(textwrap.dedent(text))


async def _boot(store: FakeAppConfigStore) -> None:
    await app_config.load(store)
    app_config.attach(store)


async def _rediscover(store: FakeAppConfigStore) -> None:
    """Pick up an edited models.yaml, as the next start would."""
    app_config._STATE.discovered = app_config._discover()
    await app_config._reload(store)


def _entries() -> dict[str, dict]:
    return {entry["key"]: entry for entry in app_config.describe()["entries"]}


_CREDENTIAL_MODELS = """
    models:
      - id: acme-chat
        base_url: https://api.acme.example/v1
        api_key: ${ACME_CREDENTIALS}
      - id: glm
        route:
          - kind: openai_compat
            base_url: https://glm.example/v1
            api_keys:
              - ${GLM_PASS}
              - ${GLM_AUTH}
      - id: vertex
        base_url: https://vertex.example/v1
        api_key: ${VERTEX_SA_JSON}
"""


# --- a credential is secret whatever it is called ------------------------------


@pytest.mark.asyncio
async def test_a_credential_reference_is_secret_in_the_table_the_api_and_the_audit(
    config_env, monkeypatch
) -> None:
    _write_models(config_env, _CREDENTIAL_MODELS)
    for key, value in _CREDENTIAL_VALUES.items():
        monkeypatch.setenv(key, value)
    store = FakeAppConfigStore()

    await _boot(store)

    for key in _CREDENTIAL_VALUES:
        assert store.rows[key].source == "env_import"
        assert store.rows[key].secret, key
    body = repr(app_config.describe())
    entries = _entries()
    for key, value in _CREDENTIAL_VALUES.items():
        assert entries[key]["secret"] is True, key
        assert entries[key]["value"] is None
        assert value not in body

    changes = await app_config.update(
        {"ACME_CREDENTIALS": "sk-live-" + "acme-0099"}, updated_by="admin"
    )
    assert [change.audit_details() for change in changes] == [
        {"key": "ACME_CREDENTIALS", "changed": True}
    ]
    assert store.rows["ACME_CREDENTIALS"].secret


@pytest.mark.asyncio
async def test_a_credential_field_makes_any_name_secret(config_env, monkeypatch) -> None:
    """The field decides, not the name: api_key, api_keys, an Authorization header."""
    _write_models(
        config_env,
        """
        models:
          - id: chat
            base_url: ${ACME_GATEWAY_URL}
            api_key: ${ACME_ONE}
            route:
              - kind: openai_compat
              - kind: openai_compat
                base_url: https://canary.example/v1
                api_keys:
                  - ${ACME_TWO}
                optional: true
              - kind: openai_compat
                base_url: https://other.example/v1
                headers:
                  Authorization: Bearer ${ACME_THREE}
        """,
    )
    monkeypatch.setenv("ACME_ONE", "one")
    store = FakeAppConfigStore()

    await _boot(store)

    entries = _entries()
    for key in ("ACME_ONE", "ACME_TWO", "ACME_THREE"):
        assert entries[key]["secret"] is True, key
    assert entries[_PLAIN_NAME]["secret"] is False
    assert store.rows["ACME_ONE"].secret


@pytest.mark.asyncio
async def test_a_built_in_setting_used_as_a_credential_turns_secret(
    config_env, monkeypatch
) -> None:
    _write_models(
        config_env,
        """
        models:
          - id: chat
            base_url: https://api.example/v1
            api_key: ${SITE_NAME}
        """,
    )
    monkeypatch.setenv("SITE_NAME", "doubles-as-a-key")
    store = FakeAppConfigStore()

    await _boot(store)

    assert _entries()["SITE_NAME"]["secret"] is True
    assert "doubles-as-a-key" not in repr(app_config.describe())


# --- the administrator's flag ------------------------------------------------


async def _boot_with_plain_variable(config_env, monkeypatch) -> FakeAppConfigStore:
    _write_models(
        config_env,
        f"""
        models:
          - id: chat
            base_url: ${{{_PLAIN_NAME}}}
            api_key: literal-key
        """,
    )
    monkeypatch.setenv(_PLAIN_NAME, "https://box.example/v1?token=from-env")
    store = FakeAppConfigStore()
    await _boot(store)
    assert _entries()[_PLAIN_NAME]["secret"] is False
    return store


@pytest.mark.asyncio
async def test_a_save_honors_the_flag_for_a_discovered_variable(config_env, monkeypatch) -> None:
    store = await _boot_with_plain_variable(config_env, monkeypatch)

    changes = await app_config.update(
        {_PLAIN_NAME: "https://box.example/v1?token=rotated"},
        {_PLAIN_NAME: True},
        updated_by="admin",
    )

    assert [change.audit_details() for change in changes] == [
        {"key": _PLAIN_NAME, "changed": True, "marked_secret": True}
    ]
    assert store.rows[_PLAIN_NAME].secret
    entry = _entries()[_PLAIN_NAME]
    assert entry["secret"] is True and entry["value"] is None

    # One-way: a later save without the flag, or with false, keeps it.
    for flags in (None, {_PLAIN_NAME: False}):
        changes = await app_config.update(
            {_PLAIN_NAME: "https://box.example/v1?token=again"}, flags, updated_by="admin"
        )
        assert changes[0].audit_details() == {"key": _PLAIN_NAME, "changed": True}
        assert store.rows[_PLAIN_NAME].secret


@pytest.mark.asyncio
async def test_the_flag_alone_marks_a_stored_row_secret(config_env, monkeypatch) -> None:
    store = await _boot_with_plain_variable(config_env, monkeypatch)
    before = store.rows[_PLAIN_NAME]
    assert before.source == "env_import" and not before.secret

    changes = await app_config.update({}, {_PLAIN_NAME: True}, updated_by="admin")

    assert [change.audit_details() for change in changes] == [
        {"key": _PLAIN_NAME, "marked_secret": True}
    ]
    after = store.rows[_PLAIN_NAME]
    assert after.secret
    # Only the flag: the value and who last set it stay as they were.
    assert (after.value, after.source, after.updated_by) == (
        before.value,
        before.source,
        before.updated_by,
    )
    assert store.writes == []
    entry = _entries()[_PLAIN_NAME]
    assert entry["secret"] is True and entry["value"] is None
    assert "token=from-env" not in repr(app_config.describe())

    # Already secret: nothing to change, and nothing to audit.
    assert await app_config.update({}, {_PLAIN_NAME: True}, updated_by="admin") == []


@pytest.mark.asyncio
async def test_the_flag_alone_keeps_a_reset_variable_secret(config_env, monkeypatch) -> None:
    store = await _boot_with_plain_variable(config_env, monkeypatch)
    await app_config.update({_PLAIN_NAME: "https://box.example/v1"}, updated_by="admin")
    await app_config.delete(_PLAIN_NAME, updated_by="admin")
    reset_entry = _entries()[_PLAIN_NAME]
    assert reset_entry["value"] == "https://box.example/v1?token=from-env"
    # The reset row still says who reset it and when: the console offers
    # "Mark as secret" on that stamp.
    assert reset_entry["updated_by"] == "admin"
    assert reset_entry["updated_at"] is not None

    await app_config.update({}, {_PLAIN_NAME: True}, updated_by="admin")

    assert store.rows[_PLAIN_NAME].source == "reset"
    assert store.rows[_PLAIN_NAME].secret
    entry = _entries()[_PLAIN_NAME]
    assert entry["source"] == "environment"
    assert entry["secret"] is True and entry["value"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,detail",
    [
        (
            "NOTHING_STORED_HERE",
            "NOTHING_STORED_HERE has no stored value to protect yet: set a value and mark it "
            "secret in the same save.",
        ),
        (_PLAIN_NAME, f"{_PLAIN_NAME} has no stored value to protect yet"),
        ("SITE_NAME", "SITE_NAME: a built-in setting keeps the registry's secret flag."),
        ("lowercase_name", "lowercase_name: a name is"),
    ],
)
async def test_the_flag_alone_is_refused_where_it_cannot_apply(
    config_env, monkeypatch, key, detail
) -> None:
    _write_models(
        config_env,
        f"""
        models:
          - id: chat
            base_url: ${{{_PLAIN_NAME}}}
            api_key: literal-key
        """,
    )
    store = FakeAppConfigStore([row("SITE_NAME", "Stored")])
    await _boot(store)
    before = dict(store.rows)

    with pytest.raises(app_config.ConfigUpdateError) as excinfo:
        await app_config.update({}, {key: True}, updated_by="admin")

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail.startswith(detail)
    assert store.rows == before


@pytest.mark.asyncio
async def test_the_flag_alone_never_reaches_an_environment_only_name(config_env) -> None:
    store = FakeAppConfigStore()
    await _boot(store)

    with pytest.raises(app_config.ConfigUpdateError) as excinfo:
        await app_config.update({}, {"DB_PASSWORD": True}, updated_by="admin")

    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_a_built_in_setting_keeps_its_own_flag_in_a_save(config_env) -> None:
    store = FakeAppConfigStore()
    await _boot(store)

    changes = await app_config.update(
        {"SITE_NAME": "Acme"}, {"SITE_NAME": True}, updated_by="admin"
    )

    assert not store.rows["SITE_NAME"].secret
    assert changes[0].audit_details() == {
        "key": "SITE_NAME",
        "old_value": None,
        "new_value": "Acme",
    }


# --- custom variables ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,flag,secret",
    [
        ("ACME_PASS", None, True),
        ("ACME_PWD", False, True),  # false never overrides the name
        ("ACME_BEARER", None, True),
        ("ACME_DSN", None, True),
        ("ACME_CERT", None, True),
        ("ACME_REGION", None, False),
        ("ACME_REGION", True, True),
    ],
)
async def test_a_new_custom_variable_defaults_to_its_names_secrecy(
    config_env, key, flag, secret
) -> None:
    store = FakeAppConfigStore()
    await _boot(store)

    changes = await app_config.update(
        {key: "value-" + "abc"}, None if flag is None else {key: flag}, updated_by="admin"
    )

    assert store.rows[key].secret is secret
    entry = _entries()[key]
    assert entry["custom"] is True
    assert entry["secret"] is secret
    assert ("value-abc" in repr(changes[0].audit_details())) is not secret


@pytest.mark.asyncio
async def test_an_existing_custom_variable_can_be_marked_secret(config_env) -> None:
    store = FakeAppConfigStore([row("ACME_REGION", "east")])
    await _boot(store)
    assert _entries()["ACME_REGION"]["value"] == "east"

    await app_config.update({}, {"ACME_REGION": True}, updated_by="admin")

    assert store.rows["ACME_REGION"].secret
    assert _entries()["ACME_REGION"]["value"] is None


@pytest.fixture
def cli_store(config_env, monkeypatch) -> FakeAppConfigStore:
    """``manage`` against an in-memory table, as tests/unit/config/test_manage_cli.py does."""
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


def test_the_command_line_gives_a_custom_variable_the_same_default(cli_store, capsys) -> None:
    """``manage set`` without --secret passes false, which never overrides the name."""
    assert manage.main(["set", "ACME_CREDENTIALS", "cli-" + "secret-value"]) == 0
    assert manage.main(["set", "ACME_REGION", "east"]) == 0

    assert cli_store.rows["ACME_CREDENTIALS"].secret
    assert not cli_store.rows["ACME_REGION"].secret
    out = capsys.readouterr().out
    assert "cli-secret-value" not in out
    assert "ACME_REGION=east" in out


# --- a reset keeps the flag ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_reset_keeps_a_secret_flag_only_the_stored_row_held(
    config_env, monkeypatch
) -> None:
    """Added as a secret custom variable, then referenced, then reset: still secret."""
    store = FakeAppConfigStore()
    await _boot(store)
    await app_config.update(
        {_PLAIN_NAME: "https://box.example/v1?token=db"}, {_PLAIN_NAME: True}, updated_by="admin"
    )
    assert _entries()[_PLAIN_NAME]["custom"] is True

    # Referenced from a field that is not a credential's: only the row's flag
    # keeps it secret.
    _write_models(
        config_env,
        f"""
        models:
          - id: chat
            base_url: ${{{_PLAIN_NAME}}}
            api_key: literal-key
        """,
    )
    monkeypatch.setenv(_PLAIN_NAME, "https://box.example/v1?token=from-env")
    await _rediscover(store)
    entry = _entries()[_PLAIN_NAME]
    assert entry["custom"] is False and entry["secret"] is True

    change = await app_config.delete(_PLAIN_NAME, updated_by="admin")

    assert change.audit_details() == {"key": _PLAIN_NAME, "changed": True}
    assert store.rows[_PLAIN_NAME].source == "reset" and store.rows[_PLAIN_NAME].secret
    entry = _entries()[_PLAIN_NAME]
    assert entry["source"] == "environment"
    assert entry["secret"] is True and entry["value"] is None
    assert "token=from-env" not in repr(app_config.describe())

    # The next save, without the flag, stores it secret and audits no value.
    changes = await app_config.update(
        {_PLAIN_NAME: "https://box.example/v1?token=rotated"}, updated_by="admin"
    )
    assert [c.audit_details() for c in changes] == [{"key": _PLAIN_NAME, "changed": True}]
    assert store.rows[_PLAIN_NAME].secret

    # And across a restart, which reads the reset row back.
    await app_config.delete(_PLAIN_NAME, updated_by="admin")
    app_config.reset_state()
    get_settings.cache_clear()
    await _boot(store)
    assert _entries()[_PLAIN_NAME]["secret"] is True


@pytest.mark.asyncio
async def test_a_refresh_sees_a_flag_set_on_a_reset_row(config_env, monkeypatch) -> None:
    """Another process marks the reset row; this one picks it up on its refresh."""
    store = await _boot_with_plain_variable(config_env, monkeypatch)
    await app_config.update({_PLAIN_NAME: "https://box.example/v1"}, updated_by="admin")
    await app_config.delete(_PLAIN_NAME, updated_by="admin")
    assert _entries()[_PLAIN_NAME]["secret"] is False

    await store.mark_secret([_PLAIN_NAME], owner="custom")
    await app_config.refresh()

    assert _entries()[_PLAIN_NAME]["secret"] is True


# --- legacy rows --------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_marks_secret_the_rows_of_secret_settings(config_env) -> None:
    """Rows stored before a name or a reference counted as a credential's."""
    _write_models(
        config_env,
        """
        models:
          - id: chat
            base_url: https://api.example/v1
            api_key: ${ACME_ZZZ}
        """,
    )
    store = FakeAppConfigStore(
        [
            row("ACME_ZZZ", "legacy-" + "credential", source="env_import"),
            row("ACME_PASS", "legacy-" + "pass"),
            row("SITE_NAME", "Public"),
            row("SMTP_PASSWORD", "", source="reset"),
        ]
    )

    await _boot(store)

    assert sorted(store.marked) == [
        "ACME_PASS",
        "ACME_ZZZ",
        "SMTP_PASSWORD",
    ]
    assert not store.rows["SITE_NAME"].secret
    # Even once the reference is gone, the row keeps the value hidden.
    _write_models(config_env, "models: []\n")
    await _rediscover(store)
    entry = _entries()["ACME_ZZZ"]
    assert entry["custom"] is True and entry["secret"] is True


@pytest.mark.asyncio
async def test_a_write_never_clears_a_stored_secret_flag(config_env) -> None:
    store = FakeAppConfigStore([row("ACME_REGION", "east", secret=True)])
    await _boot(store)

    await app_config.update({"ACME_REGION": "west"}, {"ACME_REGION": False}, updated_by="admin")

    assert store.rows["ACME_REGION"].secret
    assert _entries()["ACME_REGION"]["value"] is None
