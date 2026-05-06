---
status: draft
date: 2026-05-03
author: jason (via Claude)
related-audit: 2026-05-02 repo audit (Tier 2, item 9)
---

# Admin router split — design

## Problem

`serving/servers/routers/admin.py` is **2,585 LOC**, 32 routes, 71 functions in one file.

Concrete cost:
- Every admin PR (broadcasts, audit log, analytics, performance, exports, signup-domains, …) edits the same file → frequent merge conflicts on in-flight branches.
- `git blame` is unhelpful at this scale.
- Hot-reload + LSP performance degrade.
- New contributors take longer to find the route they're touching.
- Single function `admin_get_analytics` is 221 lines; `admin_get_performance_metrics` is 211. Reading either requires holding the whole file in mental context.

Identified in the 2026-05-02 repo audit as Tier 2 item 9 ("admin router split"). Tier 1 security/reliability/CI batches landed in #313, #317, #319 (and #318 in flight). This spec covers the first of three architectural follow-ups (D = router split; E = frontend split; C = postgres retention, deferred).

## Goal

Split `admin.py` into a **package** (`admin/`) of domain-grouped sub-routers with **zero behavior change**.

Non-goals:
- No route renames.
- No URL changes.
- No new tests for behavior (existing tests must pass unchanged).
- No refactoring of route bodies (defer to follow-up PRs once boundaries are clear).
- No type-safety improvements (`Any` reduction, `# type: ignore` removal — separate concern).

## Constraints

- `from serving.servers.routers import admin` and `admin.router` must still resolve.
- OpenAPI spec output (paths, tags, operationIds) must be byte-identical to the pre-split state — frontend's generated client and any external consumers depend on it.
- Test suite (`test/servers/test_admin*.py`, ~6 files) passes unchanged.
- `app.py`'s `app.include_router(admin.router)` line is unchanged.
- Helper functions are module-private (underscore-prefixed) and have no external imports — verified via `grep -rn "from serving.servers.routers.admin import _"` returning empty before split.

## Architecture

### Package layout

```
serving/servers/routers/admin/
  __init__.py         # ~30 LOC — aggregates sub-routers, exports `router`
  _common.py          # ~200 LOC — shared helpers (no routes)
  stats.py            # ~70 LOC  — /admin/stats, /admin/routing
  api_keys.py         # ~360 LOC — 6 routes for /admin/api-keys[/{id}]
  users.py            # ~600 LOC — 8 user routes + /admin/audit-log
  metrics.py          # ~700 LOC — request, performance, ttft-scatter, recent-requests
  analytics.py        # ~250 LOC — /admin/analytics
  broadcast.py        # ~330 LOC — 6 broadcast routes
  export.py           # ~150 LOC — /admin/export/requests
  providers.py        # ~250 LOC — provider-quotas, provider-stats, provider-token-usage
  signup_domains.py   # ~150 LOC — signup-domain routes
```

### Sub-router pattern

Each domain file:

```python
# serving/servers/routers/admin/stats.py
from fastapi import APIRouter, Depends
from serving.servers.deps import verify_admin_access
from serving.servers.routers.admin import _common  # shared helpers

router = APIRouter(prefix="/admin", tags=["admin:stats"])


@router.get("/stats")
async def get_stats(...):
    ...
```

Aggregator:

```python
# serving/servers/routers/admin/__init__.py
from fastapi import APIRouter
from . import (
    stats, api_keys, users, metrics, analytics,
    broadcast, export, providers, signup_domains,
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
```

### Helper-module shape

**Placement rule:** a helper goes in `_common.py` only if it is used by **two or more** sub-routers. Single-domain helpers move into their own domain file (still underscore-prefixed, still module-private).

Initial allocation (revisit during implementation by grepping usage):

`_common.py` (multi-domain):
- `_to_json_safe(value)` — JSON sanitization (used by users, broadcast, audit-log)
- `_serialize_for_audit(data)` — audit-log payload prep (used by users, api-keys, broadcast, signup-domains)
- `_build_histogram(values, bins)` — distribution builder (metrics + analytics)
- `_round_or_none(value, digits)` (metrics + analytics + providers)
- `_truncate_hour(dt)`, `_require_aware_utc(dt, name)` — time helpers (metrics + analytics + providers)

Single-domain (move with their owner):
- `_distribution_from_row(...)` → `metrics.py`
- `_decode_throughput_tps(...)` → `metrics.py`
- `_render_or_422(req)` → `broadcast.py`
- `_normalize_signup_domain(raw)`, `_signup_domain_to_schema(row)` → `signup_domains.py`

Helpers stay private (underscore). No re-exports from `__init__.py`. Implementation phase verifies usage with `grep -rn "_helper_name" serving/` per helper before placing.

## Migration procedure

Each step is its own commit on `jason/claude/admin-router-split`.

1. **Transitional shim.** Create `admin/__init__.py` that does `from serving.servers.routers._admin_legacy import router` (where `_admin_legacy.py` is the renamed original `admin.py`). Verify CI green; no behavior change.
2. **Extract `_common.py`.** Move helper functions out. Update `_admin_legacy.py` to `from . import _common`. Verify CI green.
3. **Extract `stats.py`** (smallest domain). Move 2 routes; update `__init__.py` to `include_router(stats.router)` and remove from `_admin_legacy.py`. Verify CI green.
4. **Extract `api_keys.py`.** Same procedure.
5. **Extract `users.py`** (includes `/admin/audit-log` since it audits user changes).
6. **Extract `metrics.py`** (request-metrics, performance-metrics, ttft-scatter, recent-requests).
7. **Extract `analytics.py`** (single route, biggest function).
8. **Extract `broadcast.py`.**
9. **Extract `export.py`.**
10. **Extract `providers.py`** (provider-quotas, provider-stats, provider-token-usage).
11. **Extract `signup_domains.py`.**
12. **Delete `_admin_legacy.py`.** It is now empty (or only contains `router = APIRouter()` boilerplate — also removed). `__init__.py` is the canonical aggregator.

After each step: `make lint && uv run pytest test/servers/ -x` locally, then push to remote and verify CI before continuing.

## Test strategy

- **Existing tests must pass unchanged.** No edits to `test/servers/test_admin*.py`.
- **One new test** in `test/servers/test_admin_router_aggregation.py`:
  - asserts `from serving.servers.routers import admin` succeeds and `admin.router` is an `APIRouter`
  - counts registered routes — must equal 32 (snapshot constant)
  - asserts no path collision (each `(method, path)` tuple unique)
  - asserts every route has tags starting with `admin:` or equal to `admin` (catches accidental tag drift)
- **OpenAPI snapshot:** add a small one-time check at end of split — diff `app.openapi()` paths between pre-split (saved as `openapi_pre_split.json`) and post-split. Must match. Discard the snapshot file in the final cleanup commit.

## Coexistence with in-flight branches

`git worktree list` shows ~6 active branches that touch `admin.py`:
- `jason/claude/audit-log-readability`
- `jason/claude/admin-cached-tokens`
- `jason/claude/merge-performance-tabs` (already merged via #310)
- `jason/claude/move-perf-metrics-to-analytics`
- `jason/claude/perf-metrics-tab`
- `jason/claude/admin-resume-hard-delete-user`

Risk: split lands → all rebases hit huge conflicts.

Mitigation:
- Single PR titled `refactor(admin): split admin.py into domain-grouped sub-routers` so the move is one revertible commit.
- Use `git diff -M99` (high rename detection) so reviewers can verify line-by-line equivalence.
- Coordinate timing: post a comment on each in-flight admin-touching PR ahead of merge giving 24h notice. Authors can either rebase post-merge OR merge their work first.
- Suggested order: merge in-flight admin.py-touching PRs first, then split.

## Rollout

- **Single PR** (despite the 11 internal commits) on `jason/claude/admin-router-split` against `dev`.
- **No feature flag** — pure refactor, no behavior change.
- **Revert plan:** `git revert -m 1 <merge-sha>` reinstates the pre-split state in one commit.

## Out of scope (follow-up work)

These are intentionally NOT part of this PR:
- Refactoring large route bodies (`admin_get_analytics` 221 LOC, `admin_get_performance_metrics` 211 LOC) — separate PR per domain after split lands.
- Reducing 463 `Any` annotations in serving/routing (audit Tier 1 item 9).
- Reducing `# type: ignore` count (audit Tier 1 item 10).
- Postgres retention / archival (Batch C — deferred).
- Admin frontend split (Batch E — separate spec).
