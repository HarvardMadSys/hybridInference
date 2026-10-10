"""``python -m serving.auth.reset_password``, without a database."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.auth import reset_password as cli
from serving.utils import password as password_utils

STRONG = "Str0ngPassword"


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
