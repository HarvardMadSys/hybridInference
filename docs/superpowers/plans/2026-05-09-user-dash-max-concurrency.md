# User Dashboard: Max Concurrency Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the authenticated user's per-user max-concurrent-request cap in the user dashboard's daily quota banner.

**Architecture:** Backend extends the existing `/user/usage` `QuotaInfo` schema with a new `max_concurrency` field. A new helper resolves the cap from runtime settings keyed by the user's role, mirroring the existing `get_default_daily_quota_for_role` pattern. Frontend extends the TS type and renders one extra line inside `UsageStats.tsx`'s blue quota banner.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, pytest, Next.js (React + TypeScript), TailwindCSS, Vitest.

**Spec:** [docs/superpowers/specs/2026-05-09-user-dash-max-concurrency-design.md](../specs/2026-05-09-user-dash-max-concurrency-design.md)

---

## File Map

- **Modify:** `apps/backend/serving/schemas_auth.py` — add `max_concurrency` to `QuotaInfo`.
- **Modify:** `apps/backend/serving/servers/routers/user_routes.py` — add helper `get_user_concurrency_for_role`; populate field in `get_usage`.
- **Create:** `tests/unit/servers/routers/test_user_routes_max_concurrency.py` — unit tests for the helper and `/user/usage` integration of the field.
- **Modify:** `apps/frontend/src/lib/api/user.ts` — add `max_concurrency?: number` to `UsageStats.quota`.
- **Modify:** `apps/frontend/src/components/features/dashboard/UsageStats.tsx` — render new subline.
- **Create:** `apps/frontend/src/components/features/dashboard/UsageStats.test.tsx` — render test.

---

## Task 1: Backend — extend `QuotaInfo` schema

**Files:**
- Modify: `apps/backend/serving/schemas_auth.py`

- [ ] **Step 1: Add field**

In `class QuotaInfo(BaseModel)`, after `increase_request_message`, add:

```python
    max_concurrency: int | None = None
```

- [ ] **Step 2: Verify import OK**

Run: `uv run --active python -c "from serving.schemas_auth import QuotaInfo; print(QuotaInfo.model_fields['max_concurrency'])"`
Expected: prints a `FieldInfo` with `default=None`.

- [ ] **Step 3: Commit**

```bash
git add apps/backend/serving/schemas_auth.py
git commit -m "feat(schema): add max_concurrency to QuotaInfo"
```

---

## Task 2: Backend — helper `get_user_concurrency_for_role` (TDD)

**Files:**
- Create: `tests/unit/servers/routers/test_user_routes_max_concurrency.py`
- Modify: `apps/backend/serving/servers/routers/user_routes.py`

- [ ] **Step 1: Write failing test for helper**

Create `tests/unit/servers/routers/test_user_routes_max_concurrency.py`:

```python
"""Tests for the per-user max-concurrency resolver and /user/usage exposure."""

from unittest.mock import AsyncMock

import pytest

from serving.servers.routers.user_routes import get_user_concurrency_for_role


@pytest.mark.asyncio
async def test_helper_reads_runtime_setting_for_role():
    rt = AsyncMock()
    rt.get_int.return_value = 7
    cap = await get_user_concurrency_for_role("pro", rt)
    rt.get_int.assert_awaited_once_with("user_concurrency_pro")
    assert cap == 7


@pytest.mark.asyncio
async def test_helper_unknown_role_falls_back_to_free_constant():
    rt = AsyncMock()
    cap = await get_user_concurrency_for_role("ghost", rt)
    rt.get_int.assert_not_awaited()

    from serving.servers.concurrency import _FALLBACK_LIMITS

    assert cap == _FALLBACK_LIMITS["free"]


@pytest.mark.asyncio
async def test_helper_runtime_settings_none_falls_back_to_constant():
    from serving.servers.concurrency import _FALLBACK_LIMITS

    cap = await get_user_concurrency_for_role("pro", None)
    assert cap == _FALLBACK_LIMITS["pro"]


@pytest.mark.asyncio
async def test_helper_lowercases_role():
    rt = AsyncMock()
    rt.get_int.return_value = 4
    cap = await get_user_concurrency_for_role("Pro", rt)
    rt.get_int.assert_awaited_once_with("user_concurrency_pro")
    assert cap == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --active pytest tests/unit/servers/routers/test_user_routes_max_concurrency.py -v`
Expected: FAIL with `ImportError: cannot import name 'get_user_concurrency_for_role'`.

- [ ] **Step 3: Implement helper**

In `apps/backend/serving/servers/routers/user_routes.py`, after the existing `get_default_daily_quota_for_role` function (around line ~204, before `def mask_key_prefix`), add:

```python
async def get_user_concurrency_for_role(
    role: str,
    runtime_settings: "RuntimeSettings | None",
) -> int:
    """Return the per-user concurrency cap for ``role``.

    Reads the ``user_concurrency_<role>`` runtime setting if registered.
    Falls back to ``_FALLBACK_LIMITS`` from ``serving.servers.concurrency``
    when runtime settings are unavailable or the role has no registered
    setting. An unknown role degrades to the ``free`` fallback.
    """
    from serving.config.runtime_settings import RUNTIME_SETTINGS_REGISTRY
    from serving.servers.concurrency import _FALLBACK_LIMITS

    role_key = (role or "free").lower()
    setting_key = f"user_concurrency_{role_key}"

    if runtime_settings is not None and setting_key in RUNTIME_SETTINGS_REGISTRY:
        return await runtime_settings.get_int(setting_key)

    return _FALLBACK_LIMITS.get(role_key, _FALLBACK_LIMITS["free"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --active pytest tests/unit/servers/routers/test_user_routes_max_concurrency.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/servers/routers/user_routes.py tests/unit/servers/routers/test_user_routes_max_concurrency.py
git commit -m "feat(user-routes): add get_user_concurrency_for_role helper"
```

---

## Task 3: Backend — wire helper into `/user/usage` (TDD)

**Files:**
- Modify: `apps/backend/serving/servers/routers/user_routes.py`
- Modify: `tests/unit/servers/routers/test_user_routes_max_concurrency.py`

- [ ] **Step 1: Write failing endpoint test**

Append to `tests/unit/servers/routers/test_user_routes_max_concurrency.py`:

```python
from datetime import datetime, timezone


@pytest.mark.asyncio
async def test_get_usage_includes_max_concurrency_when_no_key(monkeypatch):
    from serving.servers.routers import user_routes

    op_store = AsyncMock()
    op_store.get_active_key_by_account.return_value = None

    async def _fake_helper(role, rt):
        assert role == "free"
        return 99

    monkeypatch.setattr(user_routes, "get_user_concurrency_for_role", _fake_helper)
    monkeypatch.setattr(user_routes, "get_runtime_settings_instance", lambda: None)

    current_user = {"user_id": "u1", "role": "free"}

    resp = await user_routes.get_usage(
        period="today",
        timezone_name="UTC",
        current_user=current_user,
        op_store=op_store,
        log_store=None,
    )
    assert resp.quota.has_key is False
    assert resp.quota.max_concurrency == 99


@pytest.mark.asyncio
async def test_get_usage_includes_max_concurrency_when_has_key(monkeypatch):
    from serving.servers.routers import user_routes

    op_store = AsyncMock()
    op_store.get_active_key_by_account.return_value = {
        "quota_daily_cost_usd": 5.0,
    }
    op_store.get_user_cost_today.return_value = 0.0

    log_store = AsyncMock()
    log_store.get_user_usage_detail.return_value = {
        "today": {"cost_usd": 0.0, "requests": 0, "prompt_tokens": 0, "completion_tokens": 0},
        "month": {"cost_usd": 0.0, "requests": 0, "prompt_tokens": 0, "completion_tokens": 0},
    }

    async def _fake_helper(role, rt):
        assert role == "pro"
        return 12

    monkeypatch.setattr(user_routes, "get_user_concurrency_for_role", _fake_helper)
    monkeypatch.setattr(user_routes, "get_runtime_settings_instance", lambda: None)

    current_user = {"user_id": "u1", "role": "pro"}

    resp = await user_routes.get_usage(
        period="today",
        timezone_name="UTC",
        current_user=current_user,
        op_store=op_store,
        log_store=log_store,
    )
    assert resp.quota.has_key is True
    assert resp.quota.max_concurrency == 12


@pytest.mark.asyncio
async def test_get_usage_concurrency_when_runtime_unavailable(monkeypatch):
    from serving.servers.routers import user_routes

    op_store = AsyncMock()
    op_store.get_active_key_by_account.return_value = None

    def _raise():
        raise RuntimeError("not initialized")

    monkeypatch.setattr(user_routes, "get_runtime_settings_instance", _raise)

    current_user = {"user_id": "u1", "role": "trial"}

    resp = await user_routes.get_usage(
        period="today",
        timezone_name="UTC",
        current_user=current_user,
        op_store=op_store,
        log_store=None,
    )

    from serving.servers.concurrency import _FALLBACK_LIMITS

    assert resp.quota.max_concurrency == _FALLBACK_LIMITS["trial"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --active pytest tests/unit/servers/routers/test_user_routes_max_concurrency.py -v`
Expected: 3 new tests FAIL with `AttributeError` or `AssertionError` because `max_concurrency` is `None` and helper isn't called.

- [ ] **Step 3: Wire helper into `get_usage`**

In `apps/backend/serving/servers/routers/user_routes.py`, modify `get_usage` (around line 593–693):

Just after the `if not op_store: raise HTTPException(...)` line, resolve the cap once for both branches:

```python
    # Resolve per-user concurrency cap (applies regardless of API-key state).
    try:
        rt = get_runtime_settings_instance()
    except RuntimeError:
        rt = None
    max_concurrency = await get_user_concurrency_for_role(
        current_user.get("role") or "free",
        rt,
    )
```

In the early-return `QuotaInfo(...)` (no-key branch), add:

```python
                max_concurrency=max_concurrency,
```

In the final `QuotaInfo(...)` (has-key branch), add:

```python
            max_concurrency=max_concurrency,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --active pytest tests/unit/servers/routers/test_user_routes_max_concurrency.py -v`
Expected: 7 passed.

- [ ] **Step 5: Run full unit suite to catch regressions**

Run: `uv run --active pytest -q -m "not external and not dbtest" tests/unit/servers/routers`
Expected: all pass.

- [ ] **Step 6: Lint**

Run: `make format && make lint`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/serving/servers/routers/user_routes.py tests/unit/servers/routers/test_user_routes_max_concurrency.py
git commit -m "feat(user-routes): expose max_concurrency in /user/usage"
```

---

## Task 4: Frontend — extend TS type

**Files:**
- Modify: `apps/frontend/src/lib/api/user.ts`

- [ ] **Step 1: Add field to interface**

In `UsageStats` interface, inside the `quota` object, after `increase_request_message?`, add:

```ts
        max_concurrency?: number;
```

- [ ] **Step 2: Type check**

Run: `cd apps/frontend && pnpm tsc --noEmit`
Expected: no errors related to this file.

- [ ] **Step 3: Commit**

```bash
git add apps/frontend/src/lib/api/user.ts
git commit -m "feat(frontend): type max_concurrency on UsageStats.quota"
```

---

## Task 5: Frontend — render concurrency line (TDD)

**Files:**
- Create: `apps/frontend/src/components/features/dashboard/UsageStats.test.tsx`
- Modify: `apps/frontend/src/components/features/dashboard/UsageStats.tsx`

- [ ] **Step 1: Write failing render test**

Create `apps/frontend/src/components/features/dashboard/UsageStats.test.tsx`:

```tsx
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { UsageStats } from './UsageStats';

vi.mock('@/lib/hooks', () => ({
  useUsageStats: vi.fn(),
}));

import { useUsageStats } from '@/lib/hooks';

const mockedUseUsageStats = useUsageStats as unknown as ReturnType<typeof vi.fn>;

const baseStats = {
  period: 'today' as const,
  quota: {
    has_key: true,
    daily_limit_usd: 10,
    spent_today_usd: 0,
    remaining_today_usd: 10,
    reset_at: new Date('2030-01-01T00:00:00Z').toISOString(),
    reset_timezone: 'UTC',
    contact_email: 'admin@example.com',
  },
  usage: { requests: 0, prompt_tokens: 0, completion_tokens: 0, cost_usd: 0 },
};

describe('UsageStats max_concurrency', () => {
  it('renders the concurrency line when max_concurrency is set', () => {
    mockedUseUsageStats.mockReturnValue({
      data: { ...baseStats, quota: { ...baseStats.quota, max_concurrency: 5 } },
      isLoading: false,
      error: null,
    });
    render(<UsageStats />);
    expect(screen.getByText(/Max concurrent requests:\s*5/)).toBeInTheDocument();
  });

  it('hides the concurrency line when max_concurrency is undefined', () => {
    mockedUseUsageStats.mockReturnValue({
      data: baseStats,
      isLoading: false,
      error: null,
    });
    render(<UsageStats />);
    expect(screen.queryByText(/Max concurrent requests:/)).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/features/dashboard/UsageStats.test.tsx`
Expected: first test FAILS — "Unable to find element with text /Max concurrent requests/".

- [ ] **Step 3: Add the line to the banner**

In `apps/frontend/src/components/features/dashboard/UsageStats.tsx`, locate the existing block:

```tsx
                  <div className="mt-1 text-sm text-blue-800">
                    Resets at {formatResetAt(quota.reset_at)}.
                  </div>
```

Immediately after that closing `</div>`, insert:

```tsx
                  {quota.max_concurrency != null && (
                    <div className="mt-1 text-sm text-blue-800">
                      Max concurrent requests: {quota.max_concurrency}
                    </div>
                  )}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && pnpm vitest run src/components/features/dashboard/UsageStats.test.tsx`
Expected: 2 passed.

- [ ] **Step 5: Run full frontend test/lint**

Run: `cd apps/frontend && pnpm lint && pnpm tsc --noEmit && pnpm vitest run`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/components/features/dashboard/UsageStats.tsx apps/frontend/src/components/features/dashboard/UsageStats.test.tsx
git commit -m "feat(frontend): show max concurrent requests in usage banner"
```

---

## Task 6: Manual verification against running backend

- [ ] **Step 1: Start backend locally**

Run: `make dev-backend` (or whatever the local-run command is per `Makefile`/`docs/developer/`).
Confirm `/user/usage` returns the new field for an authenticated user (use the seeded admin or a free user). Example:

```bash
curl -s -H "Authorization: Bearer <token>" http://localhost:8000/user/usage | jq '.quota.max_concurrency'
```

Expected: a positive integer matching `RUNTIME_SETTINGS_REGISTRY["user_concurrency_<role>"]["default"]` for the user's role.

- [ ] **Step 2: Start frontend locally and visit the dashboard**

Run: `cd apps/frontend && pnpm dev`
Open the dashboard while logged in, confirm the blue quota banner shows `Max concurrent requests: N`.

- [ ] **Step 3: Toggle setting and refresh**

Through the admin SettingsTab, change `user_concurrency_<role>` for your test user's role; reload the dashboard; verify the displayed value updates after the runtime-settings cache TTL (~30s).

- [ ] **Step 4: No commit needed (manual verification only).**

---

## Task 7: Final gate

- [ ] **Step 1: Run full Python test gate**

Run: `make format && make lint && make test`
Expected: all pass.

- [ ] **Step 2: Run full frontend gate**

Run: `cd apps/frontend && pnpm lint && pnpm tsc --noEmit && pnpm vitest run`
Expected: all pass.

- [ ] **Step 3: Open PR against `dev`**

```bash
gh pr create --base dev --title "feat: show max concurrency on user dashboard" --body "$(cat <<'EOF'
## Summary
- Add `max_concurrency` to `QuotaInfo` and resolve it from runtime settings keyed by user role.
- Display the cap in the user dashboard's quota banner.

## Test plan
- [x] New unit tests for helper and `/user/usage` integration
- [x] New frontend render test for `UsageStats`
- [x] Manual smoke against staging

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review checklist (already applied)

- Spec coverage: each spec section maps to a task above (schema → Task 1; helper → Task 2; endpoint → Task 3; frontend type → Task 4; frontend UI + test → Task 5; acceptance → Tasks 6–7).
- No placeholders remain; every step shows the exact code or command.
- Type names consistent: `get_user_concurrency_for_role`, `max_concurrency`, `_FALLBACK_LIMITS`, `RUNTIME_SETTINGS_REGISTRY`, `RuntimeSettings` used identically across tasks.
