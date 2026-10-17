"""``python -m serving.auth.reset_password``, without a database."""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.auth import reset_password as cli
from serving.utils import password as password_utils

STRONG = "Str0ngPassword"

# users columns of a release from before login names and phone numbers, and
# of the current one.
COLUMNS_BEFORE_LOGIN_NAMES = (
    "id",
    "email",
    "password_hash",
    "user_name",
    "role",
    "status",
    "email_verified",
    "created_at",
    "last_login_at",
)
CURRENT_COLUMNS = (*COLUMNS_BEFORE_LOGIN_NAMES, "login_name", "phone", "phone_verified")


class _Connection:
    """Answers the catalog query with *columns* and every lookup with *row*; records SQL."""

    def __init__(self, columns: tuple[str, ...], row: dict[str, Any] | None) -> None:
        self.columns = columns
        self.row = row
        self.sql: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, str]]:
        self.sql.append((sql, args))
        assert "pg_attribute" in sql
        return [{"attname": column} for column in self.columns]

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.sql.append((sql, args))
        return self.row

    async def execute(self, sql: str, *args: Any) -> str:
        self.sql.append((sql, args))
        return "UPDATE 1"


class _Pool:
    def __init__(self, conn: _Connection) -> None:
        self.conn = conn
        self.closed = False

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self.conn

    async def close(self) -> None:
        self.closed = True


def _database(columns: tuple[str, ...], row: dict[str, Any] | None) -> tuple[_Pool, _Connection]:
    conn = _Connection(columns, row)
    return _Pool(conn), conn


def _store(account: dict[str, Any] | None) -> MagicMock:
    store = MagicMock()
    store.get_user_by_email = AsyncMock(return_value=account)
    store.get_user_by_login_name = AsyncMock(return_value=account)
    store.update_user_fields = AsyncMock()
    store.delete_user_sessions = AsyncMock()
    return store


ADMIN = {"id": "u-admin", "email": None, "login_name": "admin", "status": "active"}


def test_generated_passwords_are_strong_and_distinct():
    passwords = {cli.generate_password() for _ in range(20)}
    assert len(passwords) == 20
    for password in passwords:
        assert len(password) == 20
        assert password_utils.validate_password_strength(password) == (True, None)


class TestPrompt:
    def test_returns_the_password_when_both_entries_match(self):
        answers = iter([STRONG, STRONG])
        prompts: list[str] = []

        def _read(prompt: str) -> str:
            prompts.append(prompt)
            return next(answers)

        assert cli.prompt_new_password("admin", _read) == STRONG
        assert prompts == ["New password for admin: ", "Repeat the new password: "]

    def test_mismatch(self):
        answers = iter([STRONG, STRONG + "x"])
        with pytest.raises(cli.ResetPasswordError, match="do not match"):
            cli.prompt_new_password("admin", lambda _prompt: next(answers))

    def test_weak_password(self):
        with pytest.raises(cli.ResetPasswordError, match="at least 8 characters"):
            cli.prompt_new_password("admin", lambda _prompt: "Ab1")


class TestResetPassword:
    async def test_by_login_name(self):
        store = _store(ADMIN)

        account = await cli.reset_password(store, " Admin ", STRONG)

        assert account is ADMIN
        store.get_user_by_login_name.assert_awaited_once_with("Admin")
        store.get_user_by_email.assert_not_awaited()
        (user_id,) = store.update_user_fields.await_args.args
        assert user_id == "u-admin"
        new_hash = store.update_user_fields.await_args.kwargs["password_hash"]
        assert password_utils.verify_password(STRONG, new_hash)
        store.delete_user_sessions.assert_awaited_once_with("u-admin")

    async def test_by_email(self):
        store = _store({"id": "u1", "email": "a@example.com", "status": "active"})

        await cli.reset_password(store, "A@Example.com", STRONG)

        store.get_user_by_email.assert_awaited_once_with("A@Example.com")
        store.get_user_by_login_name.assert_not_awaited()

    async def test_unknown_account_changes_nothing(self):
        store = _store(None)

        with pytest.raises(cli.ResetPasswordError, match="No account"):
            await cli.reset_password(store, "ghost", STRONG)

        store.update_user_fields.assert_not_awaited()
        store.delete_user_sessions.assert_not_awaited()

    async def test_weak_password_never_reaches_the_database(self):
        store = _store(ADMIN)

        with pytest.raises(cli.ResetPasswordError):
            await cli.reset_password(store, "admin", "password")

        store.get_user_by_login_name.assert_not_awaited()
        store.update_user_fields.assert_not_awaited()


class TestDescribe:
    def test_generated_password_is_shown(self):
        lines = cli.describe_reset(ADMIN, "GeneratedPw123")
        assert lines[0] == "Password reset for admin (user id u-admin)."
        assert "New password: GeneratedPw123" in lines

    def test_prompted_password_is_not_shown(self):
        assert not any("New password" in line for line in cli.describe_reset(ADMIN, None))

    def test_inactive_account_is_flagged(self):
        lines = cli.describe_reset({**ADMIN, "status": "suspended"}, None)
        assert lines[-1] == "Note: the account is suspended, so it still cannot sign in."

    def test_a_row_without_status_gets_no_note(self):
        lines = cli.describe_reset({"id": "u1", "email": "a@example.com"}, None)
        assert lines == [
            "Password reset for a@example.com (user id u1).",
            "Every session of the account was revoked; it must sign in again.",
        ]


class TestAccountStore:
    """Lookups that work before and after the users table gains login names."""

    async def test_before_login_names_selects_only_the_columns_it_has(self):
        # The operational store would select login_name and phone here and fail
        # with UndefinedColumnError.
        row = {"id": "u1", "email": "a@example.com", "status": "active"}
        pool, conn = _database(COLUMNS_BEFORE_LOGIN_NAMES, row)

        account = await cli.AccountStore(pool).get_user_by_email("A@Example.com")

        assert account == row
        (sql, args) = conn.sql[-1]
        assert sql == "SELECT id, email, status FROM users WHERE email = $1"
        assert args == ("a@example.com",)

    async def test_before_login_names_a_login_name_is_refused_with_the_way_out(self):
        pool, conn = _database(COLUMNS_BEFORE_LOGIN_NAMES, None)

        with pytest.raises(cli.ResetPasswordError, match="no login names yet") as refused:
            await cli.AccountStore(pool).get_user_by_login_name("admin")

        assert "email address" in str(refused.value)
        assert "Nothing was changed" in str(refused.value)
        assert len(conn.sql) == 1  # the catalog read; users itself is never queried

    async def test_current_schema_finds_a_login_name(self):
        row = {"id": "u-admin", "email": None, "login_name": "admin", "status": "active"}
        pool, conn = _database(CURRENT_COLUMNS, row)
        store = cli.AccountStore(pool)

        assert await store.get_user_by_login_name(" Admin ") == row
        await store.get_user_by_email("a@example.com")

        catalog_reads = [sql for sql, _ in conn.sql if "pg_attribute" in sql]
        assert len(catalog_reads) == 1
        (sql, args) = conn.sql[1]
        assert sql == (
            "SELECT id, email, login_name, status FROM users "
            "WHERE lower(login_name) = $1 AND login_name IS NOT NULL"
        )
        assert args == ("admin",)

    async def test_no_users_table(self):
        pool, _ = _database((), None)

        with pytest.raises(cli.ResetPasswordError, match="no users table"):
            await cli.AccountStore(pool).get_user_by_email("a@example.com")

    async def test_reset_before_login_names_changes_no_schema(self):
        row = {"id": "u1", "email": "a@example.com", "status": "active"}
        pool, conn = _database(COLUMNS_BEFORE_LOGIN_NAMES, row)

        account = await cli.reset_password(cli.AccountStore(pool), "a@example.com", STRONG)

        assert account == row
        update, revoke = conn.sql[-2:]
        assert update[0] == "UPDATE users SET password_hash = $2 WHERE id = $1"
        assert update[1][0] == "u1"
        assert password_utils.verify_password(STRONG, update[1][1])
        assert revoke == ("DELETE FROM auth_sessions WHERE user_id = $1", ("u1",))
        # Reads and the two writes; no DDL.
        assert {sql.split()[0] for sql, _ in conn.sql} == {"SELECT", "UPDATE", "DELETE"}

    async def test_the_command_uses_it(self, monkeypatch, tmp_path):
        import asyncpg

        monkeypatch.chdir(tmp_path)  # no .env here
        row = {"id": "u1", "email": "a@example.com", "status": "active"}
        pool, conn = _database(COLUMNS_BEFORE_LOGIN_NAMES, row)
        monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=pool))

        assert await cli._reset_with_database("a@example.com", STRONG) == row
        assert pool.closed
        assert ("DELETE FROM auth_sessions WHERE user_id = $1", ("u1",)) in conn.sql


class TestMain:
    def _fake_database(self, monkeypatch, *, error: str | None = None) -> list[tuple[str, str]]:
        calls: list[tuple[str, str]] = []

        async def _reset(identifier: str, new_password: str) -> dict[str, Any]:
            calls.append((identifier, new_password))
            if error:
                raise cli.ResetPasswordError(error)
            return ADMIN

        monkeypatch.setattr(cli, "_reset_with_database", _reset)
        return calls

    def test_generate_prints_the_new_password(self, monkeypatch, capsys):
        calls = self._fake_database(monkeypatch)

        assert cli.main(["admin", "--generate"]) == 0

        ((identifier, password),) = calls
        assert identifier == "admin"
        assert f"New password: {password}" in capsys.readouterr().out

    def test_prompts_twice(self, monkeypatch, capsys):
        calls = self._fake_database(monkeypatch)
        prompts: list[str] = []

        def _getpass(prompt: str) -> str:
            prompts.append(prompt)
            return STRONG

        monkeypatch.setattr(cli.getpass, "getpass", _getpass)

        assert cli.main([" admin "]) == 0
        assert prompts == ["New password for admin: ", "Repeat the new password: "]
        assert calls == [("admin", STRONG)]
        out = capsys.readouterr().out
        assert "Password reset for admin" in out
        assert "New password" not in out

    def test_mismatched_prompts_exit_1(self, monkeypatch, capsys):
        calls = self._fake_database(monkeypatch)
        answers = iter([STRONG, "Different1Pw"])
        monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: next(answers))

        assert cli.main(["admin"]) == 1
        assert calls == []
        assert "do not match" in capsys.readouterr().err

    def test_failure_exits_1_with_the_reason(self, monkeypatch, capsys):
        self._fake_database(monkeypatch, error="No account has the login name 'x'.")

        assert cli.main(["x", "--generate"]) == 1
        assert "No account has the login name 'x'." in capsys.readouterr().err

    def test_cancelled_prompt_changes_nothing(self, monkeypatch, capsys):
        calls = self._fake_database(monkeypatch)

        def _interrupt(_identifier: str) -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "prompt_new_password", _interrupt)

        assert cli.main(["admin"]) == 1
        assert calls == []
        assert "Nothing was changed" in capsys.readouterr().err


async def test_the_database_is_reached_with_the_db_settings_alone(monkeypatch):
    """An invalid unrelated setting must not stop the recovery tool."""
    import asyncpg

    monkeypatch.setenv("TRUSTED_PROXIES", "not-a-network")
    monkeypatch.setenv("DB_HOST", "db.internal")
    monkeypatch.setenv("DB_PORT", "6543")
    seen: dict[str, Any] = {}

    async def _refused(**kwargs: Any):
        seen.update(kwargs)
        raise OSError("connection refused")

    monkeypatch.setattr(asyncpg, "create_pool", _refused)

    with pytest.raises(cli.ResetPasswordError, match=r"Could not connect to the database"):
        await cli._reset_with_database("admin", "a-new-Passw0rd")
    assert (seen["host"], seen["port"]) == ("db.internal", 6543)
