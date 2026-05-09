# Log Rejected Inference Requests — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist concurrency 429s, quota 429s, inference-path auth 4xxs, and Anthropic-shape model-not-found 404s into `api_logs` (the existing successful-request table) behind a runtime-toggleable admin setting (`log_rejected_requests`, default `False`).

**Architecture:** A small fire-and-forget helper `log_rejection(...)` in `serving/observability/rejection_log.py`. Helper short-circuits on toggle-off, on missing dependencies, and on non-inference paths. Each rejection site calls it via `asyncio.create_task(...)` so the HTTP rejection response is not delayed by a DB round-trip. Toggle is a new `bool` entry in the existing `RUNTIME_SETTINGS_REGISTRY`.

**Tech Stack:** FastAPI, Pydantic, asyncio, pytest / pytest-asyncio, asyncpg.

**Spec:** `docs/superpowers/specs/2026-05-04-log-rejected-requests-design.md`
**Issue:** #426

---

## File Structure

| File | Role | Status |
|---|---|---|
| `apps/backend/serving/config/runtime_settings.py` | Register `log_rejected_requests` (bool, default False) | Modify |
| `apps/backend/serving/observability/rejection_log.py` | New helper module: `INFERENCE_PATH_PREFIXES` constant + `log_rejection(...)` async function | **Create** |
| `apps/backend/serving/servers/concurrency.py` | Call helper before raising 429 in `enforce_user_concurrency` | Modify |
| `apps/backend/serving/servers/auth.py` | Call helper at 3 auth sites + 1 quota site in `verify_api_key` | Modify |
| `apps/backend/serving/servers/routers/anthropic_messages.py` | Call helper in `anthropic_messages` when `_resolve` raises 404 | Modify |
| `tests/observability/test_rejection_log.py` | New unit tests for helper (toggle, path filter, error swallow) | **Create** |
| `tests/servers/test_admin_settings.py` | Existing — assertion that `log_rejected_requests` appears in registry list | Modify |

---

## Task 1: Register the runtime setting

**Files:**
- Modify: `apps/backend/serving/config/runtime_settings.py`
- Test: `tests/servers/test_admin_settings.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/servers/test_admin_settings.py`:

```python
@pytest.mark.asyncio
async def test_list_settings_includes_log_rejected_requests(admin_client):
    """The new log_rejected_requests bool setting is exposed via /admin/settings."""
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)

    response = await client.get(
        "/admin/settings",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 200
    by_key = {item["key"]: item for item in response.json()["settings"]}
    assert "log_rejected_requests" in by_key
    assert by_key["log_rejected_requests"]["value_type"] == "bool"
    assert by_key["log_rejected_requests"]["default_value"] is False
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /home/juncheng/hybridInference-worktrees/log-rejected-requests
uv run pytest tests/servers/test_admin_settings.py::test_list_settings_includes_log_rejected_requests -v
```

Expected: FAIL — key not registered yet.

- [ ] **Step 3: Add the registry entry**

In `apps/backend/serving/config/runtime_settings.py`, append a new entry to `RUNTIME_SETTINGS_REGISTRY` (after the existing entries, alongside `log_full_payload` so all log-related toggles are grouped):

```python
"log_rejected_requests": {
    "type": "bool",
    "default": False,
    "description": (
        "Persist rejected inference requests (rate-limit, quota, auth, "
        "model-not-found) to api_logs with metadata.rejection=true."
    ),
},
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run pytest tests/servers/test_admin_settings.py::test_list_settings_includes_log_rejected_requests -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/config/runtime_settings.py tests/servers/test_admin_settings.py
git commit -m "feat(settings): register log_rejected_requests runtime toggle"
```

---

## Task 2: Create the `rejection_log` helper module

**Files:**
- Create: `apps/backend/serving/observability/rejection_log.py`
- Test: `tests/observability/test_rejection_log.py`

- [ ] **Step 1: Create the test directory**

```bash
mkdir -p /home/juncheng/hybridInference-worktrees/log-rejected-requests/tests/observability
```

If `tests/observability/__init__.py` doesn't exist, create it as an empty file:

```bash
touch /home/juncheng/hybridInference-worktrees/log-rejected-requests/tests/observability/__init__.py
```

- [ ] **Step 2: Write the failing tests**

Create `tests/observability/test_rejection_log.py`:

```python
"""Tests for the rejection_log helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.observability.rejection_log import (
    INFERENCE_PATH_PREFIXES,
    log_rejection,
)


def _fake_request(path: str = "/v1/chat/completions") -> MagicMock:
    """Minimal Request-shaped mock with the URL path the helper reads."""
    req = MagicMock()
    req.url.path = path
    # Simulate the headers FastAPI requests expose; remote-IP helper reads them.
    req.headers = {"x-forwarded-for": "203.0.113.5"}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    return req


@pytest.fixture
def fake_log_store():
    store = MagicMock()
    store.log_request = AsyncMock(return_value=None)
    return store


@pytest.fixture
def runtime_on():
    rs = MagicMock()
    rs.get_bool = AsyncMock(return_value=True)
    return rs


@pytest.fixture
def runtime_off():
    rs = MagicMock()
    rs.get_bool = AsyncMock(return_value=False)
    return rs


@pytest.mark.asyncio
async def test_inference_prefixes_set():
    """The path-filter list covers all five inference routes."""
    assert "/v1/chat/completions" in INFERENCE_PATH_PREFIXES
    assert "/v1/completions" in INFERENCE_PATH_PREFIXES
    assert "/v1/embeddings" in INFERENCE_PATH_PREFIXES
    assert "/completion" in INFERENCE_PATH_PREFIXES
    assert "/anthropic/v1/messages" in INFERENCE_PATH_PREFIXES


@pytest.mark.asyncio
async def test_toggle_off_does_not_log(fake_log_store, runtime_off):
    await log_rejection(
        log_store=fake_log_store,
        runtime_settings=runtime_off,
        request=_fake_request(),
        status_code=429,
        error_code="concurrency_limit_exceeded",
        reason="limit=1 role=free",
        user={"user_id": "u1", "role": "free"},
    )
    fake_log_store.log_request.assert_not_called()


@pytest.mark.asyncio
async def test_toggle_on_writes_row(fake_log_store, runtime_on):
    await log_rejection(
        log_store=fake_log_store,
        runtime_settings=runtime_on,
        request=_fake_request("/v1/chat/completions"),
        status_code=429,
        error_code="concurrency_limit_exceeded",
        reason="limit=1 role=free",
        user={"user_id": "u1", "role": "free"},
        model_id="gpt-4",
    )
    fake_log_store.log_request.assert_awaited_once()
    kwargs = fake_log_store.log_request.await_args.kwargs
    assert kwargs["model_id"] == "gpt-4"
    assert kwargs["provider"] == ""
    assert kwargs["prompt"] is None
    assert kwargs["response"] is None
    assert kwargs["usage"] is None
    assert kwargs["latency_ms"] == 0
    assert kwargs["status_code"] == 429
    assert kwargs["error"] == "concurrency_limit_exceeded"
    md = kwargs["metadata"]
    assert md["rejection"] is True
    assert md["reason"] == "limit=1 role=free"
    assert md["route"] == "/v1/chat/completions"
    assert md["role"] == "free"
    assert md["user_id"] == "u1"


@pytest.mark.asyncio
async def test_log_store_none_is_noop(runtime_on):
    # Should not raise even though log_store is None.
    await log_rejection(
        log_store=None,
        runtime_settings=runtime_on,
        request=_fake_request(),
        status_code=429,
        error_code="quota_exceeded",
        reason="quota=1.0 spent=1.5",
        user={"user_id": "u1", "role": "free"},
    )
    runtime_on.get_bool.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_settings_none_is_noop(fake_log_store):
    await log_rejection(
        log_store=fake_log_store,
        runtime_settings=None,
        request=_fake_request(),
        status_code=429,
        error_code="quota_exceeded",
        reason="x",
        user={"user_id": "u1"},
    )
    fake_log_store.log_request.assert_not_called()


@pytest.mark.asyncio
async def test_non_inference_path_is_noop(fake_log_store, runtime_on):
    await log_rejection(
        log_store=fake_log_store,
        runtime_settings=runtime_on,
        request=_fake_request("/admin/settings"),
        status_code=401,
        error_code="auth_invalid",
        reason="missing key",
        user=None,
    )
    fake_log_store.log_request.assert_not_called()
    # We didn't even need to read the toggle.
    runtime_on.get_bool.assert_not_called()


@pytest.mark.asyncio
async def test_unauthenticated_user_writes_null(fake_log_store, runtime_on):
    await log_rejection(
        log_store=fake_log_store,
        runtime_settings=runtime_on,
        request=_fake_request("/v1/chat/completions"),
        status_code=401,
        error_code="auth_missing",
        reason="no header",
        user=None,
    )
    fake_log_store.log_request.assert_awaited_once()
    kwargs = fake_log_store.log_request.await_args.kwargs
    md = kwargs["metadata"]
    assert md["user_id"] is None
    assert md["role"] is None


@pytest.mark.asyncio
async def test_log_store_failure_is_swallowed(fake_log_store, runtime_on, caplog):
    fake_log_store.log_request = AsyncMock(side_effect=RuntimeError("db down"))
    # Helper must not propagate the exception.
    await log_rejection(
        log_store=fake_log_store,
        runtime_settings=runtime_on,
        request=_fake_request(),
        status_code=429,
        error_code="concurrency_limit_exceeded",
        reason="x",
        user={"user_id": "u1", "role": "free"},
    )
    # Spot-check: an error-level log was emitted.
    assert any("rejection_log_failed" in rec.message for rec in caplog.records)
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
uv run pytest tests/observability/test_rejection_log.py -v
```

Expected: FAIL — module does not exist.

- [ ] **Step 4: Implement the helper**

Create `apps/backend/serving/observability/rejection_log.py`:

```python
"""Best-effort persistent log of rejected inference requests.

Writes a row to ``api_logs`` (via :class:`BaseLogStore.log_request`) for
inference-path requests rejected at the gate — concurrency limit, quota,
auth failure, model-not-found. Gated by the ``log_rejected_requests``
runtime setting (default off). Never raises: a logging failure must not
alter the rejection HTTP response.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from serving.utils import context as req_ctx
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip

if TYPE_CHECKING:
    from fastapi import Request

    from serving.config.runtime_settings import RuntimeSettings
    from serving.storage.base import BaseLogStore

logger = get_logger(__name__)

INFERENCE_PATH_PREFIXES: tuple[str, ...] = (
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/embeddings",
    "/completion",
    "/anthropic/v1/messages",
)


def _is_inference_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in INFERENCE_PATH_PREFIXES)


async def log_rejection(
    *,
    log_store: BaseLogStore | None,
    runtime_settings: RuntimeSettings | None,
    request: Request,
    status_code: int,
    error_code: str,
    reason: str,
    user: dict[str, Any] | None,
    model_id: str = "",
) -> None:
    """Persist a rejection row when the toggle is on.

    Parameters mirror the rejection context: ``error_code`` is a short
    machine-readable identifier (e.g. ``"concurrency_limit_exceeded"``);
    ``reason`` is a brief human-readable detail; ``user`` is the verified
    user dict or ``None`` for pre-auth rejections.
    """
    if log_store is None or runtime_settings is None:
        return
    if not _is_inference_path(request.url.path):
        return

    try:
        enabled = await runtime_settings.get_bool("log_rejected_requests")
    except Exception:
        logger.exception(
            "rejection_log_failed",
            extra={"event": "rejection_log_failed", "stage": "toggle_read"},
        )
        return
    if not enabled:
        return

    ctx = req_ctx.get()
    request_id = ctx.get("request_id") or ""
    metadata: dict[str, Any] = {
        "rejection": True,
        "reason": reason,
        "route": request.url.path,
        "role": user.get("role") if user else None,
        "user_id": user.get("user_id") if user else None,
        "remote_ip": get_client_ip(request),
    }

    try:
        await log_store.log_request(
            request_id=request_id,
            model_id=model_id,
            provider="",
            prompt=None,
            response=None,
            usage=None,
            latency_ms=0,
            status_code=status_code,
            error=error_code,
            params=None,
            metadata=metadata,
        )
    except Exception:
        logger.exception(
            "rejection_log_failed",
            extra={
                "event": "rejection_log_failed",
                "stage": "log_request",
                "error_code": error_code,
                "status_code": status_code,
            },
        )
```

Notes for the implementer:
- `BaseLogStore.log_request` declares `prompt: list[dict[str, Any]] | str` (i.e. it does not accept `None`). Confirm by reading `apps/backend/serving/storage/base.py` around line 665. If passing `prompt=None` violates the type, change the signature in the helper to `prompt=""` (empty string) — the Postgres column is `prompt TEXT` (nullable). Empty string is preferable to `None` here because it avoids JSON-encoding `None` as the string `"null"` in `prompt_str`.
- The Postgres `log_request` implementation (`apps/backend/serving/storage/postgres_log.py`) auto-computes `prompt_hash` even when `store_full_content` is false. For rejection rows (no real prompt), passing `prompt=""` results in a hash of the empty string; that's fine and stable.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/observability/test_rejection_log.py -v
```

Expected: all 8 tests pass. If `test_toggle_on_writes_row` fails on a `prompt` argument shape, switch the helper to pass `prompt=""` and update the assertion accordingly (`assert kwargs["prompt"] == ""`).

- [ ] **Step 6: Commit**

```bash
git add apps/backend/serving/observability/rejection_log.py tests/observability/__init__.py tests/observability/test_rejection_log.py
git commit -m "feat(observability): add log_rejection helper for inference-path 4xx"
```

---

## Task 3: Wire `log_rejection` into `enforce_user_concurrency`

**Files:**
- Modify: `apps/backend/serving/servers/concurrency.py`
- Test: `tests/servers/test_enforce_user_concurrency.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/servers/test_enforce_user_concurrency.py`:

```python
@pytest.mark.asyncio
async def test_concurrency_429_calls_log_rejection(monkeypatch):
    """When a request is rejected with 429, log_rejection is fired off."""
    from unittest.mock import AsyncMock

    log_calls: list[dict] = []

    async def fake_log_rejection(**kwargs):
        log_calls.append(kwargs)

    monkeypatch.setattr(
        "serving.servers.concurrency.log_rejection",
        fake_log_rejection,
    )

    user = {"user_id": "u1", "role": "free", "is_admin": False}
    limiter = UserConcurrencyLimiter(static_limits_provider(LIMITS))
    app = _make_app(user, limiter)

    # Stash fake services on app.state so the wired call can find them.
    app.state.services = type("S", (), {})()
    app.state.services.log_store = AsyncMock()
    app.state.services.runtime_settings = AsyncMock()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Saturate the user's slot first.
        granted, _, _ = await limiter.try_acquire(user["user_id"], "free", False)
        assert granted

        resp = await client.get("/probe")
        assert resp.status_code == 429

    # Helper should have been invoked exactly once with concurrency error code.
    assert len(log_calls) == 1
    call = log_calls[0]
    assert call["status_code"] == 429
    assert call["error_code"] == "concurrency_limit_exceeded"
    assert call["user"]["user_id"] == "u1"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/servers/test_enforce_user_concurrency.py::test_concurrency_429_calls_log_rejection -v
```

Expected: FAIL — `serving.servers.concurrency.log_rejection` not imported / not called.

- [ ] **Step 3: Add the call in `enforce_user_concurrency`**

In `apps/backend/serving/servers/concurrency.py`, near the top of the existing dependency block (the `from typing import TYPE_CHECKING, Any` block at line ~169), add an import for the helper:

```python
from serving.observability.rejection_log import log_rejection
```

Place it at module level (not inside the function), grouped with the other `serving.*` imports. If the existing top-of-file import block does not have a `serving.observability` line yet, add it after `from serving.observability.metrics import (...)`.

Then locate the `if not granted:` branch in `enforce_user_concurrency` (around line 175 in the original file). Just before `raise HTTPException(...)`, insert the fire-and-forget call:

```python
        services = getattr(request.app.state, "services", None)
        log_store = getattr(services, "log_store", None) if services else None
        runtime_settings = getattr(services, "runtime_settings", None) if services else None
        asyncio.create_task(
            log_rejection(
                log_store=log_store,
                runtime_settings=runtime_settings,
                request=request,
                status_code=429,
                error_code="concurrency_limit_exceeded",
                reason=f"limit={limit} role={role_label}",
                user=user,
            )
        )
```

`asyncio` is already imported at the top of the file. `request` is the `Request` parameter of `enforce_user_concurrency`.

- [ ] **Step 4: Run the affected tests to verify**

```bash
uv run pytest tests/servers/test_enforce_user_concurrency.py -v
```

Expected: all tests pass, including the new one. Existing tests don't set `app.state.services`, so the helper takes the `log_store is None` short-circuit and does nothing — no regression.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/servers/concurrency.py tests/servers/test_enforce_user_concurrency.py
git commit -m "feat(concurrency): emit log_rejection on 429"
```

---

## Task 4: Wire `log_rejection` into `verify_api_key`

**Files:**
- Modify: `apps/backend/serving/servers/auth.py`
- Test: `tests/servers/test_auth_routes.py` (existing) or `tests/servers/test_auth_rejection_log.py` (new — pick whichever fits the existing test layout; the plan uses a new file for clarity)

- [ ] **Step 1: Sanity-check existing tests still pass**

```bash
uv run pytest tests/servers/test_auth_routes.py -q
```

Expected: all pass on the unmodified branch (baseline before edits).

- [ ] **Step 2: Write failing tests for the four sites**

Create `tests/servers/test_auth_rejection_log.py`:

```python
"""Tests verifying verify_api_key fires log_rejection at its rejection sites."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Header, Request
from httpx import ASGITransport, AsyncClient

from serving.servers.auth import verify_api_key


def _build_app(monkeypatch, *, op_store_user: dict[str, Any] | None) -> tuple[FastAPI, list[dict]]:
    """Wire up a tiny app whose only endpoint depends on verify_api_key.

    Returns the app plus the list that captures log_rejection invocations.
    """
    log_calls: list[dict] = []

    async def fake_log_rejection(**kwargs):
        log_calls.append(kwargs)

    monkeypatch.setattr(
        "serving.servers.auth.log_rejection",
        fake_log_rejection,
    )

    app = FastAPI()

    async def fake_op_store_dep():
        op = MagicMock()
        op.get_auth_context_by_key_hash = AsyncMock(return_value=op_store_user)
        op.get_user_cost_today = AsyncMock(return_value=0.0)
        op.update_key_last_used = AsyncMock()
        return op

    async def fake_log_store_dep():
        return MagicMock()

    from serving.servers.deps import get_log_store, get_operational_store

    app.dependency_overrides[get_operational_store] = fake_op_store_dep
    app.dependency_overrides[get_log_store] = fake_log_store_dep

    @app.get("/v1/chat/completions")
    async def hit(user: dict = pytest.importorskip("fastapi").Depends(verify_api_key)):
        return {"ok": True}

    # Stub services so the helper can read log_store / runtime_settings.
    app.state.services = type("S", (), {})()
    app.state.services.log_store = MagicMock()
    app.state.services.runtime_settings = MagicMock()
    return app, log_calls


@pytest.mark.asyncio
async def test_missing_api_key_logs_rejection(monkeypatch):
    """No Authorization header → 401 + log_rejection(error_code='auth_missing')."""
    app, log_calls = _build_app(monkeypatch, op_store_user=None)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/chat/completions")
    assert resp.status_code == 401
    assert len(log_calls) == 1
    assert log_calls[0]["error_code"] == "auth_missing"
    assert log_calls[0]["status_code"] == 401
    assert log_calls[0]["user"] is None


@pytest.mark.asyncio
async def test_invalid_api_key_logs_rejection(monkeypatch):
    """Unknown key → 401 + log_rejection(error_code='auth_invalid')."""
    app, log_calls = _build_app(monkeypatch, op_store_user=None)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer hyi-bogus"},
        )
    assert resp.status_code == 401
    assert len(log_calls) == 1
    assert log_calls[0]["error_code"] == "auth_invalid"


@pytest.mark.asyncio
async def test_quota_exceeded_logs_rejection(monkeypatch):
    """Authenticated user over quota → 429 + log_rejection(error_code='quota_exceeded')."""
    user_row = {
        "id": 1,
        "user_id": "u1",
        "user_name": "Test",
        "role": "free",
        "email": None,
        "email_verified": True,
        "quota_daily_cost_usd": 0.001,  # very low
    }
    app, log_calls = _build_app(monkeypatch, op_store_user=user_row)

    # Override get_user_cost_today to push us over the quota.
    async def fake_op_store_dep():
        op = MagicMock()
        op.get_auth_context_by_key_hash = AsyncMock(return_value=user_row)
        op.get_user_cost_today = AsyncMock(return_value=10.0)
        op.update_key_last_used = AsyncMock()
        return op

    from serving.servers.deps import get_operational_store

    app.dependency_overrides[get_operational_store] = fake_op_store_dep

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer hyi-valid"},
        )
    assert resp.status_code == 429
    assert len(log_calls) == 1
    assert log_calls[0]["error_code"] == "quota_exceeded"
    assert log_calls[0]["user"]["user_id"] == "u1"
```

(The third common rejection — email-not-verified 403 — is intentionally omitted from this PR; it's gated by `signup_require_email_verification` and produces a 403 not a 4xx-rate-limit. If you choose to include it, add a fourth test analogous to the above; the spec leaves it optional.)

- [ ] **Step 3: Run the tests to verify they fail**

```bash
uv run pytest tests/servers/test_auth_rejection_log.py -v
```

Expected: FAIL — `log_rejection` is not imported in `serving.servers.auth`.

- [ ] **Step 4: Wire the helper into the three sites**

In `apps/backend/serving/servers/auth.py`:

a) Add an import at the top, grouped with the other `serving.*` imports:

```python
from serving.observability.rejection_log import log_rejection
```

b) Define a small local helper inside the module (top-level, just above `verify_api_key`) to keep the four call sites readable:

```python
def _services_from_request(request: Request):
    services = getattr(request.app.state, "services", None)
    log_store = getattr(services, "log_store", None) if services else None
    runtime_settings = getattr(services, "runtime_settings", None) if services else None
    return log_store, runtime_settings
```

c) **Site 1: missing API key** (currently around line 129). Just before `raise HTTPException(status_code=401, detail="Missing API key. ...")`, insert:

```python
        log_store_, rs_ = _services_from_request(request)
        asyncio.create_task(
            log_rejection(
                log_store=log_store_,
                runtime_settings=rs_,
                request=request,
                status_code=401,
                error_code="auth_missing",
                reason="missing_api_key",
                user=None,
            )
        )
```

`asyncio` may need to be imported at the top of `auth.py`; check the existing imports and add `import asyncio` if missing.

d) **Site 2: invalid API key** (currently around line 173). Just before `raise HTTPException(status_code=401, detail="Invalid or expired API key")`:

```python
        log_store_, rs_ = _services_from_request(request)
        asyncio.create_task(
            log_rejection(
                log_store=log_store_,
                runtime_settings=rs_,
                request=request,
                status_code=401,
                error_code="auth_invalid",
                reason=f"key_prefix={api_key[:6] if api_key else None}",
                user=None,
            )
        )
```

e) **Site 3: quota exceeded** (currently around line 221). Just before the `raise HTTPException(status_code=429, ...)`:

```python
        log_store_, rs_ = _services_from_request(request)
        asyncio.create_task(
            log_rejection(
                log_store=log_store_,
                runtime_settings=rs_,
                request=request,
                status_code=429,
                error_code="quota_exceeded",
                reason=(
                    f"quota_usd={quota_daily_cost_usd:.4f} "
                    f"spent_usd={cost_spent:.4f}"
                ),
                user={
                    "user_id": user["user_id"],
                    "role": user.get("role") or "free",
                },
            )
        )
```

(We don't pass the full DB user row because callers' tests expect a small dict; this also avoids accidentally logging fields like `auth_key_hash`.)

- [ ] **Step 5: Run the new tests + the full auth test file**

```bash
uv run pytest tests/servers/test_auth_rejection_log.py tests/servers/test_auth_routes.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/serving/servers/auth.py tests/servers/test_auth_rejection_log.py
git commit -m "feat(auth): emit log_rejection for missing/invalid key + quota 429"
```

---

## Task 5: Wire `log_rejection` into the Anthropic route

**Files:**
- Modify: `apps/backend/serving/servers/routers/anthropic_messages.py`
- Test: `tests/servers/test_anthropic_messages_rejection.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/servers/test_anthropic_messages_rejection.py`:

```python
"""Test that anthropic /v1/messages emits log_rejection on 404."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import get_log_store, get_router


@pytest.mark.asyncio
async def test_anthropic_unknown_model_logs_rejection(monkeypatch):
    log_calls: list[dict] = []

    async def fake_log_rejection(**kwargs):
        log_calls.append(kwargs)

    monkeypatch.setattr(
        "serving.servers.routers.anthropic_messages.log_rejection",
        fake_log_rejection,
    )

    from serving.servers.routers import anthropic_messages as mod

    app = FastAPI()
    app.include_router(mod.router)

    # Stub user, router (with no routes), no concurrency limiter.
    user = {"user_id": "u1", "role": "free", "is_admin": False, "authenticated": True}

    fake_router = MagicMock()
    fake_router.routes = {}

    async def _verify(): return user
    async def _get_router(): return fake_router
    async def _get_log_store(): return MagicMock()
    async def _enforce(): yield

    app.dependency_overrides[verify_api_key] = _verify
    app.dependency_overrides[get_router] = _get_router
    app.dependency_overrides[get_log_store] = _get_log_store
    app.dependency_overrides[enforce_user_concurrency] = _enforce

    app.state.services = type("S", (), {})()
    app.state.services.log_store = MagicMock()
    app.state.services.runtime_settings = MagicMock()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/anthropic/v1/messages",
            json={
                "model": "claude-bogus-9000",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"Authorization": "Bearer test"},
        )

    assert resp.status_code == 404
    assert len(log_calls) == 1
    assert log_calls[0]["error_code"] == "model_not_found"
    assert log_calls[0]["model_id"] == "claude-bogus-9000"
    assert log_calls[0]["user"]["user_id"] == "u1"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/servers/test_anthropic_messages_rejection.py -v
```

Expected: FAIL — helper not imported / not called.

- [ ] **Step 3: Wire the helper**

In `apps/backend/serving/servers/routers/anthropic_messages.py`:

a) Add the import alongside other `serving.*` imports near the top:

```python
from serving.observability.rejection_log import log_rejection
```

`asyncio` is already imported at the top; if not, add `import asyncio`.

b) Find the existing `try/except HTTPException` block in `anthropic_messages` (currently at lines 499-502):

```python
    try:
        canonical, _route, adapter = _resolve(model_id, router_exec, user_ctx)
    except HTTPException as exc:
        return _anthropic_error(exc.status_code, str(exc.detail))
```

Insert the rejection log inside the `except` branch, before returning:

```python
    try:
        canonical, _route, adapter = _resolve(model_id, router_exec, user_ctx)
    except HTTPException as exc:
        services = getattr(request.app.state, "services", None)
        log_store_ = getattr(services, "log_store", None) if services else None
        runtime_settings_ = getattr(services, "runtime_settings", None) if services else None
        asyncio.create_task(
            log_rejection(
                log_store=log_store_,
                runtime_settings=runtime_settings_,
                request=request,
                status_code=exc.status_code,
                error_code="model_not_found",
                reason=str(exc.detail),
                user={
                    "user_id": user_ctx.get("user_id"),
                    "role": user_ctx.get("role"),
                },
                model_id=model_id,
            )
        )
        return _anthropic_error(exc.status_code, str(exc.detail))
```

`request` is already a parameter of the route function (`anthropic_messages`).

- [ ] **Step 4: Run the test + the existing anthropic tests to verify no regression**

```bash
uv run pytest tests/servers/test_anthropic_messages_rejection.py tests/servers/test_anthropic_messages.py -v 2>&1 | tail -30
```

Expected: all pass. (If `tests/servers/test_anthropic_messages.py` does not exist, just run the new test.)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/servers/routers/anthropic_messages.py tests/servers/test_anthropic_messages_rejection.py
git commit -m "feat(anthropic): emit log_rejection on model-not-found 404"
```

---

## Task 6: Final sweep — full tests, ruff, pydocstyle

**Files:** none (verification only)

- [ ] **Step 1: Run all tests touched by the change + adjacent suites**

```bash
cd /home/juncheng/hybridInference-worktrees/log-rejected-requests
uv run pytest tests/observability tests/servers/test_admin_settings.py tests/servers/test_enforce_user_concurrency.py tests/servers/test_concurrency_endpoint.py tests/servers/test_user_concurrency_limiter.py tests/servers/test_concurrency_runtime_resize.py tests/servers/test_auth_routes.py tests/servers/test_auth_rejection_log.py tests/servers/test_anthropic_messages_rejection.py -q 2>&1 | tail -10
```

Expected: all pass (plus the usual environmental skips for tests requiring Postgres).

- [ ] **Step 2: Run ruff format + check**

```bash
uv run ruff format apps/backend tests
uv run ruff check apps/backend tests
```

Expected: format reports `0 files reformatted` (or just the touched files) and check reports `All checks passed!`.

- [ ] **Step 3: Run pydocstyle on the new module**

```bash
uv run pydocstyle apps/backend/serving/observability/rejection_log.py
```

Expected: no errors. (CI runs pydocstyle on `apps/backend`; we already learned this in PR #423.)

- [ ] **Step 4: Commit any formatting changes**

```bash
git add -A
git diff --cached --quiet || git commit -m "style: ruff format"
```

---

## Self-Review Checklist (writer)

- [x] **Spec coverage:** every spec section maps to a task —
      Goals (toggle, default off, never raise) → Tasks 1+2;
      Helper module → Task 2;
      Concurrency wiring → Task 3;
      Quota + auth wiring → Task 4;
      Anthropic 404 wiring → Task 5;
      Filtering / metadata.rejection → covered by helper payload (asserted in Task 2 tests);
      Final lint sweep → Task 6.
- [x] **No placeholders:** every step contains either runnable code or a runnable command. The optional email-not-verified 403 is explicitly called out as out of scope, not a TBD.
- [x] **Type consistency:** `log_rejection` signature matches across Tasks 2, 3, 4, 5; `INFERENCE_PATH_PREFIXES` import / constant naming matches; `error_code` literal values are spelled identically in tests and prod code.
- [x] **Pre-existing test layout:** Task 4 introduces a new test file rather than modifying `test_auth_routes.py`; this avoids fixture clashes with the existing tests in that file.
- [x] **No DB schema migration**: `model_id=""` and `provider=""` honor the existing NOT NULL constraints; no `ALTER TABLE` required.
- [x] **Fail-open behavior**: helper short-circuits cleanly on `log_store=None`, `runtime_settings=None`, non-inference path, toggle off, and on internal exceptions. All five paths tested.
