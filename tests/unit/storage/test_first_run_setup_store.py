"""PostgresOperationalStore: email-less accounts and the first-run setup claim.

Mocked connections, no database. ``tests/integration/storage/
test_first_run_setup_postgres.py`` runs the same claim against PostgreSQL.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.postgres_operational import PostgresOperationalStore

_LOCK_SQL = "pg_advisory_xact_lock(hashtext('hybridinference:first-run-setup'))"


@pytest.fixture
def pg_conn() -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    @asynccontextmanager
    async def _transaction():
        yield

    conn.transaction = _transaction
    return conn


@pytest.fixture
def store(pg_conn: MagicMock) -> PostgresOperationalStore:
    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield pg_conn

    pool.acquire = _acquire
    return PostgresOperationalStore(pool)


def _admin_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "user_id": "u-admin",
        "login_name": "Admin",
        "password_hash": "argon2-hash",
        "user_name": "Ops",
        "marker_key": "setup_completed_at",
        "marker_value": "2026-10-10T12:00:00+00:00",
        "code_key": "setup_code",
        "admin_ip": "203.0.113.7",
    }
    kwargs.update(overrides)
    return kwargs


def _code_kwargs(candidate: str = "CANDIDATE234") -> dict[str, Any]:
    return {
        "marker_key": "setup_completed_at",
        "code_key": "setup_code",
        "completed_at": "2026-10-10T12:00:00+00:00",
        "candidate_code": candidate,
    }


class TestUserLookups:
    async def test_get_user_by_login_name_is_case_insensitive(self, store, pg_conn):
        pg_conn.fetchrow.return_value = {
            "id": "u1",
            "email": None,
            "login_name": "admin",
            "preferences": "{}",
        }

        row = await store.get_user_by_login_name("  Admin ")

        sql, param = pg_conn.fetchrow.await_args.args
        assert "lower(login_name) = $1" in sql
        assert "login_name IS NOT NULL" in sql
        assert param == "admin"
        assert row["login_name"] == "admin"
        assert row["email"] is None
        assert row["preferences"] == {}

    @pytest.mark.parametrize("method", ["get_user_by_id", "get_user_by_email"])
    async def test_single_user_lookups_return_login_name(self, store, pg_conn, method):
        await getattr(store, method)("x@example.com")
        sql = pg_conn.fetchrow.await_args.args[0]
        assert "login_name" in sql

    async def test_list_users_search_matches_login_name(self, store, pg_conn):
        pg_conn.fetchrow.return_value = {"total": 0}
        await store.list_users(search="adm")
        count_sql = pg_conn.fetchrow.await_args_list[0].args[0]
        assert "u.login_name ILIKE" in count_sql
        row_sql = pg_conn.fetch.await_args_list[-1].args[0]
        assert "u.login_name" in row_sql


class TestCreateUser:
    async def test_email_less_account(self, store, pg_conn):
        await store.create_user(
            user_id="u1", email=None, password_hash="h", login_name="Admin", email_verified=True
        )
        sql, *params = pg_conn.execute.await_args.args
        assert "login_name" in sql
        assert params[1] is None  # email
        assert params[-1] == "admin"  # login_name, lowercased

    async def test_signup_account_is_unchanged(self, store, pg_conn):
        await store.create_user(user_id="u1", email="Bob@Example.com", password_hash="h")
        params = pg_conn.execute.await_args.args[1:]
        assert params[1] == "bob@example.com"
        assert params[-1] is None


class TestGetOrCreateSetupCode:
    async def test_settled_deployment_takes_no_lock(self, store, pg_conn):
        pg_conn.fetchval.side_effect = [1]

        assert await store.get_or_create_setup_code(**_code_kwargs()) is None

        pg_conn.execute.assert_not_awaited()

    async def test_marker_written_while_waiting_for_the_lock(self, store, pg_conn):
        pg_conn.fetchval.side_effect = [None, 1]

        assert await store.get_or_create_setup_code(**_code_kwargs()) is None

        written = [c.args[0] for c in pg_conn.execute.await_args_list]
        assert len(written) == 1  # only the lock statement
        assert _LOCK_SQL in written[0]

    async def test_existing_users_record_the_marker_and_drop_the_code(self, store, pg_conn, caplog):
        pg_conn.fetchval.side_effect = [None, None, True]

        assert await store.get_or_create_setup_code(**_code_kwargs()) is None

        statements = [c.args for c in pg_conn.execute.await_args_list]
        assert _LOCK_SQL in statements[0][0]
        marker_sql, *marker_params = statements[1]
        assert "INSERT INTO site_settings" in marker_sql
        assert "ON CONFLICT (key) DO NOTHING" in marker_sql
        assert "'existing-users'" in marker_sql
        assert marker_params == ["setup_completed_at", "2026-10-10T12:00:00+00:00"]
        assert statements[2] == ("DELETE FROM site_settings WHERE key = $1", "setup_code")
        assert "already has user accounts" in caplog.text

    async def test_pending_stores_the_candidate_unless_a_code_exists(self, store, pg_conn):
        pg_conn.fetchval.side_effect = [None, None, False, "STOREDCODE23"]

        code = await store.get_or_create_setup_code(**_code_kwargs("CANDIDATE234"))

        assert code == "STOREDCODE23"  # whatever is stored wins
        statements = [c.args for c in pg_conn.execute.await_args_list]
        assert _LOCK_SQL in statements[0][0]
        insert_sql, *insert_params = statements[1]
        assert "ON CONFLICT (key) DO NOTHING" in insert_sql
        assert insert_params == ["setup_code", "CANDIDATE234"]
        assert len(statements) == 2


class TestCreateFirstAdmin:
    async def test_takes_the_lock_before_checking(self, store, pg_conn):
        pg_conn.fetchval.side_effect = [None, False]

        assert await store.create_first_admin(**_admin_kwargs()) is True

        first_statement = pg_conn.execute.await_args_list[0].args[0]
        assert _LOCK_SQL in first_statement

    async def test_inserts_an_active_verified_admin_without_email(self, store, pg_conn):
        pg_conn.fetchval.side_effect = [None, False]

        await store.create_first_admin(**_admin_kwargs())

        statements = [c.args for c in pg_conn.execute.await_args_list]
        user_sql, *user_params = next(s for s in statements if "INSERT INTO users" in s[0])
        assert "NULL" in user_sql
        assert "'admin', 'active', TRUE" in user_sql
        assert user_params == ["u-admin", "admin", "argon2-hash", "Ops"]

        marker_sql, *marker_params = next(
            s for s in statements if "INSERT INTO site_settings" in s[0]
        )
        assert marker_params == ["setup_completed_at", "2026-10-10T12:00:00+00:00", "admin"]
        assert "ON CONFLICT" not in marker_sql
        assert ("DELETE FROM site_settings WHERE key = $1", "setup_code") in statements

        _audit_sql, *audit_params = next(s for s in statements if "admin_audit_log" in s[0])
        assert audit_params[:3] == ["203.0.113.7", "setup.admin_created", "u-admin"]
        assert json.loads(audit_params[3]) == {"login_name": "admin", "user_name": "Ops"}

    @pytest.mark.parametrize(
        ("marker", "has_users"),
        [("2026-01-01T00:00:00+00:00", False), (None, True)],
        ids=["marker-present", "users-present"],
    )
    async def test_backs_out_once_setup_completed(self, store, pg_conn, marker, has_users):
        pg_conn.fetchval.side_effect = [marker, has_users]

        assert await store.create_first_admin(**_admin_kwargs()) is False

        written = [c.args[0] for c in pg_conn.execute.await_args_list]
        assert len(written) == 1  # only the lock statement
        assert _LOCK_SQL in written[0]


class _SimulatedDatabase:
    """Just enough of PostgreSQL to model concurrent setup claims.

    Transactions see committed rows plus their own writes (READ COMMITTED),
    and their writes become visible to others when they commit. A ``DELETE``
    removes only a row it could see when it ran, so a row someone else
    commits afterwards survives it. ``pg_advisory_xact_lock`` blocks until the
    holder's transaction ends.
    """

    def __init__(self) -> None:
        self.users: list[tuple[Any, ...]] = []
        self.settings: dict[str, str] = {}
        self.audit: list[tuple[Any, ...]] = []
        self.advisory_lock = asyncio.Lock()
        self.lock_attempts = 0

    def store(self) -> PostgresOperationalStore:
        pool = MagicMock()

        @asynccontextmanager
        async def _acquire():
            yield _SimulatedConnection(self)

        pool.acquire = _acquire
        return PostgresOperationalStore(pool)


class _SimulatedConnection:
    def __init__(self, db: _SimulatedDatabase) -> None:
        self._db = db
        self._ops: list[tuple[Any, ...]] = []
        self._holds_lock = False

    def _settings(self) -> dict[str, str]:
        view = dict(self._db.settings)
        for op in self._ops:
            _apply_setting(view, op)
        return view

    @asynccontextmanager
    async def transaction(self):
        try:
            yield
        finally:
            for op in self._ops:
                if op[0] == "user":
                    self._db.users.append(op[1])
                elif op[0] == "audit":
                    self._db.audit.append(op[1])
                else:
                    _apply_setting(self._db.settings, op)
            self._ops.clear()
            if self._holds_lock:
                self._holds_lock = False
                self._db.advisory_lock.release()

    async def execute(self, sql: str, *args: Any) -> str:
        if "pg_advisory_xact_lock" in sql:
            self._db.lock_attempts += 1
            await self._db.advisory_lock.acquire()
            self._holds_lock = True
        elif "INSERT INTO users" in sql:
            self._ops.append(("user", args))
        elif "INSERT INTO site_settings" in sql:
            self._ops.append(("insert", args[0], args[1], "ON CONFLICT" in sql))
        elif "DELETE FROM site_settings" in sql:
            self._ops.append(("delete", args[0], args[0] in self._settings()))
        elif "INSERT INTO admin_audit_log" in sql:
            self._ops.append(("audit", args))
        else:
            raise AssertionError(f"unexpected statement: {sql}")
        # Let the other task run between statements, as a real round trip would.
        await asyncio.sleep(0)
        return "OK"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        await asyncio.sleep(0)
        if "FROM site_settings" in sql:
            value = self._settings().get(args[0])
            if sql.startswith("SELECT 1"):
                return 1 if value is not None else None
            return value
        if "FROM users" in sql:
            return bool(self._db.users) or any(op[0] == "user" for op in self._ops)
        raise AssertionError(f"unexpected query: {sql}")


def _apply_setting(settings: dict[str, str], op: tuple[Any, ...]) -> None:
    if op[0] == "insert":
        _kind, key, value, on_conflict_do_nothing = op
        if key in settings and not on_conflict_do_nothing:
            raise AssertionError(f"duplicate key {key!r}")
        settings.setdefault(key, value)
    elif op[0] == "delete":
        _kind, key, visible_when_run = op
        if visible_when_run:
            settings.pop(key, None)


async def test_concurrent_claims_create_exactly_one_admin():
    db = _SimulatedDatabase()

    results = await asyncio.gather(
        db.store().create_first_admin(**_admin_kwargs(user_id="u-1", login_name="first")),
        db.store().create_first_admin(**_admin_kwargs(user_id="u-2", login_name="second")),
        db.store().create_first_admin(**_admin_kwargs(user_id="u-3", login_name="third")),
    )

    assert sorted(results) == [False, False, True]
    assert db.lock_attempts == 3
    assert len(db.users) == 1
    assert len(db.audit) == 1
    assert list(db.settings) == ["setup_completed_at"]


async def test_processes_booting_together_share_one_code():
    db = _SimulatedDatabase()

    codes = await asyncio.gather(
        *(db.store().get_or_create_setup_code(**_code_kwargs(f"CANDIDATE{n}")) for n in range(4))
    )

    assert len(set(codes)) == 1
    assert db.settings == {"setup_code": codes[0]}
    # A later boot (a restart) keeps it.
    assert await db.store().get_or_create_setup_code(**_code_kwargs("NEWCANDIDATE")) == codes[0]


@pytest.mark.parametrize("existing_code", ["EXISTINGCODE", None], ids=["stored", "none-yet"])
@pytest.mark.parametrize("code_first", [True, False], ids=["code-first", "claim-first"])
async def test_a_code_never_outlives_the_claim(code_first, existing_code):
    """A process booting while the claim runs cannot leave a code behind."""
    db = _SimulatedDatabase()
    if existing_code:
        db.settings["setup_code"] = existing_code

    async def _once_locked(coro):
        task = asyncio.create_task(coro)
        while not db.advisory_lock.locked():
            await asyncio.sleep(0)
        return task

    if code_first:
        boot = await _once_locked(db.store().get_or_create_setup_code(**_code_kwargs()))
        claim = asyncio.create_task(db.store().create_first_admin(**_admin_kwargs()))
    else:
        claim = await _once_locked(db.store().create_first_admin(**_admin_kwargs()))
        boot = asyncio.create_task(db.store().get_or_create_setup_code(**_code_kwargs()))
    code, created = await boot, await claim

    if code_first:
        assert code == (existing_code or "CANDIDATE234")
    else:
        assert code is None
    assert created is True
    assert "setup_code" not in db.settings
    assert "setup_completed_at" in db.settings
