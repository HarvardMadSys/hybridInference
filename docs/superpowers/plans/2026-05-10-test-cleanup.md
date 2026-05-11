# Test Fixture Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract duplicated auth/server test helper logic into a shared helper module, refactor the two auth fixture stacks to use it, and keep existing test behavior unchanged.

**Architecture:** Keep pytest fixtures in `tests/servers/conftest.py` and `tests/servers/conftest_auth.py`, but move repeated low-level logic into a new helper module under `tests/fixtures/`. The cleanup is intentionally asymmetric: share DB safety checks, auth env defaults, user-seeding, and login helpers while preserving the separate lifecycle semantics of the full app-lifespan fixtures and the narrower DB-backed auth fixtures.

**Tech Stack:** pytest, pytest-asyncio, FastAPI, httpx, asyncpg, Python 3.10+

---

## File Map

- `tests/fixtures/auth_helpers.py`
  New shared helper module for auth test environment defaults, DB safety guards, DB cleanup, user seeding, and login/header helpers.
- `tests/unit/test_auth_test_helpers.py`
  New focused unit tests for the pure and semi-pure helper functions in `tests/fixtures/auth_helpers.py`.
- `tests/servers/conftest_auth.py`
  Refactor to import and use shared helpers instead of maintaining its own near-duplicate env, DB, user-seeding, and login logic.
- `tests/servers/conftest.py`
  Refactor only the auth-specific fixture section to use shared helper functions while keeping the existing session-level fixture structure and full-lifespan behavior.
- `tests/servers/test_internal.py`
  Remove the local duplicate auth test-user fixture in favor of the shared `test_user` fixture from `tests.servers.conftest_auth`.
- `tests/servers/test_auth_routes.py`
  Primary DB-backed regression target for `conftest_auth` consumers.
- `tests/servers/test_user_routes.py`
  Secondary DB-backed regression target for `conftest_auth` consumers.
- `tests/servers/test_admin_mode.py`
  DB-backed regression target that still uses its own user helpers but depends on `auth_backend` and `clean_auth_tables`.
- `tests/servers/test_email_verification_required.py`
  Regression target for the full app-lifespan auth fixtures in `tests/servers/conftest.py`.
- `tests/servers/test_concurrency_endpoint.py`
  Regression target for `auth_app` / `auth_client` behavior after the `tests/servers/conftest.py` refactor.

## Task 1: Add Shared Auth Test Helpers With Focused Unit Coverage

**Files:**
- Create: `tests/fixtures/auth_helpers.py`
- Create: `tests/unit/test_auth_test_helpers.py`

- [ ] **Step 1: Write the failing helper tests**

Create `tests/unit/test_auth_test_helpers.py` with focused coverage for the helper API before implementing it:

```python
from __future__ import annotations

from unittest.mock import call
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.fixtures import auth_helpers


def test_build_auth_test_env_defaults_uses_test_db_inputs(monkeypatch):
    monkeypatch.setenv("TEST_DB_HOST", "db-host")
    monkeypatch.setenv("TEST_DB_PORT", "5544")
    monkeypatch.setenv("TEST_DB_NAME", "freeinference_test_db")
    monkeypatch.setenv("TEST_DB_USER", "db-user")
    monkeypatch.setenv("TEST_DB_PASSWORD", "db-pass")

    result = auth_helpers.build_auth_test_env_defaults()

    assert result["DB_ENABLED"] == "true"
    assert result["DB_HOST"] == "db-host"
    assert result["DB_PORT"] == "5544"
    assert result["DB_NAME"] == "freeinference_test_db"
    assert result["DB_USER"] == "db-user"
    assert result["DB_PASSWORD"] == "db-pass"
    assert result["API_KEY_SECRET"]
    assert result["MODELS_CONFIG"] == "test/fixtures/test_models.yaml"


def test_build_auth_test_env_defaults_applies_overrides():
    result = auth_helpers.build_auth_test_env_defaults(
        {
            "DB_ENABLED": "false",
            "BASE_URL": "http://test",
            "SIGNUP_DEFAULT_DAILY_QUOTA_USD": "42.00",
        }
    )

    assert result["DB_ENABLED"] == "false"
    assert result["BASE_URL"] == "http://test"
    assert result["SIGNUP_DEFAULT_DAILY_QUOTA_USD"] == "42.00"


def test_assert_test_db_name_accepts_dedicated_test_db():
    auth_helpers.assert_test_db_name("freeinference_test_db")


def test_assert_test_db_name_rejects_non_test_db():
    with pytest.raises(pytest.fail.Exception, match="refusing to run tests"):
        auth_helpers.assert_test_db_name("freeinference_prod")


@pytest.mark.asyncio
async def test_assert_test_db_from_pool_queries_current_database():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value="freeinference_test_db")
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire_cm

    await auth_helpers.assert_test_db_from_pool(pool, context="unit-test")

    conn.fetchval.assert_awaited_once_with("SELECT current_database()")


@pytest.mark.asyncio
async def test_cleanup_auth_tables_deletes_expected_tables_in_order():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value="freeinference_test_db")
    conn.execute = AsyncMock()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire_cm

    await auth_helpers.cleanup_auth_tables(pool)

    assert conn.execute.await_args_list == [
        call("DELETE FROM email_verification_tokens"),
        call("DELETE FROM password_reset_tokens"),
        call("DELETE FROM auth_sessions"),
        call("DELETE FROM api_keys WHERE account_id IS NOT NULL"),
        call("DELETE FROM signup_allowed_domains"),
        call("DELETE FROM users"),
    ]
```

- [ ] **Step 2: Run the new helper tests to verify they fail**

Run: `uv run --active pytest tests/unit/test_auth_test_helpers.py -v`

Expected: FAIL with `ModuleNotFoundError` for `tests.fixtures.auth_helpers` and/or missing helper attributes.

- [ ] **Step 3: Write the minimal shared helper module**

Create `tests/fixtures/auth_helpers.py` with the following implementation:

```python
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import pytest
from httpx import AsyncClient

from tests.fixtures.auth_factories import create_test_user

ALLOWED_TEST_DB_PATTERN = "_test_"


def build_auth_test_env_defaults(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        "DB_ENABLED": "true",
        "DB_HOST": os.getenv("TEST_DB_HOST", "localhost"),
        "DB_PORT": os.getenv("TEST_DB_PORT", "5432"),
        "DB_NAME": os.getenv("TEST_DB_NAME", "freeinference_test_db"),
        "DB_USER": os.getenv("TEST_DB_USER", "postgres"),
        "DB_PASSWORD": os.getenv("TEST_DB_PASSWORD", "postgres"),
        "JWT_SECRET_KEY": "test-secret-key-for-testing-only-do-not-use-in-production",
        "JWT_ALGORITHM": "HS256",
        "JWT_ACCESS_TOKEN_EXPIRE_MINUTES": "15",
        "JWT_REFRESH_TOKEN_EXPIRE_DAYS": "30",
        "API_KEY_SECRET": "test-api-key-secret-for-testing-only",
        "COOKIE_SECURE": "false",
        "COOKIE_SAMESITE": "lax",
        "SIGNUP_ENABLED": "1",
        "SIGNUP_DEFAULT_DAILY_QUOTA_USD": "100.00",
        "SIGNUP_REQUIRE_EMAIL_VERIFICATION": "0",
        "SMTP_HOST": "",
        "SMTP_USER": "",
        "SMTP_PASSWORD": "",
        "BASE_URL": "http://localhost:8000",
        "MODELS_CONFIG": "test/fixtures/test_models.yaml",
        "ROUTING_CONFIG": "test/fixtures/test_routing.yaml",
    }
    if overrides:
        env.update(dict(overrides))
    return env


def assert_test_db_name(db_name: str, context: str = "") -> None:
    if ALLOWED_TEST_DB_PATTERN not in (db_name or ""):
        pytest.fail(
            f"SAFETY: refusing to run tests against database '{db_name}' "
            f"(name does not contain '{ALLOWED_TEST_DB_PATTERN}')"
            f"{f' [{context}]' if context else ''}. "
            "Set TEST_DB_NAME / DB_NAME to a dedicated test database."
        )


async def assert_test_db_from_pool(pool, context: str = "") -> None:
    async with pool.acquire() as conn:
        db_name = await conn.fetchval("SELECT current_database()")
    assert_test_db_name(db_name, context)


async def cleanup_auth_tables(pool) -> None:
    async with pool.acquire() as conn:
        db_name = await conn.fetchval("SELECT current_database()")
        assert_test_db_name(db_name)
        await conn.execute("DELETE FROM email_verification_tokens")
        await conn.execute("DELETE FROM password_reset_tokens")
        await conn.execute("DELETE FROM auth_sessions")
        await conn.execute("DELETE FROM api_keys WHERE account_id IS NOT NULL")
        await conn.execute("DELETE FROM signup_allowed_domains")
        await conn.execute("DELETE FROM users")


async def seed_test_user(operational_store, **overrides: Any) -> dict[str, Any]:
    user_data = create_test_user(**overrides)
    await operational_store.create_user(
        user_id=user_data["id"],
        email=user_data["email"],
        password_hash=user_data["password_hash"],
        user_name=user_data["user_name"],
        email_verified=user_data["email_verified"],
        status=user_data["status"],
    )
    return user_data


async def login_and_get_auth_headers(
    client: AsyncClient,
    *,
    email: str,
    password: str,
) -> dict[str, str]:
    response = await client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200
    access_token = response.json()["access_token"]
    return {"Authorization": f"Bearer {access_token}"}
```

Do not add fixture decorators to this new module.

- [ ] **Step 4: Run the helper tests to verify they pass**

Run: `uv run --active pytest tests/unit/test_auth_test_helpers.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/auth_helpers.py tests/unit/test_auth_test_helpers.py
git commit -m "test: add shared auth test helpers"
```

## Task 2: Refactor `tests/servers/conftest_auth.py` To Use Shared Helpers

**Files:**
- Modify: `tests/servers/conftest_auth.py`
- Test: `tests/servers/test_auth_routes.py`
- Test: `tests/servers/test_user_routes.py`

- [ ] **Step 1: Write a regression test that exercises the DB-backed auth helper path through existing fixtures**

Add this focused test near the top of `tests/servers/test_auth_routes.py` after the existing `auth_test_user` fixture block so the fixture stack is exercised by login via `auth_headers`:

```python
@pytest.mark.asyncio
async def test_auth_headers_fixture_logs_in_seeded_user(auth_app_client: AsyncClient, test_user, auth_headers):
    response = await auth_app_client.get("/user/me", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == test_user["id"]
    assert data["email"] == test_user["email"].lower()
```

This test should keep passing before and after the refactor. It is the safety rail for the `test_user` plus `auth_headers` helper extraction.

- [ ] **Step 2: Run the focused DB-backed auth tests before refactoring**

Run: `uv run --active pytest tests/servers/test_auth_routes.py -k "auth_headers_fixture_logs_in_seeded_user or signup_success" -v -m dbtest`

Expected: PASS before the refactor, confirming the fixture behavior you are about to preserve.

- [ ] **Step 3: Refactor `tests/servers/conftest_auth.py` to import and use the shared helpers**

Apply these edits in `tests/servers/conftest_auth.py`:

1. Replace the local import of `create_test_user` with shared helper imports:

```python
from tests.fixtures.auth_helpers import (
    assert_test_db_from_pool,
    assert_test_db_name,
    build_auth_test_env_defaults,
    cleanup_auth_tables,
    login_and_get_auth_headers,
    seed_test_user,
)
```

2. Replace the `auth_env` fixture body with shared env defaults:

```python
@pytest.fixture
def auth_env(monkeypatch):
    """Set up environment variables for auth testing."""
    from serving.config.settings import get_settings

    get_settings.cache_clear()
    test_env = build_auth_test_env_defaults()
    for key, value in test_env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return test_env
```

3. Delete `_ALLOWED_TEST_DB_PATTERN`, `_guard_test_db_only`, and `_cleanup_pg_tables` entirely.

4. In `_init_pg_backend()`, replace the manual DB-name guard and pool check:

```python
    test_db_name = os.getenv("TEST_DB_NAME", "freeinference_test_db")
    assert_test_db_name(test_db_name, context="auth_backend init")
```

and later:

```python
    if logger.pool:
        await assert_test_db_from_pool(logger.pool, context="auth_backend pool")
```

5. Replace the `clean_auth_tables` fixture body with the shared cleanup helper:

```python
@pytest_asyncio.fixture
async def clean_auth_tables(auth_backend):
    """Clean auth-related tables before and after each test."""
    _operational_store, _log_store, db_logger, _backend = auth_backend

    await cleanup_auth_tables(db_logger.pool)
    yield
    await cleanup_auth_tables(db_logger.pool)
```

6. Replace the `test_user` fixture body with the shared seeding helper:

```python
@pytest_asyncio.fixture
async def test_user(auth_backend, clean_auth_tables):
    """Create a test user in the database."""
    operational_store, _, _, _ = auth_backend
    return await seed_test_user(operational_store)
```

7. Replace the `auth_headers` fixture body with the shared login helper:

```python
@pytest_asyncio.fixture
async def auth_headers(test_user, auth_app_client):
    """Get authentication headers for a test user."""
    return await login_and_get_auth_headers(
        auth_app_client,
        email=test_user["email"],
        password=test_user["password"],
    )
```

Leave `test_user_with_key`, `auth_app_services`, `auth_test_app`, and `auth_app_client` as fixtures in this file. They still own pytest scope and app wiring.

- [ ] **Step 4: Run the DB-backed auth regression tests to verify they still pass**

Run:

- `uv run --active pytest tests/servers/test_auth_routes.py -v -m dbtest`
- `uv run --active pytest tests/servers/test_user_routes.py -v -m dbtest`

Expected: PASS, including the new `test_auth_headers_fixture_logs_in_seeded_user` case.

- [ ] **Step 5: Commit**

```bash
git add tests/servers/conftest_auth.py tests/servers/test_auth_routes.py tests/servers/test_user_routes.py
git commit -m "test: share db-backed auth fixture helpers"
```

## Task 3: Refactor `tests/servers/conftest.py` Auth Fixtures To Reuse The Same Helpers

**Files:**
- Modify: `tests/servers/conftest.py`
- Test: `tests/servers/test_email_verification_required.py`
- Test: `tests/servers/test_concurrency_endpoint.py`

- [ ] **Step 1: Add a focused regression test for the full-lifespan auth signup/login path**

In `tests/servers/test_email_verification_required.py`, add this test near the existing auth flow coverage:

```python
@pytest.mark.asyncio
async def test_auth_client_fixture_can_signup_and_login(auth_client):
    signup_data = {
        "email": f"fixture-login-{os.urandom(4).hex()}@signuptest.dev",
        "password": "SecurePass123!",
        "user_name": "Fixture Login",
        "accepted_tos": True,
    }

    signup_response = await auth_client.post("/auth/signup", json=signup_data)
    assert signup_response.status_code == 201

    login_response = await auth_client.post(
        "/auth/login",
        json={"email": signup_data["email"], "password": signup_data["password"]},
    )
    assert login_response.status_code == 200
    assert "access_token" in login_response.json()
```

This test is deliberately end-to-end through the real `auth_client` fixture and should remain green after the helper extraction.

- [ ] **Step 2: Run the focused full-lifespan auth tests before refactoring**

Run: `uv run --active pytest tests/servers/test_email_verification_required.py -k "auth_client_fixture_can_signup_and_login or verified_user_can_login" -v`

Expected: PASS before refactoring.

- [ ] **Step 3: Refactor the auth-specific section of `tests/servers/conftest.py` to use shared helpers**

In `tests/servers/conftest.py`:

1. Add these imports near the top-level imports:

```python
from tests.fixtures.auth_helpers import (
    assert_test_db_from_pool,
    assert_test_db_name,
    build_auth_test_env_defaults,
    login_and_get_auth_headers,
)
```

2. Delete the local `_ALLOWED_TEST_DB_PATTERN`, `_assert_test_db_name`, and `_assert_test_db_from_pool` definitions.

3. In `auth_test_env`, replace the hand-built `_AUTH_VARS`/`_SERVICE_VARS` block with a shared-defaults call plus explicit local overrides for the full-lifespan fixture semantics:

```python
    shared_defaults = build_auth_test_env_defaults(
        {
            "DB_HOST": _db_host,
            "DB_PORT": _db_port,
            "DB_NAME": _db_name,
            "DB_USER": _db_user,
            "DB_PASSWORD": _db_pass,
            "TEST_DB_HOST": _db_host,
            "TEST_DB_PORT": _db_port,
            "TEST_DB_NAME": _db_name,
            "TEST_DB_USER": _db_user,
            "TEST_DB_PASSWORD": _db_pass,
            "COOKIE_SECURE": "0",
            "API_KEY_SECRET": "test-api-key-secret",
            "JWT_SECRET_KEY": "test-secret-key-32-chars-long!!",
            "BASE_URL": "http://test",
            "METRICS_ENABLED": "0",
        }
    )
    all_vars = dict(shared_defaults)
```

Keep the xdist worker DB provisioning logic exactly where it is today.

4. In `auth_app`, replace the local pre-flight and post-startup DB checks:

```python
    assert_test_db_name(os.environ.get("DB_NAME", ""), context="auth_app pre-flight DB_NAME")
```

and later:

```python
            await assert_test_db_from_pool(db_logger.pool, context="auth_app fixture")
```

5. In `auth_client_authenticated_user_fixture`, replace the manual login request with the shared login helper and preserve the returned access token shape:

```python
@pytest_asyncio.fixture(name="auth_client_authenticated_user")
async def auth_client_authenticated_user_fixture(auth_client, auth_client_test_user):
    headers = await login_and_get_auth_headers(
        auth_client,
        email=auth_client_test_user["email"],
        password=auth_client_test_user["password"],
    )
    return {
        **auth_client_test_user,
        "access_token": headers["Authorization"].removeprefix("Bearer "),
    }
```

Do not move `auth_app`, `auth_client`, or `require_db` out of `tests/servers/conftest.py`.

- [ ] **Step 4: Run the full-lifespan auth regression tests to verify they still pass**

Run:

- `uv run --active pytest tests/servers/test_email_verification_required.py -v`
- `uv run --active pytest tests/servers/test_concurrency_endpoint.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/servers/conftest.py tests/servers/test_email_verification_required.py tests/servers/test_concurrency_endpoint.py
git commit -m "test: share full-lifespan auth fixture helpers"
```

## Task 4: Remove A Local Duplicate Consumer Fixture And Run Final Regression Coverage

**Files:**
- Modify: `tests/servers/test_internal.py`
- Test: `tests/servers/test_admin_mode.py`
- Test: `tests/servers/test_internal.py`
- Test: `tests/unit/test_auth_test_helpers.py`

- [ ] **Step 1: Make `test_internal.py` consume the shared `test_user` fixture instead of its local duplicate**

In `tests/servers/test_internal.py`:

1. Delete this local fixture entirely:

```python
@pytest_asyncio.fixture
async def auth_test_user(auth_backend, clean_auth_tables):
    """Create a user backed by the auth-specific DB fixtures."""
    operational_store, _, _, _ = auth_backend
    user_data = create_test_user()

    await operational_store.create_user(
        user_id=user_data["id"],
        email=user_data["email"],
        password_hash=user_data["password_hash"],
        user_name=user_data["user_name"],
        email_verified=user_data["email_verified"],
        status=user_data["status"],
    )

    yield user_data
```

2. Remove the now-unused import:

```python
from tests.fixtures.auth_factories import create_test_user
```

3. Rename the test parameters from `auth_test_user` to `test_user` and update call sites:

```python
    async def test_verify_admin_allows_admin_session(
        self,
        internal_client: AsyncClient,
        auth_backend,
        test_user,
    ) -> None:
        operational_store, _, _, _ = auth_backend
        refresh_token = "test-refresh-admin"
        await _set_user_role(operational_store, test_user["id"], "admin")
        await _create_refresh_session(operational_store, test_user["id"], refresh_token)
```

Apply the same `auth_test_user` -> `test_user` change to the non-admin case.

- [ ] **Step 2: Run the focused internal-route regression test**

Run: `uv run --active pytest tests/servers/test_internal.py -v -m dbtest`

Expected: PASS.

- [ ] **Step 3: Run the final combined validation set**

Run:

- `uv run --active pytest tests/unit/test_auth_test_helpers.py -v`
- `uv run --active pytest tests/servers/test_auth_routes.py tests/servers/test_user_routes.py tests/servers/test_internal.py tests/servers/test_admin_mode.py -v -m dbtest`
- `uv run --active pytest tests/servers/test_email_verification_required.py tests/servers/test_concurrency_endpoint.py -v`

Expected: PASS across all targeted helper consumers.

- [ ] **Step 4: Run formatting and lint checks for touched files**

Run:

- `make format`
- `make lint`

Expected: PASS, or only pre-existing unrelated failures outside the touched test-support files.

- [ ] **Step 5: Commit**

```bash
git add tests/servers/test_internal.py
git commit -m "test: remove duplicate auth test fixtures"
```

## Self-Review Checklist

- Spec coverage:
  - shared helper module: covered by Task 1
  - `tests/servers/conftest_auth.py` helper extraction: covered by Task 2
  - `tests/servers/conftest.py` helper extraction: covered by Task 3
  - one small opportunistic consumer cleanup: covered by Task 4
  - targeted validation of both fixture families: covered by Tasks 2-4
- Placeholder scan:
  - no `TODO`, `TBD`, or “similar to above” steps remain
  - each code-changing step includes exact code snippets or explicit replacement targets
  - each verification step includes exact commands
- Type and naming consistency:
  - helper names used throughout the plan are `build_auth_test_env_defaults`, `assert_test_db_name`, `assert_test_db_from_pool`, `cleanup_auth_tables`, `seed_test_user`, and `login_and_get_auth_headers`
  - fixture names preserved by the refactor are `auth_env`, `auth_backend`, `clean_auth_tables`, `test_user`, `auth_headers`, `auth_app`, and `auth_client`
