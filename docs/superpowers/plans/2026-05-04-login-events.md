# Login Events Audit Table — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist every `/login` outcome (success + 7 failure paths) into a new `login_events` table, with admin-gated purge endpoints for retention (by-age) and GDPR (by-user). Always-on logging — no runtime toggle.

**Architecture:** A new operational-store table, fronted by three new abstract methods on `OperationalStore` (`record_login_event`, `purge_login_events_older_than`, `purge_login_events_for_user`). Eight call sites in `POST /login` invoke the writer via a tiny `_record` closure that catches DB exceptions so audit failures don't break login. A new admin router (`admin/login_events.py`) exposes one `DELETE /admin/login-events` endpoint that takes one of `?older_than_days=` or `?user_id=`. The existing `hard_delete_user` flow gets a one-line extension to sweep the new rows.

**Tech Stack:** FastAPI, asyncpg (Postgres), httpx + Cloudflare D1 (alt store), pytest / pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-05-04-login-events-design.md`
**Issue:** #428

---

## File Structure

| File | Role | Status |
|---|---|---|
| `apps/backend/serving/storage/base.py` | +3 abstract methods on `OperationalStore` | Modify |
| `apps/backend/serving/storage/postgres_operational.py` | New `login_events` table + indexes; concrete impls of the three methods; `hard_delete_user` extended | Modify |
| `apps/backend/serving/storage/d1_operational.py` | Concrete impls (D1 SQL dialect); `hard_delete_user` extended | Modify |
| `apps/backend/serving/storage/cache.py` | Three pass-through methods on `CachedOperationalStore` | Modify |
| `apps/backend/serving/storage/dual_write.py` | Three dual-write methods on `DualWriteOperationalStore` | Modify |
| `apps/backend/serving/servers/routers/auth_routes.py` | `_record` helper inside `login`; eight call sites | Modify |
| `apps/backend/serving/servers/routers/admin/login_events.py` | **New** — `DELETE /admin/login-events` | Create |
| `apps/backend/serving/servers/routers/admin/__init__.py` | Include the new router | Modify |
| `tests/unit/storage/test_login_events.py` | **New** — Postgres concrete-impl tests (table CRUD + purge) | Create |
| `tests/servers/test_auth_routes_login_events.py` | **New** — `/login` writes the right `login_events` row at every outcome | Create |
| `tests/servers/test_admin_login_events.py` | **New** — admin endpoint behaviour (by-age, by-user, both/neither, auth) | Create |

---

## Task 1: Storage interface + Postgres implementation + tests

**Files:**
- Modify: `apps/backend/serving/storage/base.py`
- Modify: `apps/backend/serving/storage/postgres_operational.py`
- Modify: `apps/backend/serving/storage/cache.py`
- Modify: `apps/backend/serving/storage/dual_write.py`
- Modify: `apps/backend/serving/storage/d1_operational.py`
- Test: `tests/unit/storage/test_login_events.py`

This is a single task because adding `@abstractmethod` to the base ripples to every concrete subclass — they must all implement (or stub) the methods at the same time, otherwise instantiation breaks across the suite.

- [ ] **Step 1: Write the failing tests for the Postgres impl**

Create `tests/unit/storage/test_login_events.py`:

```python
"""Postgres-fixture tests for the login_events table and store methods.

Skipped when PG_TEST_DSN is unset (matches existing storage tests).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from serving.storage.postgres_operational import PostgresOperationalStore

pytestmark = pytest.mark.skipif(
    not os.environ.get("PG_TEST_DSN"),
    reason="PG_TEST_DSN not set; skipping Postgres-fixture tests",
)


@pytest.fixture
async def store() -> PostgresOperationalStore:
    pool = await asyncpg.create_pool(os.environ["PG_TEST_DSN"], min_size=1, max_size=2)
    s = PostgresOperationalStore(pool)
    await s.initialize()
    # Clean slate for the test run.
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM login_events")
    try:
        yield s
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_record_login_event_success(store: PostgresOperationalStore):
    await store.record_login_event(
        email="alice@example.com",
        outcome="success",
        failure_reason=None,
        user_id="u1",
        ip="203.0.113.5",
        user_agent="curl/8",
    )
    async with store._pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM login_events WHERE email=$1", "alice@example.com")
    assert row["outcome"] == "success"
    assert row["failure_reason"] is None
    assert row["user_id"] == "u1"
    assert row["ip"] == "203.0.113.5"
    assert row["user_agent"] == "curl/8"


@pytest.mark.asyncio
async def test_record_login_event_failure_unknown_user(store: PostgresOperationalStore):
    await store.record_login_event(
        email="bogus@example.com",
        outcome="failure",
        failure_reason="user_not_found",
        user_id=None,
        ip=None,
        user_agent=None,
    )
    async with store._pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM login_events WHERE email=$1", "bogus@example.com")
    assert row["user_id"] is None
    assert row["outcome"] == "failure"
    assert row["failure_reason"] == "user_not_found"


@pytest.mark.asyncio
async def test_record_login_event_rejects_bad_outcome(store: PostgresOperationalStore):
    """The CHECK constraint rejects unknown outcome values."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await store.record_login_event(
            email="x@y.com",
            outcome="maybe",
            failure_reason=None,
            user_id=None,
            ip=None,
            user_agent=None,
        )


@pytest.mark.asyncio
async def test_purge_older_than_days(store: PostgresOperationalStore):
    # Insert two rows: one fresh, one 30 days old.
    await store.record_login_event(
        email="fresh@example.com", outcome="success",
        failure_reason=None, user_id="u_fresh", ip=None, user_agent=None,
    )
    async with store._pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO login_events (created_at, email, outcome, failure_reason, user_id) "
            "VALUES ($1, $2, $3, $4, $5)",
            datetime.now(timezone.utc) - timedelta(days=30),
            "old@example.com", "success", None, "u_old",
        )

    deleted = await store.purge_login_events_older_than(7)
    assert deleted == 1

    async with store._pool.acquire() as conn:
        rows = await conn.fetch("SELECT email FROM login_events ORDER BY created_at")
    assert [r["email"] for r in rows] == ["fresh@example.com"]


@pytest.mark.asyncio
async def test_purge_for_user(store: PostgresOperationalStore):
    for uid, email in (("u1", "a@x.com"), ("u1", "a@x.com"), ("u2", "b@x.com")):
        await store.record_login_event(
            email=email, outcome="success", failure_reason=None,
            user_id=uid, ip=None, user_agent=None,
        )
    deleted = await store.purge_login_events_for_user("u1")
    assert deleted == 2
    async with store._pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM login_events")
    assert [r["user_id"] for r in rows] == ["u2"]


@pytest.mark.asyncio
async def test_initialize_is_idempotent(store: PostgresOperationalStore):
    # Calling initialize twice must not raise.
    await store.initialize()
    await store.initialize()
```

- [ ] **Step 2: Run the tests to verify they fail (table or methods missing)**

```bash
cd /home/juncheng/hybridInference-worktrees/login-events
PG_TEST_DSN=postgres://postgres:postgres@localhost:5432/test uv run pytest tests/unit/storage/test_login_events.py -v
```

Expected: tests SKIP when PG is not available (acceptable in dev environments without local Postgres). When PG is available, FAIL because `record_login_event` doesn't exist.

If you can't run with a real Postgres, the test design is still correct; you'll exercise the same paths in Task 6 via a Docker fixture if available.

- [ ] **Step 3: Add abstract methods to `OperationalStore`**

Open `apps/backend/serving/storage/base.py`. Locate the section just before `# -- email verification tokens --------------` (currently around line 404). Add a new section:

```python
    # -- login events (audit) ------------------------------------------------

    @abstractmethod
    async def record_login_event(
        self,
        *,
        email: str,
        outcome: str,
        failure_reason: str | None,
        user_id: str | None,
        ip: str | None,
        user_agent: str | None,
    ) -> None:
        """Insert one row into ``login_events``. Best-effort for callers —
        callers may catch + log on exception so audit failures don't break
        login. ``outcome`` must be one of ``'success'`` | ``'failure'``."""

    @abstractmethod
    async def purge_login_events_older_than(self, days: int) -> int:
        """Delete ``login_events`` rows older than ``days``. Returns the
        deleted row count."""

    @abstractmethod
    async def purge_login_events_for_user(self, user_id: str) -> int:
        """Delete all ``login_events`` rows for ``user_id``. Returns the
        deleted row count."""
```

- [ ] **Step 4: Add the table + indexes + concrete methods to `PostgresOperationalStore`**

Open `apps/backend/serving/storage/postgres_operational.py`. Inside `_create_tables` (currently around line 56), add the new table after the existing `auth_sessions` block:

```python
        # --- login_events ---
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS login_events (
                id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                user_id TEXT,
                email TEXT NOT NULL,
                outcome TEXT NOT NULL
                    CHECK (outcome IN ('success', 'failure')),
                failure_reason TEXT,
                ip TEXT,
                user_agent TEXT
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_events_user "
            "ON login_events (user_id, created_at DESC) WHERE user_id IS NOT NULL"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_events_email "
            "ON login_events (email, created_at DESC)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_events_created "
            "ON login_events (created_at DESC)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_events_failures "
            "ON login_events (created_at DESC) WHERE outcome = 'failure'"
        )
```

Then add the three concrete methods near the end of the class (before any `__all__` if present, or just at the bottom of the class definition):

```python
    # -- login events --------------------------------------------------------

    async def record_login_event(
        self,
        *,
        email: str,
        outcome: str,
        failure_reason: str | None,
        user_id: str | None,
        ip: str | None,
        user_agent: str | None,
    ) -> None:
        """Insert one ``login_events`` row."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO login_events
                    (user_id, email, outcome, failure_reason, ip, user_agent)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                user_id,
                email,
                outcome,
                failure_reason,
                ip,
                user_agent,
            )

    async def purge_login_events_older_than(self, days: int) -> int:
        """Delete rows older than ``days`` days. Returns the deleted count."""
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM login_events "
                "WHERE created_at < NOW() - ($1::int || ' days')::interval",
                days,
            )
        try:
            return int(status.rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            return 0

    async def purge_login_events_for_user(self, user_id: str) -> int:
        """Delete all rows for ``user_id``. Returns the deleted count."""
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM login_events WHERE user_id = $1", user_id
            )
        try:
            return int(status.rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            return 0
```

The `($1::int || ' days')::interval` form is the safe asyncpg way to parameterize an `INTERVAL` literal — `INTERVAL '$1 days'` doesn't substitute the parameter.

- [ ] **Step 5: Add pass-through on `CachedOperationalStore`**

Open `apps/backend/serving/storage/cache.py`. Inside the `CachedOperationalStore` class (around line 100), add a new section near the existing `# -- sessions (pass-through)` block (around line 430):

```python
    # -- login events (pass-through) -----------------------------------------

    async def record_login_event(
        self,
        *,
        email: str,
        outcome: str,
        failure_reason: str | None,
        user_id: str | None,
        ip: str | None,
        user_agent: str | None,
    ) -> None:
        await self._store.record_login_event(
            email=email,
            outcome=outcome,
            failure_reason=failure_reason,
            user_id=user_id,
            ip=ip,
            user_agent=user_agent,
        )

    async def purge_login_events_older_than(self, days: int) -> int:
        return await self._store.purge_login_events_older_than(days)

    async def purge_login_events_for_user(self, user_id: str) -> int:
        return await self._store.purge_login_events_for_user(user_id)
```

- [ ] **Step 6: Add dual-write impls on `DualWriteOperationalStore`**

Open `apps/backend/serving/storage/dual_write.py`. After `update_user_last_login` (around line 219), add:

```python
    async def record_login_event(
        self,
        *,
        email: str,
        outcome: str,
        failure_reason: str | None,
        user_id: str | None,
        ip: str | None,
        user_agent: str | None,
    ) -> None:
        """Write to primary, then shadow."""
        kwargs = dict(
            email=email,
            outcome=outcome,
            failure_reason=failure_reason,
            user_id=user_id,
            ip=ip,
            user_agent=user_agent,
        )
        await self._primary.record_login_event(**kwargs)
        await self._do_shadow(
            "record_login_event",
            self._shadow.record_login_event(**kwargs),
            user_id=user_id,
        )

    async def purge_login_events_older_than(self, days: int) -> int:
        """Run on primary; mirror on shadow. Returns primary's count."""
        deleted = await self._primary.purge_login_events_older_than(days)
        await self._do_shadow(
            "purge_login_events_older_than",
            self._shadow.purge_login_events_older_than(days),
        )
        return deleted

    async def purge_login_events_for_user(self, user_id: str) -> int:
        """Run on primary; mirror on shadow. Returns primary's count."""
        deleted = await self._primary.purge_login_events_for_user(user_id)
        await self._do_shadow(
            "purge_login_events_for_user",
            self._shadow.purge_login_events_for_user(user_id),
            user_id=user_id,
        )
        return deleted
```

- [ ] **Step 7: Add D1 impls**

Open `apps/backend/serving/storage/d1_operational.py`. First, extend `D1OperationalStore.initialize()` (or wherever tables are created) to also create `login_events`. Find the existing CREATE-TABLE block; add a parallel one for login_events:

```python
        # login_events (audit)
        await self._client.execute(
            """
            CREATE TABLE IF NOT EXISTS login_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT (CURRENT_TIMESTAMP),
                user_id TEXT,
                email TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure')),
                failure_reason TEXT,
                ip TEXT,
                user_agent TEXT
            )
            """
        )
        await self._client.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_events_user "
            "ON login_events (user_id, created_at DESC)"
        )
        await self._client.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_events_email "
            "ON login_events (email, created_at DESC)"
        )
        await self._client.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_events_created "
            "ON login_events (created_at DESC)"
        )
```

Then add the three concrete methods near the end of the class:

```python
    # -- login events --------------------------------------------------------

    async def record_login_event(
        self,
        *,
        email: str,
        outcome: str,
        failure_reason: str | None,
        user_id: str | None,
        ip: str | None,
        user_agent: str | None,
    ) -> None:
        await self._client.execute(
            "INSERT INTO login_events (user_id, email, outcome, failure_reason, ip, user_agent) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [user_id, email, outcome, failure_reason, ip, user_agent],
        )

    async def purge_login_events_older_than(self, days: int) -> int:
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        result = await self._client.execute(
            "DELETE FROM login_events WHERE created_at < ?",
            [cutoff],
        )
        return int(result.get("meta", {}).get("changes", 0))

    async def purge_login_events_for_user(self, user_id: str) -> int:
        result = await self._client.execute(
            "DELETE FROM login_events WHERE user_id = ?",
            [user_id],
        )
        return int(result.get("meta", {}).get("changes", 0))
```

D1 lacks `INTERVAL` so we precompute the cutoff timestamp client-side. The `result["meta"]["changes"]` shape is what the existing D1 client returns for DML — verify by skimming `d1_client.py` and matching the existing `delete_user_sessions` style.

- [ ] **Step 8: Run the storage tests + suite-wide instantiation tests**

```bash
cd /home/juncheng/hybridInference-worktrees/login-events
PG_TEST_DSN=postgres://postgres:postgres@localhost:5432/test uv run pytest tests/unit/storage/ tests/servers/test_admin_settings.py -q 2>&1 | tail -10
```

Expected: the new login_events Postgres tests PASS (or SKIP if no DB), and existing tests that instantiate `CachedOperationalStore` / `DualWriteOperationalStore` / `D1OperationalStore` still pass — they don't fail with `TypeError: Can't instantiate abstract class`.

- [ ] **Step 9: Commit**

```bash
git add \
  apps/backend/serving/storage/base.py \
  apps/backend/serving/storage/postgres_operational.py \
  apps/backend/serving/storage/cache.py \
  apps/backend/serving/storage/dual_write.py \
  apps/backend/serving/storage/d1_operational.py \
  tests/unit/storage/test_login_events.py
git commit -m "feat(storage): add login_events table + record/purge methods"
```

---

## Task 2: Wire `record_login_event` into `/login`

**Files:**
- Modify: `apps/backend/serving/servers/routers/auth_routes.py`
- Test: `tests/servers/test_auth_routes_login_events.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/servers/test_auth_routes_login_events.py`:

```python
"""Tests verifying /login writes a login_events row at every outcome path."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import get_operational_store
from serving.servers.routers.auth_routes import router as auth_router


@pytest.fixture
def fake_op_store() -> MagicMock:
    op = MagicMock()
    op.get_user_by_email = AsyncMock(return_value=None)
    op.update_user_last_login = AsyncMock()
    op.update_user_fields = AsyncMock()
    op.create_session = AsyncMock()
    op.record_login_event = AsyncMock()
    return op


@pytest.fixture
def app(fake_op_store, monkeypatch) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_operational_store] = lambda: fake_op_store
    # Patch rate-limit so it doesn't reject in tests by default.
    async def _allow(email, ip):
        return True, None
    monkeypatch.setattr(
        "serving.servers.routers.auth_routes.check_and_record_login",
        _allow,
    )
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    return app


async def _post_login(app, email="x@y.com", password="pw"):
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        return await client.post(
            "/login",
            json={"email": email, "password": password},
        )


@pytest.mark.asyncio
async def test_login_records_user_not_found(app, fake_op_store):
    fake_op_store.get_user_by_email.return_value = None
    resp = await _post_login(app, email="ghost@example.com")
    assert resp.status_code == 401
    fake_op_store.record_login_event.assert_awaited_once()
    kw = fake_op_store.record_login_event.await_args.kwargs
    assert kw["email"] == "ghost@example.com"
    assert kw["outcome"] == "failure"
    assert kw["failure_reason"] == "user_not_found"
    assert kw["user_id"] is None


@pytest.mark.asyncio
async def test_login_records_invalid_password(app, fake_op_store, monkeypatch):
    fake_op_store.get_user_by_email.return_value = {
        "id": "u1", "email": "a@b.com", "user_name": "A",
        "password_hash": "$2b$12$NOTUSED", "role": "free",
        "status": "active", "email_verified": True,
        "created_at": None, "last_login_at": None,
    }
    monkeypatch.setattr(
        "serving.utils.password.verify_password", lambda a, b: False,
    )
    resp = await _post_login(app, email="a@b.com")
    assert resp.status_code == 401
    fake_op_store.record_login_event.assert_awaited_once()
    kw = fake_op_store.record_login_event.await_args.kwargs
    assert kw["failure_reason"] == "invalid_password"
    assert kw["user_id"] == "u1"


@pytest.mark.asyncio
async def test_login_records_email_unverified(app, fake_op_store, monkeypatch):
    fake_op_store.get_user_by_email.return_value = {
        "id": "u1", "email": "a@b.com", "user_name": "A",
        "password_hash": "$2b$12$NOTUSED", "role": "free",
        "status": "active", "email_verified": False,
        "created_at": None, "last_login_at": None,
    }
    monkeypatch.setattr(
        "serving.utils.password.verify_password", lambda a, b: True,
    )
    # Force the runtime-settings check to require verification.
    monkeypatch.setattr(
        "serving.config.settings.settings.signup_require_email_verification",
        True,
        raising=False,
    )
    resp = await _post_login(app, email="a@b.com")
    assert resp.status_code == 403
    kw = fake_op_store.record_login_event.await_args.kwargs
    assert kw["failure_reason"] == "email_unverified"


@pytest.mark.asyncio
async def test_login_records_pending_approval(app, fake_op_store, monkeypatch):
    fake_op_store.get_user_by_email.return_value = {
        "id": "u1", "email": "a@b.com", "user_name": "A",
        "password_hash": "$2b$12$NOTUSED", "role": "free",
        "status": "pending_approval", "email_verified": True,
        "created_at": None, "last_login_at": None,
    }
    monkeypatch.setattr("serving.utils.password.verify_password", lambda a, b: True)
    monkeypatch.setattr(
        "serving.config.settings.settings.signup_require_email_verification",
        False, raising=False,
    )
    resp = await _post_login(app, email="a@b.com")
    assert resp.status_code == 403
    kw = fake_op_store.record_login_event.await_args.kwargs
    assert kw["failure_reason"] == "account_pending_approval"


@pytest.mark.asyncio
async def test_login_records_rejected(app, fake_op_store, monkeypatch):
    fake_op_store.get_user_by_email.return_value = {
        "id": "u1", "email": "a@b.com", "user_name": "A",
        "password_hash": "$2b$12$NOTUSED", "role": "free",
        "status": "rejected", "email_verified": True,
        "created_at": None, "last_login_at": None,
    }
    monkeypatch.setattr("serving.utils.password.verify_password", lambda a, b: True)
    monkeypatch.setattr(
        "serving.config.settings.settings.signup_require_email_verification",
        False, raising=False,
    )
    resp = await _post_login(app, email="a@b.com")
    assert resp.status_code == 403
    kw = fake_op_store.record_login_event.await_args.kwargs
    assert kw["failure_reason"] == "account_rejected"


@pytest.mark.asyncio
async def test_login_records_inactive(app, fake_op_store, monkeypatch):
    fake_op_store.get_user_by_email.return_value = {
        "id": "u1", "email": "a@b.com", "user_name": "A",
        "password_hash": "$2b$12$NOTUSED", "role": "free",
        "status": "suspended", "email_verified": True,
        "created_at": None, "last_login_at": None,
    }
    monkeypatch.setattr("serving.utils.password.verify_password", lambda a, b: True)
    monkeypatch.setattr(
        "serving.config.settings.settings.signup_require_email_verification",
        False, raising=False,
    )
    resp = await _post_login(app, email="a@b.com")
    assert resp.status_code == 403
    kw = fake_op_store.record_login_event.await_args.kwargs
    assert kw["failure_reason"] == "account_inactive"


@pytest.mark.asyncio
async def test_login_records_rate_limited(app, fake_op_store, monkeypatch):
    async def _deny(email, ip):
        return False, "ip"
    monkeypatch.setattr(
        "serving.servers.routers.auth_routes.check_and_record_login", _deny,
    )
    resp = await _post_login(app, email="x@y.com")
    assert resp.status_code == 429
    fake_op_store.record_login_event.assert_awaited_once()
    kw = fake_op_store.record_login_event.await_args.kwargs
    assert kw["failure_reason"] == "rate_limited"
    assert kw["user_id"] is None


@pytest.mark.asyncio
async def test_login_records_success(app, fake_op_store, monkeypatch):
    fake_op_store.get_user_by_email.return_value = {
        "id": "u1", "email": "a@b.com", "user_name": "A",
        "password_hash": "$2b$12$NOTUSED", "role": "free",
        "status": "active", "email_verified": True,
        "created_at": None, "last_login_at": None,
    }
    monkeypatch.setattr("serving.utils.password.verify_password", lambda a, b: True)
    monkeypatch.setattr(
        "serving.config.settings.settings.signup_require_email_verification",
        False, raising=False,
    )
    resp = await _post_login(app, email="a@b.com")
    assert resp.status_code == 200
    fake_op_store.record_login_event.assert_awaited_once()
    kw = fake_op_store.record_login_event.await_args.kwargs
    assert kw["outcome"] == "success"
    assert kw["failure_reason"] is None
    assert kw["user_id"] == "u1"


@pytest.mark.asyncio
async def test_login_succeeds_when_audit_write_fails(app, fake_op_store, monkeypatch):
    """If record_login_event raises, login still returns the right status."""
    fake_op_store.record_login_event = AsyncMock(side_effect=RuntimeError("db down"))
    fake_op_store.get_user_by_email.return_value = None
    resp = await _post_login(app, email="ghost@example.com")
    assert resp.status_code == 401  # not 500
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/juncheng/hybridInference-worktrees/login-events
uv run pytest tests/servers/test_auth_routes_login_events.py -v 2>&1 | tail -10
```

Expected: many tests FAIL — `record_login_event` not called from `/login` yet.

- [ ] **Step 3: Add the `_record` helper + 8 call sites**

In `apps/backend/serving/servers/routers/auth_routes.py`, locate the `login` function (currently around line 256). Just inside the function body (after the rate-limit `client_ip = get_client_ip(request)` line, around line 277), add the helper closure:

```python
    async def _record(
        outcome: str,
        *,
        failure_reason: str | None,
        user_id: str | None,
    ) -> None:
        try:
            await op_store.record_login_event(
                email=body.email,
                outcome=outcome,
                failure_reason=failure_reason,
                user_id=user_id,
                ip=client_ip,
                user_agent=request.headers.get("user-agent"),
            )
        except Exception:
            logger.exception(
                "login_event_write_failed",
                extra={
                    "event": "login_event_write_failed",
                    "outcome": outcome,
                    "failure_reason": failure_reason,
                },
            )
```

Then insert calls at every outcome branch. Insert each `_record(...)` call **before** the matching `raise HTTPException(...)` (or, for the success branch, just before `update_user_last_login` so a hiccup in the audit insert doesn't precede the user-row update):

a) **Rate-limit 429** — currently lines 278-284. Just before `raise HTTPException(...)`, insert:

```python
        await _record("failure", failure_reason="rate_limited", user_id=None)
```

b) **`user_row is None` 401** — currently line 290. Just before `raise HTTPException(...)`:

```python
        await _record("failure", failure_reason="user_not_found", user_id=None)
```

c) **Wrong password 401** — currently line 294. Just before `raise HTTPException(...)`:

```python
        await _record("failure", failure_reason="invalid_password", user_id=user_row["id"])
```

d) **Email unverified 403** — currently line 306. Just before `raise HTTPException(...)`:

```python
        await _record("failure", failure_reason="email_unverified", user_id=user_row["id"])
```

e) **Pending approval 403** — currently line 313. Just before `raise HTTPException(...)`:

```python
        await _record("failure", failure_reason="account_pending_approval", user_id=user_row["id"])
```

f) **Rejected 403** — currently line 319. Just before `raise HTTPException(...)`:

```python
        await _record("failure", failure_reason="account_rejected", user_id=user_row["id"])
```

g) **Status not active 403** — currently line 325. Just before `raise HTTPException(...)`:

```python
        await _record("failure", failure_reason="account_inactive", user_id=user_row["id"])
```

h) **Success path** — currently around line 331, just before `await op_store.update_user_last_login(user_row["id"])`:

```python
    await _record("success", failure_reason=None, user_id=user_row["id"])
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/servers/test_auth_routes_login_events.py tests/servers/test_auth_routes.py -v 2>&1 | tail -10
```

Expected: all 9 new tests pass; existing `test_auth_routes.py` tests still pass.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/servers/routers/auth_routes.py tests/servers/test_auth_routes_login_events.py
git commit -m "feat(auth): record login_events for every /login outcome path"
```

---

## Task 3: Admin purge endpoint

**Files:**
- Create: `apps/backend/serving/servers/routers/admin/login_events.py`
- Modify: `apps/backend/serving/servers/routers/admin/__init__.py`
- Test: `tests/servers/test_admin_login_events.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/servers/test_admin_login_events.py`:

```python
"""Tests for the DELETE /admin/login-events endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router


@pytest.fixture
def mock_op_store() -> MagicMock:
    op = MagicMock()
    op.purge_login_events_older_than = AsyncMock(return_value=42)
    op.purge_login_events_for_user = AsyncMock(return_value=7)
    op.log_admin_action = AsyncMock()
    return op


@pytest.fixture
async def admin_client(monkeypatch, mock_op_store):
    app = FastAPI()
    services = AppServices(
        router=MagicMock(),
        db_logger=MagicMock(),
        operational_store=mock_op_store,
        log_store=MagicMock(),
    )
    app.state.services = services
    app.include_router(admin_router.router)

    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")

    mock_log_action = AsyncMock()
    monkeypatch.setattr(
        "serving.servers.routers.admin.login_events.log_admin_action", mock_log_action
    )
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    try:
        yield client, mock_op_store, mock_log_action
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_purge_by_age(admin_client):
    client, op, log_action = admin_client
    resp = await client.delete(
        "/admin/login-events?older_than_days=7",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 42}
    op.purge_login_events_older_than.assert_awaited_once_with(7)
    op.purge_login_events_for_user.assert_not_awaited()
    log_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_purge_by_user(admin_client):
    client, op, log_action = admin_client
    resp = await client.delete(
        "/admin/login-events?user_id=u1",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 7}
    op.purge_login_events_for_user.assert_awaited_once_with("u1")
    op.purge_login_events_older_than.assert_not_awaited()
    log_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_purge_neither_param_returns_400(admin_client):
    client, op, _ = admin_client
    resp = await client.delete(
        "/admin/login-events",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert resp.status_code == 400
    op.purge_login_events_older_than.assert_not_awaited()
    op.purge_login_events_for_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_purge_both_params_returns_400(admin_client):
    client, op, _ = admin_client
    resp = await client.delete(
        "/admin/login-events?older_than_days=7&user_id=u1",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert resp.status_code == 400
    op.purge_login_events_older_than.assert_not_awaited()
    op.purge_login_events_for_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_purge_requires_admin_auth(admin_client):
    client, _, _ = admin_client
    resp = await client.delete("/admin/login-events?older_than_days=7")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_purge_older_than_days_must_be_positive(admin_client):
    client, _, _ = admin_client
    resp = await client.delete(
        "/admin/login-events?older_than_days=0",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert resp.status_code == 422  # FastAPI Query(ge=1) validation
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/servers/test_admin_login_events.py -v 2>&1 | tail -10
```

Expected: 404 / route-not-found errors because the endpoint doesn't exist.

- [ ] **Step 3: Create the admin router**

Create `apps/backend/serving/servers/routers/admin/login_events.py`:

```python
"""Admin endpoint to purge ``login_events`` rows.

Two valid query shapes (exactly one must be supplied):

- ``?older_than_days=N`` — purge rows older than ``N`` days (retention).
- ``?user_id=...`` — purge all rows for one user (GDPR-style deletion).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, verify_admin_access
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/admin")


@router.delete("/login-events")
async def purge_login_events_endpoint(
    request: Request,
    older_than_days: int | None = Query(None, ge=1),
    user_id: str | None = Query(None, min_length=1),
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> dict[str, int]:
    """Purge ``login_events`` rows by age or by user."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    if (older_than_days is None) == (user_id is None):
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of `older_than_days` or `user_id`",
        )

    if older_than_days is not None:
        deleted = await op_store.purge_login_events_older_than(older_than_days)
        details = {"older_than_days": older_than_days, "deleted": deleted}
    else:
        deleted = await op_store.purge_login_events_for_user(user_id)
        details = {"user_id": user_id, "deleted": deleted}

    ip = get_client_ip(request)
    await log_admin_action(op_store, ip, "login_events.purge", None, details)
    return {"deleted": deleted}
```

- [ ] **Step 4: Register the new router**

Open `apps/backend/serving/servers/routers/admin/__init__.py`. Add `login_events` to the import block (currently lines 11-22) and add the `include_router` call (after the `users` line, currently line 35):

```python
from serving.servers.routers.admin import (
    analytics,
    api_keys,
    broadcast,
    export,
    login_events,
    metrics,
    providers,
    settings,
    signup_domains,
    stats,
    users,
)
```

```python
router.include_router(login_events.router)
```

(Order alphabetically after `export.router` to match the existing style.)

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/servers/test_admin_login_events.py tests/servers/test_admin_settings.py -v 2>&1 | tail -10
```

Expected: all 6 new tests pass; existing admin-settings tests still pass.

- [ ] **Step 6: Commit**

```bash
git add \
  apps/backend/serving/servers/routers/admin/login_events.py \
  apps/backend/serving/servers/routers/admin/__init__.py \
  tests/servers/test_admin_login_events.py
git commit -m "feat(admin): add DELETE /admin/login-events purge endpoint"
```

---

## Task 4: Sweep `login_events` from `hard_delete_user`

**Files:**
- Modify: `apps/backend/serving/storage/postgres_operational.py`
- Modify: `apps/backend/serving/storage/d1_operational.py`
- Test: `tests/unit/storage/test_login_events.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/storage/test_login_events.py`:

```python
@pytest.mark.asyncio
async def test_hard_delete_user_sweeps_login_events(store: PostgresOperationalStore):
    """hard_delete_user removes the user's login_events rows + reports count."""
    # Seed a target user + an unrelated user, each with login events.
    async with store._pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, user_name, password_hash, role, status) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            "doomed", "d@x.com", "Doomed", "$2b$12$X", "free", "active",
        )
        await conn.execute(
            "INSERT INTO users (id, email, user_name, password_hash, role, status) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            "kept", "k@x.com", "Kept", "$2b$12$X", "free", "active",
        )

    for _ in range(3):
        await store.record_login_event(
            email="d@x.com", outcome="success", failure_reason=None,
            user_id="doomed", ip=None, user_agent=None,
        )
    await store.record_login_event(
        email="k@x.com", outcome="success", failure_reason=None,
        user_id="kept", ip=None, user_agent=None,
    )

    counts = await store.hard_delete_user(
        "doomed",
        admin_ip="127.0.0.1",
        admin_id="admin1",
        reason="test",
        email="d@x.com",
    )
    assert counts.get("login_events") == 3

    async with store._pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM login_events")
    assert {r["user_id"] for r in rows} == {"kept"}
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
PG_TEST_DSN=... uv run pytest tests/unit/storage/test_login_events.py::test_hard_delete_user_sweeps_login_events -v
```

Expected: FAIL — `counts` doesn't include `login_events`.

- [ ] **Step 3: Extend `PostgresOperationalStore.hard_delete_user`**

Open `apps/backend/serving/storage/postgres_operational.py`. Inside `hard_delete_user` (currently around line 530), add a `DELETE FROM login_events` execution inside the transaction, parallel to the existing `auth_sessions` delete (around line 559):

```python
            sessions_status = await conn.execute(
                "DELETE FROM auth_sessions WHERE user_id = $1", user_id
            )
            login_events_status = await conn.execute(
                "DELETE FROM login_events WHERE user_id = $1", user_id
            )
```

Then add the `login_events` count to the `counts` dict (after `auth_sessions`, currently around line 578):

```python
            counts = {
                "api_keys": _row_count(keys_status),
                "auth_sessions": _row_count(sessions_status),
                "login_events": _row_count(login_events_status),
                "email_verification_tokens": _row_count(verif_status),
                "password_reset_tokens": _row_count(reset_status),
                "user_daily_cost": _row_count(cost_status),
                "admin_audit_log": _row_count(audit_status),
                "users": _row_count(user_status),
            }
```

- [ ] **Step 4: Extend `D1OperationalStore.hard_delete_user`**

Open `apps/backend/serving/storage/d1_operational.py`. Inside `hard_delete_user` (currently around line 373), add the parallel `DELETE FROM login_events` and include `login_events` in the returned counts dict, mirroring the Postgres change. (D1 returns row counts via `meta.changes` from each statement — match the existing style for `auth_sessions` deletion in this method.)

- [ ] **Step 5: Run the new test + the full storage suite**

```bash
PG_TEST_DSN=... uv run pytest tests/unit/storage -v 2>&1 | tail -10
```

Expected: all storage tests pass.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/serving/storage/postgres_operational.py apps/backend/serving/storage/d1_operational.py tests/unit/storage/test_login_events.py
git commit -m "feat(storage): sweep login_events in hard_delete_user"
```

---

## Task 5: Final sweep — full tests, ruff, pydocstyle

**Files:** none (verification only)

- [ ] **Step 1: Run all touched test suites**

```bash
cd /home/juncheng/hybridInference-worktrees/login-events
uv run pytest tests/unit/storage/ tests/servers/test_auth_routes.py tests/servers/test_auth_routes_login_events.py tests/servers/test_admin_login_events.py tests/servers/test_admin_settings.py -q 2>&1 | tail -10
```

Expected: all pass (Postgres-fixture tests SKIP if no PG; that's fine).

- [ ] **Step 2: Run ruff format + check**

```bash
uv run ruff format apps/backend tests
uv run ruff check apps/backend tests
```

Expected: format reports `0 files reformatted` (or just the touched files); check reports `All checks passed!`.

- [ ] **Step 3: Run pydocstyle on the new module + the routes/storage files we modified**

```bash
uv run pydocstyle apps/backend/serving/servers/routers/admin/login_events.py
uv run pydocstyle apps/backend/serving/storage/postgres_operational.py 2>&1 | grep -A1 "login_events\|record_login_event\|purge_login" | head -20 || true
```

Expected: no errors. (CI runs pydocstyle on `apps/backend`; we already learned this in PR #423.)

- [ ] **Step 4: Commit any formatting changes**

```bash
git add -A
git diff --cached --quiet || git commit -m "style: ruff format"
```

---

## Self-Review Checklist (writer)

- [x] **Spec coverage:**
      §1 Storage (table + indexes) → Task 1 (Postgres + D1).
      §2 Storage interface → Task 1 (abstract methods + 4 concrete impls).
      §3 Insertion sites in `/login` → Task 2 (helper + 8 sites + audit-failure-swallow test).
      §4 Admin purge endpoint → Task 3 (router + registration + 6 tests).
      §5 User-deletion sweep → Task 4 (Postgres + D1 + integration test).
      Lint sweep → Task 5.
- [x] **No placeholders:** every step has runnable code or a runnable command.
- [x] **Type consistency:** `record_login_event(...)` keyword args spelled identically across base / Postgres / Cache / DualWrite / D1 / call sites / tests; the seven `failure_reason` codes match the spec verbatim.
- [x] **Always-on logging:** no toggle wired anywhere — matches the spec's "always-on" Goal.
- [x] **Audit-failure swallow:** Task 2 Step 1 includes `test_login_succeeds_when_audit_write_fails` — implementation is the `try/except Exception` in the `_record` closure.
- [x] **`hard_delete_user` parity:** Task 4 covers both Postgres and D1.
- [x] **Endpoint validation:** `older_than_days` has `Query(ge=1)`; both/neither rejected with 400; auth via `verify_admin_access`.
