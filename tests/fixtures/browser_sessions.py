"""An in-memory stand-in for the operational store's browser-session methods.

Route tests that build their store from mocks still exercise sign-in, refresh
and logout, which now go through browser sessions. ``install`` gives such a
store working versions of those methods, backed by dicts, joined to the store's
own ``get_user_by_id`` the way the Postgres query joins ``users``.

A browser lists the auth sessions the routes created, read back from the
``create_session`` / ``create_phone_session`` mocks' calls, and a legacy one the
store's ``get_session_by_token_hash`` mock answers with. The store's own mocks
are left in place, so a test can still change what they return; one that hands
out a legacy session some other way calls ``remember_session``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock


class BrowserSessionFake:
    """Browser sessions, their accounts and the auth sessions behind them."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.browsers: dict[str, dict[str, Any]] = {}
        self.members: dict[tuple[str, str], str] = {}
        self.sessions: dict[str, dict[str, Any]] = {}

    def remember_session(self, row: dict[str, Any]) -> None:
        """Record an auth session a browser may list."""
        self.sessions[row["id"]] = {**self.sessions.get(row["id"], {}), **row}

    def _session(self, auth_session_id: str) -> dict[str, Any] | None:
        if auth_session_id in self.sessions:
            return self.sessions[auth_session_id]
        for name in ("create_session", "create_phone_session"):
            mock = getattr(self.store, name, None)
            if not isinstance(mock, AsyncMock):
                continue
            for call in mock.await_args_list:
                if call.kwargs.get("session_id") == auth_session_id:
                    self.remember_session(
                        {
                            "id": auth_session_id,
                            "user_id": call.kwargs.get("user_id"),
                            "sid": call.kwargs.get("sid"),
                            "expires_at": call.kwargs.get("expires_at"),
                            "revoked": False,
                        }
                    )
                    return self.sessions[auth_session_id]
        lookup = getattr(self.store, "get_session_by_token_hash", None)
        legacy = getattr(lookup, "return_value", None)
        if isinstance(legacy, dict) and legacy.get("id") == auth_session_id:
            self.remember_session(dict(legacy))
            return self.sessions[auth_session_id]
        return None

    # -- store methods ---------------------------------------------------------

    async def create_browser_session(
        self, *, browser_session_id: str, secret_hash: str, expires_at: datetime
    ) -> None:
        self.browsers[browser_session_id] = {
            "id": browser_session_id,
            "secret_hash": secret_hash,
            "active_user_id": None,
            "expires_at": expires_at,
            "revoked": False,
        }

    async def get_browser_session(self, secret_hash: str) -> dict[str, Any] | None:
        for row in self.browsers.values():
            if row["secret_hash"] == secret_hash:
                return {key: row[key] for key in ("id", "active_user_id", "expires_at", "revoked")}
        return None

    async def rotate_browser_session(
        self,
        browser_session_id: str,
        *,
        current_secret_hash: str,
        new_secret_hash: str,
        expires_at: datetime | None = None,
    ) -> bool:
        row = self.browsers.get(browser_session_id)
        if row is None or row["revoked"] or row["secret_hash"] != current_secret_hash:
            return False
        row["secret_hash"] = new_secret_hash
        if expires_at is not None and expires_at > row["expires_at"]:
            row["expires_at"] = expires_at
        return True

    async def list_browser_session_accounts(self, browser_session_id: str) -> list[dict[str, Any]]:
        rows = []
        for (bid, user_id), auth_session_id in self.members.items():
            if bid != browser_session_id:
                continue
            session = self._session(auth_session_id) or {}
            user = await self.store.get_user_by_id(user_id) or {}
            rows.append(
                {
                    "user_id": user_id,
                    "auth_session_id": auth_session_id,
                    "added_at": datetime.now(UTC),
                    "sid": session.get("sid"),
                    "session_expires_at": session.get("expires_at"),
                    "session_revoked": session.get("revoked"),
                    "email": user.get("email"),
                    "login_name": user.get("login_name"),
                    "phone": user.get("phone"),
                    "phone_verified": user.get("phone_verified"),
                    "user_name": user.get("user_name"),
                    "role": user.get("role"),
                    "status": user.get("status"),
                    "email_verified": user.get("email_verified"),
                    "has_password": bool(user.get("password_hash")),
                    "created_at": user.get("created_at") or datetime.now(UTC),
                    "last_login_at": user.get("last_login_at"),
                }
            )
        return rows

    async def attach_browser_session_account(
        self, browser_session_id: str, *, user_id: str, auth_session_id: str
    ) -> bool:
        browser = self.browsers.get(browser_session_id)
        if browser is None or browser["revoked"]:
            return False
        previous = self.members.get((browser_session_id, user_id))
        if previous is not None and previous != auth_session_id and self._session(previous):
            self.sessions[previous]["revoked"] = True
        self.members[(browser_session_id, user_id)] = auth_session_id
        browser["active_user_id"] = user_id
        return True

    async def set_browser_session_active(
        self, browser_session_id: str, user_id: str | None
    ) -> None:
        self.browsers[browser_session_id]["active_user_id"] = user_id

    async def detach_browser_session_account(self, browser_session_id: str, user_id: str) -> int:
        auth_session_id = self.members.pop((browser_session_id, user_id), None)
        if auth_session_id is not None and self._session(auth_session_id):
            self.sessions[auth_session_id]["revoked"] = True
        browser = self.browsers[browser_session_id]
        if browser["active_user_id"] == user_id:
            browser["active_user_id"] = None
        remaining = sum(1 for bid, _ in self.members if bid == browser_session_id)
        if remaining == 0:
            browser.update(revoked=True, active_user_id=None)
        return remaining

    async def revoke_browser_session(self, browser_session_id: str) -> None:
        for key in [key for key in self.members if key[0] == browser_session_id]:
            auth_session_id = self.members.pop(key)
            if self._session(auth_session_id):
                self.sessions[auth_session_id]["revoked"] = True
        self.browsers[browser_session_id].update(revoked=True, active_user_id=None)


def install(store: Any) -> BrowserSessionFake:
    """Give a mock store working browser-session methods; return the fake behind them."""
    fake = BrowserSessionFake(store)
    for name in (
        "create_browser_session",
        "get_browser_session",
        "rotate_browser_session",
        "list_browser_session_accounts",
        "attach_browser_session_account",
        "set_browser_session_active",
        "detach_browser_session_account",
        "revoke_browser_session",
    ):
        setattr(store, name, AsyncMock(side_effect=getattr(fake, name)))

    if not isinstance(getattr(store, "rotate_session", None), AsyncMock):
        store.rotate_session = AsyncMock()
    return fake
