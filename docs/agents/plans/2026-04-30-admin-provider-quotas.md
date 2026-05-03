# Admin Dashboard — Provider Quotas Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new "Providers" tab to the admin dashboard showing each upstream LLM provider's masked API key and live remaining quota (Chutes, ZAI, MiniMax, Ollama Cloud).

**Architecture:** New `GET /admin/provider-quotas` endpoint runs 4 async fetchers in parallel via `asyncio.gather`, returns a unified shape; frontend renders a grid of provider cards on the new tab.

**Tech Stack:** FastAPI, Pydantic, aiohttp (matches existing serving HTTP client convention), pytest with `unittest.mock.AsyncMock`, BeautifulSoup4 for HTML scraping (Ollama), Next.js + Tailwind for frontend.

**Spec:** [docs/agents/specs/2026-04-30-admin-provider-quotas-design.md](docs/agents/specs/2026-04-30-admin-provider-quotas-design.md)

---

## File Map

**New files:**
- `serving/admin/__init__.py` — package marker
- `serving/admin/provider_quotas.py` — fetcher functions + key-masking helper
- `test/servers/test_admin_provider_quotas.py` — backend tests

**Modified files:**
- `serving/config/settings.py` — add `minimax_session_cookie`, `ollama_session_cookie`
- `serving/schemas_admin.py` — add `ProviderQuotaUsage`, `ProviderQuotaResult`, `AdminProviderQuotasResponse`
- `serving/servers/routers/admin.py` — add `GET /admin/provider-quotas` route
- `frontend/src/lib/api/admin.ts` — add types + `getProviderQuotas()`
- `frontend/src/app/dashboard/admin/page.tsx` — add `providers` tab and UI
- `pyproject.toml` — add `beautifulsoup4` dep
- `.env.example` — document new env vars (if file exists; else create)

---

## Task 0: Setup — Pull, Worktree, Branch

**Files:** none yet.

- [ ] **Step 1: Pull origin/dev**

```bash
cd /srv/hybridInference
git fetch origin
git checkout dev
git pull origin dev
```

- [ ] **Step 2: Create worktree on a new branch**

```bash
cd /srv/hybridInference
git worktree add .worktrees/admin-provider-quotas -b jason/claude/admin-provider-quotas origin/dev
cd .worktrees/admin-provider-quotas
```

All subsequent tasks run inside `/srv/hybridInference/.worktrees/admin-provider-quotas`.

- [ ] **Step 3: Verify the worktree is on the new branch**

```bash
git status
git log --oneline -1
```

Expected: `On branch jason/claude/admin-provider-quotas`, HEAD at the latest dev commit.

---

## Task 1: Add cookie settings to Pydantic config

**Files:**
- Modify: `serving/config/settings.py`

- [ ] **Step 1: Add `minimax_session_cookie` and `ollama_session_cookie` fields**

Find the `# Claude subscription` block in `serving/config/settings.py` (around line 79). After the `claude_sub_failure_threshold` field, add a new block:

```python
    # Provider quota cookies (admin dashboard "Providers" tab)
    # Pasted from browser DevTools after logging into the provider's web dashboard.
    # Re-paste when the cookie expires.
    minimax_session_cookie: str = ""
    ollama_session_cookie: str = ""
```

- [ ] **Step 2: Verify the file imports cleanly**

```bash
cd /srv/hybridInference/.worktrees/admin-provider-quotas
uv run python -c "from serving.config.settings import settings; print('minimax:', repr(settings.minimax_session_cookie)); print('ollama:', repr(settings.ollama_session_cookie))"
```

Expected: prints both as `''` (empty string defaults).

- [ ] **Step 3: Commit**

```bash
git add serving/config/settings.py
git commit -m "feat(admin): add cookie env vars for MiniMax/Ollama quota fetchers

Two new env vars (MINIMAX_SESSION_COOKIE, OLLAMA_SESSION_COOKIE) used
by the upcoming provider-quotas admin endpoint. Both default to empty
string; admin pastes them from browser DevTools and re-pastes when
they expire.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: Add Pydantic schemas for the response

**Files:**
- Modify: `serving/schemas_admin.py`

- [ ] **Step 1: Add new schemas at the end of `serving/schemas_admin.py`** (before the `__all__` list)

```python
# ========================================
# Provider Quotas (Admin Dashboard)
# ========================================


class ProviderQuotaUsage(BaseModel):
    """A single usage measurement for a provider (e.g. monthly cost, request count)."""

    label: str = Field(..., description="Human-readable label, e.g. 'Monthly', '4-hour window'")
    used: float | None = Field(None, description="Amount consumed (None if unknown)")
    limit: float | None = Field(None, description="Total quota limit (None if unlimited or unknown)")
    unit: str = Field(..., description="Unit string, e.g. 'USD', 'tokens', 'requests'")
    reset_at: datetime | None = Field(None, description="When this usage window resets (UTC)")


class ProviderQuotaResult(BaseModel):
    """Result of querying a single upstream provider's quota."""

    name: str = Field(..., description="Lowercase identifier: chutes | zai | minimax | ollama")
    display_name: str = Field(..., description="Human-readable name")
    key_configured: bool = Field(..., description="True if credentials are present in env")
    key_masked: str | None = Field(None, description="Masked key/cookie (None if not configured)")
    fetched_at: datetime | None = Field(None, description="When the quota was fetched (UTC)")
    ok: bool = Field(..., description="True if quota fetch succeeded")
    error: str | None = Field(
        None,
        description="Short reason code if !ok: 'auth_failed' | 'timeout' | 'not_configured' | 'parse_error' | 'unexpected'",
    )
    usages: list[ProviderQuotaUsage] = Field(default_factory=list)


class AdminProviderQuotasResponse(BaseModel):
    """Aggregated response for the admin provider-quotas endpoint."""

    generated_at: datetime
    providers: list[ProviderQuotaResult]
```

- [ ] **Step 2: Add the new class names to `__all__`**

In the `__all__` list at the bottom of the file, add (alphabetically):

```python
    "AdminProviderQuotasResponse",
    "ProviderQuotaResult",
    "ProviderQuotaUsage",
```

(Sort alphabetically with the existing entries — they're already alphabetized.)

- [ ] **Step 3: Verify schemas import**

```bash
cd /srv/hybridInference/.worktrees/admin-provider-quotas
uv run python -c "from serving.schemas_admin import AdminProviderQuotasResponse, ProviderQuotaResult, ProviderQuotaUsage; print('OK')"
```

Expected: prints `OK`.

- [ ] **Step 4: Commit**

```bash
git add serving/schemas_admin.py
git commit -m "feat(admin): add Pydantic schemas for provider quotas response

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Create `provider_quotas.py` module skeleton with `_mask_key` helper (TDD)

**Files:**
- Create: `serving/admin/__init__.py`
- Create: `serving/admin/provider_quotas.py`
- Create: `test/servers/test_admin_provider_quotas.py`

- [ ] **Step 1: Write failing tests for `_mask_key`**

Create `test/servers/test_admin_provider_quotas.py`:

```python
"""Tests for the admin provider-quotas module."""

from __future__ import annotations

from serving.admin.provider_quotas import _mask_key


class TestMaskKey:
    def test_normal_length_key_shows_prefix_and_suffix(self):
        # >= 16 chars: first 8 + "..." + last 4
        assert _mask_key("cpk_ab123456cccccccxyz9") == "cpk_ab12...xyz9"

    def test_exactly_16_char_key_uses_full_form(self):
        assert _mask_key("0123456789abcdef") == "01234567...cdef"

    def test_15_char_key_uses_placeholder(self):
        assert _mask_key("0123456789abcde") == "***configured***"

    def test_short_key_returns_placeholder(self):
        assert _mask_key("short") == "***configured***"

    def test_long_cookie_string_gets_masked(self):
        cookie = "session=abc123def456ghi789jkl012mno345"
        result = _mask_key(cookie)
        assert result.startswith("session=")
        assert "..." in result
        assert len(result) == 8 + 3 + 4
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /srv/hybridInference/.worktrees/admin-provider-quotas
uv run pytest test/servers/test_admin_provider_quotas.py -v
```

Expected: ImportError or ModuleNotFoundError on `serving.admin.provider_quotas`.

- [ ] **Step 3: Create `serving/admin/__init__.py`** (empty file)

```bash
mkdir -p serving/admin
: > serving/admin/__init__.py
```

- [ ] **Step 4: Create `serving/admin/provider_quotas.py` with the helper**

```python
"""Provider quota fetchers for the admin dashboard 'Providers' tab.

Each public fetcher returns a `ProviderQuotaResult`. Errors are converted
to structured results — fetchers never raise out of the gather.
"""

from __future__ import annotations


def _mask_key(key: str) -> str:
    """Mask an API key or cookie for display.

    Returns first 8 + '...' + last 4 if key is at least 16 chars; otherwise
    returns a generic placeholder so we never leak short secrets.
    """
    if len(key) >= 16:
        return f"{key[:8]}...{key[-4:]}"
    return "***configured***"
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest test/servers/test_admin_provider_quotas.py -v
```

Expected: all 5 tests pass.

- [ ] **Step 6: Commit**

```bash
git add serving/admin/__init__.py serving/admin/provider_quotas.py test/servers/test_admin_provider_quotas.py
git commit -m "feat(admin): scaffold provider_quotas module with _mask_key helper

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: Implement Chutes fetcher (TDD)

**Files:**
- Modify: `serving/admin/provider_quotas.py`
- Modify: `test/servers/test_admin_provider_quotas.py`

- [ ] **Step 1: Write failing tests for `fetch_chutes`**

Append to `test/servers/test_admin_provider_quotas.py`:

```python
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from serving.admin.provider_quotas import fetch_chutes


def _mock_aiohttp_get(*, status: int = 200, json_data: dict | None = None, raise_exc: Exception | None = None):
    """Build a context-manager mock for `aiohttp.ClientSession().get(...)`."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=json_data or {})
    response.text = AsyncMock(return_value="")

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock()
    if raise_exc is not None:
        session.get = MagicMock(side_effect=raise_exc)
    else:
        session.get = MagicMock(return_value=cm)

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=None)
    return session_cm


class TestFetchChutes:
    @pytest.mark.asyncio
    async def test_not_configured_when_key_missing(self, monkeypatch):
        monkeypatch.delenv("CHUTES_API_KEY", raising=False)
        result = await fetch_chutes()
        assert result.ok is False
        assert result.error == "not_configured"
        assert result.key_configured is False
        assert result.name == "chutes"
        assert result.display_name == "Chutes"

    @pytest.mark.asyncio
    async def test_success_returns_usages(self, monkeypatch):
        monkeypatch.setenv("CHUTES_API_KEY", "cpk_abcdef1234567890xyz")
        payload = {
            "monthly": {"used": 4.20, "limit": 100.0, "reset_at": "2026-05-01T00:00:00Z"},
            "rolling": {"used": 1.10, "limit": 10.0, "window": "4h"},
        }
        with patch("serving.admin.provider_quotas.aiohttp.ClientSession", return_value=_mock_aiohttp_get(status=200, json_data=payload)):
            result = await fetch_chutes()
        assert result.ok is True
        assert result.key_configured is True
        assert result.key_masked == "cpk_abcd...3xyz" or result.key_masked.startswith("cpk_abcd")
        assert any(u.label.lower().startswith("month") for u in result.usages)
        assert any("4" in u.label or "rolling" in u.label.lower() for u in result.usages)

    @pytest.mark.asyncio
    async def test_auth_failed_on_401(self, monkeypatch):
        monkeypatch.setenv("CHUTES_API_KEY", "cpk_abcdef1234567890xyz")
        with patch("serving.admin.provider_quotas.aiohttp.ClientSession", return_value=_mock_aiohttp_get(status=401)):
            result = await fetch_chutes()
        assert result.ok is False
        assert result.error == "auth_failed"

    @pytest.mark.asyncio
    async def test_timeout_returns_timeout_error(self, monkeypatch):
        import asyncio
        monkeypatch.setenv("CHUTES_API_KEY", "cpk_abcdef1234567890xyz")
        with patch("serving.admin.provider_quotas.aiohttp.ClientSession", return_value=_mock_aiohttp_get(raise_exc=asyncio.TimeoutError())):
            result = await fetch_chutes()
        assert result.ok is False
        assert result.error == "timeout"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest test/servers/test_admin_provider_quotas.py::TestFetchChutes -v
```

Expected: ImportError on `fetch_chutes`.

- [ ] **Step 3: Implement `fetch_chutes` in `serving/admin/provider_quotas.py`**

Add imports at top of file:

```python
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

import aiohttp

from serving.schemas_admin import ProviderQuotaResult, ProviderQuotaUsage

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 8
```

Add a small helper for building error results (used by all fetchers):

```python
def _err(name: str, display_name: str, key: str, reason: str) -> ProviderQuotaResult:
    return ProviderQuotaResult(
        name=name,
        display_name=display_name,
        key_configured=bool(key),
        key_masked=_mask_key(key) if key else None,
        fetched_at=datetime.now(timezone.utc),
        ok=False,
        error=reason,
        usages=[],
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)
```

Add the Chutes fetcher:

```python
async def fetch_chutes() -> ProviderQuotaResult:
    """Fetch quota usage from Chutes via /users/me/subscription_usage."""
    key = os.getenv("CHUTES_API_KEY", "")
    if not key:
        return ProviderQuotaResult(
            name="chutes",
            display_name="Chutes",
            key_configured=False,
            key_masked=None,
            fetched_at=_now(),
            ok=False,
            error="not_configured",
            usages=[],
        )

    url = "https://api.chutes.ai/users/me/subscription_usage"
    headers = {"Authorization": f"Bearer {key}"}
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status in (401, 403):
                    return _err("chutes", "Chutes", key, "auth_failed")
                if resp.status >= 400:
                    return _err("chutes", "Chutes", key, "unexpected")
                try:
                    data: dict[str, Any] = await resp.json()
                except Exception:
                    return _err("chutes", "Chutes", key, "parse_error")
    except asyncio.TimeoutError:
        return _err("chutes", "Chutes", key, "timeout")
    except aiohttp.ClientError:
        return _err("chutes", "Chutes", key, "unexpected")
    except Exception:
        logger.exception("fetch_chutes: unexpected error")
        return _err("chutes", "Chutes", key, "unexpected")

    usages = _parse_chutes_usage(data)
    return ProviderQuotaResult(
        name="chutes",
        display_name="Chutes",
        key_configured=True,
        key_masked=_mask_key(key),
        fetched_at=_now(),
        ok=True,
        error=None,
        usages=usages,
    )


def _parse_chutes_usage(data: dict[str, Any]) -> list[ProviderQuotaUsage]:
    """Best-effort parse of the Chutes subscription_usage payload.

    The exact response schema is not formally documented; we look for
    common keys and degrade gracefully if missing.
    """
    usages: list[ProviderQuotaUsage] = []
    for key, label_default in (("monthly", "Monthly"), ("rolling", "Rolling window")):
        block = data.get(key)
        if not isinstance(block, dict):
            continue
        used = block.get("used")
        limit = block.get("limit")
        unit = block.get("unit", "USD")
        window = block.get("window")
        label = f"{label_default} ({window})" if window else label_default
        reset = block.get("reset_at")
        reset_dt = None
        if isinstance(reset, str):
            try:
                reset_dt = datetime.fromisoformat(reset.replace("Z", "+00:00"))
            except ValueError:
                reset_dt = None
        usages.append(
            ProviderQuotaUsage(
                label=label,
                used=float(used) if isinstance(used, (int, float)) else None,
                limit=float(limit) if isinstance(limit, (int, float)) else None,
                unit=str(unit),
                reset_at=reset_dt,
            )
        )
    return usages
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest test/servers/test_admin_provider_quotas.py::TestFetchChutes -v
```

Expected: all 4 tests pass. If `pytest-asyncio` is not configured, add `pytestmark = pytest.mark.asyncio` at the top of the test module, or apply `@pytest.mark.asyncio` per test (already added in step 1). Confirm pytest-asyncio is installed: `uv run python -c "import pytest_asyncio"`.

- [ ] **Step 5: Commit**

```bash
git add serving/admin/provider_quotas.py test/servers/test_admin_provider_quotas.py
git commit -m "feat(admin): add Chutes provider quota fetcher

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: Implement ZAI fetcher (TDD)

**Files:**
- Modify: `serving/admin/provider_quotas.py`
- Modify: `test/servers/test_admin_provider_quotas.py`

- [ ] **Step 1: Append failing tests for `fetch_zai`**

Append to `test/servers/test_admin_provider_quotas.py`:

```python
from serving.admin.provider_quotas import fetch_zai


class TestFetchZai:
    @pytest.mark.asyncio
    async def test_not_configured_when_key_missing(self, monkeypatch):
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        result = await fetch_zai()
        assert result.ok is False
        assert result.error == "not_configured"
        assert result.name == "zai"

    @pytest.mark.asyncio
    async def test_success_parses_token_and_time_limits(self, monkeypatch):
        monkeypatch.setenv("ZAI_API_KEY", "zai_abc1234567890xyz9")
        payload = {
            "limits": [
                {"type": "TOKENS_LIMIT", "percentage": 0.42, "currentValue": 4200, "limit": 10000},
                {"type": "TIME_LIMIT", "percentage": 0.10, "currentValue": 6, "limit": 60},
            ]
        }
        with patch("serving.admin.provider_quotas.aiohttp.ClientSession", return_value=_mock_aiohttp_get(status=200, json_data=payload)):
            result = await fetch_zai()
        assert result.ok is True
        assert len(result.usages) == 2
        labels = [u.label for u in result.usages]
        assert any("Token" in label for label in labels)
        assert any("Time" in label for label in labels)

    @pytest.mark.asyncio
    async def test_auth_failed_on_401(self, monkeypatch):
        monkeypatch.setenv("ZAI_API_KEY", "zai_abc1234567890xyz9")
        with patch("serving.admin.provider_quotas.aiohttp.ClientSession", return_value=_mock_aiohttp_get(status=401)):
            result = await fetch_zai()
        assert result.ok is False
        assert result.error == "auth_failed"

    @pytest.mark.asyncio
    async def test_parse_error_on_unexpected_shape(self, monkeypatch):
        monkeypatch.setenv("ZAI_API_KEY", "zai_abc1234567890xyz9")
        with patch("serving.admin.provider_quotas.aiohttp.ClientSession", return_value=_mock_aiohttp_get(status=200, json_data={"unrelated": "junk"})):
            result = await fetch_zai()
        # No "limits" key — we treat as parse_error
        assert result.ok is False
        assert result.error == "parse_error"
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest test/servers/test_admin_provider_quotas.py::TestFetchZai -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `fetch_zai` in `serving/admin/provider_quotas.py`**

Append:

```python
async def fetch_zai() -> ProviderQuotaResult:
    """Fetch quota usage from ZAI via /api/monitor/usage/quota/limit.

    Endpoint discovered from ZAI's official `glm-plan-usage` plugin. The
    plugin uses `ANTHROPIC_AUTH_TOKEN`; we attempt with `ZAI_API_KEY`. If
    the API key is rejected we surface `auth_failed`.
    """
    key = os.getenv("ZAI_API_KEY", "")
    if not key:
        return ProviderQuotaResult(
            name="zai",
            display_name="ZAI",
            key_configured=False,
            key_masked=None,
            fetched_at=_now(),
            ok=False,
            error="not_configured",
            usages=[],
        )

    url = "https://api.z.ai/api/monitor/usage/quota/limit"
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept-Language": "en-US,en",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status in (401, 403):
                    return _err("zai", "ZAI", key, "auth_failed")
                if resp.status >= 400:
                    return _err("zai", "ZAI", key, "unexpected")
                try:
                    data: dict[str, Any] = await resp.json()
                except Exception:
                    return _err("zai", "ZAI", key, "parse_error")
    except asyncio.TimeoutError:
        return _err("zai", "ZAI", key, "timeout")
    except aiohttp.ClientError:
        return _err("zai", "ZAI", key, "unexpected")
    except Exception:
        logger.exception("fetch_zai: unexpected error")
        return _err("zai", "ZAI", key, "unexpected")

    # Some ZAI responses wrap data in a "data" key
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    limits = body.get("limits") if isinstance(body, dict) else None
    if not isinstance(limits, list):
        return _err("zai", "ZAI", key, "parse_error")

    usages: list[ProviderQuotaUsage] = []
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type", "")).upper()
        used = entry.get("currentValue") if "currentValue" in entry else entry.get("used")
        limit = entry.get("limit") if "limit" in entry else None
        if kind == "TOKENS_LIMIT":
            label, unit = "Tokens", "tokens"
        elif kind == "TIME_LIMIT":
            label, unit = "Time", "minutes"
        else:
            label, unit = kind.replace("_", " ").title() or "Quota", ""
        usages.append(
            ProviderQuotaUsage(
                label=label,
                used=float(used) if isinstance(used, (int, float)) else None,
                limit=float(limit) if isinstance(limit, (int, float)) else None,
                unit=unit,
                reset_at=None,
            )
        )

    return ProviderQuotaResult(
        name="zai",
        display_name="ZAI",
        key_configured=True,
        key_masked=_mask_key(key),
        fetched_at=_now(),
        ok=True,
        error=None,
        usages=usages,
    )
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest test/servers/test_admin_provider_quotas.py::TestFetchZai -v
```

Expected: all 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add serving/admin/provider_quotas.py test/servers/test_admin_provider_quotas.py
git commit -m "feat(admin): add ZAI provider quota fetcher

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: Implement MiniMax fetcher (TDD)

**Files:**
- Modify: `serving/admin/provider_quotas.py`
- Modify: `test/servers/test_admin_provider_quotas.py`

- [ ] **Step 1: Append failing tests for `fetch_minimax`**

```python
from serving.admin.provider_quotas import fetch_minimax


class TestFetchMinimax:
    @pytest.mark.asyncio
    async def test_not_configured_when_cookie_missing(self, monkeypatch):
        monkeypatch.delenv("MINIMAX_SESSION_COOKIE", raising=False)
        result = await fetch_minimax()
        assert result.ok is False
        assert result.error == "not_configured"
        assert result.name == "minimax"

    @pytest.mark.asyncio
    async def test_auth_failed_on_cookie_rejected(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_SESSION_COOKIE", "session=abcdefghijklmnop")
        # MiniMax returns HTTP 200 with status_code 1004 in body when cookie missing
        payload = {"base_resp": {"status_code": 1004, "status_msg": "cookie is missing, log in again"}}
        with patch("serving.admin.provider_quotas.aiohttp.ClientSession", return_value=_mock_aiohttp_get(status=200, json_data=payload)):
            result = await fetch_minimax()
        assert result.ok is False
        assert result.error == "auth_failed"

    @pytest.mark.asyncio
    async def test_success_parses_remains(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_SESSION_COOKIE", "session=abcdefghijklmnop")
        payload = {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "data": {
                "model_remains": [
                    {
                        "model_name": "MiniMax-M2.7",
                        "remain_count": 720,
                        "total_count": 1000,
                        "start_time": "2026-04-29T00:00:00Z",
                        "end_time": "2026-04-30T00:00:00Z",
                    }
                ]
            },
        }
        with patch("serving.admin.provider_quotas.aiohttp.ClientSession", return_value=_mock_aiohttp_get(status=200, json_data=payload)):
            result = await fetch_minimax()
        assert result.ok is True
        assert len(result.usages) >= 1
        u = result.usages[0]
        # used = total - remain
        assert u.used == 280.0
        assert u.limit == 1000.0
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest test/servers/test_admin_provider_quotas.py::TestFetchMinimax -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `fetch_minimax`**

Append to `serving/admin/provider_quotas.py`:

```python
async def fetch_minimax() -> ProviderQuotaResult:
    """Fetch coding-plan quota from MiniMax via cookie-authed endpoint.

    The endpoint requires browser session cookies; API key auth returns
    `{"base_resp": {"status_code": 1004, "status_msg": "cookie missing"}}`.
    """
    cookie = os.getenv("MINIMAX_SESSION_COOKIE", "")
    if not cookie:
        return ProviderQuotaResult(
            name="minimax",
            display_name="MiniMax",
            key_configured=False,
            key_masked=None,
            fetched_at=_now(),
            ok=False,
            error="not_configured",
            usages=[],
        )

    url = "https://api.minimaxi.com/v1/api/openplatform/coding_plan/remains"
    headers = {"Cookie": cookie}
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status in (401, 403):
                    return _err("minimax", "MiniMax", cookie, "auth_failed")
                if resp.status >= 400:
                    return _err("minimax", "MiniMax", cookie, "unexpected")
                try:
                    data: dict[str, Any] = await resp.json()
                except Exception:
                    return _err("minimax", "MiniMax", cookie, "parse_error")
    except asyncio.TimeoutError:
        return _err("minimax", "MiniMax", cookie, "timeout")
    except aiohttp.ClientError:
        return _err("minimax", "MiniMax", cookie, "unexpected")
    except Exception:
        logger.exception("fetch_minimax: unexpected error")
        return _err("minimax", "MiniMax", cookie, "unexpected")

    base_resp = data.get("base_resp") if isinstance(data.get("base_resp"), dict) else None
    if base_resp and base_resp.get("status_code") == 1004:
        return _err("minimax", "MiniMax", cookie, "auth_failed")
    if base_resp and base_resp.get("status_code") not in (None, 0):
        return _err("minimax", "MiniMax", cookie, "unexpected")

    body = data.get("data") if isinstance(data.get("data"), dict) else data
    model_remains = body.get("model_remains") if isinstance(body, dict) else None
    if not isinstance(model_remains, list) or not model_remains:
        return _err("minimax", "MiniMax", cookie, "parse_error")

    usages: list[ProviderQuotaUsage] = []
    for entry in model_remains:
        if not isinstance(entry, dict):
            continue
        model_name = str(entry.get("model_name", "Coding plan"))
        remain = entry.get("remain_count")
        total = entry.get("total_count")
        end = entry.get("end_time")
        reset_dt = None
        if isinstance(end, str):
            try:
                reset_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            except ValueError:
                reset_dt = None
        used = None
        if isinstance(remain, (int, float)) and isinstance(total, (int, float)):
            used = float(total - remain)
        usages.append(
            ProviderQuotaUsage(
                label=model_name,
                used=used,
                limit=float(total) if isinstance(total, (int, float)) else None,
                unit="requests",
                reset_at=reset_dt,
            )
        )

    if not usages:
        return _err("minimax", "MiniMax", cookie, "parse_error")

    return ProviderQuotaResult(
        name="minimax",
        display_name="MiniMax",
        key_configured=True,
        key_masked=_mask_key(cookie),
        fetched_at=_now(),
        ok=True,
        error=None,
        usages=usages,
    )
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest test/servers/test_admin_provider_quotas.py::TestFetchMinimax -v
```

Expected: all 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add serving/admin/provider_quotas.py test/servers/test_admin_provider_quotas.py
git commit -m "feat(admin): add MiniMax provider quota fetcher

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: Add `beautifulsoup4` dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add `beautifulsoup4` to dependencies**

Open `pyproject.toml`, locate the main `dependencies = [...]` list (around line 14), and add (in alphabetical position):

```
    "beautifulsoup4>=4.12.0",
```

- [ ] **Step 2: Sync the lockfile**

```bash
cd /srv/hybridInference/.worktrees/admin-provider-quotas
uv lock
```

Expected: `uv.lock` is updated to include `beautifulsoup4`.

- [ ] **Step 3: Verify it imports**

```bash
uv run python -c "from bs4 import BeautifulSoup; print(BeautifulSoup('<p>hi</p>', 'html.parser').get_text())"
```

Expected: prints `hi`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore(deps): add beautifulsoup4 for Ollama settings page scrape

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: Implement Ollama fetcher (TDD)

**Files:**
- Modify: `serving/admin/provider_quotas.py`
- Modify: `test/servers/test_admin_provider_quotas.py`

The Ollama settings page exact HTML structure is unknown without a logged-in session. The fetcher uses a defensive parser that looks for usage figures by text proximity (e.g., "session", "weekly", "of"). If the parser can't find usage data, it returns `parse_error`. The admin can paste their cookie and check the resulting error to know if the parser needs updating.

- [ ] **Step 1: Append failing tests for `fetch_ollama`**

```python
from serving.admin.provider_quotas import fetch_ollama


class TestFetchOllama:
    @pytest.mark.asyncio
    async def test_not_configured_when_cookie_missing(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_SESSION_COOKIE", raising=False)
        result = await fetch_ollama()
        assert result.ok is False
        assert result.error == "not_configured"
        assert result.name == "ollama"

    @pytest.mark.asyncio
    async def test_redirected_to_login_returns_auth_failed(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_SESSION_COOKIE", "ollama_session=abcdefghijklmnop")
        # If cookie is invalid, ollama.com redirects to a sign-in page.
        # We simulate by returning HTML with no usage data and a sign-in link.
        html = "<html><body><a href='/signin'>Sign in</a></body></html>"
        response_mock = MagicMock()
        response_mock.status = 200
        response_mock.text = AsyncMock(return_value=html)
        response_mock.json = AsyncMock(return_value={})
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=response_mock)
        cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get = MagicMock(return_value=cm)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=None)
        with patch("serving.admin.provider_quotas.aiohttp.ClientSession", return_value=session_cm):
            result = await fetch_ollama()
        assert result.ok is False
        assert result.error in ("auth_failed", "parse_error")

    @pytest.mark.asyncio
    async def test_parses_session_and_weekly_usage(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_SESSION_COOKIE", "ollama_session=abcdefghijklmnop")
        # Simulated HTML with the usage labels we look for.
        html = """
        <html><body>
          <h2>Usage</h2>
          <div>Session usage: 42 of 100 requests</div>
          <div>Weekly usage: 320 of 5000 requests</div>
        </body></html>
        """
        response_mock = MagicMock()
        response_mock.status = 200
        response_mock.text = AsyncMock(return_value=html)
        response_mock.json = AsyncMock(return_value={})
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=response_mock)
        cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get = MagicMock(return_value=cm)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=None)
        with patch("serving.admin.provider_quotas.aiohttp.ClientSession", return_value=session_cm):
            result = await fetch_ollama()
        assert result.ok is True
        assert len(result.usages) >= 2
        labels = [u.label.lower() for u in result.usages]
        assert any("session" in label for label in labels)
        assert any("week" in label for label in labels)
        session_use = next(u for u in result.usages if "session" in u.label.lower())
        assert session_use.used == 42.0
        assert session_use.limit == 100.0
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest test/servers/test_admin_provider_quotas.py::TestFetchOllama -v
```

Expected: ImportError on `fetch_ollama`.

- [ ] **Step 3: Implement `fetch_ollama`**

Append to `serving/admin/provider_quotas.py`:

```python
import re

from bs4 import BeautifulSoup


_USAGE_PATTERN = re.compile(
    r"(?P<label>session|weekly|monthly|daily)\s+usage[:\s]+(?P<used>[\d,]+)\s+of\s+(?P<limit>[\d,]+)\s+(?P<unit>requests?|tokens?|messages?)",
    re.IGNORECASE,
)


async def fetch_ollama() -> ProviderQuotaResult:
    """Scrape Ollama Cloud usage from the settings page (cookie-authenticated).

    Ollama exposes no quota API; we GET https://ollama.com/settings with the
    admin's session cookie and parse usage figures from the HTML. If the
    page structure changes, the fetcher returns parse_error so the admin
    knows the parser needs updating.
    """
    cookie = os.getenv("OLLAMA_SESSION_COOKIE", "")
    if not cookie:
        return ProviderQuotaResult(
            name="ollama",
            display_name="Ollama Cloud",
            key_configured=False,
            key_masked=None,
            fetched_at=_now(),
            ok=False,
            error="not_configured",
            usages=[],
        )

    url = "https://ollama.com/settings"
    headers = {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (compatible; freeinference-admin/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    }
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308, 401, 403):
                    return _err("ollama", "Ollama Cloud", cookie, "auth_failed")
                if resp.status >= 400:
                    return _err("ollama", "Ollama Cloud", cookie, "unexpected")
                html = await resp.text()
    except asyncio.TimeoutError:
        return _err("ollama", "Ollama Cloud", cookie, "timeout")
    except aiohttp.ClientError:
        return _err("ollama", "Ollama Cloud", cookie, "unexpected")
    except Exception:
        logger.exception("fetch_ollama: unexpected error")
        return _err("ollama", "Ollama Cloud", cookie, "unexpected")

    usages = _parse_ollama_html(html)
    if not usages:
        # Authenticated pages have usage figures; their absence usually
        # means cookie expired and we got a sign-in page instead.
        if "sign in" in html.lower() or "login" in html.lower():
            return _err("ollama", "Ollama Cloud", cookie, "auth_failed")
        return _err("ollama", "Ollama Cloud", cookie, "parse_error")

    return ProviderQuotaResult(
        name="ollama",
        display_name="Ollama Cloud",
        key_configured=True,
        key_masked=_mask_key(cookie),
        fetched_at=_now(),
        ok=True,
        error=None,
        usages=usages,
    )


def _parse_ollama_html(html: str) -> list[ProviderQuotaUsage]:
    """Best-effort extraction of usage figures from the Ollama settings page.

    Looks for text matches like 'Session usage: 42 of 100 requests'. Returns
    empty list if no recognizable usage rows found.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    usages: list[ProviderQuotaUsage] = []
    for match in _USAGE_PATTERN.finditer(text):
        try:
            used = float(match.group("used").replace(",", ""))
            limit = float(match.group("limit").replace(",", ""))
        except ValueError:
            continue
        unit = match.group("unit").lower().rstrip("s") + "s"  # normalize plural
        label = f"{match.group('label').capitalize()} usage"
        usages.append(
            ProviderQuotaUsage(
                label=label,
                used=used,
                limit=limit,
                unit=unit,
                reset_at=None,
            )
        )
    return usages
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest test/servers/test_admin_provider_quotas.py::TestFetchOllama -v
```

Expected: all 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add serving/admin/provider_quotas.py test/servers/test_admin_provider_quotas.py
git commit -m "feat(admin): add Ollama Cloud quota scraper (HTML parse)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: Add the admin route (TDD)

**Files:**
- Modify: `serving/servers/routers/admin.py`
- Modify: `serving/admin/provider_quotas.py` (add `gather_all` helper)
- Modify: `test/servers/test_admin_provider_quotas.py`

- [ ] **Step 1: Append failing test for `gather_all` aggregator**

Add to `test/servers/test_admin_provider_quotas.py`:

```python
from serving.admin.provider_quotas import gather_all


class TestGatherAll:
    @pytest.mark.asyncio
    async def test_gather_all_returns_four_results_even_if_one_raises(self, monkeypatch):
        monkeypatch.delenv("CHUTES_API_KEY", raising=False)
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_SESSION_COOKIE", raising=False)
        monkeypatch.delenv("OLLAMA_SESSION_COOKIE", raising=False)

        results = await gather_all()
        assert len(results) == 4
        names = {r.name for r in results}
        assert names == {"chutes", "zai", "minimax", "ollama"}
        assert all(r.error == "not_configured" for r in results)

    @pytest.mark.asyncio
    async def test_gather_all_handles_unexpected_exception(self, monkeypatch):
        async def boom():
            raise RuntimeError("simulated failure")

        # Patch one fetcher to raise; the gather should still return 4 results
        monkeypatch.setattr("serving.admin.provider_quotas.fetch_chutes", boom)
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_SESSION_COOKIE", raising=False)
        monkeypatch.delenv("OLLAMA_SESSION_COOKIE", raising=False)

        results = await gather_all()
        assert len(results) == 4
        chutes = next(r for r in results if r.name == "chutes")
        assert chutes.ok is False
        assert chutes.error == "unexpected"
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest test/servers/test_admin_provider_quotas.py::TestGatherAll -v
```

Expected: ImportError on `gather_all`.

- [ ] **Step 3: Implement `gather_all` in `serving/admin/provider_quotas.py`**

Append:

```python
async def gather_all() -> list[ProviderQuotaResult]:
    """Run all 4 provider fetchers in parallel; never raise.

    If a fetcher raises (rather than returning an error result), the
    exception is caught and converted to a `ProviderQuotaResult(ok=False,
    error='unexpected')` so the admin endpoint can always respond with a
    well-formed payload.
    """
    fetchers = [
        ("chutes", "Chutes", fetch_chutes),
        ("zai", "ZAI", fetch_zai),
        ("minimax", "MiniMax", fetch_minimax),
        ("ollama", "Ollama Cloud", fetch_ollama),
    ]
    raw = await asyncio.gather(
        *(f() for _, _, f in fetchers),
        return_exceptions=True,
    )
    out: list[ProviderQuotaResult] = []
    for (name, display_name, _), result in zip(fetchers, raw, strict=True):
        if isinstance(result, ProviderQuotaResult):
            out.append(result)
        else:
            logger.exception(
                "gather_all: %s fetcher raised", name, exc_info=result
            )
            out.append(
                ProviderQuotaResult(
                    name=name,
                    display_name=display_name,
                    key_configured=False,
                    key_masked=None,
                    fetched_at=_now(),
                    ok=False,
                    error="unexpected",
                    usages=[],
                )
            )
    return out
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest test/servers/test_admin_provider_quotas.py::TestGatherAll -v
```

Expected: 2 tests pass.

- [ ] **Step 5: Append failing test for the admin route**

```python
class TestProviderQuotasRoute:
    @pytest.mark.asyncio
    async def test_route_requires_admin_auth(self, admin_client):
        # No Authorization header → 401
        resp = await admin_client.get("/admin/provider-quotas")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_route_returns_aggregated_response(self, admin_client, monkeypatch, admin_jwt_header):
        # Wire all four env vars away → all return not_configured
        monkeypatch.delenv("CHUTES_API_KEY", raising=False)
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_SESSION_COOKIE", raising=False)
        monkeypatch.delenv("OLLAMA_SESSION_COOKIE", raising=False)

        resp = await admin_client.get("/admin/provider-quotas", headers=admin_jwt_header)
        assert resp.status_code == 200
        body = resp.json()
        assert "generated_at" in body
        assert len(body["providers"]) == 4
        assert {p["name"] for p in body["providers"]} == {"chutes", "zai", "minimax", "ollama"}
```

The `admin_client` and `admin_jwt_header` fixtures should already exist in `test/servers/conftest_auth.py` or `test_admin.py`. Inspect those before running:

```bash
grep -rn "admin_jwt_header\|admin_client" test/servers/conftest*.py test/servers/test_admin*.py | head -10
```

If `admin_jwt_header` doesn't exist by that exact name, find the equivalent (e.g. `admin_token_header`, or a fixture that returns `{"Authorization": "Bearer <token>"}`) and use the existing one. Update the test code to match. **Do not invent fixtures** — adapt to whatever the conftest provides.

If no admin auth fixture exists, replace the second test with a simpler one that mocks `verify_admin_access`:

```python
    @pytest.mark.asyncio
    async def test_route_returns_aggregated_response(self, admin_app, monkeypatch):
        from httpx import ASGITransport, AsyncClient
        from serving.servers.deps import verify_admin_access

        async def _fake_admin(*args, **kwargs):
            return "admin@test"

        admin_app.dependency_overrides[verify_admin_access] = _fake_admin

        monkeypatch.delenv("CHUTES_API_KEY", raising=False)
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_SESSION_COOKIE", raising=False)
        monkeypatch.delenv("OLLAMA_SESSION_COOKIE", raising=False)

        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/provider-quotas")
        admin_app.dependency_overrides.clear()
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["providers"]) == 4
```

- [ ] **Step 6: Run to verify failure**

```bash
uv run pytest test/servers/test_admin_provider_quotas.py::TestProviderQuotasRoute -v
```

Expected: 404 on the GET (route doesn't exist) or fixture errors.

- [ ] **Step 7: Add the route to `serving/servers/routers/admin.py`**

At the top of the file, find the existing schema imports (around line 11) and add:

```python
from serving.schemas_admin import (
    ...,  # existing imports
    AdminProviderQuotasResponse,
)
```

Add at the bottom of the file (after the last route):

```python
@router.get("/admin/provider-quotas", response_model=AdminProviderQuotasResponse)
async def admin_provider_quotas(
    admin_ip: str = Depends(verify_admin_access),
) -> AdminProviderQuotasResponse:
    """Return current quota status for each upstream LLM provider."""
    from datetime import datetime, timezone

    from serving.admin.provider_quotas import gather_all

    providers = await gather_all()
    return AdminProviderQuotasResponse(
        generated_at=datetime.now(timezone.utc),
        providers=providers,
    )
```

- [ ] **Step 8: Run tests**

```bash
uv run pytest test/servers/test_admin_provider_quotas.py::TestProviderQuotasRoute -v
```

Expected: both tests pass.

- [ ] **Step 9: Run the full test file end-to-end**

```bash
uv run pytest test/servers/test_admin_provider_quotas.py -v
```

Expected: all tests pass.

- [ ] **Step 10: Commit**

```bash
git add serving/admin/provider_quotas.py serving/servers/routers/admin.py test/servers/test_admin_provider_quotas.py
git commit -m "feat(admin): add GET /admin/provider-quotas route

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 10: Frontend — types and API client

**Files:**
- Modify: `frontend/src/lib/api/admin.ts`

- [ ] **Step 1: Append types and the API call function**

Append to `frontend/src/lib/api/admin.ts`:

```typescript
// ========================================
// Provider Quotas
// ========================================

export interface ProviderQuotaUsage {
  label: string;
  used: number | null;
  limit: number | null;
  unit: string;
  reset_at: string | null;
}

export interface ProviderQuotaResult {
  name: string;
  display_name: string;
  key_configured: boolean;
  key_masked: string | null;
  fetched_at: string | null;
  ok: boolean;
  error: string | null;
  usages: ProviderQuotaUsage[];
}

export interface AdminProviderQuotasResponse {
  generated_at: string;
  providers: ProviderQuotaResult[];
}

export async function getProviderQuotas(): Promise<AdminProviderQuotasResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/provider-quotas');
  return jsonOrThrow<AdminProviderQuotasResponse>(resp);
}
```

- [ ] **Step 2: Verify the file type-checks**

```bash
cd /srv/hybridInference/.worktrees/admin-provider-quotas/frontend
npm run typecheck 2>&1 | head -20
```

Expected: no new errors. (If `typecheck` script doesn't exist, run `npx tsc --noEmit` instead.)

- [ ] **Step 3: Commit**

```bash
cd /srv/hybridInference/.worktrees/admin-provider-quotas
git add frontend/src/lib/api/admin.ts
git commit -m "feat(admin): add provider quotas API client

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 11: Frontend — Providers tab UI

**Files:**
- Modify: `frontend/src/app/dashboard/admin/page.tsx`

The new tab goes between `requests` and `audit`, so the order becomes: Users → Recent Requests → Providers → Audit Log.

- [ ] **Step 1: Update the imports**

Find the import block at the top of `frontend/src/app/dashboard/admin/page.tsx` (around line 6) and add `ProviderQuotaResult, getProviderQuotas` to the existing imports from `@/lib/api/admin`:

```typescript
import {
  AdminUser,
  AdminRecentRequestItem,
  AdminRequestMetricsWindow,
  AuditLogEntry,
  ProviderQuotaResult,
  StatusCounts,
  UserDetail,
  UserSortBy,
  listUsers,
  getUserDetail,
  updateUser,
  approveUser,
  rejectUser,
  deleteUser,
  regenerateApiKeyAdmin,
  listAuditLog,
  listRecentRequests,
  getRequestMetrics,
  getProviderQuotas,
} from '@/lib/api/admin';
```

- [ ] **Step 2: Update the tab type and active-tab state**

Find the line `const [activeTab, setActiveTab] = useState<'users' | 'audit' | 'requests'>('users');` (around line 154) and change to:

```typescript
const [activeTab, setActiveTab] = useState<'users' | 'audit' | 'requests' | 'providers'>('users');
```

Update the `useEffect` block immediately below to accept the new tab:

```typescript
useEffect(() => {
  const params = new URLSearchParams(window.location.search);
  const tab = params.get('tab');
  if (tab === 'users' || tab === 'audit' || tab === 'requests' || tab === 'providers') {
    setActiveTab(tab);
  }
}, []);
```

Update the `onTabChange` function signature (around line 447) to:

```typescript
const onTabChange = (tab: 'users' | 'audit' | 'requests' | 'providers') => {
```

- [ ] **Step 3: Add provider-quotas state and loader**

After the `// Requests state` block (around line 213), add:

```typescript
// Providers state
const [providerQuotas, setProviderQuotas] = useState<ProviderQuotaResult[]>([]);
const [providerQuotasLoading, setProviderQuotasLoading] = useState(false);
```

Then add a loader callback after `loadRequestMetrics` (around line 277):

```typescript
const loadProviderQuotas = useCallback(async () => {
  setProviderQuotasLoading(true);
  setError(null);
  try {
    const d = await getProviderQuotas();
    setProviderQuotas(d.providers);
  } catch (e) {
    setError(getErrorMessage(e));
  } finally {
    setProviderQuotasLoading(false);
  }
}, []);
```

Add a `useEffect` near the existing tab-change effects (around line 298):

```typescript
useEffect(() => {
  if (activeTab === 'providers') loadProviderQuotas();
}, [loadProviderQuotas, activeTab]);
```

- [ ] **Step 4: Wire `refreshActiveTab` to handle providers**

Find `refreshActiveTab` (around line 455) and update:

```typescript
const refreshActiveTab = () => {
  if (activeTab === 'users') {
    load();
    return;
  }
  if (activeTab === 'audit') {
    loadAudit();
    return;
  }
  if (activeTab === 'providers') {
    loadProviderQuotas();
    return;
  }
  loadRequests();
  loadRequestMetrics();
};
```

Also update the loading-state expression in the Refresh button disabled prop (around line 494):

```typescript
disabled={loading || auditLoading || reqLoading || reqMetricsLoading || providerQuotasLoading}
```

And the inner text (next line):

```typescript
{loading || auditLoading || reqLoading || reqMetricsLoading || providerQuotasLoading ? 'Loading...' : 'Refresh'}
```

- [ ] **Step 5: Update the tab-button list**

Find the `(['users', 'requests', 'audit'] as const).map(...)` block (around line 509) and change to:

```typescript
{(['users', 'requests', 'providers', 'audit'] as const).map((tab) => (
  <button
    key={tab}
    onClick={() => onTabChange(tab)}
    className={`rounded-md px-3.5 py-1.5 text-[13px] font-medium transition ${
      activeTab === tab
        ? 'bg-gray-900 text-white'
        : 'text-gray-500 hover:bg-gray-100 hover:text-gray-900'
    }`}
  >
    {tab === 'users'
      ? 'Users'
      : tab === 'requests'
        ? 'Recent Requests'
        : tab === 'providers'
          ? 'Providers'
          : 'Audit Log'}
  </button>
))}
```

- [ ] **Step 6: Add the Providers tab content**

Find the closing `)}` of the Audit Log tab block (around line 1072) and **before** the start of the Requests tab block (around line 1075), the natural spot for a new tab block is between Audit and Requests. Easier and cleaner: add the new block **after** the Audit Log tab block (around line 1072, before `{/* ========== Requests Tab ========== */}`).

Add this block:

```tsx
{/* ========== Providers Tab ========== */}
{activeTab === 'providers' && (
  <div className="mt-6">
    {providerQuotasLoading ? (
      <div className="flex justify-center py-24">
        <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
      </div>
    ) : providerQuotas.length === 0 ? (
      <div className="py-24 text-center">
        <p className="text-[13px] text-gray-400">No provider data.</p>
      </div>
    ) : (
      <div className="grid gap-3 sm:grid-cols-2">
        {providerQuotas.map((p) => (
          <ProviderCard key={p.name} provider={p} />
        ))}
      </div>
    )}
  </div>
)}
```

- [ ] **Step 7: Add the `ProviderCard` component**

Above the `AdminPage` component declaration (i.e., near the other helper components like `RequestMetricsCard`), add:

```tsx
function pct(used: number | null, limit: number | null): number | null {
  if (used == null || limit == null || limit <= 0) return null;
  return Math.min(100, (used / limit) * 100);
}

function formatNum(v: number | null): string {
  if (v == null) return '—';
  if (Math.abs(v) < 1 && v !== 0) return v.toFixed(4);
  if (Number.isInteger(v)) return v.toLocaleString();
  return v.toFixed(2);
}

function ProviderCard({ provider }: { provider: ProviderQuotaResult }) {
  const stripeColor = provider.ok
    ? 'bg-emerald-500'
    : provider.error === 'not_configured'
      ? 'bg-gray-300'
      : 'bg-red-400';

  return (
    <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
      <div className={`h-1 ${stripeColor}`} />
      <div className="p-4">
        <div className="flex items-baseline justify-between gap-3">
          <h3 className="text-[15px] font-semibold text-gray-900">{provider.display_name}</h3>
          <span
            className={`tabular-nums text-[11px] ${provider.key_configured ? 'text-gray-500' : 'text-gray-400'}`}
          >
            {provider.key_masked ?? 'Not configured'}
          </span>
        </div>

        {provider.ok ? (
          provider.usages.length === 0 ? (
            <p className="mt-3 text-[12px] text-gray-400">No usage data returned.</p>
          ) : (
            <div className="mt-3 space-y-3">
              {provider.usages.map((u, i) => {
                const p = pct(u.used, u.limit);
                return (
                  <div key={i}>
                    <div className="flex items-baseline justify-between text-[12px]">
                      <span className="text-gray-600">{u.label}</span>
                      <span className="tabular-nums text-gray-700">
                        {formatNum(u.used)}
                        {u.limit != null && ` / ${formatNum(u.limit)}`} {u.unit}
                        {p != null && (
                          <span className="ml-1 text-gray-400">({p.toFixed(0)}%)</span>
                        )}
                      </span>
                    </div>
                    {p != null && (
                      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-gray-100">
                        <div
                          className={`h-full ${
                            p >= 90 ? 'bg-red-400' : p >= 70 ? 'bg-amber-400' : 'bg-gray-900'
                          }`}
                          style={{ width: `${p}%` }}
                        />
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )
        ) : (
          <p className="mt-3 text-[12px] text-gray-400">
            Quota unavailable — <span className="text-gray-500">{provider.error}</span>
          </p>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 8: Type-check and lint**

```bash
cd /srv/hybridInference/.worktrees/admin-provider-quotas/frontend
npm run typecheck 2>&1 | tail -20
npm run lint 2>&1 | tail -20
```

Expected: no new errors. Fix any introduced.

- [ ] **Step 9: Commit**

```bash
cd /srv/hybridInference/.worktrees/admin-provider-quotas
git add frontend/src/app/dashboard/admin/page.tsx
git commit -m "feat(admin): add Providers tab to admin dashboard

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 12: Document the new env vars

**Files:**
- Modify: `.env.example` (or create if missing)

- [ ] **Step 1: Check if `.env.example` exists**

```bash
ls /srv/hybridInference/.worktrees/admin-provider-quotas/.env.example 2>&1 || echo MISSING
```

If MISSING, just skip this task (the cookie env vars are documented in the spec and in the settings.py docstring).

- [ ] **Step 2: If it exists, append a new section**

Append to `.env.example`:

```
# ====== Admin Dashboard: Provider Quotas Tab ======
# Browser session cookies pasted from DevTools after logging in.
# Used by the Admin Dashboard "Providers" tab to fetch live quota.
# Re-paste when these expire.
MINIMAX_SESSION_COOKIE=
OLLAMA_SESSION_COOKIE=
```

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "docs: document MINIMAX_SESSION_COOKIE and OLLAMA_SESSION_COOKIE

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 13: Manual verification

**Files:** none.

This is a non-coding verification step. Run it once before opening the PR.

- [ ] **Step 1: Run the full test suite to make sure nothing is broken**

```bash
cd /srv/hybridInference/.worktrees/admin-provider-quotas
make test 2>&1 | tail -30
```

Expected: all tests pass (or only pre-existing failures unrelated to this change). Investigate any new failures introduced by this branch.

- [ ] **Step 2: Run the backend locally**

```bash
cd /srv/hybridInference/.worktrees/admin-provider-quotas
# Make sure .env has CHUTES_API_KEY at minimum (the only one with a working API)
uv run uvicorn serving.servers.app:app --host 127.0.0.1 --port 8080 --reload &
sleep 3
```

- [ ] **Step 3: Hit the endpoint with admin auth**

If `ADMIN_TOKEN` is set in the environment:

```bash
curl -sS http://127.0.0.1:8080/admin/provider-quotas -H "Authorization: Bearer $ADMIN_TOKEN" | jq
```

Expected: a JSON response with `generated_at` and `providers: [chutes, zai, minimax, ollama]`. Chutes should have `ok: true` if the key is real; others may show `not_configured` or `auth_failed` depending on what's in the env. **None should be HTTP 500.**

- [ ] **Step 4: Verify the frontend tab renders**

```bash
cd /srv/hybridInference/.worktrees/admin-provider-quotas/frontend
npm run dev &
sleep 5
```

Visit `http://localhost:3001/dashboard/admin?tab=providers` in a browser as an admin user. Confirm:
- The "Providers" tab button appears between "Recent Requests" and "Audit Log".
- Clicking it shows 4 cards (Chutes, ZAI, MiniMax, Ollama Cloud).
- Each card shows the masked key or "Not configured".
- Cards in error states show "Quota unavailable — <error code>".
- Refresh button re-fetches.

- [ ] **Step 5: Stop the dev servers**

```bash
kill %1 %2 2>/dev/null || true
```

- [ ] **Step 6: Commit any tweaks**

If steps 3 or 4 surfaced issues, fix them and commit. Otherwise no commit.

---

## Task 14: Open the PR

**Files:** none.

- [ ] **Step 1: Push the branch**

```bash
cd /srv/hybridInference/.worktrees/admin-provider-quotas
git push -u origin jason/claude/admin-provider-quotas
```

- [ ] **Step 2: Open the PR against `dev`**

```bash
gh pr create --base dev --title "feat(admin): provider quotas tab" --body "$(cat <<'EOF'
## Summary
- New "Providers" tab in admin dashboard showing masked API key + live quota for Chutes, ZAI, MiniMax, Ollama Cloud
- Backend endpoint `GET /admin/provider-quotas` aggregates 4 async fetchers in parallel; never returns 500 from a provider failure
- Two new env vars: `MINIMAX_SESSION_COOKIE`, `OLLAMA_SESSION_COOKIE` (browser session cookies, since those providers require cookie auth)

## Spec
- `docs/agents/specs/2026-04-30-admin-provider-quotas-design.md`

## Test plan
- [ ] `make test` passes locally
- [ ] `/admin/provider-quotas` returns aggregated JSON with 4 entries
- [ ] Frontend tab renders correctly for configured / not_configured / auth_failed states
- [ ] Refresh button works
EOF
)"
```

Expected: PR URL printed.

- [ ] **Step 3: Report the PR URL back**

Print the URL returned by `gh pr create`.

---

## Self-Review Checklist

After implementing every task above, verify against the spec:

- [ ] **Spec coverage**
  - Section 2 (per-provider fetcher): Tasks 4–8 implement all four
  - Section 3 (backend endpoint and data shape): Tasks 2 + 9
  - Section 4 (frontend tab): Tasks 10–11
  - Section 5 (error handling, testing, security): covered across Tasks 4–9 (each fetcher has explicit timeout/auth/parse tests; the route never raises)
  - Configuration changes: Tasks 1 + 12

- [ ] **Type consistency**
  - Backend `ProviderQuotaResult` ↔ frontend `ProviderQuotaResult` field-by-field match (`name`, `display_name`, `key_configured`, `key_masked`, `fetched_at`, `ok`, `error`, `usages`)
  - Function names: `fetch_chutes`, `fetch_zai`, `fetch_minimax`, `fetch_ollama`, `gather_all`, `_mask_key`, `_err`, `_now`, `_parse_chutes_usage`, `_parse_ollama_html` — all referenced consistently
  - Tab names: `'users' | 'requests' | 'providers' | 'audit'` consistent across `useState` type, `onTabChange` signature, URL parsing, and tab-button list
  - Env var names: `CHUTES_API_KEY`, `ZAI_API_KEY`, `MINIMAX_SESSION_COOKIE`, `OLLAMA_SESSION_COOKIE` — same in code and tests

- [ ] **No placeholders** — every step contains either real code, a real command, or a documented decision (e.g., the `admin_jwt_header` fixture lookup in Task 9 step 5 explicitly tells the engineer how to adapt to whatever exists).
