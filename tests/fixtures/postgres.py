"""The one way a test reaches the PostgreSQL test database.

Every database test gets its connection from here. The inputs are
``TEST_DB_HOST``, ``TEST_DB_PORT``, ``TEST_DB_USER``, ``TEST_DB_PASSWORD`` and
``TEST_DB_NAME`` and nothing else; the ``DB_*`` variables the gateway itself
reads are *derived* from them (:meth:`DatabaseTarget.app_env`), never the other
way round. Before this module, about thirty files each read their own mix of
``TEST_DB_*``, ``TEST_PG_DSN``, ``PG_TEST_DSN`` and ``DB_*``, fell back to
``localhost:5432`` when one was missing, and skipped with their own wording when
the connection failed -- which is how CI stayed green for months while ~300
database tests never ran.

``TEST_DB_REQUIRED=1`` marks an environment that must have a database (CI, and
``make test-db``). There, :func:`database_unavailable` fails the test instead of
skipping it, and an unset ``TEST_DB_HOST`` / ``TEST_DB_PORT`` is an error rather
than a silent ``localhost:5432``. Without it, a machine with no database skips
these tests as before.

The schema is prepared here too (:func:`prepare_schema`), by the same code in CI
and locally: ``python -m tests.fixtures.postgres prepare --workers N``.

See ``docs/agents/specs/2026-10-18-test-database-contract-design.zh.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn
from urllib.parse import quote

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

    import asyncpg

ALLOWED_TEST_DB_PATTERN = "_test_"
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 5432
DEFAULT_USER = "postgres"
DEFAULT_PASSWORD = "postgres"
DEFAULT_NAME = "hybridinference_test_db"
MAINTENANCE_DATABASE = "postgres"

REQUIRED_ENV = "TEST_DB_REQUIRED"

#: The one erasure-fence secret every test uses. The fence pins the first
#: secret's fingerprint into a database and refuses any other afterwards, and a
#: local test database is reused across runs and files: two suites with two
#: secrets (the gateway falling back to API_KEY_SECRET was one) failed whichever
#: ran second. ``tests/conftest.py`` exports it as ``ERASURE_FENCE_SECRET``; a
#: test that constructs a store passes ``fence_secret=postgres.FENCE_SECRET``.
FENCE_SECRET = "test-fence-secret-not-for-production"
#: Read by nothing any more; setting one alone would silently fall back to the
#: defaults, so :func:`configuration_problem` refuses it instead.
RETIRED_ENV = ("TEST_PG_DSN", "PG_TEST_DSN")

_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+")
_TRUE = {"1", "true", "yes", "on"}

_REPO_ROOT = Path(__file__).resolve().parents[2]
#: pytest's ``pythonpath`` in ``pyproject.toml``. The command line is not run by
#: pytest, so it puts these first itself (:func:`_use_this_checkout`).
PYTHONPATH = ("apps/backend", ".")


class TestDatabaseConfigError(RuntimeError):
    """The test-database environment is inconsistent; no connection was tried."""

    __test__ = False


@dataclass(frozen=True)
class DatabaseTarget:
    """Where the test database is, resolved for this xdist worker."""

    host: str
    port: int
    user: str
    password: str
    #: ``TEST_DB_NAME`` exactly as given. Never written back anywhere: a fixture
    #: that stored the suffixed name there made the next one look for
    #: ``*_gw0_gw0``.
    base_name: str
    #: The database this worker uses: ``base_name``, or ``base_name_gwN``.
    name: str

    def connect_kwargs(self, database: str | None = None) -> dict[str, Any]:
        """Keyword arguments for ``asyncpg.connect`` / ``asyncpg.create_pool``."""
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": database or self.name,
        }

    def dsn(self, database: str | None = None) -> str:
        """A ``postgresql://`` URL for code that takes one."""
        user = quote(self.user, safe="")
        password = quote(self.password, safe="")
        return f"postgresql://{user}:{password}@{self.host}:{self.port}/{database or self.name}"

    def app_env(self) -> dict[str, str]:
        """The ``DB_*`` variables that point the gateway at this database."""
        return {
            "DB_HOST": self.host,
            "DB_PORT": str(self.port),
            "DB_NAME": self.name,
            "DB_USER": self.user,
            "DB_PASSWORD": self.password,
        }

    def describe(self, database: str | None = None) -> str:
        if database == MAINTENANCE_DATABASE:
            return f"server at {self.host}:{self.port}"
        return f"test database {database or self.name} at {self.host}:{self.port}"


def database_required() -> bool:
    """Whether this environment must have a database (``TEST_DB_REQUIRED``)."""
    return os.environ.get(REQUIRED_ENV, "").strip().lower() in _TRUE


def worker_id() -> str:
    """The xdist worker id, or ``master`` when running serially."""
    return os.environ.get("PYTEST_XDIST_WORKER", "master")


def worker_database_name(base_name: str, worker: str | None = None) -> str:
    """``base_name`` for a serial run, ``base_name_<worker>`` under xdist."""
    worker = worker_id() if worker is None else worker
    return base_name if worker == "master" else f"{base_name}_{worker}"


def assert_test_db_name(db_name: str, context: str = "") -> None:
    """Refuse a database whose name does not mark it as a test database."""
    if ALLOWED_TEST_DB_PATTERN not in (db_name or ""):
        pytest.fail(
            f"SAFETY: refusing to run tests against database '{db_name}' "
            f"(name does not contain '{ALLOWED_TEST_DB_PATTERN}')"
            f"{f' [{context}]' if context else ''}. "
            "Set TEST_DB_NAME to a dedicated test database."
        )


async def assert_test_db_from_pool(pool: Any, context: str = "") -> None:
    """Check the database a pool is actually connected to, not the one we asked for."""
    async with pool.acquire() as conn:
        await assert_test_db_from_connection(conn, context)


async def assert_test_db_from_connection(conn: Any, context: str = "") -> None:
    assert_test_db_name(await conn.fetchval("SELECT current_database()"), context)


def configuration_problem() -> str | None:
    """Why the environment cannot be used, or ``None``. Tries no connection."""
    retired = [name for name in RETIRED_ENV if os.environ.get(name)]
    if retired and not os.environ.get("TEST_DB_HOST"):
        return (
            f"{', '.join(retired)} is no longer read by the test suite. Set TEST_DB_HOST, "
            "TEST_DB_PORT, TEST_DB_USER, TEST_DB_PASSWORD and TEST_DB_NAME instead "
            "(see tests/fixtures/postgres.py)."
        )
    if database_required():
        missing = [name for name in ("TEST_DB_HOST", "TEST_DB_PORT") if not os.environ.get(name)]
        if missing:
            return (
                f"{REQUIRED_ENV} is set but {', '.join(missing)} is not; refusing to fall back "
                f"to {DEFAULT_HOST}:{DEFAULT_PORT}, which may be a different PostgreSQL."
            )
    port = os.environ.get("TEST_DB_PORT")
    if port and not port.isdigit():
        return f"TEST_DB_PORT must be a port number, got {port!r}."
    base_name = os.environ.get("TEST_DB_NAME") or DEFAULT_NAME
    if not _IDENTIFIER.fullmatch(base_name):
        return f"TEST_DB_NAME must be letters, digits and underscores, got {base_name!r}."
    if ALLOWED_TEST_DB_PATTERN not in base_name:
        return (
            f"TEST_DB_NAME={base_name!r} does not contain {ALLOWED_TEST_DB_PATTERN!r}; "
            "refusing to run tests against it."
        )
    return None


def target(*, worker: str | None = None) -> DatabaseTarget:
    """Resolve the test database for this worker from ``TEST_DB_*``."""
    problem = configuration_problem()
    if problem:
        raise TestDatabaseConfigError(problem)
    base_name = os.environ.get("TEST_DB_NAME") or DEFAULT_NAME
    return DatabaseTarget(
        host=os.environ.get("TEST_DB_HOST") or DEFAULT_HOST,
        port=int(os.environ.get("TEST_DB_PORT") or DEFAULT_PORT),
        user=os.environ.get("TEST_DB_USER") or DEFAULT_USER,
        password=os.environ.get("TEST_DB_PASSWORD") or DEFAULT_PASSWORD,
        base_name=base_name,
        name=worker_database_name(base_name, worker),
    )


def database_unavailable(reason: str) -> NoReturn:
    """The only exit for "no database here": fail where one is required, else skip."""
    if database_required():
        pytest.fail(f"{reason} ({REQUIRED_ENV} is set, so this is a failure, not a skip)")
    pytest.skip(reason)


def unavailable_errors() -> tuple[type[BaseException], ...]:
    """The exceptions that mean "no usable database here", and nothing else.

    Unreachable or timed-out server, a refused login, a database that does not
    exist, a server still starting or out of connections. An SQL, argument or
    ``init=`` callback error is a bug in the test or the code under test and
    must surface as one, so it is deliberately not in this list.
    """
    import asyncpg

    return (
        OSError,  # refused, unresolvable host, and TimeoutError
        asyncpg.PostgresConnectionError,
        asyncpg.InvalidAuthorizationSpecificationError,
        asyncpg.InvalidCatalogNameError,
        asyncpg.CannotConnectNowError,
        asyncpg.TooManyConnectionsError,
    )


@contextlib.contextmanager
def connecting(what: str) -> Iterator[None]:
    """Turn a connection failure inside the block into :func:`database_unavailable`.

    Wrap only the call that connects (``asyncpg.connect``, ``create_pool``,
    ``DatabaseLogger.initialize``); every other exception propagates unchanged.
    """
    try:
        yield
    except unavailable_errors() as exc:
        database_unavailable(f"PostgreSQL {what} is unavailable: {exc}")


_ready_databases: set[str] = set()


async def _ensure_ready(db: DatabaseTarget, database: str | None) -> None:
    """Create this worker's database and bring it to the full schema, once per process.

    Fixtures used to disagree about who creates the worker database (the rest
    skipped with "database … does not exist" whenever they ran first) and about
    who migrates it (a file that created a minimal ``api_logs`` for itself broke
    every later file that needed the real one). Doing both on first use here
    makes a database test independent of what ran before it, on an empty
    database just as on one CI prepared, where this only reads the catalog.
    A *database* the caller names is the caller's to manage.
    """
    import asyncpg

    if database is not None or db.name in _ready_databases:
        return
    await ensure_database(db.name)
    with connecting(db.describe()):
        pool = await asyncpg.create_pool(**db.connect_kwargs(), min_size=1, max_size=2)
    try:
        await assert_test_db_from_pool(pool, "prepare")
        await prepare_schema(pool)
    finally:
        await pool.close()
    _ready_databases.add(db.name)


async def worker_connect_kwargs() -> dict[str, Any]:
    """:meth:`DatabaseTarget.connect_kwargs` for code that opens its own pool.

    For handing to gateway code such as ``DatabaseLogger``: the worker's
    database is created and prepared first, as :func:`connect` and
    :func:`create_pool` do. Wrap the gateway's own connect in
    :func:`connecting`.
    """
    db = target()
    await _ensure_ready(db, None)
    return db.connect_kwargs()


_reachable_databases: set[str] = set()


def worker_dsn() -> str:
    """:meth:`DatabaseTarget.dsn` for synchronous fixtures, checked reachable.

    For fixtures that hand a DSN to gateway code (``DatabaseLogger({"dsn": …})``)
    and so cannot route its connection errors through :func:`database_unavailable`
    themselves: the worker's database is created and prepared, connected to once
    and checked to be a test database, or the test is skipped (failed under
    ``TEST_DB_REQUIRED``) here. Must be called outside a running event loop, as a
    plain, non-async fixture is.
    """
    db = target()
    if db.name not in _reachable_databases:

        async def _probe() -> None:
            conn = await connect(timeout=2)
            await conn.close()

        asyncio.run(_probe())
        _reachable_databases.add(db.name)
    return db.dsn()


async def connect(
    *, database: str | None = None, timeout: float = 5, **kwargs: Any
) -> asyncpg.Connection:
    """Connect to the worker's test database (or *database* on the same server).

    Anything but the maintenance database is checked to really be a test
    database once connected.
    """
    import asyncpg

    db = target()
    await _ensure_ready(db, database)
    with connecting(db.describe(database)):
        conn = await asyncpg.connect(**db.connect_kwargs(database), timeout=timeout, **kwargs)
    if database != MAINTENANCE_DATABASE:
        try:
            await assert_test_db_from_connection(conn, "connect")
        except BaseException:
            await conn.close()
            raise
    return conn


async def connect_maintenance(*, timeout: float = 5) -> asyncpg.Connection:
    """Connect to the server's ``postgres`` database, for CREATE / DROP DATABASE."""
    return await connect(database=MAINTENANCE_DATABASE, timeout=timeout)


async def create_pool(*, database: str | None = None, **kwargs: Any) -> asyncpg.Pool:
    """A pool on the worker's test database. Extra keyword arguments go to asyncpg.

    ``host``, ``port`` and the credentials always come from ``TEST_DB_*``; pass
    ``database`` only for a database this suite created on the same server.
    """
    import asyncpg

    for fixed in ("host", "port", "user", "password", "dsn"):
        if fixed in kwargs:
            raise TypeError(f"create_pool() does not take {fixed!r}: it comes from TEST_DB_*")
    db = target()
    await _ensure_ready(db, database)
    with connecting(db.describe(database)):
        pool = await asyncpg.create_pool(**db.connect_kwargs(database), **kwargs)
    try:
        await assert_test_db_from_pool(pool, "create_pool")
    except BaseException:
        await pool.close()
        raise
    return pool


async def ensure_database(name: str, *, template: str | None = None) -> bool:
    """Create database *name* on the test server unless it exists. True if created."""
    assert_test_db_name(name, "ensure_database")
    for identifier in (name, template):
        if identifier is not None and not _IDENTIFIER.fullmatch(identifier):
            raise TestDatabaseConfigError(f"unsafe database name: {identifier!r}")
    admin = await connect_maintenance()
    try:
        if await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            return False
        # Identifiers cannot be bound as parameters; both were checked above.
        clause = f' TEMPLATE "{template}"' if template else ""
        await admin.execute(f'CREATE DATABASE "{name}"{clause}')
        return True
    finally:
        await admin.close()


async def prepare_schema(pool: Any) -> None:
    """Bring a test database to the full current schema.

    The same three steps the gateway runs at startup. Running only the first
    leaves the operational columns (``users.signup_reason`` and friends)
    missing, which is what made the server tests fail on a fresh database.
    Idempotent: on a prepared database every step only reads the catalog.
    """
    from serving.storage.database import DatabaseLogger
    from serving.storage.postgres_operational import PostgresOperationalStore
    from serving.storage.responses_store import ResponseStore

    logger = DatabaseLogger({}, store_full_prompts=False)
    logger.pool = pool
    await logger.ensure_schema()
    await PostgresOperationalStore(pool).initialize()
    await ResponseStore(pool).initialize()


#: Prefix of the comment :func:`prepare_databases` puts on a database it
#: prepares for a checkout; :func:`drop_orphaned_databases` reads it back.
OWNER_COMMENT_PREFIX = "hybridinference test database for "


async def prepare_databases(workers: int = 0, *, owner: str | None = None) -> list[str]:
    """Prepare the base database and ``_gw0 … _gw{workers-1}``. Returns their names.

    A worker database that does not exist yet is copied from the prepared base
    (fast); one left over from an earlier run is brought up to date in place.
    *owner*, a checkout path, is recorded on each database so that
    :func:`drop_orphaned_databases` can remove them once that checkout is gone.
    Databases another checkout that still exists has recorded are refused, not
    taken over: a takeover would let ``clean`` drop them when *owner* goes.
    """
    import asyncpg

    db = target(worker="master")
    names = [db.base_name] + [
        worker_database_name(db.base_name, f"gw{index}") for index in range(workers)
    ]
    if owner is not None:
        await _refuse_another_owner(names, owner)
    await ensure_database(db.base_name)
    pool = await asyncpg.create_pool(**db.connect_kwargs(db.base_name), min_size=1, max_size=2)
    try:
        await prepare_schema(pool)
    finally:
        # CREATE DATABASE … TEMPLATE needs the template to have no connections.
        await pool.close()
    for name in names[1:]:
        if await ensure_database(name, template=db.base_name):
            continue
        pool = await asyncpg.create_pool(**db.connect_kwargs(name), min_size=1, max_size=2)
        try:
            await prepare_schema(pool)
        finally:
            await pool.close()
    if owner is not None:
        await _record_owner(names, owner)
    return names


async def _refuse_another_owner(names: list[str], owner: str) -> None:
    admin = await connect_maintenance()
    try:
        rows = await admin.fetch(
            "SELECT datname, shobj_description(oid, 'pg_database') AS note "
            "FROM pg_database WHERE datname = ANY($1::text[])",
            names,
        )
    finally:
        await admin.close()
    for row in rows:
        note = row["note"] or ""
        if not note.startswith(OWNER_COMMENT_PREFIX):
            continue
        other = note[len(OWNER_COMMENT_PREFIX) :]
        if other != owner and Path(other).exists():
            raise TestDatabaseConfigError(
                f"{row['datname']} belongs to {other}, which still exists; refusing to "
                f"prepare it for {owner}. Run from that checkout, or pick another database "
                "(TEST_DB_LOCAL_NAME for ops/dev/test-db-local.sh)."
            )


async def _record_owner(names: list[str], owner: str) -> None:
    # COMMENT takes no parameters; the names were checked by ensure_database.
    literal = (OWNER_COMMENT_PREFIX + owner).replace("'", "''")
    admin = await connect_maintenance()
    try:
        for name in names:
            await admin.execute(f"COMMENT ON DATABASE \"{name}\" IS '{literal}'")
    finally:
        await admin.close()


async def drop_orphaned_databases(*, prefix: str = "") -> list[str]:
    """Drop test databases whose owning checkout no longer exists. Returns their names.

    Only databases :func:`prepare_databases` marked with an owner are
    considered; one created any other way is never touched. "No longer exists"
    is judged from this process's view of the filesystem, so run it on the
    machine (not in a sandbox) whose checkouts own the databases.
    """
    admin = await connect_maintenance()
    try:
        rows = await admin.fetch(
            "SELECT datname, shobj_description(oid, 'pg_database') AS note FROM pg_database"
        )
        dropped = []
        for row in rows:
            note, name = row["note"] or "", row["datname"]
            if not name.startswith(prefix) or not note.startswith(OWNER_COMMENT_PREFIX):
                continue
            if Path(note[len(OWNER_COMMENT_PREFIX) :]).exists():
                continue
            if ALLOWED_TEST_DB_PATTERN not in name or not _IDENTIFIER.fullmatch(name):
                continue
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            dropped.append(name)
        return dropped
    finally:
        await admin.close()


def _use_this_checkout() -> None:
    """Import the gateway from the checkout this file is in, as pytest would.

    Without it, a virtualenv shared with another checkout (a worktree using the
    main checkout's ``.venv``) resolves ``serving`` to *that* checkout and
    prepares its schema, not the one the tests about to run expect.
    """
    for entry in reversed(PYTHONPATH):
        path = str(_REPO_ROOT / entry)
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)


def _gateway_source_problem() -> str | None:
    """Why the imported gateway is not this checkout's, or ``None``."""
    import serving

    location = Path(serving.__file__).resolve()
    if location.is_relative_to(_REPO_ROOT):
        return None
    return f"serving was imported from {location}, not from {_REPO_ROOT}."


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.fixtures.postgres")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser(
        "prepare", help="create and migrate the test database and its xdist worker copies"
    )
    prepare.add_argument(
        "--workers", type=int, default=0, help="number of xdist workers (-n) to prepare for"
    )
    prepare.add_argument(
        "--owner", help="checkout path to record, so that `clean` can drop these once it is gone"
    )
    commands.add_parser("clean", help="drop test databases whose --owner checkout no longer exists")
    args = parser.parse_args(argv)

    _use_this_checkout()
    problem = configuration_problem() or (
        _gateway_source_problem() if args.command == "prepare" else None
    )
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    db = target(worker="master")
    try:
        if args.command == "clean":
            dropped = asyncio.run(drop_orphaned_databases())
            print(f"dropped {', '.join(dropped) or 'nothing'} at {db.host}:{db.port}")
            return 0
        names = asyncio.run(prepare_databases(args.workers, owner=args.owner))
    except TestDatabaseConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (pytest.skip.Exception, pytest.fail.Exception) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"prepared {', '.join(names)} at {db.host}:{db.port}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
