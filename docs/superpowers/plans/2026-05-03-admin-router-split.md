# Admin Router Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `serving/servers/routers/admin.py` (2918 LOC, 32 routes, 47 functions) into a domain-grouped sub-router package with zero behavior change.

**Architecture:** Convert single file into `serving/servers/routers/admin/` package. `__init__.py` aggregates 9 sub-routers (`stats`, `api_keys`, `users`, `metrics`, `analytics`, `broadcast`, `export`, `providers`, `signup_domains`) plus a private `_common.py` of shared helpers. Existing imports (`from serving.servers.routers import admin`) and route paths stay byte-identical.

**Tech Stack:** Python 3.12, FastAPI APIRouter, asyncpg/psycopg, pytest, ruff, pydocstyle.

**Worktree:** `/home/juncheng/hybridInference/.worktrees/admin-router-split` on branch `jason/claude/admin-router-split` (tracks origin/dev).

**Spec:** `docs/superpowers/specs/2026-05-03-admin-router-split-design.md`

---

## Pre-flight (Task 0)

### Task 0: Pre-flight verification + snapshots

**Files:**
- Create: `/tmp/admin-split-pre/openapi.json` (snapshot, never committed)
- Create: `/tmp/admin-split-pre/routes.txt` (snapshot, never committed)

- [ ] **Step 1: Verify worktree state is clean**

```bash
cd /home/juncheng/hybridInference/.worktrees/admin-router-split
git status --short
```

Expected: empty output (clean working tree).

- [ ] **Step 2: Verify branch matches origin/dev**

```bash
git fetch origin dev
git log --oneline origin/dev..HEAD
git log --oneline HEAD..origin/dev
```

Expected: only the spec commit (`bb8eef3`) ahead. Zero behind.

- [ ] **Step 3: Snapshot route count + OpenAPI paths**

```bash
mkdir -p /tmp/admin-split-pre
uv run python -c "
from serving.servers.app import create_app
import json
app = create_app()
spec = app.openapi()
admin_paths = sorted(p for p in spec['paths'] if p.startswith('/admin'))
print(f'admin route count: {len(admin_paths)}')
with open('/tmp/admin-split-pre/openapi.json', 'w') as f:
    json.dump({p: spec['paths'][p] for p in admin_paths}, f, indent=2, sort_keys=True)
with open('/tmp/admin-split-pre/routes.txt', 'w') as f:
    for p in admin_paths:
        for method in sorted(spec['paths'][p]):
            f.write(f'{method.upper()} {p}\n')
print('snapshots written to /tmp/admin-split-pre/')
"
```

Expected output: `admin route count: 32` (or whatever matches actual count — record this number; later tasks check parity).

- [ ] **Step 4: Identify external imports of underscore helpers**

```bash
grep -rn "from serving.servers.routers.admin import _" \
    --include="*.py" /home/juncheng/hybridInference/.worktrees/admin-router-split/
```

Expected: at least one hit at `test/servers/test_admin.py:271` (`_decode_throughput_tps`). Note all hits — these tests must be updated when the helper's owning sub-file is extracted.

- [ ] **Step 5: Run baseline tests**

```bash
make lint
uv run pytest test/servers/ test/unit/ -x -q 2>&1 | tail -20
```

Expected: lint clean, all tests pass (modulo `dbtest`-marked skips on local-without-PG).

- [ ] **Step 6: Commit pre-flight summary as a tracking comment**

(Nothing to commit yet — pre-flight is read-only. Move to Task 1.)

---

## Phase 1 — Transitional shim

### Task 1: Rename admin.py to _admin_legacy.py + add package __init__.py

**Files:**
- Rename: `serving/servers/routers/admin.py` → `serving/servers/routers/admin/_admin_legacy.py`
- Create: `serving/servers/routers/admin/__init__.py`

- [ ] **Step 1: Move admin.py into a new package directory**

```bash
cd /home/juncheng/hybridInference/.worktrees/admin-router-split
mkdir -p serving/servers/routers/admin
git mv serving/servers/routers/admin.py serving/servers/routers/admin/_admin_legacy.py
```

- [ ] **Step 2: Create __init__.py that re-exports the legacy router**

```python
# serving/servers/routers/admin/__init__.py
"""Admin router package.

This file aggregates per-domain sub-routers. During the in-progress
split (see docs/superpowers/specs/2026-05-03-admin-router-split-design.md)
the legacy single-file router is exposed here so callers continue to
work via `from serving.servers.routers import admin`.
"""

from serving.servers.routers.admin._admin_legacy import router

__all__ = ["router"]
```

- [ ] **Step 3: Verify imports still work**

```bash
uv run python -c "
from serving.servers.routers import admin
assert hasattr(admin, 'router'), 'admin.router missing'
from fastapi import APIRouter
assert isinstance(admin.router, APIRouter), 'admin.router not an APIRouter'
print('OK')
"
```

Expected: `OK`.

- [ ] **Step 4: Re-snapshot routes + diff vs pre-flight**

```bash
uv run python -c "
from serving.servers.app import create_app
import json
app = create_app()
spec = app.openapi()
admin_paths = sorted(p for p in spec['paths'] if p.startswith('/admin'))
with open('/tmp/admin-split-pre/openapi-after.json', 'w') as f:
    json.dump({p: spec['paths'][p] for p in admin_paths}, f, indent=2, sort_keys=True)
"
diff /tmp/admin-split-pre/openapi.json /tmp/admin-split-pre/openapi-after.json
```

Expected: zero diff.

- [ ] **Step 5: Run lint + admin tests**

```bash
make lint
uv run pytest test/servers/test_admin.py test/servers/test_admin_routing_and_errors.py -x -q 2>&1 | tail -10
```

Expected: lint clean, all admin tests pass.

- [ ] **Step 6: Commit**

```bash
git add serving/servers/routers/admin/
git commit -m "refactor(admin): convert admin.py to package with transitional shim

Step 1 of the admin router split. No behavior change — admin.py is
renamed to admin/_admin_legacy.py and re-exported from admin/__init__.py
so existing callers (\`from serving.servers.routers import admin\`)
continue to work. Subsequent commits extract one domain at a time
into sibling sub-files.

Spec: docs/superpowers/specs/2026-05-03-admin-router-split-design.md"
```

---

## Phase 2 — Extract shared helpers

### Task 2: Extract `_common.py` (multi-domain helpers only)

**Files:**
- Create: `serving/servers/routers/admin/_common.py`
- Modify: `serving/servers/routers/admin/_admin_legacy.py` — remove helpers, import from `_common`

- [ ] **Step 1: Identify which helpers are used by 2+ domains**

```bash
cd /home/juncheng/hybridInference/.worktrees/admin-router-split
for h in _to_json_safe _serialize_for_audit _build_histogram _round_or_none _truncate_hour _require_aware_utc _distribution_from_row _decode_throughput_tps _render_or_422 _normalize_signup_domain _signup_domain_to_schema; do
    count=$(grep -c "\\b${h}\\b" serving/servers/routers/admin/_admin_legacy.py)
    echo "$h: $count usages"
done
```

Expected: rough usage counts. Per spec, multi-domain helpers go to `_common.py`; single-domain helpers stay in `_admin_legacy.py` (and move with their domain in subsequent tasks).

Initial allocation (per spec):
- `_common.py`: `_to_json_safe`, `_serialize_for_audit`, `_build_histogram`, `_round_or_none`, `_truncate_hour`, `_require_aware_utc`
- Stay in `_admin_legacy.py` for now: `_distribution_from_row` (→ metrics.py), `_decode_throughput_tps` (→ metrics.py), `_render_or_422` (→ broadcast.py), `_normalize_signup_domain` + `_signup_domain_to_schema` (→ signup_domains.py)

- [ ] **Step 2: Create `_common.py` with the multi-domain helpers**

Read each helper from `_admin_legacy.py` (line numbers from pre-flight snapshot — `_to_json_safe` at line 106, `_serialize_for_audit` at line 119, `_build_histogram` at line 1236, `_round_or_none` at line 1274, `_truncate_hour` at line 2500, `_require_aware_utc` at line 2505).

Copy them verbatim into `_common.py`. Add a module docstring:

```python
# serving/servers/routers/admin/_common.py
"""Shared helpers for admin sub-routers.

A helper belongs here only if it is used by two or more sub-routers.
Single-domain helpers live in their owner file. Adding a helper here
that has only one caller is a smell — move it.
"""

from __future__ import annotations

# Copy required imports from _admin_legacy.py top:
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status

# (Add other imports as needed for the moved functions; trim what isn't used.)


def _to_json_safe(value: Any) -> Any:
    # body copied from _admin_legacy.py
    ...


def _serialize_for_audit(data: dict[str, Any]) -> dict[str, Any]:
    ...


def _build_histogram(values: list[float], bins: int) -> dict[str, Any]:
    ...


def _round_or_none(value: Any, digits: int = 2) -> float | None:
    ...


def _truncate_hour(dt: datetime) -> datetime:
    ...


def _require_aware_utc(dt: datetime, name: str) -> datetime:
    ...
```

- [ ] **Step 3: Replace helper definitions in `_admin_legacy.py` with imports**

In `_admin_legacy.py`, delete the original definitions of the 6 helpers above. Replace with a single import block near the top:

```python
from serving.servers.routers.admin._common import (
    _build_histogram,
    _require_aware_utc,
    _round_or_none,
    _serialize_for_audit,
    _to_json_safe,
    _truncate_hour,
)
```

- [ ] **Step 4: Run linter to catch unused imports / undefined names**

```bash
uv run ruff check serving/servers/routers/admin/
```

Expected: clean. If `ruff` flags imports newly-unused in `_admin_legacy.py` (e.g. modules only used by the moved helpers), remove them too.

- [ ] **Step 5: Verify imports + route equivalence**

```bash
uv run python -c "
from serving.servers.app import create_app
import json
app = create_app()
spec = app.openapi()
admin_paths = sorted(p for p in spec['paths'] if p.startswith('/admin'))
with open('/tmp/admin-split-pre/openapi-after.json', 'w') as f:
    json.dump({p: spec['paths'][p] for p in admin_paths}, f, indent=2, sort_keys=True)
"
diff /tmp/admin-split-pre/openapi.json /tmp/admin-split-pre/openapi-after.json
```

Expected: zero diff.

- [ ] **Step 6: Run tests**

```bash
uv run pytest test/servers/ test/unit/ -x -q 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add serving/servers/routers/admin/
git commit -m "refactor(admin): extract multi-domain helpers to _common.py

Move 6 helpers used by 2+ sub-routers into admin/_common.py.
Single-domain helpers (e.g. _decode_throughput_tps, _render_or_422,
_normalize_signup_domain) stay in _admin_legacy.py until their
owning domain is extracted in subsequent commits.

No behavior change."
```

---

## Phase 3 — Extract domain sub-routers (one per task)

Each domain follows the same template. Repeating the steps for each so the engineer never reads them out of order.

### Task 3: Extract `stats.py` (smallest domain — 2 routes)

**Files:**
- Create: `serving/servers/routers/admin/stats.py`
- Modify: `serving/servers/routers/admin/__init__.py` — add `include_router(stats.router)`
- Modify: `serving/servers/routers/admin/_admin_legacy.py` — remove the 2 routes

**Routes to move:** `/admin/stats` (line 124), `/admin/routing` (line 148)

- [ ] **Step 1: Create `stats.py` with the 2 routes**

```python
# serving/servers/routers/admin/stats.py
"""Admin stats + routing-info endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

# Copy ALL imports the moved routes need from _admin_legacy.py top:
# - AppServices, verify_admin_access from serving.servers.deps
# - Any response-model classes from serving.schemas (or wherever)
# - Any other helpers used inside the route bodies
# Use `grep -nE "AppServices|verify_admin_access|<other>"` against
# _admin_legacy.py to enumerate.

router = APIRouter(prefix="/admin", tags=["admin:stats"])


@router.get("/stats")
async def get_stats(
    # full signature copied from _admin_legacy.py:124-147
):
    """..."""
    # body verbatim
    ...


@router.get("/routing")
async def admin_get_routing(
    # full signature copied from _admin_legacy.py:148-177
):
    """..."""
    # body verbatim
    ...
```

When copying, ensure:
- `Depends(verify_admin_access)` is preserved on every route (PR #313 added it; don't drop).
- Path strings are exact: `/admin/stats` and `/admin/routing` (the `/admin` prefix is on the APIRouter, so the decorator argument is `/stats` and `/routing`).
- Wait — re-read carefully. With `prefix="/admin"`, the decorator path is the suffix. So `@router.get("/stats")` registers `/admin/stats`. Confirm by snapshotting routes after this task.

- [ ] **Step 2: Update `__init__.py` to include the new sub-router**

```python
# serving/servers/routers/admin/__init__.py
"""Admin router package.

Aggregates per-domain sub-routers. The legacy single-file router is
still mounted for routes that haven't been extracted yet.
"""

from fastapi import APIRouter

from serving.servers.routers.admin import stats
from serving.servers.routers.admin._admin_legacy import router as _legacy_router

router = APIRouter()
router.include_router(stats.router)
router.include_router(_legacy_router)

__all__ = ["router"]
```

- [ ] **Step 3: Remove the 2 moved routes from `_admin_legacy.py`**

Delete the function definitions and decorators for `get_stats` and `admin_get_routing`. Also remove now-unused imports (let `ruff` flag them).

- [ ] **Step 4: Run linter**

```bash
uv run ruff check serving/servers/routers/admin/
```

Expected: clean.

- [ ] **Step 5: Verify route count + OpenAPI parity**

```bash
uv run python -c "
from serving.servers.app import create_app
import json
app = create_app()
spec = app.openapi()
admin_paths = sorted(p for p in spec['paths'] if p.startswith('/admin'))
with open('/tmp/admin-split-pre/openapi-after.json', 'w') as f:
    json.dump({p: spec['paths'][p] for p in admin_paths}, f, indent=2, sort_keys=True)
"
diff /tmp/admin-split-pre/openapi.json /tmp/admin-split-pre/openapi-after.json
```

Expected: **zero diff**. If non-zero, the move dropped a route or changed a path/tag/operation_id — fix before continuing.

- [ ] **Step 6: Run tests**

```bash
uv run pytest test/servers/test_admin.py test/servers/test_admin_routing_and_errors.py -x -q 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add serving/servers/routers/admin/
git commit -m "refactor(admin): extract stats + routing endpoints to stats.py

Move /admin/stats and /admin/routing into admin/stats.py. Aggregate
into __init__.py via include_router. Legacy file shrinks; behavior,
paths, tags unchanged."
```

---

### Task 4: Extract `api_keys.py` (6 routes)

**Files:**
- Create: `serving/servers/routers/admin/api_keys.py`
- Modify: `serving/servers/routers/admin/__init__.py` — add `include_router(api_keys.router)`
- Modify: `serving/servers/routers/admin/_admin_legacy.py` — remove the 6 routes

**Routes to move:**
- `POST /admin/api-keys` (178)
- `GET /admin/api-keys` (247)
- `GET /admin/api-keys/{user_id}` (305)
- `PATCH /admin/api-keys/{user_id}` (375)
- `DELETE /admin/api-keys/{user_id}` (417)
- `POST /admin/api-keys/{user_id}/regenerate` (458)

- [ ] **Step 1: Create `api_keys.py`**

Same template as `stats.py`:
- `router = APIRouter(prefix="/admin", tags=["admin:api-keys"])`
- Copy the 6 route definitions verbatim, replacing `@router.get("/admin/api-keys")` with `@router.get("/api-keys")` (prefix handled by APIRouter).
- Copy all imports the routes need (response-model classes, `verify_admin_access`, etc.).

- [ ] **Step 2: Wire into `__init__.py`**

```python
from serving.servers.routers.admin import api_keys, stats
# ...
router.include_router(api_keys.router)
```

- [ ] **Step 3: Delete the 6 moved routes from `_admin_legacy.py`**

- [ ] **Step 4: Lint + parity check**

```bash
uv run ruff check serving/servers/routers/admin/
uv run python -c "
from serving.servers.app import create_app; import json
app = create_app(); spec = app.openapi()
admin_paths = sorted(p for p in spec['paths'] if p.startswith('/admin'))
with open('/tmp/admin-split-pre/openapi-after.json', 'w') as f:
    json.dump({p: spec['paths'][p] for p in admin_paths}, f, indent=2, sort_keys=True)
"
diff /tmp/admin-split-pre/openapi.json /tmp/admin-split-pre/openapi-after.json
```

Expected: zero diff.

- [ ] **Step 5: Run tests**

```bash
uv run pytest test/servers/test_admin_api.py test/servers/test_admin.py -x -q 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add serving/servers/routers/admin/
git commit -m "refactor(admin): extract api-keys endpoints to api_keys.py

Move 6 routes for /admin/api-keys[/{user_id}[/regenerate]] into
admin/api_keys.py. No behavior change."
```

---

### Task 5: Extract `users.py` (8 routes + audit-log)

**Files:**
- Create: `serving/servers/routers/admin/users.py`
- Modify: `serving/servers/routers/admin/__init__.py`
- Modify: `serving/servers/routers/admin/_admin_legacy.py`

**Routes to move (9 total):**
- `GET /admin/users` (511)
- `POST /admin/users/{user_id}/approve` (581)
- `POST /admin/users/{user_id}/reject` (634)
- `GET /admin/users/{user_id}/detail` (686)
- `PATCH /admin/users/{user_id}` (749)
- `GET /admin/audit-log` (846) — included here per spec (audits user changes)
- `POST /admin/users/{user_id}/delete` (903)
- `POST /admin/users/{user_id}/resume` (958)
- `POST /admin/users/{user_id}/hard-delete` (1008)

- [ ] **Step 1: Create `users.py`**

`router = APIRouter(prefix="/admin", tags=["admin:users"])`. Copy 9 route definitions, adjust decorator paths (drop `/admin` prefix). Copy required imports.

- [ ] **Step 2: Wire into `__init__.py`**

```python
from serving.servers.routers.admin import api_keys, stats, users
# ...
router.include_router(users.router)
```

- [ ] **Step 3: Delete the 9 moved routes from `_admin_legacy.py`**

- [ ] **Step 4: Lint + parity check**

(Same commands as Task 4 Step 4.) Expected: zero diff.

- [ ] **Step 5: Run tests**

```bash
uv run pytest test/servers/test_admin_users.py test/servers/test_admin_auth.py test/servers/test_admin.py -x -q 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add serving/servers/routers/admin/
git commit -m "refactor(admin): extract users + audit-log endpoints to users.py

Move 9 routes (8 user routes + /admin/audit-log) into admin/users.py.
Audit-log lives here because it audits user actions. No behavior change."
```

---

### Task 6: Extract `metrics.py` (4 routes + 2 single-domain helpers, fix 1 test import)

**Files:**
- Create: `serving/servers/routers/admin/metrics.py`
- Modify: `serving/servers/routers/admin/__init__.py`
- Modify: `serving/servers/routers/admin/_admin_legacy.py`
- Modify: `test/servers/test_admin.py` — update one helper import

**Routes to move:**
- `GET /admin/request-metrics` (1097)
- `GET /admin/performance-metrics` (1339)
- `GET /admin/ttft-scatter` (1590)
- `GET /admin/recent-requests` (1656)

**Helpers to move (single-domain, owner = metrics):**
- `_distribution_from_row` (1281)
- `_decode_throughput_tps` (1555)

- [ ] **Step 1: Create `metrics.py`**

```python
# serving/servers/routers/admin/metrics.py
"""Admin per-request and per-route metrics endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from serving.servers.routers.admin._common import (
    _build_histogram,  # multi-domain — pulled from _common
    _round_or_none,
    _truncate_hour,
    _require_aware_utc,
)

# (Plus any imports the routes need.)

router = APIRouter(prefix="/admin", tags=["admin:metrics"])


# Move single-domain helpers — keep them module-private.
def _distribution_from_row(...):
    ...


def _decode_throughput_tps(...):
    ...


@router.get("/request-metrics", response_model=AdminRequestMetricsResponse)
async def admin_get_request_metrics(...):
    ...


@router.get("/performance-metrics", response_model=AdminPerformanceMetricsResponse)
async def admin_get_performance_metrics(...):
    ...


@router.get("/ttft-scatter", response_model=AdminTtftScatterResponse)
async def admin_get_ttft_scatter(...):
    ...


@router.get("/recent-requests", response_model=AdminRecentRequestsResponse)
async def admin_list_recent_requests(...):
    ...
```

- [ ] **Step 2: Wire into `__init__.py`**

```python
from serving.servers.routers.admin import api_keys, metrics, stats, users
# ...
router.include_router(metrics.router)
```

- [ ] **Step 3: Update test import for `_decode_throughput_tps`**

In `test/servers/test_admin.py:271`, change:

```python
        from serving.servers.routers.admin import _decode_throughput_tps
```

to:

```python
        from serving.servers.routers.admin.metrics import _decode_throughput_tps
```

- [ ] **Step 4: Delete the 4 routes + 2 helpers from `_admin_legacy.py`**

Also delete now-unused imports.

- [ ] **Step 5: Lint + parity check**

(Same as before.) Expected: zero diff.

- [ ] **Step 6: Run tests**

```bash
uv run pytest test/servers/test_admin.py test/servers/test_admin_routing_and_errors.py test/servers/test_admin_provider_stats.py -x -q 2>&1 | tail -10
```

Expected: all pass. The updated `_decode_throughput_tps` import should resolve from the new location.

- [ ] **Step 7: Commit**

```bash
git add serving/servers/routers/admin/ test/servers/test_admin.py
git commit -m "refactor(admin): extract metrics endpoints to metrics.py

Move 4 routes (request-metrics, performance-metrics, ttft-scatter,
recent-requests) and 2 single-domain helpers (_distribution_from_row,
_decode_throughput_tps) into admin/metrics.py.

test_admin.py:271 updated to import _decode_throughput_tps from the
new location. No behavior change."
```

---

### Task 7: Extract `analytics.py` (1 route, biggest function)

**Files:**
- Create: `serving/servers/routers/admin/analytics.py`
- Modify: `serving/servers/routers/admin/__init__.py`
- Modify: `serving/servers/routers/admin/_admin_legacy.py`

**Routes to move:** `GET /admin/analytics` (1788) — the 221-LOC `admin_get_analytics` function.

- [ ] **Step 1: Create `analytics.py`**

```python
# serving/servers/routers/admin/analytics.py
"""Admin analytics endpoint — aggregated request stats for the admin dashboard."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from serving.servers.routers.admin._common import (
    _build_histogram,
    _round_or_none,
    _truncate_hour,
    _require_aware_utc,
)

router = APIRouter(prefix="/admin", tags=["admin:analytics"])


@router.get("/analytics", response_model=AdminAnalyticsResponse)
async def admin_get_analytics(...):
    """..."""
    ...
```

- [ ] **Step 2: Wire into `__init__.py`**

```python
from serving.servers.routers.admin import analytics, api_keys, metrics, stats, users
# ...
router.include_router(analytics.router)
```

- [ ] **Step 3: Delete the route from `_admin_legacy.py`**

- [ ] **Step 4: Lint + parity check**

Expected: zero diff.

- [ ] **Step 5: Run tests**

```bash
uv run pytest test/servers/test_admin_analytics.py -x -q 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add serving/servers/routers/admin/
git commit -m "refactor(admin): extract analytics endpoint to analytics.py

Move /admin/analytics (the 221-LOC admin_get_analytics function) into
admin/analytics.py. No behavior change."
```

---

### Task 8: Extract `broadcast.py` (6 routes + 1 helper)

**Files:**
- Create: `serving/servers/routers/admin/broadcast.py`
- Modify: `serving/servers/routers/admin/__init__.py`
- Modify: `serving/servers/routers/admin/_admin_legacy.py`

**Routes to move:**
- `POST /admin/broadcast-email/preview` (2025)
- `POST /admin/broadcast-email/test` (2057)
- `POST /admin/broadcast-email` (2095)
- `GET /admin/broadcast-email` (2191)
- `GET /admin/broadcast-email/{broadcast_id}` (2236)
- `DELETE /admin/broadcast-email/{broadcast_id}` (2309)

**Helper to move (single-domain):**
- `_render_or_422` (2011)

- [ ] **Step 1: Create `broadcast.py`**

`router = APIRouter(prefix="/admin", tags=["admin:broadcast"])`. Copy 6 routes + `_render_or_422`.

- [ ] **Step 2: Wire into `__init__.py`**

```python
from serving.servers.routers.admin import (
    analytics,
    api_keys,
    broadcast,
    metrics,
    stats,
    users,
)
# ...
router.include_router(broadcast.router)
```

- [ ] **Step 3: Delete the 6 routes + helper from `_admin_legacy.py`**

- [ ] **Step 4: Lint + parity check**

Expected: zero diff.

- [ ] **Step 5: Run tests**

```bash
uv run pytest test/servers/test_broadcast_email_endpoints.py -x -q 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add serving/servers/routers/admin/
git commit -m "refactor(admin): extract broadcast-email endpoints to broadcast.py

Move 6 routes for /admin/broadcast-email and the single-domain
_render_or_422 helper into admin/broadcast.py. No behavior change."
```

---

### Task 9: Extract `export.py` (1 route — JSONL streaming)

**Files:**
- Create: `serving/servers/routers/admin/export.py`
- Modify: `serving/servers/routers/admin/__init__.py`
- Modify: `serving/servers/routers/admin/_admin_legacy.py`

**Routes to move:** `GET /admin/export/requests` (2339) — JSONL streaming export.

- [ ] **Step 1: Create `export.py`**

`router = APIRouter(prefix="/admin", tags=["admin:export"])`. Copy `admin_export_requests`. Take care to copy any `StreamingResponse` import.

- [ ] **Step 2: Wire into `__init__.py`**

```python
from serving.servers.routers.admin import (
    analytics,
    api_keys,
    broadcast,
    export,
    metrics,
    stats,
    users,
)
# ...
router.include_router(export.router)
```

- [ ] **Step 3: Delete the route from `_admin_legacy.py`**

- [ ] **Step 4: Lint + parity check**

Expected: zero diff.

- [ ] **Step 5: Run tests**

```bash
uv run pytest test/servers/ -k "export" -x -q 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add serving/servers/routers/admin/
git commit -m "refactor(admin): extract /admin/export/requests to export.py

JSONL streaming export endpoint moved into its own file. No behavior
change."
```

---

### Task 10: Extract `providers.py` (3 routes)

**Files:**
- Create: `serving/servers/routers/admin/providers.py`
- Modify: `serving/servers/routers/admin/__init__.py`
- Modify: `serving/servers/routers/admin/_admin_legacy.py`

**Routes to move:**
- `GET /admin/provider-quotas` (2484)
- `GET /admin/api/provider-stats` (2519)
- `GET /admin/api/provider-token-usage` (2636)

Note: two of these have an extra `/api` segment in their path. Preserve exactly. After this task, an `/admin/api/...` path remains served by the same aggregate router — no behavior change.

- [ ] **Step 1: Create `providers.py`**

```python
router = APIRouter(prefix="/admin", tags=["admin:providers"])

@router.get("/provider-quotas", response_model=AdminProviderQuotasResponse)
async def admin_provider_quotas(...): ...

@router.get("/api/provider-stats", response_model=ProviderStatsResponse)
async def admin_provider_stats(...): ...

@router.get("/api/provider-token-usage", response_model=ProviderTokenUsageResponse)
async def admin_provider_token_usage(...): ...
```

- [ ] **Step 2: Wire into `__init__.py`**

```python
from serving.servers.routers.admin import (
    analytics, api_keys, broadcast, export, metrics, providers, stats, users,
)
# ...
router.include_router(providers.router)
```

- [ ] **Step 3: Delete the 3 routes from `_admin_legacy.py`**

- [ ] **Step 4: Lint + parity check**

Expected: zero diff. Pay particular attention to the `/admin/api/provider-stats` path — confirm both `/admin/provider-quotas` AND `/admin/api/provider-stats` appear in the new openapi snapshot.

- [ ] **Step 5: Run tests**

```bash
uv run pytest test/servers/test_admin_provider_quotas.py test/servers/test_admin_provider_stats.py test/servers/test_admin_token_usage.py -x -q 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add serving/servers/routers/admin/
git commit -m "refactor(admin): extract provider endpoints to providers.py

Move 3 routes (/admin/provider-quotas, /admin/api/provider-stats,
/admin/api/provider-token-usage) into admin/providers.py. The mixed
/admin/... and /admin/api/... paths are preserved as-is. No behavior
change."
```

---

### Task 11: Extract `signup_domains.py` (3 routes + 2 helpers)

**Files:**
- Create: `serving/servers/routers/admin/signup_domains.py`
- Modify: `serving/servers/routers/admin/__init__.py`
- Modify: `serving/servers/routers/admin/_admin_legacy.py`

**Routes to move:**
- `GET /admin/signup-domains` (2781)
- `POST /admin/signup-domains` (2800) — note multi-line decorator
- `DELETE /admin/signup-domains/{domain}` (2869)

**Helpers to move (single-domain):**
- `_normalize_signup_domain` (2725)
- `_signup_domain_to_schema` (2770)

- [ ] **Step 1: Create `signup_domains.py`**

`router = APIRouter(prefix="/admin", tags=["admin:signup-domains"])`. Copy 3 routes + 2 helpers.

- [ ] **Step 2: Wire into `__init__.py`**

```python
from serving.servers.routers.admin import (
    analytics, api_keys, broadcast, export, metrics, providers,
    signup_domains, stats, users,
)
# ...
router.include_router(signup_domains.router)
```

- [ ] **Step 3: Delete the 3 routes + 2 helpers from `_admin_legacy.py`**

- [ ] **Step 4: Lint + parity check**

Expected: zero diff.

- [ ] **Step 5: Run tests**

```bash
uv run pytest test/integration/test_admin_signup_domains.py -x -q 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add serving/servers/routers/admin/
git commit -m "refactor(admin): extract signup-domains endpoints to signup_domains.py

Move 3 routes for /admin/signup-domains and 2 single-domain helpers
(_normalize_signup_domain, _signup_domain_to_schema) into
admin/signup_domains.py. No behavior change."
```

---

## Phase 4 — Cleanup + tests

### Task 12: Delete `_admin_legacy.py`

**Files:**
- Delete: `serving/servers/routers/admin/_admin_legacy.py`
- Modify: `serving/servers/routers/admin/__init__.py` — remove legacy include

- [ ] **Step 1: Verify `_admin_legacy.py` has no remaining routes**

```bash
cd /home/juncheng/hybridInference/.worktrees/admin-router-split
grep -c "^@router\." serving/servers/routers/admin/_admin_legacy.py
```

Expected: `0`.

- [ ] **Step 2: Verify `_admin_legacy.py` has no remaining helpers**

```bash
grep -nE "^def |^async def " serving/servers/routers/admin/_admin_legacy.py
```

Expected: empty (or only definitions that are intentionally kept, e.g. the `router = APIRouter(...)` declaration itself — that's not a function).

- [ ] **Step 3: Inspect remaining content**

```bash
wc -l serving/servers/routers/admin/_admin_legacy.py
cat serving/servers/routers/admin/_admin_legacy.py
```

Expected: file is mostly imports + `router = APIRouter(...)` declaration. If anything functional remains, abort and route it to the right sub-file.

- [ ] **Step 4: Delete the file**

```bash
git rm serving/servers/routers/admin/_admin_legacy.py
```

- [ ] **Step 5: Update `__init__.py` to drop the legacy include**

```python
# serving/servers/routers/admin/__init__.py
"""Admin router package — aggregates per-domain sub-routers."""

from fastapi import APIRouter

from serving.servers.routers.admin import (
    analytics,
    api_keys,
    broadcast,
    export,
    metrics,
    providers,
    signup_domains,
    stats,
    users,
)

router = APIRouter()
router.include_router(stats.router)
router.include_router(api_keys.router)
router.include_router(users.router)
router.include_router(metrics.router)
router.include_router(analytics.router)
router.include_router(broadcast.router)
router.include_router(export.router)
router.include_router(providers.router)
router.include_router(signup_domains.router)

__all__ = ["router"]
```

- [ ] **Step 6: Lint + parity check**

```bash
uv run ruff check serving/servers/routers/admin/
uv run python -c "
from serving.servers.app import create_app; import json
app = create_app(); spec = app.openapi()
admin_paths = sorted(p for p in spec['paths'] if p.startswith('/admin'))
with open('/tmp/admin-split-pre/openapi-after.json', 'w') as f:
    json.dump({p: spec['paths'][p] for p in admin_paths}, f, indent=2, sort_keys=True)
"
diff /tmp/admin-split-pre/openapi.json /tmp/admin-split-pre/openapi-after.json
```

Expected: zero diff.

- [ ] **Step 7: Run full test suite (servers + unit)**

```bash
uv run pytest test/servers/ test/unit/ -x -q 2>&1 | tail -15
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add serving/servers/routers/admin/
git commit -m "refactor(admin): drop _admin_legacy.py — split complete

The transitional shim file is empty after extracting all 32 routes
into 9 domain sub-routers. __init__.py now aggregates the sub-routers
directly. Per-file LOC: see commit log of this branch."
```

### Task 13: Add aggregation test

**Files:**
- Create: `test/servers/test_admin_router_aggregation.py`

- [ ] **Step 1: Write the test**

```python
# test/servers/test_admin_router_aggregation.py
"""Sanity test for the admin router package split.

Verifies that __init__.py correctly aggregates all sub-routers and
that no route was dropped or duplicated during the split.
"""

from __future__ import annotations

from fastapi import APIRouter

from serving.servers.routers import admin


def test_admin_module_exports_router() -> None:
    """`from serving.servers.routers import admin` exposes a single APIRouter."""
    assert hasattr(admin, "router"), "admin.router missing"
    assert isinstance(admin.router, APIRouter), "admin.router is not an APIRouter"


def test_admin_router_has_expected_route_count() -> None:
    """Snapshot route count to catch accidental loss/dup during future edits.

    Bump this number deliberately when adding/removing admin routes.
    """
    EXPECTED = 32  # set from pre-flight snapshot
    routes = [r for r in admin.router.routes if hasattr(r, "path") and r.path.startswith("/admin")]
    assert len(routes) == EXPECTED, (
        f"admin route count drifted: expected {EXPECTED}, got {len(routes)}"
    )


def test_admin_routes_have_no_path_collisions() -> None:
    """Each (method, path) tuple appears at most once."""
    seen: set[tuple[str, str]] = set()
    for r in admin.router.routes:
        if not hasattr(r, "path") or not hasattr(r, "methods"):
            continue
        for method in r.methods:
            key = (method, r.path)
            assert key not in seen, f"duplicate route: {method} {r.path}"
            seen.add(key)


def test_admin_routes_carry_admin_tag() -> None:
    """Every admin route has a tag starting with 'admin:' (or equal to 'admin')."""
    for r in admin.router.routes:
        if not hasattr(r, "path") or not r.path.startswith("/admin"):
            continue
        tags = getattr(r, "tags", []) or []
        assert tags, f"route {r.path} has no tags"
        assert all(t == "admin" or t.startswith("admin:") for t in tags), (
            f"route {r.path} has unexpected tags: {tags}"
        )
```

- [ ] **Step 2: Run the new test**

```bash
uv run pytest test/servers/test_admin_router_aggregation.py -x -v 2>&1 | tail -15
```

Expected: 4 tests pass. If `test_admin_router_has_expected_route_count` fails, update `EXPECTED` to the actual count from pre-flight snapshot.

- [ ] **Step 3: Lint**

```bash
uv run ruff check test/servers/test_admin_router_aggregation.py
uv run ruff format test/servers/test_admin_router_aggregation.py
uv run pydocstyle test/servers/test_admin_router_aggregation.py
```

Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add test/servers/test_admin_router_aggregation.py
git commit -m "test(admin): add aggregation sanity test for the router split

Snapshots route count, asserts no path collisions, asserts every admin
route carries an admin:* tag. Catches accidental route loss when the
package gains/loses sub-routers in the future."
```

### Task 14: Final OpenAPI parity verification + push + PR

**Files:** none modified (verification only)

- [ ] **Step 1: Final OpenAPI byte-equality check vs pre-flight**

```bash
uv run python -c "
from serving.servers.app import create_app; import json
app = create_app(); spec = app.openapi()
admin_paths = sorted(p for p in spec['paths'] if p.startswith('/admin'))
with open('/tmp/admin-split-pre/openapi-final.json', 'w') as f:
    json.dump({p: spec['paths'][p] for p in admin_paths}, f, indent=2, sort_keys=True)
"
diff /tmp/admin-split-pre/openapi.json /tmp/admin-split-pre/openapi-final.json
```

Expected: zero diff. If diff is non-empty, **stop** and reconcile before opening PR.

- [ ] **Step 2: Full test suite + lint**

```bash
make lint
uv run pytest test/servers/ test/unit/ -x -q 2>&1 | tail -20
```

Expected: lint clean, all tests pass.

- [ ] **Step 3: Verify the commit log reads clean**

```bash
git log --oneline origin/dev..HEAD
```

Expected: 13 commits — one spec, one shim, one helpers, 9 domain extracts, one cleanup, one aggregation test. (Plus this final task adds zero commits.)

- [ ] **Step 4: Push the branch**

```bash
git push -u origin jason/claude/admin-router-split
```

- [ ] **Step 5: Open the PR**

```bash
gh pr create --base dev --head jason/claude/admin-router-split \
    --title "refactor(admin): split admin.py into domain-grouped sub-routers" \
    --body "$(cat <<'EOF'
## Summary

Splits `serving/servers/routers/admin.py` (was 2918 LOC, 32 routes, 47 functions) into a `serving/servers/routers/admin/` package of 9 domain-grouped sub-routers. **Zero behavior change** — paths, tags, operationIds, and the `from serving.servers.routers import admin` import surface all preserved.

Spec: `docs/superpowers/specs/2026-05-03-admin-router-split-design.md`
Plan: `docs/superpowers/plans/2026-05-03-admin-router-split.md`

## Why

From the 2026-05-02 repo audit (Tier 2 item 9). admin.py is the single-file merge bottleneck for every admin PR. Splitting unblocks parallel work on broadcasts, audit-log, analytics, performance, exports, and signup-domains without merge conflicts.

## Per-domain LOC after split

| File | Routes | LOC (approx) |
|------|--------|--------------|
| `__init__.py` | (aggregator) | ~30 |
| `_common.py` | (helpers) | ~200 |
| `stats.py` | 2 | ~70 |
| `api_keys.py` | 6 | ~360 |
| `users.py` | 9 | ~600 |
| `metrics.py` | 4 | ~700 |
| `analytics.py` | 1 | ~250 |
| `broadcast.py` | 6 | ~330 |
| `export.py` | 1 | ~150 |
| `providers.py` | 3 | ~250 |
| `signup_domains.py` | 3 | ~150 |

## Test plan

- [x] OpenAPI byte-equality vs pre-split snapshot — verified zero diff after every commit
- [x] Existing `test/servers/test_admin*.py` pass unchanged (one helper-import update in `test_admin.py`)
- [x] New `test/servers/test_admin_router_aggregation.py` snapshots route count + asserts no collisions
- [x] `make lint` clean
- [ ] CI green
- [ ] Manual: hit `/admin/stats`, `/admin/users`, `/admin/analytics` on staging post-merge

## Coordination with in-flight branches

Several branches touch `admin.py`:
- `jason/claude/audit-log-readability`
- `jason/claude/admin-cached-tokens`
- `jason/claude/move-perf-metrics-to-analytics`
- `jason/claude/perf-metrics-tab`
- `jason/claude/admin-resume-hard-delete-user`

Suggest these merge first (or rebase post-merge with `git diff -M99` to verify their changes still apply at the new file boundaries).

## Revert plan

`git revert -m 1 <merge-sha>` reinstates the pre-split state in one commit.
EOF
)"
```

Expected: PR URL printed.

- [ ] **Step 6: Watch CI**

```bash
sleep 30 && gh pr checks <PR_NUMBER>
```

Address any CI failures iteratively (per CLAUDE.md every-2-min check).

---

## Notes for the engineer

- **Worktree paths matter.** Earlier worktrees in this repo ended up nested due to CWD drift in shell sessions. Always use absolute paths for `git -C` operations or `cd` explicitly into the worktree before running git commands.
- **Imports.** When copying routes into a new sub-file, the most common mistake is missing an import that lives at the top of `_admin_legacy.py`. Run `uv run ruff check` after each move; it flags both undefined names and unused imports cheaply.
- **The `/admin` prefix.** Each sub-router uses `APIRouter(prefix="/admin", ...)`. Decorator paths drop the `/admin` prefix. The OpenAPI parity check catches mistakes here.
- **Helpers in `_common.py` vs domain files.** Re-grep usage if in doubt: `grep -rn "_helper_name" serving/servers/routers/admin/`. A helper used by exactly one sub-file belongs in that sub-file. Move it back if it migrated to `_common.py` by mistake.
- **No backwards-compat shims at the end.** The transitional `_admin_legacy.py` exists only during the split. Task 12 deletes it. Don't keep it "just in case".
- **Per-task commit hygiene.** Each domain extract is one commit. Resist combining. The bisect+revert ergonomics are the whole point.
