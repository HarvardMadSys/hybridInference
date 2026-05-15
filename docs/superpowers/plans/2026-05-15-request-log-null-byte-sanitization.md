# Request Log Null-Byte Sanitization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent `api_logs` inserts from failing when request-log payloads contain embedded null bytes and keep request rows logging successfully.

**Architecture:** Add one shared recursive string sanitizer in `serving.storage.utils`, then apply it at the request-log serialization boundary in both PostgreSQL log implementations. Validate with a focused regression test that reproduces the production `\u0000 cannot be converted to text` condition using nested payload content.

**Tech Stack:** Python 3.12, pytest, asyncpg-style async pools, PostgreSQL request logging

---

## File Map

- `apps/backend/serving/storage/utils.py`
  Add the shared recursive null-byte sanitizer used by request-log serialization.
- `apps/backend/serving/storage/database.py`
  Sanitize request-log content before building SQL parameters for `api_logs`.
- `apps/backend/serving/storage/postgres_log.py`
  Mirror the same sanitization in the `LogStore` implementation used by the app.
- `tests/unit/storage/test_request_log_sanitization.py`
  Focused regression coverage for null-byte sanitization in request logging.
- `tests/unit/serving/test_completions_tracked_tasks.py`
  Existing request-log tracked-task regression target to re-run after the fix.

### Task 1: Reproduce The Failure In A Focused Unit Test

**Files:**
- Create: `tests/unit/storage/test_request_log_sanitization.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/storage/test_request_log_sanitization.py` with this test:

```python
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.postgres_log import PostgresLogStore


@pytest.mark.asyncio
async def test_postgres_log_request_strips_null_bytes_from_all_serialized_fields() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire_cm

    store = PostgresLogStore(pool, store_full_prompts=True)

    await store.log_request(
        request_id="req-1",
        model_id="glm-5.1",
        provider="zhipu",
        prompt=[{"role": "user", "content": "hel\x00lo"}],
        response={"message": {"content": "wor\x00ld"}},
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        latency_ms=12,
        status_code=200,
        error="bad\x00error",
        params={"tools": [{"name": "to\x00ol"}]},
        metadata={"user_id": "user\x00-1", "nested": {"note": "n\x00ote"}},
        request_payload={"messages": [{"content": "pa\x00yload"}]},
    )

    args = conn.execute.await_args.args
    prompt_str = args[17]
    response_str = args[18]
    request_payload_str = args[19]
    error_str = args[21]
    metadata_str = args[24]
    tools_str = args[25]

    assert "\x00" not in prompt_str
    assert "\x00" not in response_str
    assert "\x00" not in request_payload_str
    assert "\x00" not in error_str
    assert "\x00" not in metadata_str
    assert "\x00" not in tools_str

    assert json.loads(prompt_str)[0]["content"] == "hello"
    assert json.loads(response_str)["message"]["content"] == "world"
    assert json.loads(request_payload_str)["messages"][0]["content"] == "payload"
    assert error_str == "baderror"
    assert json.loads(metadata_str)["user_id"] == "user-1"
    assert json.loads(metadata_str)["nested"]["note"] == "note"
    assert json.loads(tools_str)[0]["name"] == "tool"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/storage/test_request_log_sanitization.py -v`

Expected: FAIL because the serialized SQL parameters still contain `\x00`.

### Task 2: Add Shared Sanitization And Apply It To Request Logging

**Files:**
- Modify: `apps/backend/serving/storage/utils.py`
- Modify: `apps/backend/serving/storage/database.py`
- Modify: `apps/backend/serving/storage/postgres_log.py`

- [ ] **Step 1: Add the shared sanitizer**

Update `apps/backend/serving/storage/utils.py` with this function:

```python
def strip_null_bytes(value: Any) -> Any:
    """Recursively remove PostgreSQL-incompatible null bytes from strings."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {k: strip_null_bytes(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_null_bytes(v) for v in value]
    if isinstance(value, tuple):
        return [strip_null_bytes(v) for v in value]
    return value
```

- [ ] **Step 2: Apply the sanitizer in `DatabaseLogger.log_request`**

Sanitize `prompt`, `response`, `request_payload`, `metadata`, `params.tools`, and
`error` before serialization and SQL binding. Keep the existing privacy-mode
branch intact.

- [ ] **Step 3: Apply the sanitizer in `PostgresLogStore.log_request`**

Mirror the same sanitization logic in `apps/backend/serving/storage/postgres_log.py`.

- [ ] **Step 4: Run the focused regression test to verify it passes**

Run: `uv run pytest tests/unit/storage/test_request_log_sanitization.py -v`

Expected: PASS.

### Task 3: Verify Existing Request-Log Behavior Still Holds

**Files:**
- Test: `tests/unit/serving/test_completions_tracked_tasks.py`

- [ ] **Step 1: Run the request-log tracked-task tests**

Run: `uv run pytest tests/unit/serving/test_completions_tracked_tasks.py -v`

Expected: PASS.

- [ ] **Step 2: Run both validation targets together**

Run: `uv run pytest tests/unit/storage/test_request_log_sanitization.py tests/unit/serving/test_completions_tracked_tasks.py -v`

Expected: PASS.
