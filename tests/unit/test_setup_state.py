"""First-run setup state: the shared setup code, the marker, and the upgrade path."""

from __future__ import annotations

import asyncio
import logging
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving import setup_state
from serving.setup_state import (
    SETUP_CODE_ALPHABET,
    SETUP_CODE_KEY,
    SETUP_CODE_LENGTH,
    SETUP_MARKER_KEY,
    complete_setup,
    format_setup_code,
    generate_setup_code,
    init_setup_state,
    is_setup_required,
    mark_setup_completed,
    normalize_setup_code,
    refresh_setup_state,
    verify_setup_code,
)

STORED_CODE = "ABCDEFGHJKLM"
_LOGGED_CODE = re.compile(r"enter setup code ([A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4})")


def _store(code: str | None = STORED_CODE) -> MagicMock:
    """A store whose setup is pending with *code* stored, or complete when None."""
    store = MagicMock()
    store.get_or_create_setup_code = AsyncMock(return_value=code)
    store.create_first_admin = AsyncMock(return_value=True)
    return store


def _logged_codes(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        match.group(1)
        for record in caplog.records
        if (match := _LOGGED_CODE.search(record.getMessage()))
    ]


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger="serving.setup_state")
    return caplog


class TestSetupCode:
    def test_code_uses_only_the_unambiguous_alphabet(self):
        for _ in range(50):
            code = generate_setup_code()
            assert len(code) == SETUP_CODE_LENGTH
            assert set(code) <= set(SETUP_CODE_ALPHABET)
        assert not set("01IO") & set(SETUP_CODE_ALPHABET)

    def test_codes_differ(self):
        assert len({generate_setup_code() for _ in range(20)}) == 20

    def test_format_groups_in_fours(self):
        assert format_setup_code("ABCDEFGHJKLM") == "ABCD-EFGH-JKLM"

    @pytest.mark.parametrize(
        "typed", ["ABCD-EFGH-JKLM", "abcd-efgh-jklm", " abcd efgh jklm ", "ABCDEFGHJKLM"]
    )
    def test_normalize_ignores_case_spaces_and_dashes(self, typed):
        assert normalize_setup_code(typed) == "ABCDEFGHJKLM"


class TestInitSetupState:
    def test_not_required_before_init(self):
        assert is_setup_required() is False
        assert verify_setup_code("ABCD-EFGH-JKLM") is False

    async def test_database_free_mode_has_no_setup(self):
        await init_setup_state(None)
        assert is_setup_required() is False
        assert await refresh_setup_state() is False

    async def test_complete_setup_logs_no_code(self, logs):
        store = _store(code=None)

        await init_setup_state(store)

        assert is_setup_required() is False
        assert _logged_codes(logs) == []

    async def test_asks_the_store_for_the_shared_code(self):
        store = _store()

        await init_setup_state(store)

        kwargs = store.get_or_create_setup_code.await_args.kwargs
        assert kwargs["marker_key"] == SETUP_MARKER_KEY
        assert kwargs["code_key"] == SETUP_CODE_KEY
        assert kwargs["completed_at"].endswith("+00:00")
        assert len(kwargs["candidate_code"]) == SETUP_CODE_LENGTH

    async def test_pending_logs_the_stored_code(self, logs, monkeypatch):
        monkeypatch.setenv("FRONTEND_URL", "https://console.example.org/")

        await init_setup_state(_store())

        assert is_setup_required() is True
        (warning,) = [r for r in logs.records if "First-run setup is pending" in r.getMessage()]
        assert warning.levelno == logging.WARNING
        assert warning.getMessage() == (
            "First-run setup is pending. Open https://console.example.org/setup "
            "and enter setup code ABCD-EFGH-JKLM"
        )
        assert verify_setup_code("ABCD-EFGH-JKLM")
        assert verify_setup_code("abcd efgh jklm")
        assert not verify_setup_code("AAAA-AAAA-AAAA")
        assert not verify_setup_code("")

    async def test_the_code_survives_a_restart(self, logs):
        """A restart reads the stored code back; the operator's copy stays valid."""
        store = _store()
        await init_setup_state(store)
        await init_setup_state(store)  # the restarted process

        assert _logged_codes(logs) == ["ABCD-EFGH-JKLM", "ABCD-EFGH-JKLM"]
        assert verify_setup_code("ABCD-EFGH-JKLM")

    async def test_only_a_hash_of_the_code_is_kept(self):
        await init_setup_state(_store())

        state = setup_state._state
        assert STORED_CODE not in repr(state)
        assert isinstance(state.code_digest, bytes)

    async def test_the_code_is_shown_whatever_the_log_level(self, logs):
        """LOG_LEVEL=ERROR must not hide the one line setup cannot do without."""
        root = logging.getLogger()
        module_logger = logging.getLogger("serving.setup_state")
        saved = root.level, module_logger.level
        root.setLevel(logging.ERROR)
        module_logger.setLevel(logging.NOTSET)
        try:
            assert not module_logger.isEnabledFor(logging.WARNING)
            await init_setup_state(_store())
        finally:
            root.setLevel(saved[0])
            module_logger.setLevel(saved[1])

        assert _logged_codes(logs) == ["ABCD-EFGH-JKLM"]

    async def test_unreadable_state_fails_closed_without_a_code(self, logs):
        store = MagicMock()
        store.get_or_create_setup_code = AsyncMock(side_effect=RuntimeError("db down"))

        await init_setup_state(store)

        assert is_setup_required() is True
        assert not verify_setup_code(STORED_CODE)
        assert _logged_codes(logs) == []


class TestRefreshSetupState:
    async def test_notices_setup_completed_elsewhere(self, monkeypatch):
        store = _store()
        await init_setup_state(store)
        assert is_setup_required() is True

        store.get_or_create_setup_code.return_value = None
        monkeypatch.setattr(setup_state._state, "checked_at", 0.0)

        assert await refresh_setup_state() is False
        assert is_setup_required() is False
        assert not verify_setup_code(STORED_CODE)

    async def test_picks_up_the_code_once_the_database_answers(self, logs, monkeypatch):
        store = MagicMock()
        store.get_or_create_setup_code = AsyncMock(side_effect=OSError("refused"))
        await init_setup_state(store)
        assert not verify_setup_code(STORED_CODE)

        store.get_or_create_setup_code = AsyncMock(return_value=STORED_CODE)
        monkeypatch.setattr(setup_state._state, "checked_at", 0.0)

        assert await refresh_setup_state() is True
        assert verify_setup_code(STORED_CODE)
        assert _logged_codes(logs) == ["ABCD-EFGH-JKLM"]

    async def test_a_known_code_is_not_announced_again(self, logs, monkeypatch):
        store = _store()
        await init_setup_state(store)
        monkeypatch.setattr(setup_state._state, "checked_at", 0.0)

        assert await refresh_setup_state() is True

        assert _logged_codes(logs) == ["ABCD-EFGH-JKLM"]

    async def test_rereads_at_most_every_interval(self):
        store = _store()
        await init_setup_state(store)
        store.get_or_create_setup_code.reset_mock()

        # init just read it, so an immediate refresh stays in memory.
        assert await refresh_setup_state() is True
        store.get_or_create_setup_code.assert_not_awaited()

    async def test_failed_or_slow_read_stays_pending(self, monkeypatch):
        store = _store()
        await init_setup_state(store)

        async def _hang(**_kwargs):
            await asyncio.sleep(10)

        store.get_or_create_setup_code = AsyncMock(side_effect=_hang)
        monkeypatch.setattr(setup_state, "_REFRESH_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(setup_state._state, "checked_at", 0.0)
        assert await refresh_setup_state() is True

        store.get_or_create_setup_code = AsyncMock(side_effect=OSError("refused"))
        monkeypatch.setattr(setup_state._state, "checked_at", 0.0)
        assert await refresh_setup_state() is True
        assert is_setup_required() is True
        assert verify_setup_code(STORED_CODE)

    async def test_complete_setup_is_never_reread(self):
        store = _store(code=None)
        await init_setup_state(store)
        store.get_or_create_setup_code.reset_mock()

        assert await refresh_setup_state() is False
        store.get_or_create_setup_code.assert_not_awaited()


class TestCompleteSetup:
    async def test_creates_the_admin_and_drops_the_code(self):
        store = _store()
        await init_setup_state(store)

        created = await complete_setup(
            store,
            user_id="u1",
            login_name="admin",
            password_hash="hash",
            user_name="Ops",
            admin_ip="203.0.113.7",
        )

        assert created is True
        kwargs = store.create_first_admin.await_args.kwargs
        assert kwargs["marker_key"] == SETUP_MARKER_KEY
        assert kwargs["code_key"] == SETUP_CODE_KEY
        assert kwargs["marker_value"].endswith("+00:00")
        assert kwargs["login_name"] == "admin"
        assert kwargs["admin_ip"] == "203.0.113.7"
        assert is_setup_required() is False
        assert not verify_setup_code(STORED_CODE)

    async def test_lost_race_still_ends_pending_state(self):
        store = _store()
        store.create_first_admin.return_value = False
        await init_setup_state(store)

        created = await complete_setup(
            store,
            user_id="u1",
            login_name="admin",
            password_hash="hash",
            user_name="admin",
            admin_ip="203.0.113.7",
        )

        assert created is False
        assert is_setup_required() is False

    async def test_store_error_keeps_setup_pending(self):
        store = _store()
        store.create_first_admin.side_effect = RuntimeError("db down")
        await init_setup_state(store)

        with pytest.raises(RuntimeError):
            await complete_setup(
                store,
                user_id="u1",
                login_name="admin",
                password_hash="hash",
                user_name="admin",
                admin_ip="203.0.113.7",
            )

        assert is_setup_required() is True
        assert verify_setup_code(STORED_CODE)

    def test_mark_completed_forgets_the_code(self):
        setup_state._state.required = True
        setup_state._state.code_digest = b"x" * 32

        mark_setup_completed()

        assert is_setup_required() is False
        assert setup_state._state.code_digest is None
