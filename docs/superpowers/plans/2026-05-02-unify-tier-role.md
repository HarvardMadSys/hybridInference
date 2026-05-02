# Unify User Tier/Role; Remove Enterprise — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drop `api_keys.tier` (free/pro/enterprise) entirely. `users.role` (free/pro/internal/admin) becomes the single source of truth for billing/access classification. Remove all references to the unused `enterprise` value.

**Architecture:** The `tier` concept is fully eliminated from API responses, schemas, JWT, the **OperationalStore contract** (and its three implementations: postgres, d1, legacy direct-asyncpg in `database.py`), the cache + dual-write wrappers, the D1 schema SQL, the auth middleware, all route handlers, the frontend, and the test fixtures. Per-user gating already uses `role`; nothing in production logic reads `tier`. Frontend `ROLE_RANK` is also fixed to include the missing `pro` rank, and the role dropdown gains a `pro` option.

**Tech Stack:** FastAPI, Pydantic, asyncpg/PostgreSQL, Cloudflare D1 (SQLite-compatible HTTP API), Next.js, React, TypeScript, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-05-02-unify-tier-role-design.md`

**Worktree note:** Storage layer was refactored in PR #196 (Cloudflare D1 migration) into an `OperationalStore` contract with two backends (`postgres_operational.py`, `d1_operational.py`) plus `cache.py` and `dual_write.py` wrappers. This plan was rewritten on 2026-05-02 to cover that surface area. The original plan against the legacy direct-asyncpg `database.py` model is preserved in git history at commit `e80b5cd`.

---

## Task 0: Sync, worktree, branch — DONE

Worktree already exists at `/home/juncheng/hybridInference-worktrees/jason-claude-unify-tier-role` on branch `jason/claude/unify-tier-role`, based on `origin/dev`, with the spec + plan commits cherry-picked on top. All subsequent tasks run in this worktree.

---

## Task 1: Backend schemas — drop tier fields and enterprise patterns

**Files:**
- Modify: `serving/schemas_admin.py`
- Modify: `serving/schemas_auth.py`

- [ ] **Step 1: Remove every `tier` and `key_tier` field from `serving/schemas_admin.py`.**

Run `grep -n 'tier\|enterprise' serving/schemas_admin.py` and delete:

  - In `CreateAPIKeyRequest`: the line `tier: str = Field("free", pattern="^(free|pro|enterprise)$", description="User tier")`
  - In `CreateAPIKeyResponse`: the line `tier: str`
  - In `APIKeyListItem`: the line `tier: str`
  - In `APIKeyDetailResponse`: the line `tier: str`
  - In `UpdateAPIKeyRequest`: the line `tier: str | None = Field(None, pattern="^(free|pro|enterprise)$")`
  - In `UserListItem`: the line `key_tier: str | None = None`
  - In `UserDetailResponse`: the line `key_tier: str | None = None`
  - In `UpdateUserRequest`: the line `tier: str | None = Field(None, pattern="^(free|pro|enterprise)$")` — keep `role` field intact

- [ ] **Step 2: Remove `tier` from `serving/schemas_auth.py` `UserInfo`.**

Find the line `tier: str = "free"` (around line 74) and delete it. Keep `role: str = "free"`.

- [ ] **Step 3: Verify**

```bash
grep -n 'tier\|enterprise' serving/schemas_admin.py serving/schemas_auth.py
```

Expected: zero matches.

- [ ] **Step 4: Commit**

```bash
git add serving/schemas_admin.py serving/schemas_auth.py
git commit -m "refactor(schemas): drop tier from admin/auth pydantic models"
```

---

## Task 2: Backend JWT — drop tier param

**Files:**
- Modify: `serving/utils/jwt.py`

- [ ] **Step 1: Edit `create_access_token` signature and payload**

Open `serving/utils/jwt.py`. In `create_access_token`:

  - Remove the `tier: str = "free",` parameter from the signature
  - Remove the `tier: User tier (default: free).` line from the docstring
  - Remove the `"tier": tier,` line from the `payload` dict

The final function should accept `(user_id, email, session_id=None, expires_delta=None, is_admin=False, role="free")` and the payload should not contain `tier`.

- [ ] **Step 2: Verify**

```bash
grep -n 'tier' serving/utils/jwt.py
```

Expected: zero matches.

- [ ] **Step 3: Commit**

```bash
git add serving/utils/jwt.py
git commit -m "refactor(jwt): drop tier claim from access token"
```

---

## Task 3: Storage contract — drop tier from `OperationalStore` interface

**Files:**
- Modify: `serving/storage/base.py`

- [ ] **Step 1: Remove `tier` from `API_KEYS_MUTABLE_COLUMNS`**

Find around line 50–61:

```python
API_KEYS_MUTABLE_COLUMNS: dict[str, str] = {
    "user_name": "user_name",
    "status": "status",
    "quota_daily_cost_usd": "quota_daily_cost_usd",
    "quota_monthly_cost_usd": "quota_monthly_cost_usd",
    "expires_at": "expires_at",
    "last_used_at": "last_used_at",
    "tier": "tier",
    "notes": "notes",
    "metadata": "metadata",
    "account_id": "account_id",
}
```

Delete the `"tier": "tier",` line.

- [ ] **Step 2: Edit `get_auth_context_by_key_hash` docstring**

Find:

```
        Returns the full projection needed in a **single** call:
        ``id, user_id, user_name, quota_daily_cost_usd, tier, email, role``.
```

Replace with:

```
        Returns the full projection needed in a **single** call:
        ``id, user_id, user_name, quota_daily_cost_usd, email, role``.
```

- [ ] **Step 3: Edit `create_key` abstract signature**

Find the `tier: str = "free",` parameter and delete it.

- [ ] **Step 4: Edit `list_keys` abstract signature**

Find the `tier: str | None = None,` parameter and delete it.

- [ ] **Step 5: Edit `get_key_by_account_or_user` docstring**

Find:

```
        """Fetch key row by account_id or user_id (for login tier lookup)."""
```

Replace with:

```
        """Fetch key row by account_id or user_id (for login lookup)."""
```

- [ ] **Step 6: Verify**

```bash
grep -n 'tier' serving/storage/base.py
```

Expected: zero matches.

- [ ] **Step 7: Commit**

```bash
git add serving/storage/base.py
git commit -m "refactor(storage/base): drop tier from OperationalStore contract"
```

---

## Task 4: Storage backends — Postgres + D1 + legacy + D1 schema SQL

**Files:**
- Modify: `serving/storage/postgres_operational.py`
- Modify: `serving/storage/d1_operational.py`
- Modify: `serving/storage/d1_schema.sql`
- Modify: `serving/storage/database.py`

The contract change in Task 3 makes the abstract signatures no longer accept `tier`. Each implementation must match.

- [ ] **Step 1: Edit `serving/storage/postgres_operational.py`**

Run `grep -n 'tier' serving/storage/postgres_operational.py` to enumerate sites. Apply these edits (line numbers approximate — locate by surrounding context):

  - **`CREATE TABLE api_keys`** (around line 195): delete the line `tier TEXT DEFAULT 'free',` from the inline DDL. Also if there is an `ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS tier ...` migration in the same file, delete that ALTER.
  - **Add a new migration** immediately after the other `ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS ...` migrations and before the next `CREATE TABLE`:

    ```python
            # Drop legacy 'tier' column — tier/role unified on users.role
            # (spec: docs/superpowers/specs/2026-05-02-unify-tier-role-design.md).
            await conn.execute("""
                ALTER TABLE api_keys DROP COLUMN IF EXISTS tier
            """)
    ```

  - **`list_users` SELECT** (around lines 527 + 541): in both the simple-path and CTE-path SELECTs, drop `, k.tier AS key_tier` from the projection. Two occurrences.
  - **`get_auth_context_by_key_hash` SELECT** (around line 712): drop `k.tier,` from the column list.
  - **`create_key` signature** (around line 751): drop the `tier: str = "free",` parameter.
  - **`create_key` INSERT** (around line 763): drop `tier,` from the column list and drop the `tier,` value from the VALUES list. Adjust the placeholder count ($1, $2, …) if the SQL is built with positional params; renumber so it's contiguous. Also drop the corresponding `tier,` argument from the `await conn.execute(sql, *args)` (or fetchrow) call below it.
  - **`list_keys` signature** (around line 795): drop the `tier: str | None = None,` parameter.
  - **`list_keys` filter** (around lines 806–808): delete the block:

    ```python
            if tier:
                where_clauses.append(f"tier = ${len(params) + 1}")
                params.append(tier)
    ```

  - **`list_keys` SELECT** (around lines 822, 837): drop `tier,` from the projection. Two occurrences.
  - **`get_key_by_account_or_user`** (around line 901): the SELECT is `SELECT tier FROM api_keys WHERE ...`. Replace with `SELECT id FROM api_keys WHERE ...`. After Task 7, no caller reads any column from this row, but the row's truthiness is no longer used either. The simplest, safest change is to keep the method returning a row when an active key exists (so `key_row` is non-None) by selecting `id`.

- [ ] **Step 2: Verify postgres_operational.py**

```bash
grep -n 'tier' serving/storage/postgres_operational.py
```

Expected: zero matches.

- [ ] **Step 3: Edit `serving/storage/d1_operational.py` (D1 backend)**

Apply the symmetric changes:

  - **`list_users` SELECT** (around line 314): drop `, k.tier AS key_tier`.
  - **`get_auth_context_by_key_hash` SELECT** (around line 387): drop `k.tier,` from the column list.
  - **`create_key` signature** (around line 427): drop `tier: str = "free",`.
  - **`create_key` INSERT SQL** (around line 439): drop `tier, ` from the column list and the corresponding `?` placeholder from VALUES. Drop the `tier,` argument from the params tuple at line 449.
  - **`list_keys` signature** (around line 476): drop `tier: str | None = None,`.
  - **`list_keys` filter** (around lines 487–489): delete:

    ```python
            if tier:
                where_clauses.append("tier = ?")
                params.append(tier)
    ```

  - **`list_keys` SELECT** (around lines 501, 515): drop `tier,` from projection. Two occurrences.
  - **`get_key_by_account_or_user`** (around line 592): same change as postgres — replace `SELECT tier FROM api_keys WHERE ...` with `SELECT id FROM api_keys WHERE ...`.

- [ ] **Step 4: Verify d1_operational.py**

```bash
grep -n 'tier' serving/storage/d1_operational.py
```

Expected: zero matches.

- [ ] **Step 5: Edit `serving/storage/d1_schema.sql`**

Find at line 54:

```sql
    tier                     TEXT DEFAULT 'free',
```

Delete that line.

- [ ] **Step 6: Edit `serving/storage/database.py` (legacy direct-asyncpg path)**

This file is the old path; it is still imported by integration tests and may still be referenced by some code paths. Apply the same migration pattern as in `postgres_operational.py`:

  - In the `CREATE TABLE IF NOT EXISTS api_keys` DDL (around line 265), delete the `tier TEXT DEFAULT 'free',` line.
  - Add an idempotent migration:

    ```python
                await conn.execute("""
                    ALTER TABLE api_keys DROP COLUMN IF EXISTS tier
                """)
    ```

    Place it next to the other `ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS ...` migrations.

- [ ] **Step 7: Verify**

```bash
grep -n 'tier' serving/storage/database.py serving/storage/d1_schema.sql
```

Expected: only the legitimate role-related comment at `database.py:487` (`# Expand the role CHECK constraint to include the "pro" tier.`) remains. Anything else must be cleaned up.

- [ ] **Step 8: Commit**

```bash
git add serving/storage/postgres_operational.py serving/storage/d1_operational.py serving/storage/d1_schema.sql serving/storage/database.py
git commit -m "refactor(storage): drop tier column from postgres/d1/legacy backends"
```

---

## Task 5: Storage middleware — cache + dual_write delegate signatures

**Files:**
- Modify: `serving/storage/cache.py`
- Modify: `serving/storage/dual_write.py`

These wrappers forward arguments to the inner store. After the contract change, the `tier` parameter is gone from inner methods, so wrappers must drop it too.

- [ ] **Step 1: Edit `serving/storage/cache.py`**

  - **`create_key`** (around line 332): drop the `tier: str = "free",` parameter from the signature, and drop the `tier=tier,` line from the forwarded kwargs to `self._store.create_key(...)`.
  - **`list_keys`** (around line 363): drop the `tier: str | None = None,` parameter from the signature, and update the forwarded call from `await self._store.list_keys(status=status, tier=tier, limit=limit, offset=offset)` to `await self._store.list_keys(status=status, limit=limit, offset=offset)`.

- [ ] **Step 2: Edit `serving/storage/dual_write.py`**

  - **`list_keys`** (around line 270): drop the `tier: str | None = None,` parameter and update the forwarded call to drop `tier=tier`.
  - **`create_key`** (around line 298): drop the `tier: str = "free",` parameter and drop `tier=tier,` from BOTH forwarded calls (`self._primary.create_key(...)` and `self._shadow.create_key(...)`).

- [ ] **Step 3: Verify**

```bash
grep -n 'tier' serving/storage/cache.py serving/storage/dual_write.py
```

Expected: zero matches.

- [ ] **Step 4: Commit**

```bash
git add serving/storage/cache.py serving/storage/dual_write.py
git commit -m "refactor(storage): drop tier from cache/dual_write wrappers"
```

---

## Task 6: Auth middleware — `serving/servers/deps.py` + `serving/servers/auth.py`

**Files:**
- Modify: `serving/servers/deps.py`
- Modify: `serving/servers/auth.py`

- [ ] **Step 1: Edit `serving/servers/deps.py`**

Run `grep -n 'tier' serving/servers/deps.py`. Expected hits at lines 147, 178, 219.

  - Line 147 (docstring): change `User context dictionary with user_id, email, tier, etc.` to `User context dictionary with user_id, email, role, etc.`
  - Line 178: delete the line `tier = payload.get("tier", "free")`
  - Line 219: delete the line `"tier": tier,` from the returned context dict

- [ ] **Step 2: Edit `serving/servers/auth.py`**

Run `grep -n 'tier' serving/servers/auth.py`. Expected hits at lines 87, 95, 217.

  - Line 87 (docstring): change `Returns user context dict with user_id, tier, etc.` to `Returns user context dict with user_id, role, etc.`
  - Line 95: delete the line `"tier": "free",` (this is part of an admin/dev fallback context dict — drop the field).
  - Line 217: delete the line `"tier": user["tier"],` from the returned context dict.

- [ ] **Step 3: Verify**

```bash
grep -n 'tier' serving/servers/deps.py serving/servers/auth.py
```

Expected: zero matches.

- [ ] **Step 4: Commit**

```bash
git add serving/servers/deps.py serving/servers/auth.py
git commit -m "refactor(auth): drop tier from middleware context"
```

---

## Task 7: Auth routes — login / refresh / google

**Files:**
- Modify: `serving/servers/routers/auth_routes.py`

- [ ] **Step 1: Login flow — drop tier fetch and pass-through**

Find and edit the login flow (around line 287–340).

Old (around 287–294):

```python
    # Update last login timestamp and fetch API key tier
    await op_store.update_user_last_login(user_row["id"])
    key_row = await op_store.get_key_by_account_or_user(user_row["id"])

    # Create session and tokens
    session_id = generate_session_id()
    user_role = user_row["role"] or "free"
    user_tier = (key_row["tier"] if key_row else None) or "free"
```

New:

```python
    # Update last login timestamp
    await op_store.update_user_last_login(user_row["id"])

    # Create session and tokens
    session_id = generate_session_id()
    user_role = user_row["role"] or "free"
```

Old (around 305–312, `create_access_token` call):

```python
    access_token, jti = create_access_token(
        user_id=user_row["id"],
        email=user_row["email"],
        tier=user_tier,
        session_id=session_id,
        is_admin=is_admin,
        role=user_role,
    )
```

New (drop `tier=user_tier,`):

```python
    access_token, jti = create_access_token(
        user_id=user_row["id"],
        email=user_row["email"],
        session_id=session_id,
        is_admin=is_admin,
        role=user_role,
    )
```

Old (around 333–340, `LoginResponse` builder):

```python
        user=UserInfo(
            id=user_row["id"],
            email=user_row["email"],
            user_name=user_row["user_name"],
            tier=user_tier,
            role=user_role,
            ...
```

New (drop `tier=user_tier,`):

```python
        user=UserInfo(
            id=user_row["id"],
            email=user_row["email"],
            user_name=user_row["user_name"],
            role=user_role,
            ...
```

- [ ] **Step 2: Refresh flow — drop tier fetch and pass-through**

Find around line 420–456:

Old:

```python
    # Get user info and API key tier
    user_row = await op_store.get_user_by_id(session_row["user_id"])

    if not user_row or user_row["status"] != "active":
        raise HTTPException(...)

    require_verification = os.getenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "1") == "1"
    if require_verification and not user_row["email_verified"]:
        raise HTTPException(...)

    # Fetch API key tier
    key_row = await op_store.get_key_by_account_or_user(user_row["id"])
    user_tier = (key_row["tier"] if key_row else None) or "free"

    # Create new access token
    user_role = user_row["role"] or "free"
```

New:

```python
    user_row = await op_store.get_user_by_id(session_row["user_id"])

    if not user_row or user_row["status"] != "active":
        raise HTTPException(...)

    require_verification = os.getenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "1") == "1"
    if require_verification and not user_row["email_verified"]:
        raise HTTPException(...)

    # Create new access token
    user_role = user_row["role"] or "free"
```

(Keep the existing `HTTPException(...)` bodies intact — only the tier-fetch lines and the comments referencing tier are removed.)

Old (refresh `create_access_token` call):

```python
    access_token, jti = create_access_token(
        user_id=user_row["id"],
        email=user_row["email"],
        tier=user_tier,
        session_id=session_row["sid"],
        is_admin=is_admin,
        role=user_role,
    )
```

New: drop the `tier=user_tier,` line.

- [ ] **Step 3: Google OAuth flow — apply same pattern**

```bash
grep -n 'user_tier\|key_row\["tier"\]\|tier=user_tier\|tier=' serving/servers/routers/auth_routes.py
```

If any matches remain (typically a Google OAuth callback further down the file), apply the same removals: drop the `op_store.get_key_by_account_or_user(...)` call when its only purpose is the tier lookup, drop the `user_tier = ...` assignment, drop `tier=user_tier` from `create_access_token(...)` and from the `UserInfo(...)` constructor.

- [ ] **Step 4: Verify**

```bash
grep -n 'tier' serving/servers/routers/auth_routes.py
```

Expected: zero matches.

- [ ] **Step 5: Commit**

```bash
git add serving/servers/routers/auth_routes.py
git commit -m "refactor(auth_routes): drop tier from login/refresh/google flows"
```

---

## Task 8: User routes — drop `tier=` from every `UserInfo` builder

**Files:**
- Modify: `serving/servers/routers/user_routes.py`

- [ ] **Step 1: Enumerate sites**

```bash
grep -n 'tier' serving/servers/routers/user_routes.py
```

Expected hits at lines 136 (docstring), 150, 267, 280, 476, 624.

- [ ] **Step 2: Edit each site**

For each `UserInfo(...)` builder, drop the `tier=current_user.get("tier", "free"),` line. Concrete sites:

  - Line 136 (docstring): change `Returns user profile including email, tier, role, status, and account creation date.` → `Returns user profile including email, role, status, and account creation date.`
  - Line 150: drop `tier=current_user.get("tier", "free"),` from the `/me` response.
  - Line 267: drop `tier=current_user.get("tier", "free"),` from the next `UserInfo(...)` site.
  - Line 280: drop `"tier": current_user.get("tier", "free"),` from a dict literal.
  - Line 476: drop `tier=current_user.get("tier", "free"),`.
  - Line 624: drop `tier=current_user.get("tier", "free"),`.

If any of these sites turn out to be inside an unrelated context (e.g., a non-`UserInfo` dict that the spec wants to preserve), surface it instead of dropping blindly. The rule: if the field is the literal string `tier` carrying a copy of the JWT/user tier, it goes.

- [ ] **Step 3: Verify**

```bash
grep -n 'tier' serving/servers/routers/user_routes.py
```

Expected: zero matches.

- [ ] **Step 4: Commit**

```bash
git add serving/servers/routers/user_routes.py
git commit -m "refactor(user_routes): drop tier from /me and profile responses"
```

---

## Task 9: Admin routes — drop tier from API key CRUD and user views

**Files:**
- Modify: `serving/servers/routers/admin.py`

The admin route now delegates to `op_store` for all DB calls. The work here is to drop `tier` from:

- create-key endpoint (request payload, audit log, response builder, store call)
- list-keys endpoint (query param, store call, response row builder)
- key-detail endpoint (response builder)
- update-key endpoint (no tier-specific work — tier is allowlist-removed in Task 3)
- list-users endpoint (`UserListItem` builder)
- user-detail endpoint (`UserDetailResponse` builder)
- update-user endpoint (key_fields whitelist)

- [ ] **Step 1: Enumerate sites**

```bash
grep -n 'tier' serving/servers/routers/admin.py
```

Expected hits around lines 224, 238, 248, 260, 271, 280, 301, 375, 585, 748, 772, 831.

- [ ] **Step 2: Edit `create_api_key` endpoint (around lines 224–248)**

Current shape: passes `tier=payload.tier` to `op_store.create_key(...)` and includes `"tier": payload.tier` in the audit-log details, plus `tier=payload.tier` in `CreateAPIKeyResponse(...)`.

  - Drop `tier=payload.tier,` from the `op_store.create_key(...)` call.
  - Drop `"tier": payload.tier,` from the audit-log details dict.
  - Drop `tier=payload.tier,` from the `CreateAPIKeyResponse(...)` constructor.

- [ ] **Step 3: Edit `list_api_keys` endpoint (around lines 260–301)**

  - Drop the function parameter `tier: str | None = None,`.
  - Drop `Filter by tier (free|pro|enterprise)` from the docstring.
  - Update the call from `await op_store.list_keys(status=status, tier=tier, limit=limit, offset=offset)` to `await op_store.list_keys(status=status, limit=limit, offset=offset)`.
  - In the response-row builder around line 301, drop `tier=row["tier"],` from `APIKeyListItem(...)`.

- [ ] **Step 4: Edit `get_api_key_detail` (around line 375)**

Drop `tier=row["tier"],` from the `APIKeyDetailResponse(...)` constructor.

- [ ] **Step 5: Edit `list_users` `UserListItem` builder (around line 585)**

Drop `key_tier=row.get("key_tier"),` from `UserListItem(...)`.

- [ ] **Step 6: Edit `get_user_detail` `UserDetailResponse` builder (around line 748)**

Drop `key_tier=key_row["tier"] if key_row else None,` from `UserDetailResponse(...)`.

- [ ] **Step 7: Edit `update_user` (around lines 772 + 831)**

  - Line 772 docstring: change `Update user account status or API key settings (tier, quota).` → `Update user account status or API key settings (quota).`
  - Line 831: change

    ```python
            if k in ("tier", "quota_daily_cost_usd", "quota_monthly_cost_usd")
    ```

    to

    ```python
            if k in ("quota_daily_cost_usd", "quota_monthly_cost_usd")
    ```

- [ ] **Step 8: Verify**

```bash
grep -n 'tier' serving/servers/routers/admin.py
```

Expected: zero matches.

- [ ] **Step 9: Commit**

```bash
git add serving/servers/routers/admin.py
git commit -m "refactor(admin): drop tier from api-key crud, user list, user detail, update"
```

---

## Task 10: Backend settings — drop unused signup_default_tier

**Files:**
- Modify: `serving/config/settings.py`

- [ ] **Step 1: Edit**

Find around line 46:

```python
    # Signup
    signup_enabled: bool = True
    signup_default_tier: str = "free"
    signup_default_daily_quota_usd: float = 100.00
```

Delete the `signup_default_tier: str = "free"` line.

- [ ] **Step 2: Verify**

```bash
grep -rn 'signup_default_tier' serving/ frontend/ test/ 2>/dev/null | grep -v __pycache__
```

Expected: zero matches.

- [ ] **Step 3: Commit**

```bash
git add serving/config/settings.py
git commit -m "refactor(settings): remove unused signup_default_tier"
```

---

## Task 11: Frontend types — drop tier from auth/admin types

**Files:**
- Modify: `frontend/src/lib/api/admin.ts`
- Modify: `frontend/src/lib/api/auth.ts`

- [ ] **Step 1: Edit `frontend/src/lib/api/admin.ts`**

Drop these fields:

  - `AdminUser`: drop the `key_tier: string | null;` field
  - `AdminApiKey`: drop the `tier: string;` field
  - `CreateApiKeyRequest`: drop the `tier?: string;` field
  - `CreateApiKeyResponse`: drop the `tier: string;` field
  - `UserDetail`: drop the `key_tier: string | null;` field
  - `UpdateUserData`: drop the `tier?: string;` field

Drop these call-site usages:

  - `listApiKeys(status?, tier?, limit, offset)`: drop the `tier?: string` parameter and the `if (tier) params.set('tier', tier);` line
  - `createApiKeyAdmin(data)`: drop the `tier: data.tier || 'free',` line from the JSON body

- [ ] **Step 2: Edit `frontend/src/lib/api/auth.ts`**

```bash
grep -n 'tier' frontend/src/lib/api/auth.ts
```

If a `tier?:` or `tier:` field exists on `LoginResponse` or any related interface, delete it. If `grep` returns nothing, this step is a no-op.

- [ ] **Step 3: Verify**

```bash
grep -n 'tier' frontend/src/lib/api/admin.ts frontend/src/lib/api/auth.ts
```

Expected: zero matches.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/api/admin.ts frontend/src/lib/api/auth.ts
git commit -m "refactor(frontend/api): drop tier from admin and auth types"
```

---

## Task 12: Frontend AuthProvider — drop tier; fix ROLE_RANK

**Files:**
- Modify: `frontend/src/components/providers/AuthProvider.tsx`

- [ ] **Step 1: Edit `User` interface — drop `tier: string;`**

- [ ] **Step 2: Edit `ROLE_RANK` to match backend**

From:

```typescript
const ROLE_RANK: Record<string, number> = {
  free: 0,
  internal: 1,
  admin: 2,
};
```

To (matches `serving/config/settings.py:172`):

```typescript
const ROLE_RANK: Record<string, number> = {
  free: 0,
  pro: 1,
  internal: 2,
  admin: 3,
};
```

- [ ] **Step 3: Edit `refreshUser` setState — drop `tier: me.tier,` line.**

- [ ] **Step 4: Verify**

```bash
grep -n 'tier' frontend/src/components/providers/AuthProvider.tsx
```

Expected: zero matches.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/providers/AuthProvider.tsx
git commit -m "refactor(AuthProvider): drop tier; add pro to ROLE_RANK"
```

---

## Task 13: Frontend admin page — drop tier UI; remove enterprise; add pro role option

**Files:**
- Modify: `frontend/src/app/dashboard/admin/page.tsx`

The implementer must:

  - Drop the `editTier` / `setEditTier` `useState`.
  - Drop the `setEditTier(d.key_tier || 'free')` call in `toggleDetail` and the `editTier !== ...` diff in `doSave`.
  - Drop the `key_tier` badge in the user-row list (the JSX block `{u.has_key && u.key_tier && u.key_tier !== 'free' && (<span>...{u.key_tier}</span>)}`).
  - Drop the entire **Tier** dropdown JSX block from the user-detail edit panel — keep the `{detail.has_key && (<>...</>)}` fragment but remove the `Tier` label + select. The `Daily quota` block immediately after stays.
  - Add `<option value="pro">pro</option>` to the **Role** dropdown (which currently has only free/internal/admin), placed between `free` and `internal`.
  - Verify no stray `enterprise` option remains.

- [ ] **Step 1: Verify**

```bash
grep -n 'tier\|enterprise\|editTier\|key_tier' frontend/src/app/dashboard/admin/page.tsx
```

Expected: zero matches.

- [ ] **Step 2: Commit**

```bash
git add frontend/src/app/dashboard/admin/page.tsx
git commit -m "refactor(admin/page): drop tier UI; remove enterprise; add pro role option"
```

---

## Task 14: Tests — update fixtures and assertions

**Files:** every test file that references `tier`. Enumerate at the start.

- [ ] **Step 1: Enumerate**

```bash
grep -rn 'tier\|enterprise' test/ 2>/dev/null | grep -v __pycache__ | grep -v 'codex_token\|claude_token'
```

Expected file set (from worktree at start of refactor):

- `test/servers/test_admin_api.py`
- `test/servers/test_admin_users.py`
- `test/servers/test_user_routes.py`
- `test/servers/test_auth.py`
- `test/servers/test_compat.py`
- `test/servers/conftest_auth.py`
- `test/fixtures/auth_factories.py`
- `test/integration/test_database_integration.py`
- `test/integration/test_d1_operational_real.py`
- `test/unit/utils/test_jwt.py`
- `test/unit/storage/test_cached_store.py`
- `test/unit/storage/test_cache_invalidation.py`
- `test/unit/storage/test_d1_operational.py`
- `test/unit/storage/test_d1_sqlite_integration.py`
- `test/unit/storage/test_dual_write.py`
- `test/unit/storage/test_dual_write_integration.py`

- [ ] **Step 2: Apply per-file rules**

  - **JWT/auth tests** (`test_jwt.py`, `test_auth.py`, `conftest_auth.py`, `auth_factories.py`, `test_compat.py`): if a test calls `create_access_token(..., tier="...")` or asserts `payload["tier"] == "..."`, drop the `tier=` keyword arg and drop the assertion. Do not replace the assertion with anything; the test now exercises the no-tier behavior.
  - **Storage tests** (`test_cached_store.py`, `test_cache_invalidation.py`, `test_d1_operational.py`, `test_d1_sqlite_integration.py`, `test_dual_write.py`, `test_dual_write_integration.py`): if a test calls `create_key(..., tier="...")` or `update_key(..., tier="...")`, drop the kwarg. If a test mocks a row that includes `"tier": "..."`, drop that field. If a test asserts on a column list that includes `tier` (e.g., raw SQL string assertions like `"(key_hash, key_prefix, user_id, user_name, status, tier, ...)"`), update the assertion to match the new column list.
  - **Integration tests** (`test_database_integration.py`, `test_d1_operational_real.py`): if the test issues a raw SQL `INSERT INTO api_keys (..., tier) VALUES (..., 'enterprise')`, drop both the column and the value.
  - **Admin tests** (`test_admin_api.py`, `test_admin_users.py`):
    - `test_admin_api.py`: drop `"tier": "..."` from request bodies and from `connection.fetch.side_effect` row dicts. Update the `assert body["updated_fields"] == ["tier", "quota_daily_cost_usd"]` assertion to drop `"tier"`. The `test_update_api_key_not_found` body `{"tier": "pro"}` becomes `{"quota_daily_cost_usd": 100}`.
    - `test_admin_users.py`: drop `"key_tier": "free",` from row fixtures (lines ~99, ~505).
  - **User-routes test** (`test_user_routes.py`): drop `tier="free",` from the `create_access_token(...)` call site at line ~543.

- [ ] **Step 3: Run the affected suites**

```bash
uv run pytest test/servers test/unit/storage test/unit/utils -x
```

Expected: all pass. If anything fails, the implementer reads the error and either fixes the test (if it asserts on something that changed) or fixes the source code (if a real regression slipped through earlier tasks).

- [ ] **Step 4: Sweep for stragglers**

```bash
grep -rn 'tier\|enterprise' test/ 2>/dev/null \
  | grep -v __pycache__ \
  | grep -v 'codex_token\|claude_token\|selected_tier\|routewise_tier'
```

Expected: zero matches. If anything appears, address it.

- [ ] **Step 5: Commit**

```bash
git add test/
git commit -m "test: drop tier from admin, auth, storage, integration, jwt fixtures"
```

---

## Task 15: Full sweep — confirm enterprise gone, run lint + tests

**Files:**
- None modified directly.

- [ ] **Step 1: Confirm `enterprise` gone (except unrelated upstream fields)**

```bash
grep -rn 'enterprise' serving/ frontend/src/ test/ config/ 2>/dev/null \
  | grep -v __pycache__ | grep -v node_modules
```

Expected: only the comment in `serving/adapters/claude_token.py:82` remains:

```
serving/adapters/claude_token.py:82:    plan: str = "pro"  # pro / max / team / enterprise
```

That documents the upstream Claude API's `plan` field and is unrelated. Anything else must be cleaned up.

- [ ] **Step 2: Confirm `tier` reduced to legitimate non-user-tier uses**

```bash
grep -rn 'tier' serving/ frontend/src/ test/ 2>/dev/null \
  | grep -v __pycache__ | grep -v node_modules \
  | grep -vE 'codex_token|claude_token|selected_tier|routewise_tier|the .* tier|# .*tier'
```

Expected: zero matches outside the exception set:
- RouteWise routing-tier metric (`serving/observability/metrics.py`, `routewise_tier_decisions_total`) — unrelated subsystem
- Codex/Claude subscription tier in the upstream-API token adapters
- `selected_tier` in completions router (RouteWise output)
- Comment in `serving/storage/database.py:487` about the role pro tier

- [ ] **Step 3: Run ruff format check (per CLAUDE.md)**

```bash
uv run ruff format --check .
```

If it reports drift, run `uv run ruff format .`, review the diff, and commit as `chore: ruff format follow-up`.

- [ ] **Step 4: Run ruff lint**

```bash
uv run ruff check .
```

Fix any new lint errors (likely unused imports left by removed `Field(...)` patterns). Commit as `chore: lint cleanup`.

- [ ] **Step 5: Run full backend test suite**

```bash
uv run pytest -x
```

Expected: all pass. Investigate and fix any failure caused by the refactor.

- [ ] **Step 6: Frontend type check + build**

```bash
cd frontend && npm run typecheck && npm run lint && cd ..
```

Expected: pass. Fix any TypeScript errors (likely references to removed fields).

- [ ] **Step 7: Commit any cleanup**

```bash
git status
# if any changes:
git add -A
git commit -m "chore: lint/format follow-up after tier removal"
```

---

## Task 16: Push branch, open PR, review

**Files:**
- None modified.

- [ ] **Step 1: Push**

```bash
git push -u origin jason/claude/unify-tier-role
```

- [ ] **Step 2: Open PR to dev**

```bash
gh pr create --base dev --title "refactor: unify user tier and role; remove enterprise" --body "$(cat <<'EOF'
## Summary
- Drop `api_keys.tier` column and field; `users.role` is now the single source of truth for billing/access classification.
- Remove the unused `enterprise` value from schemas, storage layer, UI, and tests.
- Cover the new storage abstraction added in PR #196: `OperationalStore` contract, postgres + d1 backends, cache + dual_write wrappers, and the D1 schema SQL.
- Fix frontend `ROLE_RANK` to match backend (adds missing `pro` rank); surface `pro` in the admin role dropdown.

Spec: [docs/superpowers/specs/2026-05-02-unify-tier-role-design.md](docs/superpowers/specs/2026-05-02-unify-tier-role-design.md)
Plan: [docs/superpowers/plans/2026-05-02-unify-tier-role.md](docs/superpowers/plans/2026-05-02-unify-tier-role.md)

## Test plan
- [ ] `uv run ruff format --check .` passes
- [ ] `uv run ruff check .` passes
- [ ] `uv run pytest` passes
- [ ] `cd frontend && npm run typecheck && npm run lint` passes
- [ ] Login + /me returns no `tier`; role still surfaces correctly
- [ ] Admin user list/detail and API-key CRUD work end-to-end on staging
- [ ] DB migration drops `api_keys.tier` cleanly on staging Postgres and D1

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Capture URL**

The `gh pr create` output prints the PR URL. Record it; report back to the user.

- [ ] **Step 4: Review and address any issues**

```bash
gh pr view --web   # or read the diff locally
git diff origin/dev...HEAD
```

Look for:
- Any `tier` reference still in the diff outside the unrelated codex/claude/router files.
- Missing test updates.
- Schema/UI mismatches.

If issues found, fix and push to the same branch.
