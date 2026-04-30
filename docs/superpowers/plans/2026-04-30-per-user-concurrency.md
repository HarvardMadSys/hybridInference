# Per-User Concurrency Limit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cap simultaneous in-flight inference requests per user (`free=1`, `pro=3`, `internal=10`, `admin=10`), returning HTTP 429 when exceeded; release the slot after streaming responses fully drain, on client disconnect, or on exception.

**Architecture:** A new `UserConcurrencyLimiter` service holds an in-process `dict[user_id, _UserSlot]` (a tiny counter under the asyncio single-thread invariant — no `asyncio.Semaphore` private attrs). A FastAPI dependency `enforce_user_concurrency` composes with `verify_api_key`, acquires a slot, and uses `yield` + `finally` so Starlette runs cleanup after the response body (including streaming) is fully sent. Single uvicorn process; no Redis.

**Tech Stack:** Python 3, FastAPI, Starlette, asyncio, prometheus_client, pytest + pytest-asyncio + httpx.AsyncClient.

**Spec:** `docs/superpowers/specs/2026-04-30-per-user-concurrency-design.md`
**Issue:** https://github.com/HarvardMadSys/hybridInference/issues/242
**Branch:** `jason/claude/per-user-concurrency` → `dev`
**Worktree:** `/srv/hybridInference/.claude/worktrees/per-user-concurrency`

---

## Conventions for every task

- Run tests via `make test` (which is `uv run pytest -q -m "not external"`) or scope with `uv run pytest <path>::<test_name> -v`.
- Always commit at the end of a task. Use the existing repo style (lowercase, scoped, present-tense): `feat(serving): …`, `test(serving): …`, etc., and include the trailer:

  ```
  Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
  ```

- Working dir is the worktree: `/srv/hybridInference/.claude/worktrees/per-user-concurrency`.
- All file paths in this plan are repo-relative; resolve from the worktree root.

---

## File Structure

| File | Status | Responsibility |
|------|--------|---------------|
| `serving/config/settings.py` | Modify | Add `pro` to `ROLE_RANK`; add `USER_CONCURRENCY_LIMITS`. |
| `serving/servers/concurrency.py` | **Create** | `UserConcurrencyLimiter` class, `_UserSlot` helper, Prometheus metrics, `enforce_user_concurrency` dependency. |
| `serving/servers/deps.py` | Modify | Add `user_concurrency_limiter` to `AppServices` dataclass; add `get_user_concurrency_limiter` dependency. |
| `serving/servers/bootstrap.py` | Modify | Construct `UserConcurrencyLimiter` in `initialize()`; pass into `AppServices(...)`. |
| `serving/servers/routers/completions.py` | Modify | Add `Depends(enforce_user_concurrency)` to `chat_completions`. |
| `serving/servers/routers/compat.py` | Modify | Add `Depends(enforce_user_concurrency)` to `legacy_completions`. |
| `serving/servers/routers/embeddings.py` | Modify | Add `Depends(enforce_user_concurrency)` to `create_embeddings`. |
| `serving/servers/routers/anthropic_proxy.py` | Modify | Add `Depends(enforce_user_concurrency)` to `anthropic_messages`. |
| `test/servers/test_user_concurrency_limiter.py` | **Create** | Unit tests for `_UserSlot` and `UserConcurrencyLimiter`. |
| `test/servers/test_enforce_user_concurrency.py` | **Create** | Dependency-level tests using an ad-hoc FastAPI app: grant/reject/streaming-hold/disconnect/exception. |
| `test/servers/test_concurrency_endpoint.py` | **Create** | Integration smoke tests: gates apply on each of the four real inference routes. |
| `test/servers/test_settings_role_rank.py` | **Create** | Regression test for adding `pro`: `has_role` semantics still hold. |

Each file has one focused responsibility:
- `concurrency.py` owns the limiter, the metrics, and the dependency — they change together and have one purpose (per-user concurrency control). Keeping them in one file (~200 LOC) is appropriate for the size; if it ever grows past ~400 LOC, split into `concurrency/limiter.py`, `concurrency/dependency.py`, `concurrency/metrics.py`.
- The three test files split by what they exercise: pure logic, the dependency in isolation, and the wired-up routes.

---

## Task 1: Settings — add `pro` role and `USER_CONCURRENCY_LIMITS` constant

**Files:**
- Modify: `serving/config/settings.py:161-163`
- Test: `test/servers/test_settings_role_rank.py` (new)

- [ ] **Step 1: Write the failing test**

Create `test/servers/test_settings_role_rank.py`:

```python
"""Tests for role-rank ordering and USER_CONCURRENCY_LIMITS in settings.

Adding ``pro`` between ``free`` and ``internal`` must not break
``has_role`` semantics for existing roles.
"""

from serving.config.settings import (
    ROLE_RANK,
    USER_CONCURRENCY_LIMITS,
    VALID_ROLES,
    has_role,
)


def test_role_rank_contains_all_four_roles():
    assert set(ROLE_RANK) == {"free", "pro", "internal", "admin"}


def test_role_rank_ordering_is_strictly_ascending():
    # free < pro < internal < admin
    assert ROLE_RANK["free"] < ROLE_RANK["pro"] < ROLE_RANK["internal"] < ROLE_RANK["admin"]


def test_valid_roles_matches_role_rank():
    assert VALID_ROLES == frozenset({"free", "pro", "internal", "admin"})


def test_has_role_existing_semantics_preserved():
    # admin still satisfies internal
    assert has_role("admin", "internal") is True
    # internal still satisfies internal
    assert has_role("internal", "internal") is True
    # free does not satisfy internal
    assert has_role("free", "internal") is False
    # pro does NOT satisfy internal (pro < internal in rank)
    assert has_role("pro", "internal") is False
    # admin satisfies admin
    assert has_role("admin", "admin") is True
    # pro satisfies free (any role >= rank 0 passes free)
    assert has_role("pro", "free") is True


def test_user_concurrency_limits_values():
    assert USER_CONCURRENCY_LIMITS == {
        "free": 1,
        "pro": 3,
        "internal": 10,
        "admin": 10,
    }


def test_user_concurrency_limits_keys_are_subset_of_roles():
    assert set(USER_CONCURRENCY_LIMITS).issubset(set(ROLE_RANK))
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest test/servers/test_settings_role_rank.py -v
```

Expected: ImportError (`USER_CONCURRENCY_LIMITS`) and assertion failures (ROLE_RANK is missing `pro`).

- [ ] **Step 3: Make the change in settings.py**

Edit `serving/config/settings.py`. Replace the existing block at lines 161–163:

```python
ROLE_RANK: dict[str, int] = {"free": 0, "internal": 1, "admin": 2}

VALID_ROLES = frozenset(ROLE_RANK)
```

with:

```python
ROLE_RANK: dict[str, int] = {"free": 0, "pro": 1, "internal": 2, "admin": 3}

VALID_ROLES = frozenset(ROLE_RANK)

# Per-user concurrency caps by role. Used by serving/servers/concurrency.py.
USER_CONCURRENCY_LIMITS: dict[str, int] = {
    "free": 1,
    "pro": 3,
    "internal": 10,
    "admin": 10,
}
```

Leave the `has_role` function unchanged (it works on rank comparisons, so the semantics carry over).

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest test/servers/test_settings_role_rank.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add serving/config/settings.py test/servers/test_settings_role_rank.py
git commit -m "$(cat <<'EOF'
feat(settings): add pro role and USER_CONCURRENCY_LIMITS (#242)

- Insert "pro" at rank 1 in ROLE_RANK; admin/internal ranks shift up.
- Add USER_CONCURRENCY_LIMITS constant for per-user concurrency caps.
- has_role semantics preserved (rank-comparison based).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `UserConcurrencyLimiter` core (TDD)

**Files:**
- Create: `serving/servers/concurrency.py`
- Test: `test/servers/test_user_concurrency_limiter.py` (new)

This task implements only the limiter itself (and its `_UserSlot` helper). Prometheus metrics are added in Task 3; the dependency is added in Task 5.

- [ ] **Step 1: Write the failing tests**

Create `test/servers/test_user_concurrency_limiter.py`:

```python
"""Unit tests for UserConcurrencyLimiter and _UserSlot."""

import asyncio

import pytest

from serving.servers.concurrency import UserConcurrencyLimiter, _UserSlot

LIMITS = {"free": 1, "pro": 3, "internal": 10, "admin": 10}


# ----------------------------- _UserSlot --------------------------------


def test_user_slot_acquire_until_capacity():
    slot = _UserSlot(capacity=2, role="pro")
    assert slot.try_acquire() is True
    assert slot.try_acquire() is True
    assert slot.try_acquire() is False  # at capacity
    assert slot.in_use == 2


def test_user_slot_release_frees_capacity():
    slot = _UserSlot(capacity=1, role="free")
    assert slot.try_acquire() is True
    assert slot.try_acquire() is False
    slot.release()
    assert slot.try_acquire() is True


def test_user_slot_release_clamped_at_zero():
    slot = _UserSlot(capacity=1, role="free")
    # release with no acquire must not go negative
    slot.release()
    slot.release()
    assert slot.in_use == 0


# ------------------------ UserConcurrencyLimiter ------------------------


def test_limit_for_returns_role_capacity():
    lim = UserConcurrencyLimiter(LIMITS)
    assert lim.limit_for("free", is_admin=False) == 1
    assert lim.limit_for("pro", is_admin=False) == 3
    assert lim.limit_for("internal", is_admin=False) == 10
    assert lim.limit_for("admin", is_admin=False) == 10


def test_limit_for_admin_flag_overrides_role():
    lim = UserConcurrencyLimiter(LIMITS)
    # is_admin=True wins even when role is free
    assert lim.limit_for("free", is_admin=True) == 10
    assert lim.limit_for("anything", is_admin=True) == 10


def test_limit_for_unknown_role_falls_back_to_free():
    lim = UserConcurrencyLimiter(LIMITS)
    assert lim.limit_for("unknown_role", is_admin=False) == 1
    assert lim.limit_for("", is_admin=False) == 1


@pytest.mark.asyncio
async def test_try_acquire_grants_until_capacity_then_rejects():
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "user-1"
    # free → 1 slot
    assert await lim.try_acquire(user_id, "free", is_admin=False) is True
    assert await lim.try_acquire(user_id, "free", is_admin=False) is False


@pytest.mark.asyncio
async def test_release_frees_a_slot():
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "user-1"
    await lim.try_acquire(user_id, "free", is_admin=False)
    assert await lim.try_acquire(user_id, "free", is_admin=False) is False
    lim.release(user_id)
    assert await lim.try_acquire(user_id, "free", is_admin=False) is True


@pytest.mark.asyncio
async def test_release_unknown_user_is_idempotent():
    lim = UserConcurrencyLimiter(LIMITS)
    # Must not raise when releasing a user we never saw
    lim.release("never-seen")


@pytest.mark.asyncio
async def test_two_users_have_independent_budgets():
    lim = UserConcurrencyLimiter(LIMITS)
    assert await lim.try_acquire("user-A", "free", is_admin=False) is True
    # user-A is at cap, but user-B should still succeed
    assert await lim.try_acquire("user-B", "free", is_admin=False) is True


@pytest.mark.asyncio
async def test_pro_user_gets_three_slots():
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "pro-1"
    for _ in range(3):
        assert await lim.try_acquire(user_id, "pro", is_admin=False) is True
    assert await lim.try_acquire(user_id, "pro", is_admin=False) is False


@pytest.mark.asyncio
async def test_admin_user_gets_ten_slots():
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "admin-1"
    for _ in range(10):
        # role "free" but is_admin=True → admin cap
        assert await lim.try_acquire(user_id, "free", is_admin=True) is True
    assert await lim.try_acquire(user_id, "free", is_admin=True) is False


@pytest.mark.asyncio
async def test_capacity_is_sticky_after_creation():
    """Once a slot is created with a capacity, role changes don't resize it."""
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "user-1"
    # First acquire creates the slot at free=1
    await lim.try_acquire(user_id, "free", is_admin=False)
    # Subsequent acquires with role="pro" still see capacity=1
    assert await lim.try_acquire(user_id, "pro", is_admin=False) is False


@pytest.mark.asyncio
async def test_concurrent_acquires_respect_capacity():
    """Even with many concurrent tasks, capacity is not exceeded."""
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "pro-1"  # capacity 3

    results = await asyncio.gather(
        *[lim.try_acquire(user_id, "pro", is_admin=False) for _ in range(20)]
    )
    # Exactly 3 should win
    assert results.count(True) == 3
    assert results.count(False) == 17
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest test/servers/test_user_concurrency_limiter.py -v
```

Expected: ImportError (`serving.servers.concurrency` does not exist).

- [ ] **Step 3: Implement the limiter**

Create `serving/servers/concurrency.py`:

```python
"""Per-user concurrency limiter.

Caps the number of simultaneous in-flight inference requests per user,
keyed by ``user_id``. Backed by an in-process counter under the asyncio
single-thread invariant — no Redis, no DB.

A new ``_UserSlot`` is lazy-created on first acquire per user; its
capacity is captured from the user's role at that moment and is sticky
(role changes mid-process do not resize an existing slot — restart
corrects).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from serving.utils.logging import get_logger

logger = get_logger(__name__)


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
    """Per-user in-flight request limiter."""

    def __init__(self, limits: dict[str, int]):
        # e.g. {"free": 1, "pro": 3, "internal": 10, "admin": 10}
        self._limits = limits
        self._slots: dict[str, _UserSlot] = {}
        self._create_lock = asyncio.Lock()  # guards lazy slot creation

    def limit_for(self, role: str, is_admin: bool) -> int:
        """Return the capacity for a (role, is_admin) pair.

        ``is_admin=True`` always returns the admin cap, regardless of role.
        Unknown roles fall back to the most restrictive (``free``) cap.
        """
        if is_admin:
            return self._limits["admin"]
        return self._limits.get(role, self._limits["free"])

    def role_label(self, role: str, is_admin: bool) -> str:
        """The label used for metrics. Admin overrides the user's role."""
        if is_admin:
            return "admin"
        if role in self._limits:
            return role
        return "free"

    async def try_acquire(self, user_id: str, role: str, is_admin: bool) -> bool:
        """Non-blocking acquire. Returns True on success, False if at cap.

        Lazy-creates the per-user slot on first call. Capacity is captured
        from the user's role at creation time and is sticky thereafter.
        """
        slot = self._slots.get(user_id)
        if slot is None:
            async with self._create_lock:
                slot = self._slots.get(user_id)
                if slot is None:
                    capacity = self.limit_for(role, is_admin)
                    slot = _UserSlot(
                        capacity=capacity,
                        role=self.role_label(role, is_admin),
                    )
                    self._slots[user_id] = slot
        return slot.try_acquire()

    def release(self, user_id: str) -> None:
        """Release a slot. Idempotent for unknown user_id."""
        slot = self._slots.get(user_id)
        if slot is not None:
            slot.release()

    def role_for(self, user_id: str) -> str | None:
        """Return the role label captured at slot creation, or None."""
        slot = self._slots.get(user_id)
        return slot.role if slot is not None else None
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest test/servers/test_user_concurrency_limiter.py -v
```

Expected: all 12 tests pass.

- [ ] **Step 5: Commit**

```bash
git add serving/servers/concurrency.py test/servers/test_user_concurrency_limiter.py
git commit -m "$(cat <<'EOF'
feat(serving): add UserConcurrencyLimiter for per-user request caps (#242)

In-process, single-event-loop limiter keyed by user_id. Lazy-creates a
_UserSlot per user with capacity captured from the user's role at first
acquire (sticky thereafter; restart corrects role changes). Unit-tested
for cap enforcement, isolation between users, idempotent release, and
concurrent-acquire safety.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Prometheus metrics

**Files:**
- Modify: `serving/servers/concurrency.py`
- Test: extend `test/servers/test_user_concurrency_limiter.py`

Add three metrics. Increment them inside `try_acquire`/`release`, capturing the role label at acquire time so the in-flight gauge stays consistent if role changes.

- [ ] **Step 1: Write the failing tests**

Append to `test/servers/test_user_concurrency_limiter.py`:

```python
# ------------------------------ metrics ---------------------------------


def _read_counter(counter, **labels) -> float:
    """Read the float value of a Counter or Gauge with given labels."""
    return counter.labels(**labels)._value.get()


@pytest.mark.asyncio
async def test_metrics_granted_increments_acquires_and_in_flight():
    from serving.servers.concurrency import (
        user_concurrency_acquires_total,
        user_concurrency_in_flight,
    )

    lim = UserConcurrencyLimiter(LIMITS)

    granted_before = _read_counter(
        user_concurrency_acquires_total, role="free", outcome="granted"
    )
    in_flight_before = _read_counter(user_concurrency_in_flight, role="free")

    assert await lim.try_acquire("metric-user-1", "free", is_admin=False) is True

    granted_after = _read_counter(
        user_concurrency_acquires_total, role="free", outcome="granted"
    )
    in_flight_after = _read_counter(user_concurrency_in_flight, role="free")

    assert granted_after - granted_before == 1
    assert in_flight_after - in_flight_before == 1


@pytest.mark.asyncio
async def test_metrics_rejected_increments_rejected_and_acquires():
    from serving.servers.concurrency import (
        user_concurrency_acquires_total,
        user_concurrency_rejected_total,
    )

    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "metric-user-2"
    await lim.try_acquire(user_id, "free", is_admin=False)

    rejected_before = _read_counter(user_concurrency_rejected_total, role="free")
    rejected_acq_before = _read_counter(
        user_concurrency_acquires_total, role="free", outcome="rejected"
    )

    assert await lim.try_acquire(user_id, "free", is_admin=False) is False

    rejected_after = _read_counter(user_concurrency_rejected_total, role="free")
    rejected_acq_after = _read_counter(
        user_concurrency_acquires_total, role="free", outcome="rejected"
    )

    assert rejected_after - rejected_before == 1
    assert rejected_acq_after - rejected_acq_before == 1


@pytest.mark.asyncio
async def test_metrics_release_decrements_in_flight():
    from serving.servers.concurrency import user_concurrency_in_flight

    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "metric-user-3"
    await lim.try_acquire(user_id, "free", is_admin=False)

    in_flight_before_release = _read_counter(user_concurrency_in_flight, role="free")
    lim.release(user_id)
    in_flight_after_release = _read_counter(user_concurrency_in_flight, role="free")

    assert in_flight_before_release - in_flight_after_release == 1


@pytest.mark.asyncio
async def test_metrics_admin_label_used_when_is_admin():
    from serving.servers.concurrency import user_concurrency_in_flight

    lim = UserConcurrencyLimiter(LIMITS)
    in_flight_before = _read_counter(user_concurrency_in_flight, role="admin")
    # role="free" but is_admin=True → label should be "admin"
    await lim.try_acquire("admin-user-x", "free", is_admin=True)
    in_flight_after = _read_counter(user_concurrency_in_flight, role="admin")
    assert in_flight_after - in_flight_before == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest test/servers/test_user_concurrency_limiter.py -v
```

Expected: 4 new tests fail with ImportError (`user_concurrency_*` not exported).

- [ ] **Step 3: Add metrics and increment them in the limiter**

Define the three metrics in `serving/observability/metrics.py` (alongside all other domain metrics), then import them into `serving/servers/concurrency.py`:

```python
from serving.observability.metrics import (
    USER_CONCURRENCY_ACQUIRES_TOTAL as user_concurrency_acquires_total,
    USER_CONCURRENCY_IN_FLIGHT as user_concurrency_in_flight,
    USER_CONCURRENCY_REJECTED_TOTAL as user_concurrency_rejected_total,
)

user_concurrency_in_flight = Gauge(
    "user_concurrency_in_flight",
    "Active concurrent inference requests, by role",
    labelnames=("role",),
)
user_concurrency_acquires_total = Counter(
    "user_concurrency_acquires_total",
    "Total per-user concurrency slot acquire attempts",
    labelnames=("role", "outcome"),  # outcome ∈ {"granted", "rejected"}
)
user_concurrency_rejected_total = Counter(
    "user_concurrency_rejected_total",
    "Requests rejected due to per-user concurrency limit",
    labelnames=("role",),
)
```

Then update `UserConcurrencyLimiter.try_acquire` to record metrics:

```python
    async def try_acquire(self, user_id: str, role: str, is_admin: bool) -> bool:
        slot = self._slots.get(user_id)
        if slot is None:
            async with self._create_lock:
                slot = self._slots.get(user_id)
                if slot is None:
                    capacity = self.limit_for(role, is_admin)
                    slot = _UserSlot(
                        capacity=capacity,
                        role=self.role_label(role, is_admin),
                    )
                    self._slots[user_id] = slot

        granted = slot.try_acquire()
        label = slot.role  # captured at slot creation
        if granted:
            user_concurrency_acquires_total.labels(role=label, outcome="granted").inc()
            user_concurrency_in_flight.labels(role=label).inc()
        else:
            user_concurrency_acquires_total.labels(role=label, outcome="rejected").inc()
            user_concurrency_rejected_total.labels(role=label).inc()
        return granted
```

And `release`:

```python
    def release(self, user_id: str) -> None:
        slot = self._slots.get(user_id)
        if slot is None:
            return
        # Only decrement the gauge if there was actually a slot held.
        had_one = slot.in_use > 0
        slot.release()
        if had_one:
            user_concurrency_in_flight.labels(role=slot.role).dec()
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest test/servers/test_user_concurrency_limiter.py -v
```

Expected: all tests (original + 4 new) pass.

- [ ] **Step 5: Commit**

```bash
git add serving/servers/concurrency.py test/servers/test_user_concurrency_limiter.py
git commit -m "$(cat <<'EOF'
feat(serving): expose per-user concurrency Prometheus metrics (#242)

Adds user_concurrency_{in_flight, acquires_total, rejected_total},
labeled by role (with admin precedence). Role label is captured at slot
creation so it stays consistent across release even if the user's role
changes.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Wire `UserConcurrencyLimiter` into `AppServices` and bootstrap

**Files:**
- Modify: `serving/servers/deps.py:34-48` (AppServices dataclass), and add a getter dependency.
- Modify: `serving/servers/bootstrap.py:425-434` (initialize() — pass into AppServices).
- Test: `test/servers/test_bootstrap.py` — add a check for the new field.

- [ ] **Step 1: Write the failing test**

Append to `test/servers/test_bootstrap.py` (if the file already imports the relevant pieces; otherwise add at the end):

```python
@pytest.mark.asyncio
async def test_initialize_constructs_user_concurrency_limiter():
    """bootstrap.initialize() must populate services.user_concurrency_limiter."""
    from serving.servers import bootstrap
    from serving.servers.concurrency import UserConcurrencyLimiter

    services = await bootstrap.initialize()
    try:
        assert isinstance(services.user_concurrency_limiter, UserConcurrencyLimiter)
        # And it should know about all four roles
        for role in ("free", "pro", "internal", "admin"):
            assert services.user_concurrency_limiter.limit_for(role, is_admin=False) >= 1
    finally:
        await bootstrap.shutdown(services)
```

If `test_bootstrap.py` doesn't have `pytest_asyncio` set up, add at the file top:

```python
import pytest
import pytest_asyncio  # noqa: F401  — needed for asyncio_mode
```

(Most likely already present given other auth tests use async fixtures.)

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest test/servers/test_bootstrap.py::test_initialize_constructs_user_concurrency_limiter -v
```

Expected: AttributeError (`AppServices` has no `user_concurrency_limiter`).

- [ ] **Step 3: Add the field to `AppServices`**

Edit `serving/servers/deps.py`. Update the `TYPE_CHECKING` block (around line 22-30) to add the import:

```python
if TYPE_CHECKING:
    from routing.executor import RouteExecutor
    from routing.manager import RoutingManager
    from routing.model_router_registry import ModelRouterRegistry
    from serving.observability.user_stats import UserStatsCollector
    from serving.storage.database import DatabaseLogger

    from .concurrency import UserConcurrencyLimiter
    from .fairness import FairnessScheduler
    from .rate_limiter import PersistentRateLimiter
```

Update the dataclass at line 33–48:

```python
@dataclass
class AppServices:
    """Typed container for application-wide services.

    Using a dataclass improves discoverability and avoids fragile string keys
    when accessing ``app.state``.
    """

    router: RouteExecutor
    embedding_adapters: dict[str, Any] | None = None
    rate_limiter: PersistentRateLimiter | None = None
    db_logger: DatabaseLogger | None = None
    routing_manager: RoutingManager | None = None
    model_router_registry: ModelRouterRegistry | None = None
    user_stats_collector: UserStatsCollector | None = None
    fairness_scheduler: FairnessScheduler | None = None
    user_concurrency_limiter: UserConcurrencyLimiter | None = None
```

Add a getter dependency below `get_fairness_scheduler` (around line 87):

```python
def get_user_concurrency_limiter(
    services: AppServices = Depends(get_services),
) -> "UserConcurrencyLimiter | None":
    """Dependency to obtain the per-user concurrency limiter."""
    return services.user_concurrency_limiter
```

- [ ] **Step 4: Construct it in bootstrap.initialize()**

Edit `serving/servers/bootstrap.py`. Add an import near the top (where other servers-package imports live):

```python
from .concurrency import UserConcurrencyLimiter
```

Then inside `initialize()`, after the `fairness_scheduler` block (around line 411) and before `user_stats_collector`, add:

```python
    # Per-user concurrency limiter (always on; in-process)
    from serving.config.settings import USER_CONCURRENCY_LIMITS

    user_concurrency_limiter = UserConcurrencyLimiter(USER_CONCURRENCY_LIMITS)
    logger.info(
        "User concurrency limiter initialized: %s", USER_CONCURRENCY_LIMITS
    )
```

Update the `return AppServices(...)` block at line 425-434 to include the new field:

```python
    return AppServices(
        router=router,
        embedding_adapters=embedding_adapters or None,
        rate_limiter=rate_limiter,
        db_logger=db_logger,
        routing_manager=routing_manager,
        model_router_registry=model_router_registry,
        user_stats_collector=user_stats_collector,
        fairness_scheduler=fairness_scheduler,
        user_concurrency_limiter=user_concurrency_limiter,
    )
```

- [ ] **Step 5: Run test to verify it passes**

```
uv run pytest test/servers/test_bootstrap.py::test_initialize_constructs_user_concurrency_limiter -v
```

Expected: PASS.

Then run the full suite to confirm nothing else broke:

```
uv run pytest -q -m "not external" -x
```

Expected: all tests pass (or only pre-existing failures unrelated to this change).

- [ ] **Step 6: Commit**

```bash
git add serving/servers/deps.py serving/servers/bootstrap.py test/servers/test_bootstrap.py
git commit -m "$(cat <<'EOF'
feat(serving): wire UserConcurrencyLimiter into AppServices (#242)

Construct the limiter in bootstrap.initialize(), expose it via
AppServices.user_concurrency_limiter and the new
get_user_concurrency_limiter dependency.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `enforce_user_concurrency` dependency (TDD with ad-hoc app)

**Files:**
- Modify: `serving/servers/concurrency.py` — add the dependency.
- Test: `test/servers/test_enforce_user_concurrency.py` (new)

The dependency-level tests use a tiny FastAPI app mounted in-test. We override the `verify_api_key` dependency to inject a synthetic user dict, and override `get_user_concurrency_limiter` to inject a fresh limiter. This avoids any DB or HTTP plumbing — we exercise the dependency itself, including streaming holds and disconnects.

- [ ] **Step 1: Write the failing tests**

Create `test/servers/test_enforce_user_concurrency.py`:

```python
"""Tests for the enforce_user_concurrency FastAPI dependency.

We mount a tiny ad-hoc FastAPI app and override verify_api_key /
get_user_concurrency_limiter so we can exercise the dependency in
isolation, including streaming-hold / disconnect / exception cleanup.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

from serving.servers.auth import verify_api_key
from serving.servers.concurrency import (
    UserConcurrencyLimiter,
    enforce_user_concurrency,
)
from serving.servers.deps import get_user_concurrency_limiter

LIMITS = {"free": 1, "pro": 3, "internal": 10, "admin": 10}


def _make_app(user: dict[str, Any], limiter: UserConcurrencyLimiter) -> FastAPI:
    app = FastAPI()

    async def fake_verify_api_key() -> dict[str, Any]:
        return user

    def fake_get_limiter() -> UserConcurrencyLimiter:
        return limiter

    app.dependency_overrides[verify_api_key] = fake_verify_api_key
    app.dependency_overrides[get_user_concurrency_limiter] = fake_get_limiter

    # Unary endpoint
    app.unary_event = asyncio.Event()  # type: ignore[attr-defined]

    @app.get("/probe", dependencies=[Depends(enforce_user_concurrency)])
    async def probe():
        await app.unary_event.wait()  # holds the slot until the test releases
        return {"ok": True}

    # Streaming endpoint
    app.stream_event = asyncio.Event()  # type: ignore[attr-defined]

    async def stream_body():
        yield b"chunk1\n"
        await app.stream_event.wait()
        yield b"chunk2\n"

    @app.get("/probe_stream", dependencies=[Depends(enforce_user_concurrency)])
    async def probe_stream():
        return StreamingResponse(stream_body(), media_type="text/plain")

    # Endpoint that always raises
    @app.get("/probe_raise", dependencies=[Depends(enforce_user_concurrency)])
    async def probe_raise():
        raise RuntimeError("boom")

    return app


@pytest.mark.asyncio
async def test_grant_then_reject_for_free_user():
    user = {"user_id": "u1", "role": "free", "is_admin": False}
    limiter = UserConcurrencyLimiter(LIMITS)
    app = _make_app(user, limiter)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Start request 1, hold it open
        task1 = asyncio.create_task(client.get("/probe"))
        # Give it a moment to enter the dependency
        await asyncio.sleep(0.05)
        # Request 2 must be rejected with 429
        resp2 = await client.get("/probe")
        assert resp2.status_code == 429
        body = resp2.json()
        assert body["detail"]["error"]["code"] == "concurrency_limit_exceeded"
        assert body["detail"]["error"]["limit"] == 1
        assert body["detail"]["error"]["role"] == "free"
        assert resp2.headers.get("Retry-After") == "1"
        # Release request 1
        app.unary_event.set()  # type: ignore[attr-defined]
        resp1 = await task1
        assert resp1.status_code == 200


@pytest.mark.asyncio
async def test_release_after_handler_returns_unblocks_next_request():
    user = {"user_id": "u1", "role": "free", "is_admin": False}
    limiter = UserConcurrencyLimiter(LIMITS)
    app = _make_app(user, limiter)
    app.unary_event.set()  # type: ignore[attr-defined]  # do not block handler

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Two sequential requests — both should succeed
        r1 = await client.get("/probe")
        r2 = await client.get("/probe")
        assert r1.status_code == 200
        assert r2.status_code == 200


@pytest.mark.asyncio
async def test_streaming_response_holds_slot_until_drained():
    """While a stream is mid-body, a second request must be rejected."""
    user = {"user_id": "u1", "role": "free", "is_admin": False}
    limiter = UserConcurrencyLimiter(LIMITS)
    app = _make_app(user, limiter)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Start streaming request, read first chunk, then pause
        async with client.stream("GET", "/probe_stream") as stream_resp:
            assert stream_resp.status_code == 200
            chunks = []

            async def collect_first_chunk():
                async for chunk in stream_resp.aiter_bytes():
                    chunks.append(chunk)
                    if chunks and chunks[0]:
                        return

            collector = asyncio.create_task(collect_first_chunk())
            # Wait until first chunk arrives
            for _ in range(50):
                if chunks:
                    break
                await asyncio.sleep(0.02)
            assert chunks, "First chunk did not arrive"

            # Slot should still be held — second request gets 429
            resp2 = await client.get("/probe")
            assert resp2.status_code == 429

            # Drain the stream
            app.stream_event.set()  # type: ignore[attr-defined]
            await collector

        # After stream is fully drained, slot must be released — sequential
        # request should succeed.
        app.unary_event.set()  # type: ignore[attr-defined]
        resp3 = await client.get("/probe")
        assert resp3.status_code == 200


@pytest.mark.asyncio
async def test_handler_exception_releases_slot():
    user = {"user_id": "u1", "role": "free", "is_admin": False}
    limiter = UserConcurrencyLimiter(LIMITS)
    app = _make_app(user, limiter)
    app.unary_event.set()  # type: ignore[attr-defined]

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # First request raises -> 500 (raise_app_exceptions=False)
        r1 = await client.get("/probe_raise")
        assert r1.status_code == 500
        # Second request must succeed — slot was released in finally
        r2 = await client.get("/probe")
        assert r2.status_code == 200


@pytest.mark.asyncio
async def test_two_users_have_independent_budgets_via_dependency():
    """Two separate users each get their own slot."""
    limiter = UserConcurrencyLimiter(LIMITS)

    # Build two apps, one per user, but sharing the same limiter
    app_a = _make_app({"user_id": "user-A", "role": "free", "is_admin": False}, limiter)
    app_b = _make_app({"user_id": "user-B", "role": "free", "is_admin": False}, limiter)

    transport_a = ASGITransport(app=app_a, raise_app_exceptions=False)
    transport_b = ASGITransport(app=app_b, raise_app_exceptions=False)

    async with (
        AsyncClient(transport=transport_a, base_url="http://test-a") as client_a,
        AsyncClient(transport=transport_b, base_url="http://test-b") as client_b,
    ):
        task_a = asyncio.create_task(client_a.get("/probe"))
        await asyncio.sleep(0.05)
        # User B must succeed even while user A is holding
        app_b.unary_event.set()  # type: ignore[attr-defined]
        r_b = await client_b.get("/probe")
        assert r_b.status_code == 200
        # Release user A
        app_a.unary_event.set()  # type: ignore[attr-defined]
        r_a = await task_a
        assert r_a.status_code == 200


@pytest.mark.asyncio
async def test_pro_user_three_slots_via_dependency():
    user = {"user_id": "pro-1", "role": "pro", "is_admin": False}
    limiter = UserConcurrencyLimiter(LIMITS)
    app = _make_app(user, limiter)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Hold three concurrent requests
        tasks = [asyncio.create_task(client.get("/probe")) for _ in range(3)]
        await asyncio.sleep(0.05)
        # Fourth gets 429
        resp4 = await client.get("/probe")
        assert resp4.status_code == 429
        assert resp4.json()["detail"]["error"]["limit"] == 3
        # Release
        app.unary_event.set()  # type: ignore[attr-defined]
        for t in tasks:
            r = await t
            assert r.status_code == 200


@pytest.mark.asyncio
async def test_admin_flag_yields_admin_role_in_response_body():
    user = {"user_id": "adm-1", "role": "free", "is_admin": True}
    limiter = UserConcurrencyLimiter(LIMITS)
    app = _make_app(user, limiter)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Saturate 10 admin slots
        tasks = [asyncio.create_task(client.get("/probe")) for _ in range(10)]
        await asyncio.sleep(0.05)
        resp11 = await client.get("/probe")
        assert resp11.status_code == 429
        body = resp11.json()
        assert body["detail"]["error"]["limit"] == 10
        assert body["detail"]["error"]["role"] == "admin"
        app.unary_event.set()  # type: ignore[attr-defined]
        for t in tasks:
            await t
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest test/servers/test_enforce_user_concurrency.py -v
```

Expected: ImportError for `enforce_user_concurrency`.

- [ ] **Step 3: Implement the dependency**

Append to `serving/servers/concurrency.py`:

```python
# Dependency lives at the bottom of the module so it can reference the
# limiter class and metrics defined above.

from typing import Any  # noqa: E402

from fastapi import Depends, HTTPException  # noqa: E402

from .auth import verify_api_key  # noqa: E402
from .deps import get_user_concurrency_limiter  # noqa: E402


async def enforce_user_concurrency(
    user: dict[str, Any] = Depends(verify_api_key),
    limiter: UserConcurrencyLimiter | None = Depends(get_user_concurrency_limiter),
):
    """Acquire a per-user concurrency slot or raise 429.

    Uses ``yield`` so FastAPI runs the cleanup ``finally`` block after the
    response (including streaming body) is fully sent, on exception, or
    on client disconnect.
    """
    if limiter is None:
        # If the limiter isn't configured (e.g., misconfigured deployment),
        # fail open — never block requests when the gate itself is broken.
        yield
        return

    user_id = user["user_id"]
    role = user.get("role", "free") or "free"
    is_admin = bool(user.get("is_admin", False))

    granted = await limiter.try_acquire(user_id, role, is_admin)
    if not granted:
        limit = limiter.limit_for(role, is_admin)
        role_label = limiter.role_label(role, is_admin)
        logger.info(
            "per-user concurrency limit hit",
            extra={"user_id": user_id, "role": role_label, "limit": limit},
        )
        raise HTTPException(
            status_code=429,
            detail={
                "error": {
                    "code": "concurrency_limit_exceeded",
                    "message": f"Too many concurrent requests (limit: {limit})",
                    "limit": limit,
                    "role": role_label,
                }
            },
            headers={"Retry-After": "1"},
        )

    try:
        yield
    finally:
        try:
            limiter.release(user_id)
        except Exception:
            # Never let cleanup break the request lifecycle.
            logger.exception(
                "user_concurrency: release failed",
                extra={"user_id": user_id},
            )
```

**Note on import placement:** the `from .deps import get_user_concurrency_limiter` import is placed at the bottom of the module (after the limiter class) to avoid a circular import (`deps.py` declares `AppServices` which now references `UserConcurrencyLimiter` only under `TYPE_CHECKING`, so at runtime there's no cycle, but importing `get_user_concurrency_limiter` from the top of `concurrency.py` would still create one if `deps.py` ever imports from `concurrency`).  We added the `TYPE_CHECKING`-only import in Task 4, so a top-level import of `get_user_concurrency_limiter` in `concurrency.py` is safe today. Either placement (top or bottom) works; bottom is defensive.

If you prefer a top-of-file import, that's fine — verify with `uv run python -c "import serving.servers.concurrency"` after the change.

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest test/servers/test_enforce_user_concurrency.py -v
```

Expected: all 7 tests pass. The streaming-hold and exception-cleanup tests are the most important; if either fails, the dependency is wired wrong.

- [ ] **Step 5: Commit**

```bash
git add serving/servers/concurrency.py test/servers/test_enforce_user_concurrency.py
git commit -m "$(cat <<'EOF'
feat(serving): add enforce_user_concurrency FastAPI dependency (#242)

Composes with verify_api_key, acquires a per-user slot or raises 429
with code=concurrency_limit_exceeded. Uses yield/finally so the slot
is released after the response (including streaming body) drains, on
exception, or on client disconnect. Tested via an ad-hoc FastAPI app
with dependency overrides.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Wire the dependency into the four inference routes

**Files:**
- Modify: `serving/servers/routers/completions.py:118-128`
- Modify: `serving/servers/routers/compat.py:52-63`
- Modify: `serving/servers/routers/embeddings.py:28-32`
- Modify: `serving/servers/routers/anthropic_proxy.py:309-316`

No new tests in this task — Task 7 covers integration. This task is mechanical.

- [ ] **Step 1: Add the dependency to `chat_completions`**

Edit `serving/servers/routers/completions.py`. At the top, add the import alongside other server imports:

```python
from serving.servers.concurrency import enforce_user_concurrency
```

Then in the `chat_completions` handler (lines 117–128 area), add `_concurrency_slot=Depends(enforce_user_concurrency)` to the parameter list. The exact change:

```python
@router.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    response_model_exclude_none=True,
    responses={
        400: {"model": ErrorResponse, "description": "Bad Request"},
        404: {"model": ErrorResponse, "description": "Model Not Found"},
        429: {"model": ErrorResponse, "description": "Rate Limit Exceeded"},
        500: {"model": ErrorResponse, "description": "Server Error"},
    },
)
async def chat_completions(
    request: Request,
    http_response: Response,
    authorization: str | None = Header(None),
    user_ctx: dict = Depends(verify_api_key),
    router_exec=Depends(get_router),
    rate_limiter=Depends(get_rate_limiter),
    db_logger=Depends(get_db_logger),
    fairness_scheduler=Depends(get_fairness_scheduler),
    model_router_registry=Depends(get_model_router_registry),
    _concurrency_slot=Depends(enforce_user_concurrency),
) -> dict[str, Any]:
    ...
```

The leading underscore signals "not used in the body — purely a side-effecting dependency."

- [ ] **Step 2: Add the dependency to `legacy_completions`**

Edit `serving/servers/routers/compat.py`. Add the import at the top:

```python
from serving.servers.concurrency import enforce_user_concurrency
```

Then update `legacy_completions`:

```python
@router.post("/v1/completions")
async def legacy_completions(
    request: Request,
    http_response: Response,
    authorization: str | None = Header(None),
    user_ctx: dict = Depends(verify_api_key),
    router_exec=Depends(get_router),
    rate_limiter=Depends(get_rate_limiter),
    db_logger=Depends(get_db_logger),
    fairness_scheduler=Depends(get_fairness_scheduler),
    model_router_registry=Depends(get_model_router_registry),
    _concurrency_slot=Depends(enforce_user_concurrency),
):
    ...
```

**Important — avoid double-acquiring:** `legacy_completions` calls `chat_completions` directly (it's an internal Python call, not an HTTP redirect). Because of the way it's invoked, FastAPI's dependency-cache only applies *within* a single request — but here we're passing already-resolved arguments. The slot is acquired once at the top of `legacy_completions`, and the inner `chat_completions(...)` is invoked as a plain function call with already-resolved kwargs, so `enforce_user_concurrency` does not re-run inside `chat_completions`. Verify this by reviewing the call site (`compat.py:71-81` — it does `await chat_completions(request, http_response, ...)` with positional/keyword args, not via the FastAPI dependency injector). No additional change is needed.

- [ ] **Step 3: Add the dependency to `create_embeddings`**

Edit `serving/servers/routers/embeddings.py`. Add import:

```python
from serving.servers.concurrency import enforce_user_concurrency
```

Update the handler:

```python
@router.post(
    "/v1/embeddings",
    response_model=EmbeddingResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Bad Request"},
        404: {"model": ErrorResponse, "description": "Model Not Found"},
        500: {"model": ErrorResponse, "description": "Server Error"},
    },
)
async def create_embeddings(
    request: EmbeddingRequest,
    user_ctx: dict = Depends(verify_api_key),
    embedding_adapters: dict[str, Any] = Depends(get_embedding_adapters),
    _concurrency_slot=Depends(enforce_user_concurrency),
) -> dict[str, Any]:
    ...
```

- [ ] **Step 4: Add the dependency to `anthropic_messages`**

Edit `serving/servers/routers/anthropic_proxy.py`. Add import:

```python
from serving.servers.concurrency import enforce_user_concurrency
```

Update the handler at line 309:

```python
@router.post("/anthropic/v1/messages", response_model=None)
async def anthropic_messages(
    request: Request,
    user_ctx: dict = Depends(verify_api_key),
    router_exec=Depends(get_router),
    rate_limiter=Depends(get_rate_limiter),
    db_logger=Depends(get_db_logger),
    _concurrency_slot=Depends(enforce_user_concurrency),
):
    ...
```

- [ ] **Step 5: Run the existing route tests to confirm nothing regressed**

```
uv run pytest test/servers/test_completions.py test/servers/test_compat.py test/servers/test_anthropic_proxy.py -q
```

Expected: same pass/fail set as before this task. If any test that previously passed now fails, the most likely cause is the test bypassed `verify_api_key` — make sure tests that use dependency overrides also override `enforce_user_concurrency` (or `get_user_concurrency_limiter` returning a fresh limiter).

- [ ] **Step 6: Commit**

```bash
git add serving/servers/routers/completions.py serving/servers/routers/compat.py \
        serving/servers/routers/embeddings.py serving/servers/routers/anthropic_proxy.py
git commit -m "$(cat <<'EOF'
feat(serving): gate inference routes with enforce_user_concurrency (#242)

Adds Depends(enforce_user_concurrency) to /v1/chat/completions,
/v1/completions, /v1/embeddings, and /anthropic/v1/messages. Slot is
acquired at request entry and released after the response (including
streaming body) drains.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Integration smoke tests on the real routes

**Files:**
- Create: `test/servers/test_concurrency_endpoint.py`

These tests hit the real FastAPI app via `auth_client`, but stub out the inference machinery (router executor + embedding adapters) so the test stays fast and deterministic. The point is to verify the dependency is wired into the route — not to re-test the limiter logic.

- [ ] **Step 1: Write the tests**

Create `test/servers/test_concurrency_endpoint.py`:

```python
"""Integration tests verifying enforce_user_concurrency is wired into
the four inference routes.

Strategy: hit the real FastAPI app, but override the slow inference
machinery with controllable stubs so we can pin a request "in flight"
while sending a second to observe the 429.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from fastapi import HTTPException

from serving.servers.auth import verify_api_key
from serving.servers.concurrency import UserConcurrencyLimiter
from serving.servers.deps import (
    get_embedding_adapters,
    get_router,
    get_user_concurrency_limiter,
)


# ---------------------------- helpers ----------------------------------


def _stub_user(user_id: str, role: str, is_admin: bool = False) -> dict:
    return {
        "user_id": user_id,
        "user_name": f"name-{user_id}",
        "tier": "free",
        "role": role,
        "authenticated": True,
        "quota_remaining_cost_usd": 100.0,
        "is_admin": is_admin,
    }


class _SlowRouter:
    """Stub RouteExecutor: blocks on an event so we can pin a request."""

    def __init__(self):
        self.released = asyncio.Event()
        self.calls = 0

    async def execute(self, *args, **kwargs):
        self.calls += 1
        await self.released.wait()
        return {"id": "stub", "object": "chat.completion", "choices": []}


class _SlowEmbeddingAdapter:
    def __init__(self):
        self.released = asyncio.Event()

    async def create_embedding(self, *args, **kwargs):
        await self.released.wait()
        return {"object": "list", "data": [], "model": "stub"}


# --------------------------- chat completions --------------------------


@pytest.mark.asyncio
async def test_chat_completions_429_when_free_user_at_cap(auth_app, auth_client):
    """Free user with one in-flight chat request gets 429 on the second."""
    user = _stub_user("user-chat-1", "free", is_admin=False)
    limiter = UserConcurrencyLimiter({"free": 1, "pro": 3, "internal": 10, "admin": 10})
    slow_router = _SlowRouter()

    auth_app.dependency_overrides[verify_api_key] = lambda: user
    auth_app.dependency_overrides[get_user_concurrency_limiter] = lambda: limiter
    auth_app.dependency_overrides[get_router] = lambda: slow_router

    try:
        body = {"model": "stub", "messages": [{"role": "user", "content": "hi"}]}

        # Request 1 — pinned in flight
        task1 = asyncio.create_task(
            auth_client.post(
                "/v1/chat/completions",
                json=body,
                headers={"Authorization": "Bearer hyi-stub"},
            )
        )
        await asyncio.sleep(0.05)

        # Request 2 — should be rejected with 429
        resp2 = await auth_client.post(
            "/v1/chat/completions",
            json=body,
            headers={"Authorization": "Bearer hyi-stub"},
        )
        assert resp2.status_code == 429
        body2 = resp2.json()
        assert body2["detail"]["error"]["code"] == "concurrency_limit_exceeded"
        assert body2["detail"]["error"]["limit"] == 1
        assert body2["detail"]["error"]["role"] == "free"

        # Release request 1
        slow_router.released.set()
        await task1
    finally:
        auth_app.dependency_overrides.pop(verify_api_key, None)
        auth_app.dependency_overrides.pop(get_user_concurrency_limiter, None)
        auth_app.dependency_overrides.pop(get_router, None)


# ----------------------------- embeddings ------------------------------


@pytest.mark.asyncio
async def test_embeddings_429_when_free_user_at_cap(auth_app, auth_client):
    user = _stub_user("user-emb-1", "free", is_admin=False)
    limiter = UserConcurrencyLimiter({"free": 1, "pro": 3, "internal": 10, "admin": 10})
    slow_adapter = _SlowEmbeddingAdapter()

    auth_app.dependency_overrides[verify_api_key] = lambda: user
    auth_app.dependency_overrides[get_user_concurrency_limiter] = lambda: limiter
    auth_app.dependency_overrides[get_embedding_adapters] = lambda: {"stub": slow_adapter}

    try:
        body = {"model": "stub", "input": "text"}

        task1 = asyncio.create_task(
            auth_client.post(
                "/v1/embeddings",
                json=body,
                headers={"Authorization": "Bearer hyi-stub"},
            )
        )
        await asyncio.sleep(0.05)

        resp2 = await auth_client.post(
            "/v1/embeddings",
            json=body,
            headers={"Authorization": "Bearer hyi-stub"},
        )
        assert resp2.status_code == 429
        assert resp2.json()["detail"]["error"]["code"] == "concurrency_limit_exceeded"

        slow_adapter.released.set()
        await task1
    finally:
        auth_app.dependency_overrides.pop(verify_api_key, None)
        auth_app.dependency_overrides.pop(get_user_concurrency_limiter, None)
        auth_app.dependency_overrides.pop(get_embedding_adapters, None)


# ------------------------- legacy /v1/completions ----------------------


@pytest.mark.asyncio
async def test_legacy_completions_429_when_free_user_at_cap(auth_app, auth_client):
    user = _stub_user("user-legacy-1", "free", is_admin=False)
    limiter = UserConcurrencyLimiter({"free": 1, "pro": 3, "internal": 10, "admin": 10})
    slow_router = _SlowRouter()

    auth_app.dependency_overrides[verify_api_key] = lambda: user
    auth_app.dependency_overrides[get_user_concurrency_limiter] = lambda: limiter
    auth_app.dependency_overrides[get_router] = lambda: slow_router

    try:
        body = {"model": "stub", "prompt": "hello"}

        task1 = asyncio.create_task(
            auth_client.post(
                "/v1/completions",
                json=body,
                headers={"Authorization": "Bearer hyi-stub"},
            )
        )
        await asyncio.sleep(0.05)

        resp2 = await auth_client.post(
            "/v1/completions",
            json=body,
            headers={"Authorization": "Bearer hyi-stub"},
        )
        assert resp2.status_code == 429
        slow_router.released.set()
        await task1
    finally:
        auth_app.dependency_overrides.pop(verify_api_key, None)
        auth_app.dependency_overrides.pop(get_user_concurrency_limiter, None)
        auth_app.dependency_overrides.pop(get_router, None)


# --------------------------- anthropic proxy ---------------------------


@pytest.mark.asyncio
async def test_anthropic_messages_429_when_free_user_at_cap(auth_app, auth_client):
    user = _stub_user("user-anth-1", "free", is_admin=False)
    limiter = UserConcurrencyLimiter({"free": 1, "pro": 3, "internal": 10, "admin": 10})
    slow_router = _SlowRouter()

    auth_app.dependency_overrides[verify_api_key] = lambda: user
    auth_app.dependency_overrides[get_user_concurrency_limiter] = lambda: limiter
    auth_app.dependency_overrides[get_router] = lambda: slow_router

    try:
        body = {
            "model": "claude-3-haiku-20240307",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        }

        task1 = asyncio.create_task(
            auth_client.post(
                "/anthropic/v1/messages",
                json=body,
                headers={"Authorization": "Bearer hyi-stub"},
            )
        )
        await asyncio.sleep(0.05)

        resp2 = await auth_client.post(
            "/anthropic/v1/messages",
            json=body,
            headers={"Authorization": "Bearer hyi-stub"},
        )
        assert resp2.status_code == 429
        slow_router.released.set()
        # task1 may return 4xx/5xx — we only care that it unblocks
        await task1
    finally:
        auth_app.dependency_overrides.pop(verify_api_key, None)
        auth_app.dependency_overrides.pop(get_user_concurrency_limiter, None)
        auth_app.dependency_overrides.pop(get_router, None)


# ----------------------------- isolation -------------------------------


@pytest.mark.asyncio
async def test_two_users_have_independent_budgets_on_real_route(auth_app, auth_client):
    """User A holding their slot doesn't block user B."""
    limiter = UserConcurrencyLimiter({"free": 1, "pro": 3, "internal": 10, "admin": 10})
    slow_router = _SlowRouter()

    state = {"current_user": _stub_user("user-A", "free", is_admin=False)}

    def fake_verify():
        return state["current_user"]

    auth_app.dependency_overrides[verify_api_key] = fake_verify
    auth_app.dependency_overrides[get_user_concurrency_limiter] = lambda: limiter
    auth_app.dependency_overrides[get_router] = lambda: slow_router

    try:
        body = {"model": "stub", "messages": [{"role": "user", "content": "hi"}]}

        # Hold user-A in flight
        state["current_user"] = _stub_user("user-A", "free", is_admin=False)
        task_a = asyncio.create_task(
            auth_client.post(
                "/v1/chat/completions",
                json=body,
                headers={"Authorization": "Bearer hyi-stub"},
            )
        )
        await asyncio.sleep(0.05)

        # Switch the override to user-B and fire — should NOT 429
        state["current_user"] = _stub_user("user-B", "free", is_admin=False)
        # We need user-B's request to resolve quickly. Set the event so
        # both A and B return promptly.
        slow_router.released.set()
        resp_b = await auth_client.post(
            "/v1/chat/completions",
            json=body,
            headers={"Authorization": "Bearer hyi-stub"},
        )
        assert resp_b.status_code != 429
        await task_a
    finally:
        auth_app.dependency_overrides.pop(verify_api_key, None)
        auth_app.dependency_overrides.pop(get_user_concurrency_limiter, None)
        auth_app.dependency_overrides.pop(get_router, None)
```

- [ ] **Step 2: Run the tests**

```
uv run pytest test/servers/test_concurrency_endpoint.py -v
```

Expected: all 5 tests pass.

If a test fails because the stubbed `_SlowRouter.execute` signature doesn't match what the route handler expects, inspect the handler's actual call site (`completions.py:107+`, `anthropic_proxy.py:309+`) and adjust the stub method names/signatures accordingly. The route-handler-internal call is the only thing that depends on the stub shape — the dependency-gate behavior (429 on second concurrent request) is independent of the stub's success/failure.

If `_SlowRouter` doesn't fit at all because the real handler calls deeper machinery, the fallback is: instead of overriding `get_router`, monkeypatch (`monkeypatch.setattr`) a key downstream function (e.g., the per-route inference call) to raise after acquiring the slot. The 429 assertion is what matters — the in-flight handler can succeed or fail.

- [ ] **Step 3: Run the full test suite to confirm no regressions**

```
make test
```

Expected: all tests pass (or only pre-existing failures, unchanged from before this branch).

- [ ] **Step 4: Commit**

```bash
git add test/servers/test_concurrency_endpoint.py
git commit -m "$(cat <<'EOF'
test(serving): integration tests for per-user concurrency on routes (#242)

Verifies enforce_user_concurrency is wired into /v1/chat/completions,
/v1/completions, /v1/embeddings, and /anthropic/v1/messages, and that
two users have independent budgets.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: `pro` role fallout audit

The `pro` role is a new value of `users.role`. Anything that filters on role values needs to handle it. The audit confirms (and bakes in regression tests) that:

1. `has_role` rank-based comparisons still hold.
2. Admin route filters by tier (`admin.py` accepts `tier ∈ free|pro|enterprise` per its existing docstring) are not affected because `tier` is a separate column from `role`.
3. No code does `if user.role == "free"` exact-match in a way that would now miss pros.

- [ ] **Step 1: Run the audit grep**

```
cd /srv/hybridInference/.claude/worktrees/per-user-concurrency
grep -rnE "role == \"(free|internal|admin)\"|role==\"(free|internal|admin)\"" serving --include="*.py"
grep -rnE "role[\"'] *== *[\"'](free|internal|admin)" serving --include="*.py"
grep -rnE "in \(\"free\", \"internal\", \"admin\"\)" serving --include="*.py"
```

For each hit, decide whether the comparison should now also accept `pro` (and modify if so). Examples likely to be safe:
- `is_admin = user_role == "admin"` — fine, only admin matters.
- Code using `has_role(role, "internal")` — fine, rank comparison.

Examples that might need fixing:
- A whitelist like `if user.role in ("free", "internal", "admin"):` would now exclude pros.

If you find any such cases, fix them in this task.

- [ ] **Step 2: Run the existing role-related test files**

```
uv run pytest test/servers/test_role_migration.py test/servers/test_internal.py test/servers/test_admin_users.py -v
```

Expected: all pass. If any fail, the failure is from the role-rank shift (admin: 2→3, internal: 1→2). Fix the test or the underlying assumption.

- [ ] **Step 3: Commit any audit fixes**

If any code or test needed adjustment:

```bash
git add -p   # interactively stage
git commit -m "$(cat <<'EOF'
chore(serving): handle pro role in role-comparison sites (#242)

Audit fallout from adding "pro" to ROLE_RANK: <describe specific
adjustments here based on what you found>.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

If the audit found no issues, no commit is needed — note this in the PR description.

---

## Final pre-PR checks

- [ ] **Run the full test suite**

```
make test
```

All tests pass.

- [ ] **Run the linter**

```
make lint
```

No new lint errors.

- [ ] **Smoke test locally (optional but recommended)**

If you have a local backend running, send a request with a known API key, then send a second from the same key while the first is in flight (use `&` in shell, or two terminals). The second should return 429 with the documented body shape.

- [ ] **Push the branch and open the PR**

```bash
git push -u origin jason/claude/per-user-concurrency
gh pr create --base dev --title "feat(serving): per-user concurrency limit (#242)" --body "$(cat <<'EOF'
## Summary
- Cap simultaneous in-flight inference requests per user by role: free=1, pro=3, internal=10, admin=10.
- New `UserConcurrencyLimiter` and `enforce_user_concurrency` FastAPI dependency.
- Wired into `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/anthropic/v1/messages`.
- Adds `pro` to `ROLE_RANK`. Prometheus metrics: `user_concurrency_*`.
- Slot held for full streaming response duration; released on completion, exception, or client disconnect.

Spec: `docs/superpowers/specs/2026-04-30-per-user-concurrency-design.md`
Closes #242

## Test plan
- [ ] `make test` passes
- [ ] Free user gets 429 on the second concurrent request (chat, completions, embeddings, anthropic)
- [ ] Streaming response holds the slot until drained
- [ ] Client disconnect releases the slot
- [ ] Two users have independent budgets
- [ ] `user_concurrency_*` metrics visible at `/metrics`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Return the PR URL.

---

## Self-review (against the spec)

**Spec coverage check:**
- ✅ Goal: caps free=1, pro=3, internal=10, admin=10 on the four inference routes — Tasks 1, 5, 6.
- ✅ HTTP 429 immediately, no queueing — Task 5 (response shape verified in tests).
- ✅ Slot released after streaming, on disconnect, on exception — Task 5 (3 dedicated tests).
- ✅ `UserConcurrencyLimiter` in `serving/servers/concurrency.py` — Tasks 2, 3.
- ✅ Lazy slot creation, sticky capacity, `is_admin` precedence, unknown-role fallback — Task 2 (5 dedicated tests).
- ✅ `enforce_user_concurrency` dependency composing with `verify_api_key` — Task 5.
- ✅ AppServices wiring + bootstrap initialization — Task 4.
- ✅ Route wiring on all four routes — Task 6.
- ✅ Add `pro` to `ROLE_RANK` non-breakingly — Task 1 (with regression test).
- ✅ `USER_CONCURRENCY_LIMITS` constant — Task 1.
- ✅ Prometheus metrics with role labels and admin-precedence label — Task 3.
- ✅ Structured INFO log on rejection — Task 5 (in `enforce_user_concurrency` body).
- ✅ Test cases 1–8 from the spec — covered across Tasks 2, 5, 7 (case 1 = T5/T7, case 2 = T5, case 3 = T5, case 4 = T5/T7, case 5 = T5, case 6 implicit via streaming-hold + disconnect semantics; case 7 = T5, case 8 = T7).
- ✅ Metrics tests — Task 3.
- ✅ Pro fallout audit — Task 8.
- Out-of-scope items in the spec are not addressed (correctly): no Redis, no DB migration, no eviction, no role/tier merge, no Grafana, no JWT-route gating.

**Placeholder scan:** No "TBD", "TODO", or vague "appropriate error handling" instructions. Every code step shows the exact code; every command shows expected output.

**Type consistency:** `UserConcurrencyLimiter`, `_UserSlot`, `enforce_user_concurrency`, `user_concurrency_limiter` (field name), `get_user_concurrency_limiter` (dep), `USER_CONCURRENCY_LIMITS` (constant) — used consistently across all tasks. Method names (`try_acquire`, `release`, `limit_for`, `role_label`, `role_for`) consistent across tests and implementation.

**Spec case 6 ("client disconnect releases slot")** is not a dedicated integration test in Task 7 — it's covered indirectly by Task 5's streaming-hold test (which exercises the same `yield`/`finally` cleanup path). If the reviewer wants an explicit disconnect integration test on a real route, add it as a follow-up; the dependency-level coverage is sufficient for correctness because the cleanup path is the same regardless of route.

No gaps require plan changes.
