# Admin Request Log Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a JSONL streaming export endpoint to the admin API and a corresponding Export panel in the admin dashboard's Recent Requests tab.

**Architecture:** A new `GET /admin/export/requests` FastAPI route uses `StreamingResponse` with an async generator that queries `api_logs` in batches of 500 rows and yields each row as a JSON line. The frontend adds an `exportRequests()` API function and an inline export panel to the existing Recent Requests filter bar.

**Tech Stack:** FastAPI, asyncpg, Python `json` stdlib (backend); Next.js 15, React 18, TypeScript, Tailwind CSS (frontend).

---

## File Map

| File | Change |
|---|---|
| `serving/servers/routers/admin.py` | Add `GET /admin/export/requests` route after line 1721 |
| `frontend/src/lib/api/admin.ts` | Add `exportRequests()` function after line 360 |
| `frontend/src/app/dashboard/admin/page.tsx` | Add export state vars + Export button + inline panel |
| `test/servers/test_admin_export.py` | New test file for export endpoint |

---

## Task 1: Backend — streaming export endpoint

**Files:**
- Modify: `serving/servers/routers/admin.py`
- Test: `test/servers/test_admin_export.py` (new)

### Step 1.1 — Write the failing test

Create `test/servers/test_admin_export.py`:

```python
"""Tests for GET /admin/export/requests streaming JSONL endpoint."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


def _make_mock_row(
    *,
    request_id: str = "req-abc",
    user_id: str = "user-1",
    user_name: str = "alice",
    user_email: str = "alice@example.com",
    model_id: str = "gpt-4o",
    provider: str = "openai",
    timestamp: datetime | None = None,
    status_code: int = 200,
    latency_ms: int = 1200,
    ttft_ms: int = 300,
    prompt_tokens: int = 50,
    completion_tokens: int = 100,
    total_tokens: int = 150,
    cost_usd: Decimal = Decimal("0.00120000"),
    error: str | None = None,
    prompt: str = "Hello",
    response: str = "World",
) -> dict:
    return {
        "request_id": request_id,
        "user_id": user_id,
        "user_name": user_name,
        "user_email": user_email,
        "model_id": model_id,
        "provider": provider,
        "timestamp": timestamp or datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        "status_code": status_code,
        "latency_ms": latency_ms,
        "ttft_ms": ttft_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "error": error,
        "prompt": prompt,
        "response": response,
    }


def _make_mock_db(rows_per_batch: list[list[dict]]):
    """Return a mock db_logger whose pool yields rows_per_batch in successive fetches."""
    mock_db = MagicMock()
    mock_pool = MagicMock()
    mock_db.pool = mock_pool

    fetch_results = iter(rows_per_batch)

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=lambda *a, **kw: next(fetch_results, []))

    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_pool.acquire = MagicMock(return_value=acquire_ctx)

    return mock_db


@pytest.fixture()
def mock_db():
    """Default empty mock db — override per test via _make_mock_db."""
    return _make_mock_db([[], []])


@pytest.fixture()
def client(mock_db):
    """TestClient with admin dependency overrides pre-applied.

    NOTE: Check the existing conftest.py in test/servers/ for the correct
    app import path — adjust `from serving.servers.app import app` if needed.
    """
    from serving.servers.deps import get_db_logger, verify_admin_access

    # Adjust this import to match your actual app factory location:
    from serving.servers.app import app

    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.dependency_overrides[get_db_logger] = lambda: mock_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_export_streams_jsonl():
    """Endpoint returns JSONL with one record per row, no prompt/response by default."""
    from serving.servers.deps import get_db_logger, verify_admin_access
    from serving.servers.app import app

    row = _make_mock_row()
    db = _make_mock_db([[row], []])
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.dependency_overrides[get_db_logger] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(
            "/admin/export/requests"
            "?start_time=2024-01-01T00:00:00Z&end_time=2024-12-31T23:59:59Z",
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert "application/x-ndjson" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]

    lines = [l for l in resp.text.strip().split("\n") if l]
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["request_id"] == "req-abc"
    assert record["model_id"] == "gpt-4o"
    assert record["provider"] == "openai"
    assert record["ttft_ms"] == 300
    assert record["latency_ms"] == 1200
    assert record["prompt_tokens"] == 50
    assert record["completion_tokens"] == 100
    assert record["total_tokens"] == 150
    assert record["status_code"] == 200
    assert record["error"] is None
    # prompt/response must NOT be present without include_content
    assert "prompt" not in record
    assert "response" not in record


def test_export_includes_content_when_requested():
    """With include_content=true, prompt and response appear in each record."""
    from serving.servers.deps import get_db_logger, verify_admin_access
    from serving.servers.app import app

    row = _make_mock_row(prompt="Say hi", response="Hi there")
    db = _make_mock_db([[row], []])
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.dependency_overrides[get_db_logger] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(
            "/admin/export/requests"
            "?start_time=2024-01-01T00:00:00Z&end_time=2024-12-31T23:59:59Z"
            "&include_content=true",
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    lines = [l for l in resp.text.strip().split("\n") if l]
    record = json.loads(lines[0])
    assert record["prompt"] == "Say hi"
    assert record["response"] == "Hi there"


def test_export_streams_multiple_batches():
    """Generator continues fetching until an empty batch is returned."""
    from serving.servers.deps import get_db_logger, verify_admin_access
    from serving.servers.app import app

    batch1 = [_make_mock_row(request_id=f"req-{i}") for i in range(3)]
    batch2 = [_make_mock_row(request_id=f"req-{i}") for i in range(3, 5)]
    db = _make_mock_db([batch1, batch2, []])
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.dependency_overrides[get_db_logger] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(
            "/admin/export/requests"
            "?start_time=2024-01-01T00:00:00Z&end_time=2024-12-31T23:59:59Z",
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    lines = [l for l in resp.text.strip().split("\n") if l]
    assert len(lines) == 5


def test_export_missing_start_time_returns_422():
    """start_time is required; missing it returns HTTP 422."""
    from serving.servers.deps import get_db_logger, verify_admin_access
    from serving.servers.app import app

    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.dependency_overrides[get_db_logger] = lambda: MagicMock()
    try:
        client = TestClient(app)
        resp = client.get("/admin/export/requests")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422


def test_export_no_db_returns_500():
    """Returns 500 when db_logger has no pool."""
    from serving.servers.deps import get_db_logger, verify_admin_access
    from serving.servers.app import app

    no_db = MagicMock()
    no_db.pool = None
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.dependency_overrides[get_db_logger] = lambda: no_db
    try:
        client = TestClient(app)
        resp = client.get(
            "/admin/export/requests"
            "?start_time=2024-01-01T00:00:00Z&end_time=2024-12-31T23:59:59Z",
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 500
```

### Step 1.2 — Run the test to confirm it fails

```bash
cd /srv/hybridInference
python -m pytest test/servers/test_admin_export.py -v 2>&1 | head -40
```

Expected: import errors or `404` on `/admin/export/requests` — confirming the endpoint doesn't exist yet.

### Step 1.3 — Add `StreamingResponse` import and `AsyncGenerator` to `admin.py`

In `serving/servers/routers/admin.py`, update the two existing import lines:

Old:
```python
from fastapi import APIRouter, Depends, HTTPException, Request
```
New:
```python
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
```

Old:
```python
from typing import Any, Literal
```
New:
```python
from typing import Any, AsyncGenerator, Literal
```

### Step 1.4 — Append the new route at the end of `admin.py` (after line 1721)

```python
@router.get("/admin/export/requests")
async def admin_export_requests(
    request: Request,
    start_time: datetime,
    end_time: datetime | None = None,
    user_id: str | None = None,
    model_id: str | None = None,
    errors_only: bool = False,
    include_content: bool = False,
    admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> StreamingResponse:
    """Stream all request logs matching the given filters as JSONL.

    Query Parameters:
    - start_time: ISO8601 datetime, inclusive lower bound (required)
    - end_time: ISO8601 datetime, inclusive upper bound (defaults to now)
    - user_id: Filter by user ID
    - model_id: Filter by model ID
    - errors_only: If true, only include requests with errors
    - include_content: If true, include prompt and response fields

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    if end_time is None:
        end_time = datetime.now(timezone.utc)

    where_clauses: list[str] = ["l.timestamp >= $1", "l.timestamp <= $2"]
    params: list[Any] = [start_time, end_time]

    if user_id:
        where_clauses.append(f"l.user_id = ${len(params) + 1}")
        params.append(user_id)

    if model_id:
        where_clauses.append(f"l.model_id = ${len(params) + 1}")
        params.append(model_id)

    if errors_only:
        where_clauses.append(
            "(l.error IS NOT NULL OR l.status_code IS NULL "
            "OR l.status_code < 200 OR l.status_code >= 400)"
        )

    where_sql = "WHERE " + " AND ".join(where_clauses)
    content_cols = ", l.prompt, l.response" if include_content else ""
    batch_size = 500

    async def generate() -> AsyncGenerator[str, None]:
        offset = 0
        while True:
            limit_idx = len(params) + 1
            offset_idx = len(params) + 2
            async with db_logger.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT
                        l.request_id, l.user_id, u.user_name, u.email AS user_email,
                        l.model_id, l.provider, l.timestamp,
                        l.status_code, l.latency_ms, l.ttft_ms,
                        l.prompt_tokens, l.completion_tokens, l.total_tokens,
                        l.cost_usd, l.error{content_cols}
                    FROM api_logs l
                    LEFT JOIN users u ON u.id = l.user_id
                    {where_sql}
                    ORDER BY l.timestamp DESC
                    LIMIT ${limit_idx} OFFSET ${offset_idx}
                    """,
                    *params,
                    batch_size,
                    offset,
                )
            if not rows:
                break
            for row in rows:
                record: dict[str, Any] = {
                    "request_id": row["request_id"],
                    "timestamp": row["timestamp"].isoformat(),
                    "user_id": row["user_id"],
                    "user_name": row["user_name"],
                    "user_email": row["user_email"],
                    "model_id": row["model_id"],
                    "provider": row["provider"],
                    "ttft_ms": row["ttft_ms"],
                    "latency_ms": row["latency_ms"],
                    "prompt_tokens": row["prompt_tokens"],
                    "completion_tokens": row["completion_tokens"],
                    "total_tokens": row["total_tokens"],
                    "cost_usd": (
                        float(row["cost_usd"]) if row["cost_usd"] is not None else None
                    ),
                    "status_code": row["status_code"],
                    "error": row["error"],
                }
                if include_content:
                    record["prompt"] = row["prompt"]
                    record["response"] = row["response"]
                yield json.dumps(record) + "\n"
            offset += batch_size

    start_str = start_time.strftime("%Y%m%d")
    end_str = end_time.strftime("%Y%m%d")
    filename = f"requests-{start_str}-{end_str}.jsonl"

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
```

### Step 1.5 — Run tests to verify they pass

```bash
cd /srv/hybridInference
python -m pytest test/servers/test_admin_export.py -v
```

Expected output:
```
test/servers/test_admin_export.py::test_export_streams_jsonl PASSED
test/servers/test_admin_export.py::test_export_includes_content_when_requested PASSED
test/servers/test_admin_export.py::test_export_streams_multiple_batches PASSED
test/servers/test_admin_export.py::test_export_missing_start_time_returns_422 PASSED
test/servers/test_admin_export.py::test_export_no_db_returns_500 PASSED
5 passed
```

### Step 1.6 — Commit

```bash
git add serving/servers/routers/admin.py test/servers/test_admin_export.py
git commit -m "feat: add GET /admin/export/requests streaming JSONL endpoint"
```

---

## Task 2: Frontend API function

**Files:**
- Modify: `frontend/src/lib/api/admin.ts` (append after line 360)

### Step 2.1 — Write the failing manual test spec (no automated test for Blob download)

There is no automated test for the Blob download flow (it requires a real browser). Instead, confirm by manual smoke test after Task 3 UI is in place. Continue to Step 2.2.

### Step 2.2 — Append `exportRequests` to `admin.ts`

Add after the last line (360) of `frontend/src/lib/api/admin.ts`:

```typescript
export interface ExportRequestsParams {
  startTime: string;
  endTime: string;
  userId?: string;
  modelId?: string;
  errorsOnly?: boolean;
  includeContent?: boolean;
}

export async function exportRequests(params: ExportRequestsParams): Promise<void> {
  const qs = new URLSearchParams({
    start_time: params.startTime,
    end_time: params.endTime,
  });
  if (params.userId) qs.set('user_id', params.userId);
  if (params.modelId) qs.set('model_id', params.modelId);
  if (params.errorsOnly) qs.set('errors_only', 'true');
  if (params.includeContent) qs.set('include_content', 'true');

  const resp = await fetchWithAuth(API_BASE, `/admin/export/requests?${qs.toString()}`);
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? 'Export failed');
  }

  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const startDate = params.startTime.slice(0, 10);
  const endDate = params.endTime.slice(0, 10);
  a.href = url;
  a.download = `requests-${startDate}-${endDate}.jsonl`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
```

### Step 2.3 — Type-check

```bash
cd /srv/hybridInference/frontend
npx tsc --noEmit 2>&1 | head -30
```

Expected: no errors related to `admin.ts`.

### Step 2.4 — Commit

```bash
git add frontend/src/lib/api/admin.ts
git commit -m "feat: add exportRequests API function to admin client"
```

---

## Task 3: Frontend UI — Export button and panel

**Files:**
- Modify: `frontend/src/app/dashboard/admin/page.tsx`

### Step 3.1 — Add `exportRequests` to the import in `admin/page.tsx`

Find the line near the top of [frontend/src/app/dashboard/admin/page.tsx](frontend/src/app/dashboard/admin/page.tsx) that imports from `@/lib/api/admin`. It looks like:

```typescript
import { listRecentRequests, ... } from '@/lib/api/admin';
```

Add `exportRequests` to that import list:

```typescript
import { exportRequests, listRecentRequests, ... } from '@/lib/api/admin';
```

### Step 3.2 — Add export state variables

After line 216 (after the existing `const REQ_PAGE_SIZE = 50;` line), add:

```typescript
  const [showExportPanel, setShowExportPanel] = useState(false);
  const [exportStartDate, setExportStartDate] = useState('');
  const [exportEndDate, setExportEndDate] = useState(
    () => new Date().toISOString().slice(0, 10),
  );
  const [exportIncludeContent, setExportIncludeContent] = useState(false);
  const [exportLoading, setExportLoading] = useState(false);
```

### Step 3.3 — Add Export button to the filter bar

In the filter bar section (around line 1138), the last element inside the `<div className="flex flex-wrap items-center gap-3">` is:

```tsx
              <span className="text-[12px] text-gray-400 tabular-nums">{reqTotal} entries</span>
```

Add the Export button immediately after that `<span>`:

```tsx
              <button
                type="button"
                onClick={() => setShowExportPanel((v) => !v)}
                className="ml-auto rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] text-gray-600 hover:bg-gray-50"
              >
                Export JSONL
              </button>
```

### Step 3.4 — Add the export panel after the filter bar

After the closing `</div>` of the filter bar (the one containing all the inputs), add:

```tsx
            {showExportPanel && (
              <div className="mt-3 rounded-lg border border-gray-200 bg-gray-50 p-4">
                <div className="flex flex-wrap items-end gap-3">
                  <div className="flex flex-col gap-1">
                    <label className="text-[12px] text-gray-500">Start date</label>
                    <input
                      type="date"
                      value={exportStartDate}
                      onChange={(e) => setExportStartDate(e.target.value)}
                      className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                    />
                  </div>
                  <div className="flex flex-col gap-1">
                    <label className="text-[12px] text-gray-500">End date</label>
                    <input
                      type="date"
                      value={exportEndDate}
                      onChange={(e) => setExportEndDate(e.target.value)}
                      className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                    />
                  </div>
                  <label className="flex items-center gap-1.5 pb-2 text-[13px] text-gray-600 cursor-pointer select-none">
                    <input
                      type="checkbox"
                      checked={exportIncludeContent}
                      onChange={(e) => setExportIncludeContent(e.target.checked)}
                      className="rounded border-gray-300"
                    />
                    Include prompt &amp; response
                  </label>
                  <div className="ml-auto flex items-center gap-2 pb-2">
                    <button
                      type="button"
                      onClick={() => {
                        setShowExportPanel(false);
                        setExportStartDate('');
                        setExportEndDate(new Date().toISOString().slice(0, 10));
                        setExportIncludeContent(false);
                      }}
                      className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] text-gray-600 hover:bg-gray-50"
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      disabled={!exportStartDate || exportLoading}
                      onClick={async () => {
                        if (!exportStartDate) return;
                        setExportLoading(true);
                        try {
                          await exportRequests({
                            startTime: new Date(exportStartDate + 'T00:00:00').toISOString(),
                            endTime: new Date(exportEndDate + 'T23:59:59').toISOString(),
                            userId: reqUserFilter || undefined,
                            modelId: reqModelFilter || undefined,
                            errorsOnly: reqErrorsOnly || undefined,
                            includeContent: exportIncludeContent || undefined,
                          });
                          setShowExportPanel(false);
                        } catch (err) {
                          toast.error(
                            'Export failed: ' +
                              (err instanceof Error ? err.message : 'Unknown error'),
                          );
                        } finally {
                          setExportLoading(false);
                        }
                      }}
                      className="rounded-lg bg-gray-900 px-3 py-2 text-[13px] text-white hover:bg-gray-700 disabled:opacity-50"
                    >
                      {exportLoading ? 'Exporting…' : 'Export'}
                    </button>
                  </div>
                </div>
              </div>
            )}
```

### Step 3.5 — Type-check

```bash
cd /srv/hybridInference/frontend
npx tsc --noEmit 2>&1 | head -30
```

Expected: no TypeScript errors.

### Step 3.6 — Commit

```bash
git add frontend/src/app/dashboard/admin/page.tsx
git commit -m "feat: add Export JSONL panel to admin Recent Requests tab"
```

---

## Task 4: Manual smoke test

### Step 4.1 — Start the dev server

```bash
cd /srv/hybridInference/frontend
npm run dev
```

Open the admin dashboard in the browser and navigate to the Recent Requests tab.

### Step 4.2 — Verify the Export button appears

Confirm the "Export JSONL" button is visible in the filter bar, to the right of the entries count.

### Step 4.3 — Verify the export panel opens and closes

- Click "Export JSONL" → panel appears with Start date, End date, Include prompt & response checkbox, Export and Cancel buttons.
- Click "Cancel" → panel closes, state resets.

### Step 4.4 — Verify the Export button is disabled without a start date

- Open the panel. The Export button should be disabled (opacity-50) until a start date is entered.

### Step 4.5 — Verify a working export

- Set a start date covering known data (e.g. one month ago).
- Leave "Include prompt & response" unchecked.
- Click Export.
- Confirm a `.jsonl` file downloads.
- Open the file and verify: one JSON object per line, fields match the schema (`request_id`, `timestamp`, `model_id`, etc.), no `prompt` or `response` keys.

### Step 4.6 — Verify content export

- Repeat with "Include prompt & response" checked.
- Confirm `prompt` and `response` keys appear in each line.

### Step 4.7 — Verify filters are passed through

- Set a user ID filter in the main filter bar, then open Export and export.
- Confirm exported rows all share the same `user_id`.

---

## Task 5: Create PR

### Step 5.1 — Push branch and open PR

```bash
git push -u origin HEAD
gh pr create \
  --base dev \
  --title "feat: admin JSONL export for request logs" \
  --body "$(cat <<'EOF'
## Summary
- Adds `GET /admin/export/requests` streaming endpoint that returns all matching `api_logs` rows as JSONL
- Supports filters: date range, user_id, model_id, errors_only, include_content (prompt+response toggle)
- Adds `exportRequests()` API function in the frontend admin client
- Adds Export JSONL button + inline panel to the Recent Requests tab in the admin dashboard

## Test plan
- [ ] `pytest test/servers/test_admin_export.py` passes (5 tests)
- [ ] `npx tsc --noEmit` passes with no errors
- [ ] Manual: Export button visible in Recent Requests filter bar
- [ ] Manual: Export panel opens/closes correctly
- [ ] Manual: File downloads as `.jsonl` with correct fields
- [ ] Manual: `include_content` toggle adds/removes prompt+response fields
- [ ] Manual: Active filters (user, model, errors-only) are applied to the export

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
