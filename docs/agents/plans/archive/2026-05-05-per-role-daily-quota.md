# Per-Role Daily Quota Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-role default daily USD quota as runtime settings, plus an admin "apply to existing users" bulk operation that overwrites `quota_daily_cost_usd` on every active API key of users with the chosen role.

**Architecture:** Add 4 keys to `RUNTIME_SETTINGS_REGISTRY` (`user_daily_quota_{free,pro,internal,admin}`). Replace the single env-var `get_default_daily_quota()` with an async role-aware helper. Add new admin endpoints (`GET /admin/quota/role-apply-preview`, `POST /admin/quota/role-apply`) backed by two new `OperationalStore` methods. Augment `SettingsTab` to render an "Apply to existing users" button beside Save for any setting whose key starts with `user_daily_quota_`. Quota enforcement at `auth.py` is unchanged — it reads `api_keys.quota_daily_cost_usd`, which the apply path writes.

**Tech Stack:** Python 3.12, FastAPI, asyncpg/Postgres, Cloudflare D1, Pydantic v2, Next.js 14, React 18, TypeScript, pytest, vitest.

**Spec:** [docs/agents/specs/2026-05-05-per-role-daily-quota-design.md](../specs/2026-05-05-per-role-daily-quota-design.md)

---

## Conventions for this plan

- Branch off `dev`. Use a worktree.
- Run `make format && make test` after each task before commit. Tests in `tests/integration/` need explicit `pytest -m dbtest tests/integration/...` and a running Postgres.
- Frontend: `npm --prefix apps/frontend test` for vitest, `npm --prefix apps/frontend run lint` for lint, `npm --prefix apps/frontend run type-check` for tsc.
- Each task ends in a single git commit on the feature branch. PR target: `dev`.

---

## Task 1: Register per-role quota runtime settings

**Files:**
- Modify: `apps/backend/serving/config/runtime_settings.py` (extend `RUNTIME_SETTINGS_REGISTRY`)
- Test: `tests/unit/config/test_runtime_settings_registry.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/config/test_runtime_settings_registry.py`:

```python
"""Registry-shape tests for runtime settings."""

from serving.config.runtime_settings import RUNTIME_SETTINGS_REGISTRY


def test_per_role_daily_quota_keys_registered():
    keys = {
        "user_daily_quota_free",
        "user_daily_quota_pro",
        "user_daily_quota_internal",
        "user_daily_quota_admin",
    }
    assert keys.issubset(RUNTIME_SETTINGS_REGISTRY.keys())


def test_per_role_daily_quota_entries_are_well_formed():
    for role, expected_default in (
        ("free", 100.00),
        ("pro", 100.00),
        ("internal", 1000.00),
        ("admin", 1000.00),
    ):
        entry = RUNTIME_SETTINGS_REGISTRY[f"user_daily_quota_{role}"]
        assert entry["type"] == "float"
        assert entry["default"] == expected_default
        assert entry["min"] == 0.0
        assert "description" in entry and entry["description"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/config/test_runtime_settings_registry.py -v
```

Expected: 2 failures (`KeyError` / assert failures — keys absent).

- [ ] **Step 3: Add the four registry entries**

Edit `apps/backend/serving/config/runtime_settings.py`. Append inside `RUNTIME_SETTINGS_REGISTRY` after the existing `user_concurrency_admin` entry (around line 77):

```python
    "user_daily_quota_free": {
        "type": "float",
        "default": 100.00,
        "min": 0.0,
        "description": (
            "Default daily USD spend quota seeded onto a free-tier user's "
            "active API key at signup."
        ),
    },
    "user_daily_quota_pro": {
        "type": "float",
        "default": 100.00,
        "min": 0.0,
        "description": (
            "Default daily USD spend quota seeded onto a pro-tier user's "
            "active API key at signup."
        ),
    },
    "user_daily_quota_internal": {
        "type": "float",
        "default": 1000.00,
        "min": 0.0,
        "description": (
            "Default daily USD spend quota seeded onto an internal user's "
            "active API key at signup."
        ),
    },
    "user_daily_quota_admin": {
        "type": "float",
        "default": 1000.00,
        "min": 0.0,
        "description": (
            "Default daily USD spend quota seeded onto an admin user's "
            "active API key at signup."
        ),
    },
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/unit/config/test_runtime_settings_registry.py -v
```

Expected: 2 passes.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/config/runtime_settings.py tests/unit/config/test_runtime_settings_registry.py
git commit -m "feat(config): register per-role daily quota runtime settings"
```

---

## Task 2: Role-aware default quota helper

**Files:**
- Modify: `apps/backend/serving/servers/routers/user_routes.py` (replace `get_default_daily_quota`)
- Test: `tests/unit/servers/routers/test_user_routes_quota.py` (new)

The current sync helper at `user_routes.py:179` reads only the env var. Replace it with an async helper that consults the runtime settings registry first.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/servers/routers/test_user_routes_quota.py`:

```python
"""Tests for role-aware default-quota helper used at signup / regenerate."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from serving.servers.routers.user_routes import get_default_daily_quota_for_role


@pytest.mark.asyncio
async def test_picks_role_specific_runtime_setting():
    rt = AsyncMock()
    rt.get_float.return_value = 250.0
    quota = await get_default_daily_quota_for_role("pro", rt)
    rt.get_float.assert_awaited_once_with("user_daily_quota_pro")
    assert quota == Decimal("250.0")


@pytest.mark.asyncio
async def test_unknown_role_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("SIGNUP_DEFAULT_DAILY_QUOTA_USD", "77.00")
    rt = AsyncMock()
    quota = await get_default_daily_quota_for_role("ghost", rt)
    rt.get_float.assert_not_awaited()
    assert quota == Decimal("77.00")


@pytest.mark.asyncio
async def test_runtime_settings_none_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("SIGNUP_DEFAULT_DAILY_QUOTA_USD", "55.50")
    quota = await get_default_daily_quota_for_role("free", None)
    assert quota == Decimal("55.50")


@pytest.mark.asyncio
async def test_default_when_no_env_no_rt():
    monkeypatch_pytest = pytest.MonkeyPatch()
    monkeypatch_pytest.delenv("SIGNUP_DEFAULT_DAILY_QUOTA_USD", raising=False)
    try:
        quota = await get_default_daily_quota_for_role("free", None)
        assert quota == Decimal("100.00")
    finally:
        monkeypatch_pytest.undo()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/servers/routers/test_user_routes_quota.py -v
```

Expected: `ImportError` for `get_default_daily_quota_for_role`.

- [ ] **Step 3: Add the helper**

Edit `apps/backend/serving/servers/routers/user_routes.py`. Replace the existing `get_default_daily_quota` function (lines 179-182) with:

```python
async def get_default_daily_quota_for_role(
    role: str,
    runtime_settings: "RuntimeSettings | None",
) -> Decimal:
    """Return the default daily USD quota seeded onto a new API key.

    Reads the ``user_daily_quota_<role>`` runtime setting if present.
    Falls back to ``SIGNUP_DEFAULT_DAILY_QUOTA_USD`` env var (default 100.00)
    if the role is unknown or runtime settings are unavailable (e.g. early
    bootstrap).
    """
    from serving.config.runtime_settings import RUNTIME_SETTINGS_REGISTRY

    key = f"user_daily_quota_{role}"
    if runtime_settings is not None and key in RUNTIME_SETTINGS_REGISTRY:
        val = await runtime_settings.get_float(key)
        return Decimal(str(val))
    quota_str = os.getenv("SIGNUP_DEFAULT_DAILY_QUOTA_USD", "100.00")
    return Decimal(quota_str)
```

Add the typing import at the top of the file (forward reference avoids circular imports):

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from serving.config.runtime_settings import RuntimeSettings
```

(Skip the `TYPE_CHECKING` block if it already exists in the file.)

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/unit/servers/routers/test_user_routes_quota.py -v
```

Expected: 4 passes.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/servers/routers/user_routes.py tests/unit/servers/routers/test_user_routes_quota.py
git commit -m "feat(signup): role-aware default-quota helper"
```

---

## Task 3: Wire helper into signup + regenerate

**Files:**
- Modify: `apps/backend/serving/servers/routers/user_routes.py` (call sites at lines ~327 and ~532, plus delete the old sync helper)
- Test: `tests/api/test_signup_role_quota.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_signup_role_quota.py`:

```python
"""End-to-end check that signup uses the role-specific runtime quota."""

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_create_initial_key_uses_role_quota(monkeypatch):
    from serving.servers.routers import user_routes

    rt = AsyncMock()
    rt.get_float.return_value = 250.0

    op_store = AsyncMock()
    op_store.get_active_key_by_account.return_value = None
    op_store.create_key = AsyncMock()

    db_logger = AsyncMock()

    current_user = {
        "user_id": "u-1",
        "email": "u1@example.com",
        "role": "pro",
        "email_verified": True,
    }

    with patch.object(user_routes, "get_runtime_settings_instance", return_value=rt):
        await user_routes.create_api_key(
            request=AsyncMock(headers={}, client=AsyncMock(host="127.0.0.1")),
            current_user=current_user,
            op_store=op_store,
            db_logger=db_logger,
        )

    rt.get_float.assert_awaited_with("user_daily_quota_pro")
    op_store.create_key.assert_awaited_once()
    kwargs = op_store.create_key.call_args.kwargs
    assert kwargs["quota_daily_cost_usd"] == Decimal("250.0")
```

(If the existing signature for `create_api_key` doesn't match, adapt the call shape — the assertion that matters is the awaited `get_float` key and the `quota_daily_cost_usd=` kwarg passed to `create_key`.)

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/api/test_signup_role_quota.py -v
```

Expected: fails because `create_api_key` still calls the removed sync `get_default_daily_quota()`.

- [ ] **Step 3: Update both call sites**

Edit `apps/backend/serving/servers/routers/user_routes.py`:

At the import block, add (or merge into existing import line):

```python
from serving.config.runtime_settings import get_runtime_settings_instance
```

At line ~327 inside `create_api_key`, replace:

```python
default_quota = get_default_daily_quota()
```

with:

```python
try:
    rt = get_runtime_settings_instance()
except RuntimeError:
    rt = None
default_quota = await get_default_daily_quota_for_role(
    current_user["role"], rt
)
```

At line ~532 inside `regenerate_api_key`, apply the identical replacement.

Then **delete** the old sync helper:

```python
def get_default_daily_quota() -> Decimal:
    """Get default daily quota for new users from environment."""
    quota_str = os.getenv("SIGNUP_DEFAULT_DAILY_QUOTA_USD", "100.00")
    return Decimal(quota_str)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/api/test_signup_role_quota.py tests/unit/servers/routers/test_user_routes_quota.py -v
uv run pytest -q -m "not external and not dbtest"
```

Expected: green. Other suites untouched.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/servers/routers/user_routes.py tests/api/test_signup_role_quota.py
git commit -m "feat(signup): seed new API keys with role-specific quota"
```

---

## Task 4: Storage ABC: add count + apply methods

**Files:**
- Modify: `apps/backend/serving/storage/base.py` (add ABC methods)
- Test: `tests/unit/storage/test_operational_store_abc.py` (new — verifies signatures)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/storage/test_operational_store_abc.py`:

```python
"""Verify the OperationalStore ABC declares role-quota methods."""

import inspect

from serving.storage.base import OperationalStore


def test_count_active_keys_for_role_signature():
    sig = inspect.signature(OperationalStore.count_active_keys_for_role)
    assert list(sig.parameters) == ["self", "role"]


def test_apply_role_quota_signature():
    sig = inspect.signature(OperationalStore.apply_role_quota)
    assert list(sig.parameters) == ["self", "role", "quota"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/storage/test_operational_store_abc.py -v
```

Expected: `AttributeError: count_active_keys_for_role`.

- [ ] **Step 3: Add the abstract methods**

Edit `apps/backend/serving/storage/base.py`. Add inside the `OperationalStore` class (group near other api_keys/users methods):

```python
@abstractmethod
async def count_active_keys_for_role(self, role: str) -> tuple[int, int]:
    """Return ``(key_count, user_count)`` of active api_keys whose owner has this role.

    Used by the admin "apply role quota" preview.
    """

@abstractmethod
async def apply_role_quota(self, role: str, quota: Decimal) -> int:
    """Set ``quota_daily_cost_usd`` to ``quota`` on every active api_key
    whose owner has this role. Returns the number of rows updated.

    Atomic: a failure rolls back. Overwrites any per-key custom override.
    """
```

(`Decimal` is already imported at the top of the file. If not, add `from decimal import Decimal`.)

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/unit/storage/test_operational_store_abc.py -v
```

Expected: 2 passes. **All other store tests will now fail** because subclasses do not implement the new methods — Tasks 5–8 fix them.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/storage/base.py tests/unit/storage/test_operational_store_abc.py
git commit -m "feat(storage): declare role-quota methods on OperationalStore ABC"
```

---

## Task 5: Postgres implementation

**Files:**
- Modify: `apps/backend/serving/storage/postgres_operational.py`
- Test: `tests/integration/storage/test_postgres_role_quota.py` (new, `dbtest` marker)

- [ ] **Step 1: Write the failing integration test**

Create `tests/integration/storage/test_postgres_role_quota.py`:

```python
"""Integration tests for Postgres role-quota helpers."""

from decimal import Decimal

import pytest

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _seed_user_with_key(
    store, *, user_id, email, role, quota=Decimal("100.00"), status="active",
):
    await store.create_user(
        user_id=user_id, email=email, password_hash="x", role=role,
    )
    await store.create_key(
        key_hash=f"hash-{user_id}",
        key_prefix=f"sk-{user_id[:6]}",
        user_id=user_id,
        account_id=user_id,
        quota_daily_cost_usd=quota,
        status=status,
    )


async def test_count_active_keys_for_role(postgres_op_store):
    await _seed_user_with_key(postgres_op_store, user_id="u-pro-1", email="p1@x.com", role="pro")
    await _seed_user_with_key(postgres_op_store, user_id="u-pro-2", email="p2@x.com", role="pro")
    await _seed_user_with_key(postgres_op_store, user_id="u-free-1", email="f1@x.com", role="free")
    await _seed_user_with_key(
        postgres_op_store, user_id="u-pro-rev", email="pr@x.com", role="pro", status="revoked",
    )

    keys, users = await postgres_op_store.count_active_keys_for_role("pro")
    assert keys == 2
    assert users == 2


async def test_apply_role_quota_updates_only_matching_role(postgres_op_store):
    await _seed_user_with_key(postgres_op_store, user_id="u-pro-1", email="p1@x.com", role="pro", quota=Decimal("100.00"))
    await _seed_user_with_key(postgres_op_store, user_id="u-free-1", email="f1@x.com", role="free", quota=Decimal("100.00"))

    updated = await postgres_op_store.apply_role_quota("pro", Decimal("250.00"))
    assert updated == 1

    pro_key = await postgres_op_store.get_active_key_by_account("u-pro-1")
    free_key = await postgres_op_store.get_active_key_by_account("u-free-1")
    assert pro_key["quota_daily_cost_usd"] == Decimal("250.00")
    assert free_key["quota_daily_cost_usd"] == Decimal("100.00")


async def test_apply_role_quota_skips_revoked_keys(postgres_op_store):
    await _seed_user_with_key(
        postgres_op_store, user_id="u-pro-rev", email="pr@x.com", role="pro",
        quota=Decimal("100.00"), status="revoked",
    )
    updated = await postgres_op_store.apply_role_quota("pro", Decimal("250.00"))
    assert updated == 0
```

(If the conftest does not already provide `postgres_op_store`, copy the fixture pattern from another file in `tests/integration/storage/`. If absent, declare it inline in the new test file — bootstrap an `OperationalStore` against the test DB URL, run the schema bootstrap, and yield it.)

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest -m dbtest tests/integration/storage/test_postgres_role_quota.py -v
```

Expected: `TypeError: Can't instantiate abstract class ...` or `AttributeError`.

- [ ] **Step 3: Implement the methods**

Edit `apps/backend/serving/storage/postgres_operational.py`. Add near the other api_keys methods:

```python
async def count_active_keys_for_role(self, role: str) -> tuple[int, int]:
    pool = await self._pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*)::int AS keys,
                   COUNT(DISTINCT k.user_id)::int AS users
            FROM api_keys k
            JOIN users u ON u.id = k.user_id
            WHERE k.status = 'active' AND u.role = $1
            """,
            role,
        )
    if row is None:
        return 0, 0
    return int(row["keys"]), int(row["users"])

async def apply_role_quota(self, role: str, quota: Decimal) -> int:
    pool = await self._pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            tag = await conn.execute(
                """
                UPDATE api_keys
                SET quota_daily_cost_usd = $1
                WHERE status = 'active'
                  AND user_id IN (SELECT id FROM users WHERE role = $2)
                """,
                quota,
                role,
            )
    # asyncpg returns "UPDATE <n>" — parse trailing int.
    try:
        return int(tag.split()[-1])
    except (ValueError, IndexError):
        return 0
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest -m dbtest tests/integration/storage/test_postgres_role_quota.py -v
```

Expected: 3 passes.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/storage/postgres_operational.py tests/integration/storage/test_postgres_role_quota.py
git commit -m "feat(storage/postgres): implement role-quota count + apply"
```

---

## Task 6: D1 implementation

**Files:**
- Modify: `apps/backend/serving/storage/d1_operational.py`
- Test: `tests/unit/storage/test_d1_role_quota.py` (new, mocks the HTTP client)

D1's HTTP `meta.changes` provides the rowcount.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/storage/test_d1_role_quota.py`:

```python
"""Unit tests for D1 role-quota helpers (mocks the HTTP client)."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from serving.storage.d1_operational import D1OperationalStore


@pytest.mark.asyncio
async def test_count_active_keys_for_role():
    store = D1OperationalStore(account_id="a", database_id="d", api_token="t")
    store._execute = AsyncMock(return_value={
        "results": [{"keys": 5, "users": 4}],
        "meta": {},
    })
    keys, users = await store.count_active_keys_for_role("pro")
    assert (keys, users) == (5, 4)
    sql, params = store._execute.await_args.args[:2]
    assert "WHERE k.status = 'active'" in sql
    assert "u.role = ?" in sql
    assert params == ["pro"]


@pytest.mark.asyncio
async def test_apply_role_quota_returns_changes():
    store = D1OperationalStore(account_id="a", database_id="d", api_token="t")
    store._execute = AsyncMock(return_value={
        "results": [],
        "meta": {"changes": 7},
    })
    n = await store.apply_role_quota("pro", Decimal("250.00"))
    assert n == 7
    sql, params = store._execute.await_args.args[:2]
    assert sql.strip().startswith("UPDATE api_keys")
    assert params == [250.0, "pro"]
```

(If `D1OperationalStore`'s constructor or `_execute` shape differs, adapt — the assertions on SQL and `meta.changes` parsing are the load-bearing parts.)

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/storage/test_d1_role_quota.py -v
```

Expected: `AttributeError`.

- [ ] **Step 3: Implement the methods**

Edit `apps/backend/serving/storage/d1_operational.py`. Add near the other api_keys methods:

```python
async def count_active_keys_for_role(self, role: str) -> tuple[int, int]:
    sql = (
        "SELECT COUNT(*) AS keys, COUNT(DISTINCT k.user_id) AS users "
        "FROM api_keys k JOIN users u ON u.id = k.user_id "
        "WHERE k.status = 'active' AND u.role = ?"
    )
    res = await self._execute(sql, [role])
    rows = res.get("results") or []
    if not rows:
        return 0, 0
    row = rows[0]
    return int(row.get("keys") or 0), int(row.get("users") or 0)

async def apply_role_quota(self, role: str, quota: Decimal) -> int:
    sql = (
        "UPDATE api_keys SET quota_daily_cost_usd = ? "
        "WHERE status = 'active' "
        "AND user_id IN (SELECT id FROM users WHERE role = ?)"
    )
    res = await self._execute(sql, [float(quota), role])
    return int((res.get("meta") or {}).get("changes") or 0)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/unit/storage/test_d1_role_quota.py -v
```

Expected: 2 passes.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/storage/d1_operational.py tests/unit/storage/test_d1_role_quota.py
git commit -m "feat(storage/d1): implement role-quota count + apply"
```

---

## Task 7: Dual-write + cache wrappers

**Files:**
- Modify: `apps/backend/serving/storage/dual_write.py`
- Modify: `apps/backend/serving/storage/cache.py`
- Test: `tests/unit/storage/test_dual_write_role_quota.py` (new)
- Test: `tests/unit/storage/test_cache_role_quota.py` (new)

- [ ] **Step 1: Write failing tests**

Create `tests/unit/storage/test_dual_write_role_quota.py`:

```python
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from serving.storage.dual_write import DualWriteOperationalStore


@pytest.mark.asyncio
async def test_apply_role_quota_fans_out_returns_primary_count():
    primary = AsyncMock()
    primary.apply_role_quota.return_value = 5
    secondary = AsyncMock()
    secondary.apply_role_quota.return_value = 5

    store = DualWriteOperationalStore(primary=primary, secondary=secondary)
    n = await store.apply_role_quota("pro", Decimal("250.00"))

    assert n == 5
    primary.apply_role_quota.assert_awaited_once_with("pro", Decimal("250.00"))
    secondary.apply_role_quota.assert_awaited_once_with("pro", Decimal("250.00"))


@pytest.mark.asyncio
async def test_count_delegates_to_primary():
    primary = AsyncMock()
    primary.count_active_keys_for_role.return_value = (5, 4)
    secondary = AsyncMock()

    store = DualWriteOperationalStore(primary=primary, secondary=secondary)
    keys, users = await store.count_active_keys_for_role("pro")

    assert (keys, users) == (5, 4)
    secondary.count_active_keys_for_role.assert_not_called()
```

Create `tests/unit/storage/test_cache_role_quota.py`:

```python
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from serving.storage.cache import CachedOperationalStore


@pytest.mark.asyncio
async def test_apply_role_quota_invalidates_key_cache_and_returns_count():
    inner = AsyncMock()
    inner.apply_role_quota.return_value = 7

    store = CachedOperationalStore(inner=inner)
    # Seed the cache with a fake api_key entry that should be invalidated.
    store._key_cache = {"hash-1": ("u-1", {"quota_daily_cost_usd": Decimal("100.00")})}

    n = await store.apply_role_quota("pro", Decimal("250.00"))

    assert n == 7
    inner.apply_role_quota.assert_awaited_once_with("pro", Decimal("250.00"))
    # Coarse invalidation: cache cleared after a bulk write.
    assert store._key_cache == {}


@pytest.mark.asyncio
async def test_count_delegates_to_inner():
    inner = AsyncMock()
    inner.count_active_keys_for_role.return_value = (3, 2)
    store = CachedOperationalStore(inner=inner)
    assert await store.count_active_keys_for_role("pro") == (3, 2)
```

(Adapt the wrapper class names / cache field names to whatever the modules actually expose. The signal we care about is: dual-write fans out, cache wrapper delegates and invalidates the key cache.)

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/storage/test_dual_write_role_quota.py tests/unit/storage/test_cache_role_quota.py -v
```

Expected: `AttributeError`.

- [ ] **Step 3: Implement on dual-write**

Edit `apps/backend/serving/storage/dual_write.py`. Add near other api_keys passthroughs:

```python
async def count_active_keys_for_role(self, role: str) -> tuple[int, int]:
    return await self.primary.count_active_keys_for_role(role)

async def apply_role_quota(self, role: str, quota: Decimal) -> int:
    n = await self.primary.apply_role_quota(role, quota)
    try:
        await self.secondary.apply_role_quota(role, quota)
    except Exception:
        logger.exception("dual_write: secondary apply_role_quota failed for role=%s", role)
    return n
```

(Use whatever logger and primary/secondary attribute names the file already exposes.)

- [ ] **Step 4: Implement on cache**

Edit `apps/backend/serving/storage/cache.py`:

```python
async def count_active_keys_for_role(self, role: str) -> tuple[int, int]:
    return await self.inner.count_active_keys_for_role(role)

async def apply_role_quota(self, role: str, quota: Decimal) -> int:
    n = await self.inner.apply_role_quota(role, quota)
    # Bulk write: clear the per-key cache rather than tracking individual rows.
    self._key_cache.clear()
    return n
```

(Replace `self._key_cache` with whatever attribute the cache wrapper uses for cached api_key lookups.)

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/unit/storage/test_dual_write_role_quota.py tests/unit/storage/test_cache_role_quota.py -v
uv run pytest -q -m "not external and not dbtest"
```

Expected: green across the unit/api suites.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/serving/storage/dual_write.py apps/backend/serving/storage/cache.py tests/unit/storage/test_dual_write_role_quota.py tests/unit/storage/test_cache_role_quota.py
git commit -m "feat(storage): wire role-quota through dual-write + cache wrappers"
```

---

## Task 8: Admin endpoints — preview + apply

**Files:**
- Create: `apps/backend/serving/servers/routers/admin/quota.py`
- Modify: `apps/backend/serving/servers/routers/admin/__init__.py` (register router)
- Test: `tests/api/admin/test_quota_routes.py` (new)

- [ ] **Step 1: Write failing tests**

Create `tests/api/admin/test_quota_routes.py`:

```python
"""API surface tests for /admin/quota/role-apply{,-preview}."""

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_admin(monkeypatch):
    """Build a minimal FastAPI app mounting the admin quota router with mocked deps."""
    from fastapi import FastAPI

    from serving.servers.routers.admin import quota as quota_router

    app = FastAPI()
    app.include_router(quota_router.router)

    op_store = AsyncMock()
    op_store.count_active_keys_for_role.return_value = (5, 4)
    op_store.apply_role_quota.return_value = 5

    rt = AsyncMock()
    rt.get_float.return_value = 250.0

    from serving.servers.deps import get_operational_store, verify_admin_access
    from serving.config.runtime_settings import get_runtime_settings

    app.dependency_overrides[get_operational_store] = lambda: op_store
    app.dependency_overrides[verify_admin_access] = lambda: "admin-id"
    app.dependency_overrides[get_runtime_settings] = lambda: rt
    return app, op_store, rt


def test_preview_returns_count_and_quota(app_with_admin):
    app, op_store, rt = app_with_admin
    client = TestClient(app)
    resp = client.get("/admin/quota/role-apply-preview", params={"role": "pro"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "pro"
    assert body["quota"] == 250.0
    assert body["keys_affected"] == 5
    assert body["users_affected"] == 4
    rt.get_float.assert_awaited_with("user_daily_quota_pro")
    op_store.count_active_keys_for_role.assert_awaited_with("pro")


def test_apply_returns_keys_updated(app_with_admin):
    app, op_store, rt = app_with_admin
    client = TestClient(app)
    resp = client.post("/admin/quota/role-apply", json={"role": "pro"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "pro"
    assert body["quota"] == 250.0
    assert body["keys_updated"] == 5
    op_store.apply_role_quota.assert_awaited_with("pro", Decimal("250.0"))


def test_invalid_role_rejected(app_with_admin):
    app, _, _ = app_with_admin
    client = TestClient(app)
    assert client.get("/admin/quota/role-apply-preview", params={"role": "ghost"}).status_code == 422
    assert client.post("/admin/quota/role-apply", json={"role": "ghost"}).status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/api/admin/test_quota_routes.py -v
```

Expected: `ImportError` for `serving.servers.routers.admin.quota`.

- [ ] **Step 3: Create the router**

Create `apps/backend/serving/servers/routers/admin/quota.py`:

```python
"""Admin endpoints for per-role daily quota bulk-apply."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from serving.config.runtime_settings import (
    RUNTIME_SETTINGS_REGISTRY,
    RuntimeSettings,
    get_runtime_settings,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, verify_admin_access
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/admin")

Role = Literal["free", "pro", "internal", "admin"]


class RoleQuotaApplyRequest(BaseModel):
    role: Role


class RoleQuotaPreview(BaseModel):
    role: Role
    quota: Decimal
    keys_affected: int
    users_affected: int


class RoleQuotaApplyResult(BaseModel):
    role: Role
    quota: Decimal
    keys_updated: int


def _require_rt(rt: RuntimeSettings | None) -> RuntimeSettings:
    if rt is None:
        raise HTTPException(status_code=503, detail="Runtime settings not initialized")
    return rt


async def _quota_for_role(rt: RuntimeSettings, role: str) -> Decimal:
    key = f"user_daily_quota_{role}"
    if key not in RUNTIME_SETTINGS_REGISTRY:
        raise HTTPException(status_code=400, detail=f"No quota setting for role: {role}")
    val = await rt.get_float(key)
    return Decimal(str(val))


@router.get("/quota/role-apply-preview", response_model=RoleQuotaPreview)
async def preview_role_apply(
    role: Role = Query(...),
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    rt: RuntimeSettings | None = Depends(get_runtime_settings),
) -> RoleQuotaPreview:
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rt = _require_rt(rt)
    quota = await _quota_for_role(rt, role)
    keys, users = await op_store.count_active_keys_for_role(role)
    return RoleQuotaPreview(
        role=role, quota=quota, keys_affected=keys, users_affected=users
    )


@router.post("/quota/role-apply", response_model=RoleQuotaApplyResult)
async def apply_role_quota(
    request: Request,
    payload: RoleQuotaApplyRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    rt: RuntimeSettings | None = Depends(get_runtime_settings),
) -> RoleQuotaApplyResult:
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rt = _require_rt(rt)
    quota = await _quota_for_role(rt, payload.role)
    updated = await op_store.apply_role_quota(payload.role, quota)
    await log_admin_action(
        op_store,
        get_client_ip(request),
        "quota.role_apply",
        None,
        {"role": payload.role, "quota": float(quota), "keys_updated": updated},
    )
    return RoleQuotaApplyResult(role=payload.role, quota=quota, keys_updated=updated)
```

Edit `apps/backend/serving/servers/routers/admin/__init__.py` — add to imports and `include_router` calls:

```python
from serving.servers.routers.admin import (
    analytics,
    api_keys,
    broadcast,
    export,
    login_events,
    metrics,
    providers,
    quota,            # <— new
    settings,
    signup_domains,
    stats,
    users,
)
...
router.include_router(providers.router)
router.include_router(quota.router)        # <— new
router.include_router(settings.router)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/api/admin/test_quota_routes.py -v
uv run pytest -q -m "not external and not dbtest"
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/servers/routers/admin/quota.py apps/backend/serving/servers/routers/admin/__init__.py tests/api/admin/test_quota_routes.py
git commit -m "feat(admin-api): role quota preview + bulk apply endpoints"
```

---

## Task 9: Frontend API client

**Files:**
- Modify: `apps/frontend/src/lib/api/admin.ts` (append role-quota helpers)
- Test: `apps/frontend/src/lib/api/__tests__/admin.test.ts` (extend or create)

- [ ] **Step 1: Write failing test**

Extend (or create) `apps/frontend/src/lib/api/__tests__/admin.test.ts`:

```typescript
import { describe, expect, it, vi, afterEach } from 'vitest';
import { applyRoleQuota, previewRoleQuotaApply } from '../admin';

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('role quota client', () => {
  it('previewRoleQuotaApply hits GET preview endpoint', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({ role: 'pro', quota: 250, keys_affected: 5, users_affected: 4 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    const out = await previewRoleQuotaApply('pro');
    expect(out).toEqual({ role: 'pro', quota: 250, keys_affected: 5, users_affected: 4 });
    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/admin/quota/role-apply-preview?role=pro');
  });

  it('applyRoleQuota POSTs with role body', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({ role: 'pro', quota: 250, keys_updated: 5 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    const out = await applyRoleQuota('pro');
    expect(out).toEqual({ role: 'pro', quota: 250, keys_updated: 5 });
    const [, init] = fetchMock.mock.calls[0];
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ role: 'pro' });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
npm --prefix apps/frontend test -- src/lib/api/__tests__/admin.test.ts
```

Expected: import error for `previewRoleQuotaApply` / `applyRoleQuota`.

- [ ] **Step 3: Add the client functions**

Append to `apps/frontend/src/lib/api/admin.ts`:

```typescript
export type Role = 'free' | 'pro' | 'internal' | 'admin';

export interface RoleQuotaPreview {
  role: Role;
  quota: number;
  keys_affected: number;
  users_affected: number;
}

export interface RoleQuotaApplyResult {
  role: Role;
  quota: number;
  keys_updated: number;
}

export async function previewRoleQuotaApply(role: Role): Promise<RoleQuotaPreview> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/quota/role-apply-preview?role=${encodeURIComponent(role)}`,
  );
  return jsonOrThrow<RoleQuotaPreview>(resp);
}

export async function applyRoleQuota(role: Role): Promise<RoleQuotaApplyResult> {
  const resp = await fetchWithAuth(API_BASE, '/admin/quota/role-apply', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role }),
  });
  return jsonOrThrow<RoleQuotaApplyResult>(resp);
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
npm --prefix apps/frontend test -- src/lib/api/__tests__/admin.test.ts
npm --prefix apps/frontend run type-check
```

Expected: green, tsc clean.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/lib/api/admin.ts apps/frontend/src/lib/api/__tests__/admin.test.ts
git commit -m "feat(admin-ui): role quota API client"
```

---

## Task 10: Frontend — Apply button + confirm modal

**Files:**
- Modify: `apps/frontend/src/app/dashboard/admin/SettingsTab.tsx`
- Test: `apps/frontend/src/app/dashboard/admin/__tests__/SettingsTab.roleQuota.test.tsx` (new)

- [ ] **Step 1: Write failing test**

Create `apps/frontend/src/app/dashboard/admin/__tests__/SettingsTab.roleQuota.test.tsx`:

```typescript
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { SettingsTab } from '../SettingsTab';

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    ...actual,
    listRuntimeSettings: vi.fn(async () => ({
      settings: [
        {
          key: 'user_daily_quota_pro',
          value: 250,
          value_type: 'float',
          default_value: 100,
          description: 'pro quota',
          min: 0,
          max: null,
        },
      ],
    })),
    listSignupAllowedDomains: vi.fn(async () => ({ domains: [] })),
    previewRoleQuotaApply: vi.fn(async () => ({
      role: 'pro',
      quota: 250,
      keys_affected: 42,
      users_affected: 38,
    })),
    applyRoleQuota: vi.fn(async () => ({ role: 'pro', quota: 250, keys_updated: 42 })),
    updateRuntimeSetting: vi.fn(),
  };
});

describe('SettingsTab role quota', () => {
  it('renders Apply button only on quota settings and runs the confirm flow', async () => {
    const api = await import('@/lib/api/admin');
    render(<SettingsTab />);

    const applyBtn = await screen.findByRole('button', { name: /apply to existing users/i });
    fireEvent.click(applyBtn);
    await waitFor(() => expect(api.previewRoleQuotaApply).toHaveBeenCalledWith('pro'));

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('42');
    expect(dialog).toHaveTextContent(/pro/i);

    fireEvent.click(screen.getByRole('button', { name: /^confirm$/i }));
    await waitFor(() => expect(api.applyRoleQuota).toHaveBeenCalledWith('pro'));
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
npm --prefix apps/frontend test -- SettingsTab.roleQuota.test.tsx
```

Expected: no Apply button on screen.

- [ ] **Step 3: Augment `SettingsTab.tsx`**

Edit `apps/frontend/src/app/dashboard/admin/SettingsTab.tsx`:

Imports (extend existing import line for `@/lib/api/admin`):

```typescript
import {
  SignupAllowedDomain,
  addSignupAllowedDomain,
  applyRoleQuota,
  listSignupAllowedDomains,
  listRuntimeSettings,
  previewRoleQuotaApply,
  removeSignupAllowedDomain,
  Role,
  RoleQuotaPreview,
  RuntimeSettingItem,
  updateRuntimeSetting,
} from '@/lib/api/admin';
```

Inside `SettingsTab`, add new state slots near existing ones:

```typescript
const QUOTA_KEY_PREFIX = 'user_daily_quota_';
const [quotaConfirm, setQuotaConfirm] = useState<RoleQuotaPreview | null>(null);
const [quotaLoadingRole, setQuotaLoadingRole] = useState<Role | null>(null);
const [quotaApplyingRole, setQuotaApplyingRole] = useState<Role | null>(null);
```

Add the click handlers above the `return`:

```typescript
const onClickApply = async (role: Role) => {
  setQuotaLoadingRole(role);
  try {
    const preview = await previewRoleQuotaApply(role);
    setQuotaConfirm(preview);
  } catch (e) {
    flashToast(`Preview failed: ${getErrorMessage(e)}`);
  } finally {
    setQuotaLoadingRole(null);
  }
};

const onConfirmApply = async () => {
  if (!quotaConfirm) return;
  const role = quotaConfirm.role;
  setQuotaApplyingRole(role);
  try {
    const res = await applyRoleQuota(role);
    flashToast(`Updated ${res.keys_updated} keys for role ${role} to $${res.quota}`);
    setQuotaConfirm(null);
  } catch (e) {
    flashToast(`Apply failed: ${getErrorMessage(e)}`);
  } finally {
    setQuotaApplyingRole(null);
  }
};
```

Inside the `numericSettings.map(...)` render block, locate the `<div className="flex items-center gap-2">` containing the input + Save button (around line 329). Add an extra button after Save when the key is a quota:

```tsx
{setting.key.startsWith(QUOTA_KEY_PREFIX) && (
  <button
    type="button"
    onClick={() =>
      onClickApply(setting.key.slice(QUOTA_KEY_PREFIX.length) as Role)
    }
    disabled={
      quotaLoadingRole !== null || quotaApplyingRole !== null
    }
    className="rounded-md border border-gray-300 bg-white px-3 py-1 text-[12px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40"
  >
    {quotaLoadingRole === setting.key.slice(QUOTA_KEY_PREFIX.length)
      ? 'Loading...'
      : 'Apply to existing users'}
  </button>
)}
```

Add the confirm modal at the end of the component, beside the existing signup-domain `confirm` modal:

```tsx
{quotaConfirm && (
  <div
    role="dialog"
    aria-modal="true"
    className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
    onClick={() => quotaApplyingRole === null && setQuotaConfirm(null)}
  >
    <div
      className="w-full max-w-md rounded-xl bg-white p-5 shadow-lg"
      onClick={(e) => e.stopPropagation()}
    >
      <h3 className="text-[15px] font-semibold text-gray-900">
        Apply quota to role <code>{quotaConfirm.role}</code>
      </h3>
      <p className="mt-2 text-[13px] text-gray-600">
        This sets <code>quota_daily_cost_usd = ${quotaConfirm.quota}</code> on{' '}
        <strong>{quotaConfirm.keys_affected}</strong> active API keys belonging to{' '}
        <strong>{quotaConfirm.users_affected}</strong> users with role{' '}
        <code>{quotaConfirm.role}</code>. Custom per-key overrides will be lost.
      </p>
      <div className="mt-4 flex justify-end gap-2">
        <button
          type="button"
          onClick={() => setQuotaConfirm(null)}
          disabled={quotaApplyingRole !== null}
          className="rounded-md px-3 py-1.5 text-[13px] font-medium text-gray-700 hover:bg-gray-100"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={onConfirmApply}
          disabled={quotaApplyingRole !== null}
          className="rounded-md bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-gray-700 disabled:opacity-40"
        >
          {quotaApplyingRole !== null ? 'Applying...' : 'Confirm'}
        </button>
      </div>
    </div>
  </div>
)}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
npm --prefix apps/frontend test -- SettingsTab.roleQuota.test.tsx
npm --prefix apps/frontend run type-check
npm --prefix apps/frontend run lint
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/app/dashboard/admin/SettingsTab.tsx apps/frontend/src/app/dashboard/admin/__tests__/SettingsTab.roleQuota.test.tsx
git commit -m "feat(admin-ui): per-role quota apply button with confirm modal"
```

---

## Task 11: Deprecation comment + manual smoke test on staging

**Files:**
- Modify: `apps/backend/serving/config/settings.py` (deprecation comment)

- [ ] **Step 1: Mark env var deprecated**

Edit `apps/backend/serving/config/settings.py` line 66:

```python
# Deprecated: prefer admin Settings → user_daily_quota_<role>. Kept as
# fallback when runtime settings are unavailable (early bootstrap / DB outage).
signup_default_daily_quota_usd: float = 100.00
```

- [ ] **Step 2: Run full suite**

```bash
make format
make lint
make test
```

Expected: all green.

- [ ] **Step 3: Commit**

```bash
git add apps/backend/serving/config/settings.py
git commit -m "docs(config): mark signup_default_daily_quota_usd deprecated"
```

- [ ] **Step 4: Open PR**

Push branch, open PR targeting `dev`. Body should reference [docs/agents/specs/2026-05-05-per-role-daily-quota-design.md](../specs/2026-05-05-per-role-daily-quota-design.md).

- [ ] **Step 5: Verify on staging**

After staging deploys from `dev`:

1. Sign in to https://staging.freeinference.org as `admin@admin.com` / `admin`.
2. Open admin dashboard → Settings.
3. Confirm 4 new numeric inputs appear: `User Daily Quota Free|Pro|Internal|Admin`.
4. Change `User Daily Quota Free` to `42`. Save. Reload — value persists.
5. Click "Apply to existing users" on the free row. Confirm modal shows accurate counts. Click Confirm.
6. In a separate dashboard, inspect a free user's API key — `quota_daily_cost_usd` should now be 42.
7. Sign up a new user with a free-tier email. Inspect the new user's first API key — `quota_daily_cost_usd` should be 42.

Document anomalies in the PR before requesting review.

---

## Self-Review Notes

- **Spec coverage:** registry (T1), signup helper (T2), wiring (T3), ABC (T4), Postgres (T5), D1 (T6), dual-write+cache (T7), endpoints (T8), client (T9), UI (T10), deprecation+smoke (T11). All spec sections covered.
- **Type consistency:** `Role` literal identical in backend and frontend; `quota_daily_cost_usd` written as `Decimal` everywhere on the backend; preview/result Pydantic and TS shapes match.
- **No placeholders.** Every step has concrete code or commands. The two "adapt to local fixture/wrapper" notes (T5 conftest, T7 attribute names) are unavoidable boundaries — caveats are explicit, not handwaves.
