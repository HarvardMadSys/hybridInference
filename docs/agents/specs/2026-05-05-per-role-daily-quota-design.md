# Per-Role Daily Quota with Bulk Apply

**Date:** 2026-05-05
**Status:** Approved (awaiting user spec review)

## Problem

Today, the daily USD spend quota for any new user's API key is seeded from a single env var `SIGNUP_DEFAULT_DAILY_QUOTA_USD` (read in [`get_default_daily_quota()`](../../apps/backend/serving/servers/routers/user_routes.py#L179)). Every role (`free`, `pro`, `internal`, `admin`) gets the same starting quota. There is no admin UI to change it. To raise the quota for `internal` users, an operator must edit env vars and restart the gateway.

We want admins to set a different default daily quota per role and to bulk-apply that quota to existing users' active API keys.

## Goals

- Per-role daily quota stored as runtime settings (database-backed, hot-reloadable, exposed via existing admin Settings tab).
- New users' first API key is seeded with the role's quota at signup.
- Admin can click "Apply to existing users" per role to overwrite `quota_daily_cost_usd` on every active API key of users with that role.
- No request-time path change: quota enforcement keeps reading `api_keys.quota_daily_cost_usd` exactly as today.

## Non-Goals

- No audit log of bulk apply operations.
- No undo / snapshot.
- No partial apply (e.g. only NULL keys). Bulk apply always overwrites.
- No per-user preview list. Just a count.
- No removal of `signup_default_daily_quota_usd` env var (kept as fallback if a role's setting is unreadable).

## Architecture

```text
┌────────────────────────────┐
│ Admin Settings Tab (UI)    │
│  - 4 numeric inputs        │
│  - 4 "Apply to role" btns  │
└──────────┬─────────────────┘
           │
           │ GET  /admin/runtime-settings (existing)
           │ PUT  /admin/runtime-settings/{key} (existing)
           │ GET  /admin/quota/role-apply-preview?role=X (new)
           │ POST /admin/quota/role-apply (new)
           ▼
┌────────────────────────────┐         ┌──────────────────────────┐
│ Admin router (FastAPI)     │────────▶│ OperationalStore         │
│  - validate role enum      │         │  - apply_role_quota()    │
│  - require_admin           │         │  - count_keys_for_role() │
└──────────┬─────────────────┘         └──────────────────────────┘
           │                                      │
           │                                      ▼
           │                            ┌──────────────────────────┐
           ▼                            │ Postgres / D1 backends   │
┌────────────────────────────┐          │  UPDATE api_keys SET     │
│ RUNTIME_SETTINGS_REGISTRY  │          │   quota_daily_cost_usd=? │
│  user_daily_quota_free     │          │   WHERE user_id IN (     │
│  user_daily_quota_pro      │          │     SELECT id FROM users │
│  user_daily_quota_internal │          │     WHERE role=?)        │
│  user_daily_quota_admin    │          │   AND status='active'    │
└────────────────────────────┘          └──────────────────────────┘

Signup path:
auth_routes.signup → user_routes._create_initial_api_key
  → runtime_settings.get_float("user_daily_quota_{role}")
  → store.create_api_key(quota_daily_cost_usd=...)

Quota enforcement (UNCHANGED):
auth.verify_api_key → reads api_keys.quota_daily_cost_usd directly
```

## Components

### 1. Runtime settings registry

File: [`apps/backend/serving/config/runtime_settings.py`](../../apps/backend/serving/config/runtime_settings.py)

Add 4 entries to `RUNTIME_SETTINGS_REGISTRY`:

```python
"user_daily_quota_free": {
    "type": "float",
    "default": 100.00,
    "min": 0.0,
    "description": "Default daily USD quota seeded onto new API keys for free-tier users",
},
"user_daily_quota_pro": {
    "type": "float",
    "default": 100.00,
    "min": 0.0,
    "description": "Default daily USD quota seeded onto new API keys for pro-tier users",
},
"user_daily_quota_internal": {
    "type": "float",
    "default": 1000.00,
    "min": 0.0,
    "description": "Default daily USD quota seeded onto new API keys for internal users",
},
"user_daily_quota_admin": {
    "type": "float",
    "default": 1000.00,
    "min": 0.0,
    "description": "Default daily USD quota seeded onto new API keys for admin users",
},
```

These auto-render in the Settings tab via the existing `numericSettings` filter (`value_type === 'float'`).

### 2. Signup default by role

File: [`apps/backend/serving/servers/routers/user_routes.py`](../../apps/backend/serving/servers/routers/user_routes.py)

Replace `get_default_daily_quota()` with an async role-aware helper:

```python
async def get_default_daily_quota_for_role(
    role: str,
    runtime_settings: RuntimeSettings | None,
) -> Decimal:
    """Get default daily quota for a user role.

    Falls back to SIGNUP_DEFAULT_DAILY_QUOTA_USD env var if runtime_settings
    is unavailable (e.g. early bootstrap or DB outage during signup).
    """
    if runtime_settings is not None:
        key = f"user_daily_quota_{role}"
        if key in RUNTIME_SETTINGS_REGISTRY:
            val = await runtime_settings.get_float(key)
            return Decimal(str(val))
    quota_str = os.getenv("SIGNUP_DEFAULT_DAILY_QUOTA_USD", "100.00")
    return Decimal(quota_str)
```

Update both call sites (lines 327 and 532) to pass the user's role and the injected `RuntimeSettings`.

The original sync `get_default_daily_quota()` is removed (no other callers).

### 3. Bulk-apply admin endpoints

New file: `apps/backend/serving/servers/routers/admin/quota.py`

Two endpoints under existing `/admin` prefix:

```python
class RoleQuotaApplyRequest(BaseModel):
    role: Literal["free", "pro", "internal", "admin"]

class RoleQuotaApplyPreview(BaseModel):
    role: str
    quota: Decimal
    keys_affected: int
    users_affected: int

class RoleQuotaApplyResult(BaseModel):
    role: str
    quota: Decimal
    keys_updated: int

@router.get("/quota/role-apply-preview", response_model=RoleQuotaApplyPreview,
            dependencies=[Depends(require_admin)])
async def preview(role: Literal["free","pro","internal","admin"], ...): ...

@router.post("/quota/role-apply", response_model=RoleQuotaApplyResult,
             dependencies=[Depends(require_admin)])
async def apply(req: RoleQuotaApplyRequest, ...): ...
```

Register router in [`apps/backend/serving/servers/routers/admin/__init__.py`](../../apps/backend/serving/servers/routers/admin/__init__.py).

### 4. Storage methods

File: [`apps/backend/serving/storage/base.py`](../../apps/backend/serving/storage/base.py) (`OperationalStore` ABC)

```python
@abstractmethod
async def count_active_keys_for_role(self, role: str) -> tuple[int, int]:
    """Return (key_count, user_count) for active api_keys of users with this role."""

@abstractmethod
async def apply_role_quota(self, role: str, quota: Decimal) -> int:
    """Set quota_daily_cost_usd on all active api_keys of users with this role.
    Returns number of rows updated.
    """
```

Implement in:
- [`postgres_operational.py`](../../apps/backend/serving/storage/postgres_operational.py) — single `UPDATE ... RETURNING` style or `cursor.rowcount`
- [`d1_operational.py`](../../apps/backend/serving/storage/d1_operational.py) — execute UPDATE; D1 returns meta.changes
- [`dual_write.py`](../../apps/backend/serving/storage/dual_write.py) — fan out to both backends; return primary's count
- [`cache.py`](../../apps/backend/serving/storage/cache.py) — passthrough; invalidate any cached api_key rows touched (read existing pattern for invalidation hooks)

SQL pattern:

```sql
UPDATE api_keys
SET quota_daily_cost_usd = $1
WHERE status = 'active'
  AND user_id IN (SELECT id FROM users WHERE role = $2);
```

Count query:

```sql
SELECT COUNT(*) AS keys, COUNT(DISTINCT user_id) AS users
FROM api_keys
WHERE status = 'active'
  AND user_id IN (SELECT id FROM users WHERE role = $1);
```

### 5. Frontend client

File: [`apps/frontend/src/lib/api/admin.ts`](../../apps/frontend/src/lib/api/admin.ts)

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

export async function previewRoleQuotaApply(role: Role): Promise<RoleQuotaPreview>;
export async function applyRoleQuota(role: Role): Promise<RoleQuotaApplyResult>;
```

### 6. Frontend UI

File: [`apps/frontend/src/app/dashboard/admin/SettingsTab.tsx`](../../apps/frontend/src/app/dashboard/admin/SettingsTab.tsx)

Inside the existing numeric settings render loop (around line 280), detect quota keys via prefix:

```typescript
const QUOTA_KEY_PREFIX = 'user_daily_quota_';
const isQuotaSetting = setting.key.startsWith(QUOTA_KEY_PREFIX);
const role = isQuotaSetting ? setting.key.slice(QUOTA_KEY_PREFIX.length) as Role : null;
```

When `isQuotaSetting`, render an extra "Apply to existing users" button beside Save:

- Click → `previewRoleQuotaApply(role)` → open confirm modal showing count + quota
- Confirm → `applyRoleQuota(role)` → toast `Updated {N} keys for role {role}`

Reuse existing confirm-modal pattern (the signup-domain remove modal at the bottom of the file). Wire a new `quotaApplyConfirm` state slot.

Keep numeric Save button intact: setting the value and applying it to existing users are two distinct steps — Save persists the role's default; Apply pushes that default to existing keys.

## Data flow

### Signup (new user, role=free)

1. `auth_routes.signup()` → creates user with role=`free`
2. `user_routes._create_initial_api_key()` → calls `get_default_daily_quota_for_role("free", runtime_settings)`
3. Runtime settings reads cache → DB → registry default. Returns `Decimal("100.00")`.
4. `store.create_api_key(quota_daily_cost_usd=Decimal("100.00"), ...)`

### Admin updates `user_daily_quota_pro` to 250

1. UI calls existing `PUT /admin/runtime-settings/user_daily_quota_pro` with `value=250`
2. Stored in `site_settings` table; runtime cache invalidated.
3. **Existing** API keys unaffected. New `pro` signups get 250.

### Admin clicks "Apply to existing users" for `pro`

1. UI calls `GET /admin/quota/role-apply-preview?role=pro` → `{quota: 250, keys_affected: 42, users_affected: 38}`
2. Modal: "Overwrite quota_daily_cost_usd to $250 on 42 active API keys (38 users) with role `pro`? Custom per-key overrides will be lost."
3. Confirm → `POST /admin/quota/role-apply {role: "pro"}` → endpoint reads runtime setting (250), runs UPDATE, returns `{keys_updated: 42}`.
4. Toast: "Updated 42 keys for role `pro` to $250".

### Quota check at request time

Unchanged. [`auth.py:235-239`](../../apps/backend/serving/servers/auth.py#L235-L239) reads `user["quota_daily_cost_usd"]` from the joined api_keys row.

## Error handling

- Invalid role on either endpoint → 422 (Pydantic `Literal` enforcement).
- Non-admin caller → 403 (existing `require_admin` dep).
- DB failure during apply → 500; partial updates impossible because the UPDATE is atomic for both Postgres and D1.
- DB failure during preview → 500; UI shows error toast and aborts.
- Runtime setting missing during signup → falls back to env var (logged at WARN).
- Concurrent writes (two admins applying simultaneously) → last writer wins; acceptable since quota is a flat value, not a counter.

## Testing

### Unit tests

- `tests/unit/config/test_runtime_settings.py` — registry contains 4 quota keys; correct types, defaults, mins.
- `tests/unit/servers/test_user_routes_signup.py` — `get_default_daily_quota_for_role` picks correct registry key per role; falls back to env var when runtime_settings is None.
- `tests/unit/servers/admin/test_quota_routes.py` — endpoint validates role enum; 422 on garbage role; calls store with right args.

### API tests

- `tests/api/admin/test_quota_role_apply.py` — non-admin gets 403; admin preview returns correct shape; apply returns correct shape; invalid role rejected.

### Integration tests (`dbtest` marker)

- `tests/integration/storage/test_apply_role_quota.py` — seed users of mixed roles; apply for one role; assert only matching role's active keys updated; revoked/inactive keys untouched; users of other roles untouched.
- `tests/integration/storage/test_count_active_keys_for_role.py` — counts match seed.
- `tests/integration/test_signup_role_quota.py` — sign up two users, change `user_daily_quota_free` to 50, sign up third user, assert third user's key has quota=50, first two unchanged.

### Frontend tests

- `SettingsTab.test.ts` — quota settings render Apply button; non-quota settings do not; click flows through preview → modal → apply API call.

## Migration / rollout

- No DB schema migration. New runtime settings registry entries pick up defaults the first time they're read.
- No backfill required: existing api_keys keep their current `quota_daily_cost_usd` values until an admin clicks Apply.
- `signup_default_daily_quota_usd` env var is kept as bootstrap fallback. Mark `# Deprecated: use admin Settings → user_daily_quota_<role>` comment in [settings.py](../../apps/backend/serving/config/settings.py); remove in a follow-up after staging soak.

## Open questions

None. All decisions locked through brainstorming Q&A.

## File-touch summary

**Backend (new):**
- `apps/backend/serving/servers/routers/admin/quota.py`
- tests above

**Backend (modified):**
- `apps/backend/serving/config/runtime_settings.py` — registry entries
- `apps/backend/serving/servers/routers/user_routes.py` — async role-aware quota helper
- `apps/backend/serving/servers/routers/admin/__init__.py` — register new router
- `apps/backend/serving/storage/base.py` — ABC additions
- `apps/backend/serving/storage/postgres_operational.py` — implementations
- `apps/backend/serving/storage/d1_operational.py` — implementations
- `apps/backend/serving/storage/dual_write.py` — fan-out
- `apps/backend/serving/storage/cache.py` — passthrough + invalidation
- `apps/backend/serving/config/settings.py` — deprecation comment

**Frontend (modified):**
- `apps/frontend/src/lib/api/admin.ts` — client functions
- `apps/frontend/src/app/dashboard/admin/SettingsTab.tsx` — Apply button + modal
