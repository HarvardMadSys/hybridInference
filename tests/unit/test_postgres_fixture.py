"""The test-database entry point resolves, refuses and gives up the way it says."""

from __future__ import annotations

import asyncio
import sys

import asyncpg
import pytest

from tests.fixtures import postgres

_ENV = (
    "TEST_DB_HOST",
    "TEST_DB_PORT",
    "TEST_DB_USER",
    "TEST_DB_PASSWORD",
    "TEST_DB_NAME",
    "TEST_DB_REQUIRED",
    "TEST_PG_DSN",
    "PG_TEST_DSN",
    "PYTEST_XDIST_WORKER",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def test_defaults_when_nothing_is_set():
    db = postgres.target()

    assert (db.host, db.port, db.user, db.password) == ("localhost", 5432, "postgres", "postgres")
    assert db.base_name == db.name == "hybridinference_test_db"


def test_reads_only_test_db_variables(monkeypatch):
    monkeypatch.setenv("TEST_DB_HOST", "db.test")
    monkeypatch.setenv("TEST_DB_PORT", "55432")
    monkeypatch.setenv("TEST_DB_USER", "tester")
    monkeypatch.setenv("TEST_DB_PASSWORD", "secret")
    monkeypatch.setenv("TEST_DB_NAME", "other_test_db")
    # The gateway's own variables are an output, never an input.
    monkeypatch.setenv("DB_HOST", "elsewhere")
    monkeypatch.setenv("DB_PORT", "5432")

    db = postgres.target()

    assert (db.host, db.port, db.user, db.password, db.name) == (
        "db.test",
        55432,
        "tester",
        "secret",
        "other_test_db",
    )


def test_worker_suffix_is_applied_once_and_base_name_is_kept(monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")

    db = postgres.target()

    assert db.base_name == "hybridinference_test_db"
    assert db.name == "hybridinference_test_db_gw3"
    assert postgres.target(worker="master").name == "hybridinference_test_db"


def test_app_env_points_the_gateway_at_the_worker_database(monkeypatch):
    monkeypatch.setenv("TEST_DB_PORT", "55432")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")

    assert postgres.target().app_env() == {
        "DB_HOST": "localhost",
        "DB_PORT": "55432",
        "DB_NAME": "hybridinference_test_db_gw0",
        "DB_USER": "postgres",
        "DB_PASSWORD": "postgres",
    }


def test_dsn_quotes_credentials(monkeypatch):
    monkeypatch.setenv("TEST_DB_PASSWORD", "p@ss/word")

    assert postgres.target().dsn() == (
        "postgresql://postgres:p%40ss%2Fword@localhost:5432/hybridinference_test_db"
    )
    assert postgres.target().dsn("postgres").endswith(":5432/postgres")


@pytest.mark.parametrize("retired", ["TEST_PG_DSN", "PG_TEST_DSN"])
def test_a_retired_variable_alone_is_refused(monkeypatch, retired):
    monkeypatch.setenv(retired, "postgresql://u:p@h:1/x_test_db")

    problem = postgres.configuration_problem()

    assert problem is not None and retired in problem and "TEST_DB_HOST" in problem
    with pytest.raises(postgres.TestDatabaseConfigError):
        postgres.target()


def test_a_retired_variable_next_to_test_db_host_is_ignored(monkeypatch):
    monkeypatch.setenv("TEST_PG_DSN", "postgresql://u:p@h:1/x_test_db")
    monkeypatch.setenv("TEST_DB_HOST", "db.test")

    assert postgres.configuration_problem() is None
    assert postgres.target().host == "db.test"


@pytest.mark.parametrize("missing", ["TEST_DB_HOST", "TEST_DB_PORT"])
def test_required_mode_does_not_fall_back_to_localhost(monkeypatch, missing):
    monkeypatch.setenv("TEST_DB_REQUIRED", "1")
    monkeypatch.setenv("TEST_DB_HOST", "db.test")
    monkeypatch.setenv("TEST_DB_PORT", "55432")
    monkeypatch.delenv(missing)

    problem = postgres.configuration_problem()

    assert problem is not None and missing in problem


@pytest.mark.parametrize(
    ("name", "value", "fragment"),
    [
        ("TEST_DB_PORT", "fivefourthreetwo", "port number"),
        ("TEST_DB_NAME", 'x_test_db"; DROP', "letters, digits"),
        ("TEST_DB_NAME", "hybridinference_prod", "_test_"),
    ],
)
def test_unsafe_values_are_refused(monkeypatch, name, value, fragment):
    monkeypatch.setenv(name, value)

    problem = postgres.configuration_problem()

    assert problem is not None and fragment in problem


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_required_mode_turns_unavailable_into_a_failure(monkeypatch, value):
    monkeypatch.setenv("TEST_DB_REQUIRED", value)

    with pytest.raises(pytest.fail.Exception, match="TEST_DB_REQUIRED"):
        postgres.database_unavailable("no database")


@pytest.mark.parametrize("value", [None, "", "0", "false"])
def test_otherwise_unavailable_is_a_skip(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("TEST_DB_REQUIRED", value)

    with pytest.raises(pytest.skip.Exception, match="no database"):
        postgres.database_unavailable("no database")


@pytest.mark.parametrize("fixed", ["host", "port", "user", "password", "dsn"])
async def test_create_pool_will_not_be_pointed_elsewhere(fixed):
    with pytest.raises(TypeError, match=fixed):
        await postgres.create_pool(**{fixed: "x"})


async def test_connect_failure_goes_through_the_one_exit(monkeypatch):
    # Nothing listens on port 1; the error must surface as a skip, not as an
    # OSError the test would report as broken.
    monkeypatch.setenv("TEST_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("TEST_DB_PORT", "1")

    with pytest.raises(pytest.skip.Exception, match=r"127\.0\.0\.1:1"):
        await postgres.connect(timeout=1)


def test_assert_test_db_name_rejects_a_non_test_database():
    postgres.assert_test_db_name("hybridinference_test_db")
    with pytest.raises(pytest.fail.Exception, match="refusing to run tests"):
        postgres.assert_test_db_name("hybridinference_prod")


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError("refused"),
        TimeoutError(),
        asyncio.TimeoutError(),
        asyncpg.InvalidPasswordError("bad password"),
        asyncpg.InvalidCatalogNameError("no such database"),
        asyncpg.CannotConnectNowError("starting up"),
    ],
    ids=type,
)
def test_connecting_turns_an_unreachable_database_into_the_one_exit(error):
    with (
        pytest.raises(pytest.skip.Exception, match="db at h:1 is unavailable"),
        postgres.connecting("db at h:1"),
    ):
        raise error


@pytest.mark.parametrize(
    "error",
    [
        asyncpg.UndefinedColumnError("column does not exist"),
        asyncpg.PostgresSyntaxError("syntax error"),
        TypeError("bad argument"),
        ValueError("bad value"),
    ],
    ids=type,
)
def test_connecting_lets_a_code_error_through(error):
    """An SQL or argument error is a bug, not a missing database: never a skip."""
    with pytest.raises(type(error)), postgres.connecting("db at h:1"):
        raise error


def test_cli_refuses_a_bad_environment(monkeypatch, capsys):
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("TEST_DB_NAME", "hybridinference_prod")

    assert postgres._main(["prepare"]) == 2
    assert "_test_" in capsys.readouterr().err


def test_cli_pythonpath_is_pytests():
    tomllib = pytest.importorskip("tomllib")
    pyproject = tomllib.loads((postgres._REPO_ROOT / "pyproject.toml").read_text())

    assert tuple(pyproject["tool"]["pytest"]["ini_options"]["pythonpath"]) == postgres.PYTHONPATH


def test_cli_puts_this_checkout_first(monkeypatch):
    elsewhere = "/elsewhere/apps/backend"
    backend = str(postgres._REPO_ROOT / "apps/backend")
    monkeypatch.setattr(sys, "path", [elsewhere, "/site-packages", backend])

    postgres._use_this_checkout()

    assert sys.path[:2] == [backend, str(postgres._REPO_ROOT)]
    assert sys.path.count(backend) == 1


def test_cli_refuses_another_checkouts_gateway(monkeypatch):
    import serving

    assert postgres._gateway_source_problem() is None
    monkeypatch.setattr(serving, "__file__", "/elsewhere/apps/backend/serving/__init__.py")

    problem = postgres._gateway_source_problem()

    assert problem is not None and "/elsewhere" in problem


@pytest.mark.parametrize("required", [False, True])
def test_server_session_setup_survives_an_unreachable_database(monkeypatch, required):
    """The server suite's session fixture must not skip (or fail) on its own.

    It is session-scoped and autouse: a skip there skipped every server test,
    including the many that never touch a database (``pytest.skip`` raises a
    ``BaseException``, so ``suppress(Exception)`` did not hold it).
    """
    from tests.servers.conftest import _ensure_worker_database

    monkeypatch.setenv("TEST_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("TEST_DB_PORT", "1")
    if required:
        monkeypatch.setenv("TEST_DB_REQUIRED", "1")

    _ensure_worker_database("hybridinference_test_db_gw9")
