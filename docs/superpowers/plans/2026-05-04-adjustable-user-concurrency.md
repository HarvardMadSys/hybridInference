# Adjustable Per-User Concurrency Limits — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make per-user concurrency caps adjustable at runtime via the existing admin settings API, and bump the default `free` cap from 1 to 3.

**Architecture:** Reuse `RUNTIME_SETTINGS_REGISTRY` (DB-persisted, TTL-cached, audit-logged via `PATCH /admin/settings/{key}`). Refactor `UserConcurrencyLimiter` to read fresh limits via an async `LimitsProvider` callable; existing in-memory `_UserSlot`s lazily resize on the next acquire. Admin self-lockout is prevented by adding generic `min`/`max` validation to numeric registry entries.

**Tech Stack:** FastAPI, Pydantic, asyncio, pytest / pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-05-04-adjustable-user-concurrency-design.md`

---

## File Structure

| File | Role | Status |
|---|---|---|
| `apps/backend/serving/config/runtime_settings.py` | `RUNTIME_SETTINGS_REGISTRY` adds 4 int keys with `min: 1` | Modify |
| `apps/backend/serving/servers/routers/admin/settings.py` | PATCH endpoint validates numeric `min`/`max` | Modify |
| `apps/backend/serving/servers/concurrency.py` | `UserConcurrencyLimiter` accepts a `LimitsProvider` callable; lazy slot resize; provider error fallback | Modify |
| `apps/backend/serving/servers/bootstrap.py` | Reorder limiter init after `RuntimeSettings`; build provider closure; drop static import | Modify |
| `apps/backend/serving/config/settings.py` | Remove now-unused `USER_CONCURRENCY_LIMITS` | Modify |
| `tests/servers/test_admin_settings.py` | New tests for `min`/`max` validation | Modify |
| `tests/servers/test_enforce_user_concurrency.py` | Switch to async-callable provider via test helper | Modify |
| `tests/servers/test_concurrency_endpoint.py` | Switch to async-callable provider via test helper | Modify |
| `tests/servers/test_settings_role_rank.py` | Drop the dict-shape assertion (constant goes away) | Modify |
| `tests/servers/test_concurrency_runtime_resize.py` | New tests for lazy resize, downward resize, provider error fallback | Create |

---

## Task 1: Add `min`/`max` validation to numeric runtime-settings PATCH

**Files:**
- Modify: `apps/backend/serving/servers/routers/admin/settings.py`
- Test: `tests/servers/test_admin_settings.py`

This is general infrastructure used in Task 2 to enforce the admin floor.

- [ ] **Step 1: Write the failing test**

Append to `tests/servers/test_admin_settings.py`:

```python
@pytest.mark.asyncio
async def test_update_int_setting_below_min_returns_400(monkeypatch, admin_client):
    """A numeric setting with a `min` floor rejects out-of-range values."""
    from serving.config import runtime_settings as rs_mod

    # Inject a temporary int setting with min=1 for the duration of the test.
    monkeypatch.setitem(
        rs_mod.RUNTIME_SETTINGS_REGISTRY,
        "_test_floored_int",
        {"type": "int", "default": 5, "min": 1, "description": "Test-only floored int"},
    )
    client, _, _ = admin_client

    response = await client.patch(
        "/admin/settings/_test_floored_int",
        json={"value": 0},
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 400
    assert "min" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_update_int_setting_at_min_succeeds(monkeypatch, admin_client):
    """The boundary value is accepted."""
    from serving.config import runtime_settings as rs_mod

    monkeypatch.setitem(
        rs_mod.RUNTIME_SETTINGS_REGISTRY,
        "_test_floored_int",
        {"type": "int", "default": 5, "min": 1, "description": "Test-only floored int"},
    )
    client, op_store, _ = admin_client
    op_store.get_setting.return_value = None

    response = await client.patch(
        "/admin/settings/_test_floored_int",
        json={"value": 1},
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 200
    assert response.json()["value"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /home/juncheng/hybridInference-worktrees/adjustable-user-concurrency
uv run pytest tests/servers/test_admin_settings.py::test_update_int_setting_below_min_returns_400 -v
```

Expected: FAIL — endpoint accepts the out-of-range value (returns 200 instead of 400).

- [ ] **Step 3: Implement validation in PATCH handler**

In `apps/backend/serving/servers/routers/admin/settings.py`, locate the type-check block (around lines 76-85, beginning with `if expected_type == "bool"`) and add a numeric range check after the type checks but before `old_row = await op_store.get_setting(key)`:

```python
if expected_type in ("int", "float"):
    lo = entry.get("min")
    hi = entry.get("max")
    if lo is not None and value < lo:
        raise HTTPException(
            status_code=400,
            detail=f"Setting '{key}' value {value} is below min ({lo})",
        )
    if hi is not None and value > hi:
        raise HTTPException(
            status_code=400,
            detail=f"Setting '{key}' value {value} is above max ({hi})",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/servers/test_admin_settings.py -v
```

Expected: All tests in the file pass, including the two new ones.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/servers/routers/admin/settings.py tests/servers/test_admin_settings.py
git commit -m "feat(admin): support min/max validation for numeric runtime settings"
```

---

## Task 2: Register four `user_concurrency_*` runtime settings

**Files:**
- Modify: `apps/backend/serving/config/runtime_settings.py`
- Test: `tests/servers/test_admin_settings.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/servers/test_admin_settings.py`:

```python
@pytest.mark.asyncio
async def test_list_settings_includes_user_concurrency_keys(admin_client):
    """All four user_concurrency_<role> keys are exposed via /admin/settings."""
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)

    response = await client.get(
        "/admin/settings",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 200
    keys = {item["key"] for item in response.json()["settings"]}
    assert {
        "user_concurrency_free",
        "user_concurrency_pro",
        "user_concurrency_internal",
        "user_concurrency_admin",
    }.issubset(keys)


@pytest.mark.asyncio
async def test_update_user_concurrency_admin_below_min_returns_400(admin_client):
    """Admin floor of 1 prevents self-lockout."""
    client, _, _ = admin_client

    response = await client.patch(
        "/admin/settings/user_concurrency_admin",
        json={"value": 0},
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_update_user_concurrency_admin_at_min_succeeds(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting.return_value = None

    response = await client.patch(
        "/admin/settings/user_concurrency_admin",
        json={"value": 1},
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 200
    assert response.json()["value"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/servers/test_admin_settings.py::test_list_settings_includes_user_concurrency_keys tests/servers/test_admin_settings.py::test_update_user_concurrency_admin_below_min_returns_400 -v
```

Expected: FAIL — keys aren't registered yet, so listing won't include them and PATCH returns 404.

- [ ] **Step 3: Add the four registry entries**

In `apps/backend/serving/config/runtime_settings.py`, extend `RUNTIME_SETTINGS_REGISTRY` (currently lines 25-46) with four new entries:

```python
RUNTIME_SETTINGS_REGISTRY: dict[str, dict[str, Any]] = {
    "user_auth_enabled": {
        "type": "bool",
        "default": True,
        "description": "Enable user authentication (JWT-based)",
    },
    "signup_enabled": {
        "type": "bool",
        "default": True,
        "description": "Allow new user signups",
    },
    "signup_require_email_verification": {
        "type": "bool",
        "default": True,
        "description": "Require email verification for new signups",
    },
    "log_full_payload": {
        "type": "bool",
        "default": False,
        "description": "Log full request payloads at DEBUG level",
    },
    "user_concurrency_free": {
        "type": "int",
        "default": 3,
        "min": 1,
        "description": "Per-user concurrency cap for free-tier users",
    },
    "user_concurrency_pro": {
        "type": "int",
        "default": 3,
        "min": 1,
        "description": "Per-user concurrency cap for pro-tier users",
    },
    "user_concurrency_internal": {
        "type": "int",
        "default": 10,
        "min": 1,
        "description": "Per-user concurrency cap for internal users",
    },
    "user_concurrency_admin": {
        "type": "int",
        "default": 10,
        "min": 1,
        "description": "Per-user concurrency cap for admin users",
    },
}
```

Note: existing bool entries are unchanged. The `min` field is optional and only consulted by the validation added in Task 1.

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/servers/test_admin_settings.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/config/runtime_settings.py tests/servers/test_admin_settings.py
git commit -m "feat(settings): register user_concurrency_<role> runtime settings"
```

---

## Task 3: Refactor `UserConcurrencyLimiter` to a `LimitsProvider` callable with lazy slot resize

**Files:**
- Modify: `apps/backend/serving/servers/concurrency.py`

The new constructor signature breaks the existing tests; we update them in Task 4. Tests within this task only cover the new provider/resize behavior in isolation.

- [ ] **Step 1: Write the failing tests**

Create `tests/servers/test_concurrency_runtime_resize.py`:

```python
"""Tests for runtime-resizable UserConcurrencyLimiter."""

from __future__ import annotations

import pytest

from serving.servers.concurrency import (
    UserConcurrencyLimiter,
    static_limits_provider,
)


@pytest.mark.asyncio
async def test_provider_called_each_acquire():
    """The limiter consults the provider on every acquire."""
    calls = {"n": 0}

    async def provider() -> dict[str, int]:
        calls["n"] += 1
        return {"free": 1, "pro": 3, "internal": 10, "admin": 10}

    limiter = UserConcurrencyLimiter(provider)
    await limiter.try_acquire("u1", "free", False)
    await limiter.try_acquire("u1", "free", False)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_increase_resizes_existing_slot():
    """Bumping the cap raises an existing slot's capacity on next acquire."""
    limits = {"free": 1, "pro": 3, "internal": 10, "admin": 10}

    async def provider() -> dict[str, int]:
        return dict(limits)  # snapshot per call

    limiter = UserConcurrencyLimiter(provider)
    granted, cap, _ = await limiter.try_acquire("u1", "free", False)
    assert granted and cap == 1

    # Second acquire under cap=1 must fail.
    granted, _, _ = await limiter.try_acquire("u1", "free", False)
    assert not granted

    # Bump the cap; next acquire resizes the slot, then succeeds.
    limits["free"] = 3
    granted, cap, _ = await limiter.try_acquire("u1", "free", False)
    assert granted
    assert cap == 3


@pytest.mark.asyncio
async def test_decrease_does_not_kill_in_flight_but_blocks_new():
    """Lowering the cap below current in_use leaves in-flight requests alone
    but rejects further acquires until the user drains."""
    limits = {"free": 3, "pro": 3, "internal": 10, "admin": 10}

    async def provider() -> dict[str, int]:
        return dict(limits)

    limiter = UserConcurrencyLimiter(provider)
    for _ in range(3):
        granted, _, _ = await limiter.try_acquire("u1", "free", False)
        assert granted

    # Drop cap to 1 while in_use=3.
    limits["free"] = 1

    # New acquires must fail while in_use > new cap.
    granted, cap, _ = await limiter.try_acquire("u1", "free", False)
    assert not granted
    assert cap == 1

    # Release until in_use is below the new cap.
    limiter.release("u1")
    limiter.release("u1")
    limiter.release("u1")
    granted, cap, _ = await limiter.try_acquire("u1", "free", False)
    assert granted
    assert cap == 1


@pytest.mark.asyncio
async def test_provider_error_falls_back_to_registry_defaults():
    """If the provider raises, the limiter uses registry defaults."""

    async def boom() -> dict[str, int]:
        raise RuntimeError("db offline")

    limiter = UserConcurrencyLimiter(boom)
    # Free default is 3 (per RUNTIME_SETTINGS_REGISTRY).
    granted, cap, label = await limiter.try_acquire("u1", "free", False)
    assert granted
    assert cap == 3
    assert label == "free"


@pytest.mark.asyncio
async def test_static_helper_constructs_async_provider():
    """The static_limits_provider helper wraps a plain dict."""
    p = static_limits_provider({"free": 2, "pro": 3, "internal": 10, "admin": 10})
    snapshot = await p()
    assert snapshot == {"free": 2, "pro": 3, "internal": 10, "admin": 10}


@pytest.mark.asyncio
async def test_admin_role_uses_admin_cap():
    async def provider() -> dict[str, int]:
        return {"free": 1, "pro": 3, "internal": 10, "admin": 10}

    limiter = UserConcurrencyLimiter(provider)
    granted, cap, label = await limiter.try_acquire("a1", "free", True)
    assert granted
    assert cap == 10
    assert label == "admin"


@pytest.mark.asyncio
async def test_unknown_role_falls_back_to_free_cap():
    async def provider() -> dict[str, int]:
        return {"free": 1, "pro": 3, "internal": 10, "admin": 10}

    limiter = UserConcurrencyLimiter(provider)
    granted, cap, label = await limiter.try_acquire("u1", "mystery", False)
    assert granted
    assert cap == 1
    assert label == "free"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/servers/test_concurrency_runtime_resize.py -v
```

Expected: FAIL — `static_limits_provider` doesn't exist; current `UserConcurrencyLimiter.__init__` rejects callables.

- [ ] **Step 3: Refactor the limiter**

Replace `apps/backend/serving/servers/concurrency.py` lines 13-50 (imports + `_UserSlot`) and lines 52-135 (`UserConcurrencyLimiter`) with:

```python
"""Per-user concurrency limiter.

Caps the number of simultaneous in-flight inference requests per user,
keyed by ``user_id``. Backed by an in-process counter under the asyncio
single-thread invariant — no Redis, no DB.

Limits are resolved per-call via a ``LimitsProvider`` async callable so
operators can adjust caps at runtime through the admin settings API.
Each existing ``_UserSlot`` lazily resizes on its owner's next acquire.
The slot's *role label* remains sticky to its creation-time value so
metrics stay coherent across role changes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from serving.observability.metrics import (
    USER_CONCURRENCY_ACQUIRES_TOTAL,
    USER_CONCURRENCY_IN_FLIGHT,
    USER_CONCURRENCY_REJECTED_TOTAL,
)
from serving.utils.logging import get_logger

logger = get_logger(__name__)

LimitsProvider = Callable[[], Awaitable[dict[str, int]]]

# Conservative fallback when the provider raises (e.g., DB hiccup). Kept
# in sync with the defaults declared in
# ``serving/config/runtime_settings.py`` for the ``user_concurrency_*``
# keys.
_FALLBACK_LIMITS: dict[str, int] = {
    "free": 3,
    "pro": 3,
    "internal": 10,
    "admin": 10,
}


def static_limits_provider(limits: dict[str, int]) -> LimitsProvider:
    """Wrap a plain dict in a ``LimitsProvider`` (test helper)."""
    snapshot = dict(limits)

    async def _provider() -> dict[str, int]:
        return snapshot

    return _provider


@dataclass
class _UserSlot:
    """Tiny counter for one user's in-flight requests.

    asyncio is single-threaded; ``try_acquire`` and ``release`` contain no
    ``await`` and therefore execute atomically with respect to other tasks
    on the same event loop. No internal lock is needed.
    """

    capacity: int
    role: str  # role label captured at slot creation; used for metrics
    in_use: int = 0

    def try_acquire(self) -> bool:
        if self.in_use >= self.capacity:
            return False
        self.in_use += 1
        return True

    def release(self) -> None:
        if self.in_use > 0:
            self.in_use -= 1


class UserConcurrencyLimiter:
    """Per-user in-flight request limiter with runtime-adjustable caps."""

    def __init__(self, limits_provider: LimitsProvider):
        self._provider = limits_provider
        self._slots: dict[str, _UserSlot] = {}
        self._create_lock = asyncio.Lock()  # guards lazy slot creation

    async def _read_limits(self) -> dict[str, int]:
        """Resolve current limits, falling back to defaults on error."""
        try:
            return await self._provider()
        except Exception:
            logger.exception(
                "user_concurrency: limits provider failed; falling back to defaults"
            )
            return dict(_FALLBACK_LIMITS)

    @staticmethod
    def _limit_for(role: str, is_admin: bool, limits: dict[str, int]) -> int:
        if is_admin:
            return limits.get("admin", _FALLBACK_LIMITS["admin"])
        if role in limits:
            return limits[role]
        return limits.get("free", _FALLBACK_LIMITS["free"])

    @staticmethod
    def _role_label(role: str, is_admin: bool, limits: dict[str, int]) -> str:
        if is_admin:
            return "admin"
        if role in limits:
            return role
        return "free"

    async def try_acquire(self, user_id: str, role: str, is_admin: bool) -> tuple[bool, int, str]:
        """Non-blocking acquire.

        Returns ``(granted, capacity, role_label)`` where *capacity*
        reflects the slot's **current** capacity after any lazy resize and
        *role_label* is the slot's sticky label.
        """
        limits = await self._read_limits()
        target_capacity = self._limit_for(role, is_admin, limits)
        target_label = self._role_label(role, is_admin, limits)

        slot = self._slots.get(user_id)
        if slot is None:
            async with self._create_lock:
                slot = self._slots.get(user_id)
                if slot is None:
                    slot = _UserSlot(capacity=target_capacity, role=target_label)
                    self._slots[user_id] = slot

        # Lazy resize: only `capacity` is dynamic; role label stays sticky.
        if slot.capacity != target_capacity:
            slot.capacity = target_capacity

        granted = slot.try_acquire()
        label = slot.role
        if granted:
            USER_CONCURRENCY_ACQUIRES_TOTAL.labels(role=label, outcome="granted").inc()
            USER_CONCURRENCY_IN_FLIGHT.labels(role=label).inc()
        else:
            USER_CONCURRENCY_ACQUIRES_TOTAL.labels(role=label, outcome="rejected").inc()
            USER_CONCURRENCY_REJECTED_TOTAL.labels(role=label).inc()
            logger.warning(
                "concurrency_rejected",
                extra={
                    "event": "concurrency_rejected",
                    "user_id": user_id,
                    "role": label,
                },
            )
        return granted, slot.capacity, label

    def release(self, user_id: str) -> None:
        """Release a slot. Idempotent for unknown user_id."""
        slot = self._slots.get(user_id)
        if slot is None:
            return
        had_one = slot.in_use > 0
        slot.release()
        if had_one:
            USER_CONCURRENCY_IN_FLIGHT.labels(role=slot.role).dec()

    def role_for(self, user_id: str) -> str | None:
        """Return the role label captured at slot creation, or None."""
        slot = self._slots.get(user_id)
        return slot.role if slot is not None else None
```

The block below (`from typing import TYPE_CHECKING ...` through the `enforce_user_concurrency` function) is unchanged.

- [ ] **Step 4: Run the new tests to verify they pass**

```bash
uv run pytest tests/servers/test_concurrency_runtime_resize.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/servers/concurrency.py tests/servers/test_concurrency_runtime_resize.py
git commit -m "feat(concurrency): runtime-adjustable per-user limits with lazy slot resize"
```

---

## Task 4: Update existing concurrency tests to the new constructor

**Files:**
- Modify: `tests/servers/test_enforce_user_concurrency.py`
- Modify: `tests/servers/test_concurrency_endpoint.py`

These tests today pass a dict literal to `UserConcurrencyLimiter(...)`. Wrap with `static_limits_provider`.

- [ ] **Step 1: Run the existing tests to confirm they currently fail under the new API**

```bash
uv run pytest tests/servers/test_enforce_user_concurrency.py tests/servers/test_concurrency_endpoint.py -v
```

Expected: many tests FAIL with a TypeError because `__init__` no longer accepts a dict.

- [ ] **Step 2: Update `tests/servers/test_enforce_user_concurrency.py`**

At the top of the imports block (current lines 17-23), change:

```python
from serving.servers.concurrency import (
    UserConcurrencyLimiter,
    enforce_user_concurrency,
)
```

to:

```python
from serving.servers.concurrency import (
    UserConcurrencyLimiter,
    enforce_user_concurrency,
    static_limits_provider,
)
```

Then replace every occurrence of `UserConcurrencyLimiter(LIMITS)` (currently on lines 71, 101, 128, 157, 174, 202, 224) with:

```python
UserConcurrencyLimiter(static_limits_provider(LIMITS))
```

(The `LIMITS = {"free": 1, "pro": 3, "internal": 10, "admin": 10}` constant on line 25 is preserved.)

- [ ] **Step 3: Update `tests/servers/test_concurrency_endpoint.py`**

Change the imports (currently lines 27-29):

```python
from serving.servers.concurrency import UserConcurrencyLimiter
```

to:

```python
from serving.servers.concurrency import UserConcurrencyLimiter, static_limits_provider
```

Replace the helper on line 45-46:

```python
def _make_limiter() -> UserConcurrencyLimiter:
    return UserConcurrencyLimiter({"free": 1, "pro": 3, "internal": 10, "admin": 10})
```

with:

```python
def _make_limiter() -> UserConcurrencyLimiter:
    return UserConcurrencyLimiter(
        static_limits_provider({"free": 1, "pro": 3, "internal": 10, "admin": 10})
    )
```

- [ ] **Step 4: Run both files to verify they pass**

```bash
uv run pytest tests/servers/test_enforce_user_concurrency.py tests/servers/test_concurrency_endpoint.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add tests/servers/test_enforce_user_concurrency.py tests/servers/test_concurrency_endpoint.py
git commit -m "test(concurrency): adapt existing tests to LimitsProvider constructor"
```

---

## Task 5: Wire `RuntimeSettings` into bootstrap and remove the static dict

**Files:**
- Modify: `apps/backend/serving/servers/bootstrap.py`
- Modify: `apps/backend/serving/config/settings.py`
- Modify: `tests/servers/test_settings_role_rank.py`

This task swaps the limiter's source from the static dict to the live `RuntimeSettings` and reorders bootstrap so `RuntimeSettings` is initialized before the limiter.

- [ ] **Step 1: Update `tests/servers/test_settings_role_rank.py`**

Drop the now-irrelevant assertions about `USER_CONCURRENCY_LIMITS`. Change imports (currently lines 7-12) to:

```python
from serving.config.settings import (
    ROLE_RANK,
    VALID_ROLES,
    has_role,
)
```

Delete `test_user_concurrency_limits_values` (lines 43-49) and `test_user_concurrency_limits_keys_are_subset_of_roles` (lines 52-53).

Run the file to confirm it still passes after the deletions:

```bash
uv run pytest tests/servers/test_settings_role_rank.py -v
```

Expected: tests pass (only the role-rank assertions remain).

- [ ] **Step 2: Remove `USER_CONCURRENCY_LIMITS` from `settings.py`**

Delete lines 210-216 of `apps/backend/serving/config/settings.py`:

```python
# Per-user concurrency caps by role. Used by serving/servers/concurrency.py.
USER_CONCURRENCY_LIMITS: dict[str, int] = {
    "free": 1,
    "pro": 3,
    "internal": 10,
    "admin": 10,
}
```

(Keep the `ROLE_RANK`, `VALID_ROLES`, and `is_admin_email` declarations around it intact.)

- [ ] **Step 3: Update bootstrap to wire `RuntimeSettings` into the limiter**

In `apps/backend/serving/servers/bootstrap.py`:

a) Remove `USER_CONCURRENCY_LIMITS` from the import on line 20:

```python
from serving.config.settings import get_settings
```

b) Delete the early limiter init (currently lines 387-389):

```python
# Per-user concurrency limiter (always on; in-process)
user_concurrency_limiter = UserConcurrencyLimiter(USER_CONCURRENCY_LIMITS)
logger.info("User concurrency limiter initialized: %s", USER_CONCURRENCY_LIMITS)
```

c) Just before the `return AppServices(...)` block (currently line 450), after the `runtime_settings` block has run, insert:

```python
# Per-user concurrency limiter — reads live caps from RuntimeSettings so
# operators can tune them at runtime. Falls back to registry defaults
# when runtime_settings is unavailable (e.g., DB not configured).
from serving.servers.concurrency import static_limits_provider

if runtime_settings is not None:
    rt = runtime_settings  # capture for closure

    async def _read_concurrency_limits() -> dict[str, int]:
        return {
            "free":     await rt.get_int("user_concurrency_free"),
            "pro":      await rt.get_int("user_concurrency_pro"),
            "internal": await rt.get_int("user_concurrency_internal"),
            "admin":    await rt.get_int("user_concurrency_admin"),
        }

    user_concurrency_limiter = UserConcurrencyLimiter(_read_concurrency_limits)
    logger.info("User concurrency limiter initialized (runtime-tunable)")
else:
    user_concurrency_limiter = UserConcurrencyLimiter(
        static_limits_provider({"free": 3, "pro": 3, "internal": 10, "admin": 10})
    )
    logger.warning(
        "User concurrency limiter initialized with static defaults "
        "(runtime_settings unavailable)"
    )
```

The `UserConcurrencyLimiter` import on line 29 is unchanged.

- [ ] **Step 4: Run the full backend test suite**

```bash
cd /home/juncheng/hybridInference-worktrees/adjustable-user-concurrency
uv run pytest tests/servers tests/unit tests/integration -x
```

Expected: all tests pass. Pay special attention to anything that imported `USER_CONCURRENCY_LIMITS`; the only known consumers (bootstrap, test_settings_role_rank) have been updated.

- [ ] **Step 5: Commit**

```bash
git add \
    apps/backend/serving/config/settings.py \
    apps/backend/serving/servers/bootstrap.py \
    tests/servers/test_settings_role_rank.py
git commit -m "feat(bootstrap): wire RuntimeSettings into UserConcurrencyLimiter"
```

---

## Task 6: Manual smoke + ruff format

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite once more**

```bash
cd /home/juncheng/hybridInference-worktrees/adjustable-user-concurrency
uv run pytest -x
```

Expected: all tests pass.

- [ ] **Step 2: Run `ruff format`**

```bash
uv run ruff format apps/backend tests
uv run ruff check apps/backend tests
```

Expected: format applies any pending whitespace/quote changes; check reports no errors. If `ruff check` flags anything, fix it before continuing.

- [ ] **Step 3: Commit any formatting changes**

```bash
git add -A
git diff --cached --quiet || git commit -m "style: ruff format"
```

The `git diff --cached --quiet` short-circuit avoids creating an empty commit when there are no formatting changes.

---

## Self-Review Checklist (writer)

- [x] **Spec coverage:** every spec section maps to a task —
      Registry & defaults → Tasks 1+2; Default change → Task 2 (`default: 3`);
      Validation → Task 1; Limiter refactor → Task 3; Lazy slot resize → Task 3;
      Bootstrap wiring → Task 5; Cache invalidation → no-op (existing
      `invalidate_key` already covers it); Tests → Tasks 1, 2, 3, 4.
- [x] **No placeholders:** every step contains either runnable code, a runnable
      command, or a precise textual description of a deletion.
- [x] **Type consistency:** `LimitsProvider`, `static_limits_provider`,
      `_FALLBACK_LIMITS`, and the four registry keys are spelled identically
      across tasks.
- [x] **Provider-error fallback:** spelled out in Task 3 and tested explicitly.
- [x] **Bootstrap ordering:** Task 5 explicitly moves the limiter init below
      `runtime_settings` init.
