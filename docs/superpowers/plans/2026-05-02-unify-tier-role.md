# Unify User Tier/Role; Remove Enterprise — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drop `api_keys.tier` (free/pro/enterprise) entirely. `users.role` (free/pro/internal/admin) becomes the single source of truth for billing/access classification. Remove all references to the unused `enterprise` value.

**Architecture:** The `tier` concept is fully eliminated from API responses, schemas, JWT, DB, and UI. Per-user gating already uses `role`; nothing in production logic reads `tier`. Frontend `ROLE_RANK` is also fixed to include the missing `pro` rank, and the role dropdown gains a `pro` option.

**Tech Stack:** FastAPI, Pydantic, asyncpg/PostgreSQL, Next.js, React, TypeScript, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-05-02-unify-tier-role-design.md`

---

## Task 0: Sync, worktree, branch

**Files:**
- Worktree under: `/home/juncheng/hybridInference-worktrees/jason-claude-unify-tier-role`
- Branch: `jason/claude/unify-tier-role` (off `origin/dev`)

- [ ] **Step 1: Pull latest dev**

```bash
cd /home/juncheng/hybridInference
git fetch origin
git checkout dev
git pull --ff-only origin dev
```

Expected: `Already up to date.` or fast-forward.

- [ ] **Step 2: Create worktree on new branch**

```bash
mkdir -p /home/juncheng/hybridInference-worktrees
git worktree add -b jason/claude/unify-tier-role \
  /home/juncheng/hybridInference-worktrees/jason-claude-unify-tier-role \
  origin/dev
cd /home/juncheng/hybridInference-worktrees/jason-claude-unify-tier-role
```

Expected: worktree created at the path; HEAD on new branch.

- [ ] **Step 3: Sanity check**

```bash
git rev-parse --abbrev-ref HEAD
git status
```

Expected: branch `jason/claude/unify-tier-role`, clean tree.

---

## Task 1: Backend schemas — drop tier fields and enterprise patterns

**Files:**
- Modify: `serving/schemas_admin.py`
- Modify: `serving/schemas_auth.py`

- [ ] **Step 1: Edit `serving/schemas_admin.py` — `CreateAPIKeyRequest`**

Remove the `tier` field. Find the field at line ~15:

```python
    tier: str = Field("free", pattern="^(free|pro|enterprise)$", description="User tier")
```

Delete that line entirely.

- [ ] **Step 2: Edit `serving/schemas_admin.py` — `CreateAPIKeyResponse`**

Find at line ~48:

```python
    tier: str
```

Delete that line.

- [ ] **Step 3: Edit `serving/schemas_admin.py` — `APIKeyListItem`**

Find at line ~65:

```python
    tier: str
```

Delete that line.

- [ ] **Step 4: Edit `serving/schemas_admin.py` — `APIKeyDetailResponse`**

Find at line ~105:

```python
    tier: str
```

Delete that line.

- [ ] **Step 5: Edit `serving/schemas_admin.py` — `UpdateAPIKeyRequest`**

Find at line ~121:

```python
    tier: str | None = Field(None, pattern="^(free|pro|enterprise)$")
```

Delete that line.

- [ ] **Step 6: Edit `serving/schemas_admin.py` — `UserListItem.key_tier`**

Find at line ~185:

```python
    key_tier: str | None = None
```

Delete that line.

- [ ] **Step 7: Edit `serving/schemas_admin.py` — `UserDetailResponse.key_tier`**

Find at line ~256:

```python
    key_tier: str | None = None
```

Delete that line.

- [ ] **Step 8: Edit `serving/schemas_admin.py` — `UpdateUserRequest`**

Find at line ~276:

```python
    tier: str | None = Field(None, pattern="^(free|pro|enterprise)$")
```

Delete that line. Keep the `role` field above it intact.

- [ ] **Step 9: Edit `serving/schemas_auth.py` — `UserInfo`**

Find at line ~74:

```python
    tier: str = "free"
```

Delete that line. Keep `role: str = "free"` below it.

- [ ] **Step 10: Verify no orphan references**

```bash
grep -n 'tier' serving/schemas_admin.py serving/schemas_auth.py
```

Expected: zero matches.

- [ ] **Step 11: Commit**

```bash
git add serving/schemas_admin.py serving/schemas_auth.py
git commit -m "refactor(schemas): drop tier from admin/auth pydantic models"
```

---

## Task 2: Backend JWT — drop tier param

**Files:**
- Modify: `serving/utils/jwt.py`

- [ ] **Step 1: Edit `serving/utils/jwt.py` — `create_access_token`**

Replace the function signature and payload (lines ~54–96). Old:

```python
def create_access_token(
    user_id: str,
    email: str,
    tier: str = "free",
    session_id: str | None = None,
    expires_delta: timedelta | None = None,
    is_admin: bool = False,
    role: str = "free",
) -> tuple[str, str]:
    """Create a JWT access token.

    Args:
        user_id: User ID to encode in token.
        email: User email to encode in token.
        tier: User tier (default: free).
        session_id: Session ID for token rotation (optional).
        expires_delta: Custom expiration time (default: from env).
        is_admin: Whether user has admin privileges.
        role: User permission role (free/pro/internal/admin).

    Returns:
        Tuple of (token_string, jti).
    """
    if expires_delta is None:
        expires_delta = timedelta(minutes=get_access_token_expire_minutes())

    jti = generate_jti()
    sid = session_id or generate_session_id()

    now = datetime.now(timezone.utc)
    expire = now + expires_delta

    payload = {
        "sub": user_id,
        "email": email,
        "tier": tier,
        "role": role,
        "is_admin": is_admin,
        "jti": jti,
        "sid": sid,
        "iat": now,
        "exp": expire,
    }
```

New:

```python
def create_access_token(
    user_id: str,
    email: str,
    session_id: str | None = None,
    expires_delta: timedelta | None = None,
    is_admin: bool = False,
    role: str = "free",
) -> tuple[str, str]:
    """Create a JWT access token.

    Args:
        user_id: User ID to encode in token.
        email: User email to encode in token.
        session_id: Session ID for token rotation (optional).
        expires_delta: Custom expiration time (default: from env).
        is_admin: Whether user has admin privileges.
        role: User permission role (free/pro/internal/admin).

    Returns:
        Tuple of (token_string, jti).
    """
    if expires_delta is None:
        expires_delta = timedelta(minutes=get_access_token_expire_minutes())

    jti = generate_jti()
    sid = session_id or generate_session_id()

    now = datetime.now(timezone.utc)
    expire = now + expires_delta

    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "is_admin": is_admin,
        "jti": jti,
        "sid": sid,
        "iat": now,
        "exp": expire,
    }
```

(Single Edit changes two things: signature loses `tier:` line, payload loses `"tier": tier,` line.)

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

## Task 3: Backend auth middleware — drop tier from API-key auth query

**Files:**
- Modify: `serving/servers/auth.py`

- [ ] **Step 1: Edit `serving/servers/auth.py`**

Find the SQL at line ~137:

```python
            user_row = await conn.fetchrow(
                """
                SELECT k.id, k.user_id, k.user_name, k.quota_daily_cost_usd, k.tier,
                       u.email, u.role, u.email_verified
                FROM api_keys k
                LEFT JOIN users u ON u.id = k.user_id
                WHERE k.key_hash = $1
                  AND k.status = 'active'
                  AND (k.expires_at IS NULL OR k.expires_at > NOW())
                  AND (u.id IS NULL OR u.status = 'active')
                """,
                key_hash,
            )
```

Replace with:

```python
            user_row = await conn.fetchrow(
                """
                SELECT k.id, k.user_id, k.user_name, k.quota_daily_cost_usd,
                       u.email, u.role, u.email_verified
                FROM api_keys k
                LEFT JOIN users u ON u.id = k.user_id
                WHERE k.key_hash = $1
                  AND k.status = 'active'
                  AND (k.expires_at IS NULL OR k.expires_at > NOW())
                  AND (u.id IS NULL OR u.status = 'active')
                """,
                key_hash,
            )
```

- [ ] **Step 2: Verify**

```bash
grep -n 'tier' serving/servers/auth.py
```

Expected: zero matches.

- [ ] **Step 3: Commit**

```bash
git add serving/servers/auth.py
git commit -m "refactor(auth): drop k.tier from api-key auth select"
```

---

## Task 4: Backend auth routes — drop tier from login/google/refresh

**Files:**
- Modify: `serving/servers/routers/auth_routes.py`

- [ ] **Step 1: Edit login flow — remove tier fetch and field**

Find at line ~326–340:

```python
    # Update last login timestamp and fetch API key tier
    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_login_at = NOW() WHERE id = $1",
            user_row["id"],
        )
        key_row = await conn.fetchrow(
            "SELECT tier FROM api_keys WHERE (account_id = $1 OR user_id = $1) AND status = 'active' LIMIT 1",
            user_row["id"],
        )

    # Create session and tokens
    session_id = generate_session_id()
    user_role = user_row["role"] or "free"
    user_tier = (key_row["tier"] if key_row else None) or "free"
```

Replace with:

```python
    # Update last login timestamp
    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_login_at = NOW() WHERE id = $1",
            user_row["id"],
        )

    # Create session and tokens
    session_id = generate_session_id()
    user_role = user_row["role"] or "free"
```

- [ ] **Step 2: Edit login `create_access_token` call (line ~357)**

Find:

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

Replace with (drop the `tier=` line):

```python
    access_token, jti = create_access_token(
        user_id=user_row["id"],
        email=user_row["email"],
        session_id=session_id,
        is_admin=is_admin,
        role=user_role,
    )
```

- [ ] **Step 3: Edit login `LoginResponse` (line ~389–405)**

Find:

```python
        user=UserInfo(
            id=user_row["id"],
            email=user_row["email"],
            user_name=user_row["user_name"],
            tier=user_tier,
            role=user_role,
            status=user_row["status"],
```

Replace with (drop the `tier=` line):

```python
        user=UserInfo(
            id=user_row["id"],
            email=user_row["email"],
            user_name=user_row["user_name"],
            role=user_role,
            status=user_row["status"],
```

- [ ] **Step 4: Edit refresh — drop tier fetch and call (line ~519–548)**

Find:

```python
    # Fetch API key tier
    async with db_logger.pool.acquire() as conn:
        key_row = await conn.fetchrow(
            "SELECT tier FROM api_keys WHERE (account_id = $1 OR user_id = $1) AND status = 'active' LIMIT 1",
            user_row["id"],
        )
    user_tier = (key_row["tier"] if key_row else None) or "free"

    # Create new access token
    user_role = user_row["role"] or "free"
```

Replace with:

```python
    # Create new access token
    user_role = user_row["role"] or "free"
```

Then find the `create_access_token(...)` call below it (around line ~541):

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

Replace with:

```python
    access_token, jti = create_access_token(
        user_id=user_row["id"],
        email=user_row["email"],
        session_id=session_row["sid"],
        is_admin=is_admin,
        role=user_role,
    )
```

- [ ] **Step 5: Check Google OAuth flow if present**

```bash
grep -n 'tier=user_tier\|user_tier\|key_row\["tier"\]\|SELECT tier FROM api_keys' serving/servers/routers/auth_routes.py
```

Expected: zero matches. If any remain (Google OAuth path around line ~544), apply the same removals: drop the `SELECT tier FROM api_keys` query, drop `user_tier` assignment, drop `tier=user_tier` from `create_access_token(...)` and from the `UserInfo(...)` constructor.

- [ ] **Step 6: Verify**

```bash
grep -n 'tier' serving/servers/routers/auth_routes.py
```

Expected: zero matches.

- [ ] **Step 7: Commit**

```bash
git add serving/servers/routers/auth_routes.py
git commit -m "refactor(auth_routes): drop tier from login/google/refresh"
```

---

## Task 5: Backend user routes — drop tier from /me and profile

**Files:**
- Modify: `serving/servers/routers/user_routes.py`

- [ ] **Step 1: Edit `/me` response (line ~152)**

Find:

```python
    return UserInfo(
        id=user_row["id"],
        email=user_row["email"],
        user_name=user_row["user_name"],
        tier=current_user.get("tier", "free"),
        role=user_row["role"] or "free",
        status=user_row["status"],
```

Replace with:

```python
    return UserInfo(
        id=user_row["id"],
        email=user_row["email"],
        user_name=user_row["user_name"],
        role=user_row["role"] or "free",
        status=user_row["status"],
```

- [ ] **Step 2: Edit profile-update response (line ~771)**

Find:

```python
    return UserInfo(
        id=user_row["id"],
        email=user_row["email"],
        user_name=user_row["user_name"],
        tier=current_user.get("tier", "free"),
        role=user_row["role"] or "free",
        status=user_row["status"],
```

Replace with:

```python
    return UserInfo(
        id=user_row["id"],
        email=user_row["email"],
        user_name=user_row["user_name"],
        role=user_row["role"] or "free",
        status=user_row["status"],
```

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

## Task 6: Backend admin routes — drop tier from API key CRUD and user views

**Files:**
- Modify: `serving/servers/routers/admin.py`

- [ ] **Step 1: Edit `create_api_key` INSERT (line ~228)**

Find:

```python
        row = await conn.fetchrow(
            """
            INSERT INTO api_keys (
                key_hash, key_prefix, user_id, user_name, tier,
                quota_daily_cost_usd, quota_monthly_cost_usd,
                expires_at, notes, metadata
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
            RETURNING id, created_at
            """,
            key_hash,
            key_prefix,
            payload.user_id,
            payload.user_name,
            payload.tier,
            payload.quota_daily_cost_usd,
            payload.quota_monthly_cost_usd,
            payload.expires_at,
            payload.notes,
            payload.metadata,
        )
```

Replace with:

```python
        row = await conn.fetchrow(
            """
            INSERT INTO api_keys (
                key_hash, key_prefix, user_id, user_name,
                quota_daily_cost_usd, quota_monthly_cost_usd,
                expires_at, notes, metadata
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            RETURNING id, created_at
            """,
            key_hash,
            key_prefix,
            payload.user_id,
            payload.user_name,
            payload.quota_daily_cost_usd,
            payload.quota_monthly_cost_usd,
            payload.expires_at,
            payload.notes,
            payload.metadata,
        )
```

- [ ] **Step 2: Edit `create_api_key` audit + response (line ~250–271)**

Find:

```python
    await log_admin_action(
        db_logger,
        admin_ip,
        "create_key",
        payload.user_id,
        {
            "tier": payload.tier,
            "quota_daily_usd": float(payload.quota_daily_cost_usd),
            "key_prefix": key_prefix,
        },
    )

    return CreateAPIKeyResponse(
        api_key=plaintext_key,
        user_id=payload.user_id,
        key_prefix=key_prefix,
        tier=payload.tier,
        quota_daily_cost_usd=payload.quota_daily_cost_usd,
        quota_monthly_cost_usd=payload.quota_monthly_cost_usd,
        expires_at=payload.expires_at,
        created_at=row["created_at"],
    )
```

Replace with:

```python
    await log_admin_action(
        db_logger,
        admin_ip,
        "create_key",
        payload.user_id,
        {
            "quota_daily_usd": float(payload.quota_daily_cost_usd),
            "key_prefix": key_prefix,
        },
    )

    return CreateAPIKeyResponse(
        api_key=plaintext_key,
        user_id=payload.user_id,
        key_prefix=key_prefix,
        quota_daily_cost_usd=payload.quota_daily_cost_usd,
        quota_monthly_cost_usd=payload.quota_monthly_cost_usd,
        expires_at=payload.expires_at,
        created_at=row["created_at"],
    )
```

- [ ] **Step 3: Edit `list_api_keys` — drop tier query param and filter (line ~274–308)**

Find:

```python
@router.get("/admin/api-keys", response_model=ListAPIKeysResponse)
async def list_api_keys(
    request: Request,
    status: str | None = None,
    tier: str | None = None,
    limit: int = 100,
    offset: int = 0,
    admin_ip: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> ListAPIKeysResponse:
    """List all API keys with optional filtering.

    Query Parameters:
    - status: Filter by status (active|suspended|revoked)
    - tier: Filter by tier (free|pro|enterprise)
    - limit: Max results (default: 100)
    - offset: Pagination offset

    Requires: Authorization: Bearer {ADMIN_TOKEN}
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    # Build query with filters
    where_clauses = []
    params: list[Any] = []

    if status:
        where_clauses.append(f"status = ${len(params) + 1}")
        params.append(status)

    if tier:
        where_clauses.append(f"tier = ${len(params) + 1}")
        params.append(tier)
```

Replace with:

```python
@router.get("/admin/api-keys", response_model=ListAPIKeysResponse)
async def list_api_keys(
    request: Request,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    admin_ip: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> ListAPIKeysResponse:
    """List all API keys with optional filtering.

    Query Parameters:
    - status: Filter by status (active|suspended|revoked)
    - limit: Max results (default: 100)
    - offset: Pagination offset

    Requires: Authorization: Bearer {ADMIN_TOKEN}
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    # Build query with filters
    where_clauses = []
    params: list[Any] = []

    if status:
        where_clauses.append(f"status = ${len(params) + 1}")
        params.append(status)
```

- [ ] **Step 4: Edit `list_api_keys` SELECT and row builder (line ~324–393)**

Find:

```python
        rows = await conn.fetch(
            f"""
            SELECT
                user_id, user_name, key_prefix, tier, status,
                quota_daily_cost_usd, quota_monthly_cost_usd,
                created_at, last_used_at, expires_at, notes
            FROM api_keys
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ${len(params) - 1} OFFSET ${len(params)}
            """,
            *params,
        )
```

Replace with:

```python
        rows = await conn.fetch(
            f"""
            SELECT
                user_id, user_name, key_prefix, status,
                quota_daily_cost_usd, quota_monthly_cost_usd,
                created_at, last_used_at, expires_at, notes
            FROM api_keys
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ${len(params) - 1} OFFSET ${len(params)}
            """,
            *params,
        )
```

Then find the `APIKeyListItem(...)` constructor below and remove the `tier=row["tier"],` line:

```python
            keys.append(
                APIKeyListItem(
                    user_id=user_id,
                    user_name=row["user_name"],
                    key_prefix=row["key_prefix"],
                    tier=row["tier"],
                    status=row["status"],
```

Replace with:

```python
            keys.append(
                APIKeyListItem(
                    user_id=user_id,
                    user_name=row["user_name"],
                    key_prefix=row["key_prefix"],
                    status=row["status"],
```

- [ ] **Step 5: Edit `get_api_key_detail` SELECT and response (line ~415–513)**

Find:

```python
        row = await conn.fetchrow(
            """
            SELECT
                user_id, user_name, key_prefix, tier, status,
                quota_daily_cost_usd, quota_monthly_cost_usd,
                created_at, last_used_at, expires_at, notes, metadata
            FROM api_keys
            WHERE user_id = $1
            """,
            user_id,
        )
```

Replace with:

```python
        row = await conn.fetchrow(
            """
            SELECT
                user_id, user_name, key_prefix, status,
                quota_daily_cost_usd, quota_monthly_cost_usd,
                created_at, last_used_at, expires_at, notes, metadata
            FROM api_keys
            WHERE user_id = $1
            """,
            user_id,
        )
```

Then in the `APIKeyDetailResponse(...)` builder, remove `tier=row["tier"],`:

```python
    return APIKeyDetailResponse(
        user_id=row["user_id"],
        user_name=row["user_name"],
        key_prefix=row["key_prefix"],
        tier=row["tier"],
        status=row["status"],
```

Replace with:

```python
    return APIKeyDetailResponse(
        user_id=row["user_id"],
        user_name=row["user_name"],
        key_prefix=row["key_prefix"],
        status=row["status"],
```

- [ ] **Step 6: Edit `list_users` SELECT — drop key_tier columns (line ~813 and ~832)**

Find (simple-path SELECT):

```python
                SELECT u.id, u.email, u.user_name, u.role, u.status, u.email_verified,
                       u.approval_note, u.reviewed_at, u.reviewed_by,
                       u.created_at, u.last_login_at,
                       k.key_prefix, k.status AS key_status, k.tier AS key_tier
                FROM users u
                LEFT JOIN api_keys k ON k.account_id = u.id AND k.status = 'active'
```

Replace with:

```python
                SELECT u.id, u.email, u.user_name, u.role, u.status, u.email_verified,
                       u.approval_note, u.reviewed_at, u.reviewed_by,
                       u.created_at, u.last_login_at,
                       k.key_prefix, k.status AS key_status
                FROM users u
                LEFT JOIN api_keys k ON k.account_id = u.id AND k.status = 'active'
```

Then find the CTE-path SELECT (line ~829):

```python
                f"""filtered_users AS (
                    SELECT u.id, u.email, u.user_name, u.role, u.status, u.email_verified,
                           u.approval_note, u.reviewed_at, u.reviewed_by,
                           u.created_at, u.last_login_at,
                           k.key_prefix, k.status AS key_status, k.tier AS key_tier
                    FROM users u
                    LEFT JOIN api_keys k ON k.account_id = u.id AND k.status = 'active'
                    {where_sql}
                )"""
```

Replace with:

```python
                f"""filtered_users AS (
                    SELECT u.id, u.email, u.user_name, u.role, u.status, u.email_verified,
                           u.approval_note, u.reviewed_at, u.reviewed_by,
                           u.created_at, u.last_login_at,
                           k.key_prefix, k.status AS key_status
                    FROM users u
                    LEFT JOIN api_keys k ON k.account_id = u.id AND k.status = 'active'
                    {where_sql}
                )"""
```

- [ ] **Step 7: Edit `list_users` `UserListItem(...)` builder (line ~941)**

Find:

```python
        users.append(
            UserListItem(
                id=row["id"],
                email=row["email"],
                user_name=row["user_name"],
                role=row["role"] or "free",
                status=row["status"],
                email_verified=row["email_verified"],
                approval_note=row["approval_note"],
                reviewed_at=row["reviewed_at"],
                reviewed_by=row["reviewed_by"],
                created_at=row["created_at"],
                last_login_at=row["last_login_at"],
                has_key=row["key_prefix"] is not None,
                key_prefix=row["key_prefix"],
                key_status=row["key_status"],
                key_tier=row["key_tier"],
                usage_today_usd=today,
                usage_month_usd=month,
                usage_alltime_usd=alltime,
            )
        )
```

Replace with (drop the `key_tier=` line):

```python
        users.append(
            UserListItem(
                id=row["id"],
                email=row["email"],
                user_name=row["user_name"],
                role=row["role"] or "free",
                status=row["status"],
                email_verified=row["email_verified"],
                approval_note=row["approval_note"],
                reviewed_at=row["reviewed_at"],
                reviewed_by=row["reviewed_by"],
                created_at=row["created_at"],
                last_login_at=row["last_login_at"],
                has_key=row["key_prefix"] is not None,
                key_prefix=row["key_prefix"],
                key_status=row["key_status"],
                usage_today_usd=today,
                usage_month_usd=month,
                usage_alltime_usd=alltime,
            )
        )
```

- [ ] **Step 8: Edit `get_user_detail` SELECT and response (line ~1113–1203)**

Find:

```python
        user_row = await conn.fetchrow(
            """
            SELECT u.id, u.email, u.user_name, u.role, u.status, u.email_verified,
                   u.created_at, u.last_login_at,
                   k.key_prefix, k.tier, k.quota_daily_cost_usd, k.quota_monthly_cost_usd
            FROM users u
            LEFT JOIN api_keys k ON k.account_id = u.id AND k.status = 'active'
            WHERE u.id = $1
            """,
            user_id,
        )
```

Replace with:

```python
        user_row = await conn.fetchrow(
            """
            SELECT u.id, u.email, u.user_name, u.role, u.status, u.email_verified,
                   u.created_at, u.last_login_at,
                   k.key_prefix, k.quota_daily_cost_usd, k.quota_monthly_cost_usd
            FROM users u
            LEFT JOIN api_keys k ON k.account_id = u.id AND k.status = 'active'
            WHERE u.id = $1
            """,
            user_id,
        )
```

Then find the `UserDetailResponse(...)` builder:

```python
    return UserDetailResponse(
        id=user_row["id"],
        email=user_row["email"],
        user_name=user_row["user_name"],
        role=user_row["role"] or "free",
        status=user_row["status"],
        email_verified=user_row["email_verified"],
        created_at=user_row["created_at"],
        last_login_at=user_row["last_login_at"],
        has_key=has_key,
        key_prefix=user_row["key_prefix"],
        key_tier=user_row["tier"],
        quota_daily_usd=float(user_row["quota_daily_cost_usd"])
```

Replace with (drop the `key_tier=` line):

```python
    return UserDetailResponse(
        id=user_row["id"],
        email=user_row["email"],
        user_name=user_row["user_name"],
        role=user_row["role"] or "free",
        status=user_row["status"],
        email_verified=user_row["email_verified"],
        created_at=user_row["created_at"],
        last_login_at=user_row["last_login_at"],
        has_key=has_key,
        key_prefix=user_row["key_prefix"],
        quota_daily_usd=float(user_row["quota_daily_cost_usd"])
```

- [ ] **Step 9: Edit `update_user` — drop tier from key_fields (line ~1284)**

Find:

```python
        # Update key-level fields
        key_fields = {
            k: v
            for k, v in payload_dict.items()
            if k in ("tier", "quota_daily_cost_usd", "quota_monthly_cost_usd")
        }
```

Replace with:

```python
        # Update key-level fields
        key_fields = {
            k: v
            for k, v in payload_dict.items()
            if k in ("quota_daily_cost_usd", "quota_monthly_cost_usd")
        }
```

Then find the `update_user` docstring (line ~1214):

```python
    """Update user account status or API key settings (tier, quota).
```

Replace with:

```python
    """Update user account status or API key settings (quota).
```

- [ ] **Step 10: Verify**

```bash
grep -n 'tier' serving/servers/routers/admin.py
```

Expected: zero matches.

- [ ] **Step 11: Commit**

```bash
git add serving/servers/routers/admin.py
git commit -m "refactor(admin): drop tier from api-key crud and user views"
```

---

## Task 7: Backend settings — drop unused signup_default_tier

**Files:**
- Modify: `serving/config/settings.py`

- [ ] **Step 1: Edit `serving/config/settings.py` (line ~46)**

Find:

```python
    # Signup
    signup_enabled: bool = True
    signup_default_tier: str = "free"
    signup_default_daily_quota_usd: float = 100.00
    signup_require_email_verification: bool = True
```

Replace with:

```python
    # Signup
    signup_enabled: bool = True
    signup_default_daily_quota_usd: float = 100.00
    signup_require_email_verification: bool = True
```

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

## Task 8: Backend DB migration — drop api_keys.tier column

**Files:**
- Modify: `serving/storage/database.py`

- [ ] **Step 1: Edit `serving/storage/database.py` — initial CREATE (line ~365)**

Find:

```python
            # API Keys table for user authentication
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    id BIGSERIAL PRIMARY KEY,
                    key_hash TEXT NOT NULL UNIQUE,
                    api_key_encrypted TEXT,
                    key_prefix TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    user_name TEXT,
                    status TEXT NOT NULL DEFAULT 'active',

                    quota_daily_cost_usd DECIMAL(10, 4) DEFAULT 1000.00,
                    quota_monthly_cost_usd DECIMAL(10, 4),

                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    expires_at TIMESTAMPTZ,
                    last_used_at TIMESTAMPTZ,

                    tier TEXT DEFAULT 'free',
                    notes TEXT,
                    metadata JSONB
                )
            """)
```

Replace with (drop the `tier TEXT DEFAULT 'free',` line):

```python
            # API Keys table for user authentication
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    id BIGSERIAL PRIMARY KEY,
                    key_hash TEXT NOT NULL UNIQUE,
                    api_key_encrypted TEXT,
                    key_prefix TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    user_name TEXT,
                    status TEXT NOT NULL DEFAULT 'active',

                    quota_daily_cost_usd DECIMAL(10, 4) DEFAULT 1000.00,
                    quota_monthly_cost_usd DECIMAL(10, 4),

                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    expires_at TIMESTAMPTZ,
                    last_used_at TIMESTAMPTZ,

                    notes TEXT,
                    metadata JSONB
                )
            """)
```

- [ ] **Step 2: Add migration to drop the column on existing DBs**

Locate the api_keys migrations block (between line ~393 and ~425, near the other `ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS ...` calls). After the existing migrations and before the `CREATE TABLE IF NOT EXISTS users` block at line ~428, add this new migration:

```python
            # Drop legacy 'tier' column — tier/role are now unified on users.role
            # (see docs/superpowers/specs/2026-05-02-unify-tier-role-design.md).
            # IF EXISTS makes this idempotent on fresh DBs where the column was
            # never created.
            await conn.execute("""
                ALTER TABLE api_keys DROP COLUMN IF EXISTS tier
            """)
```

The exact insertion point is immediately before the `# Users table for self-service registration` comment at line ~427. The block above it ends with the `idx_api_keys_account_active_unique` CREATE INDEX (line ~425).

- [ ] **Step 3: Verify**

```bash
grep -n 'tier' serving/storage/database.py
```

Expected: only the line `ALTER TABLE api_keys DROP COLUMN IF EXISTS tier` appears.

- [ ] **Step 4: Commit**

```bash
git add serving/storage/database.py
git commit -m "refactor(db): drop api_keys.tier column"
```

---

## Task 9: Frontend types — drop tier from auth/admin types

**Files:**
- Modify: `frontend/src/lib/api/admin.ts`
- Modify: `frontend/src/lib/api/auth.ts`

- [ ] **Step 1: Edit `frontend/src/lib/api/admin.ts` — `AdminUser`**

Find at line ~10:

```typescript
export interface AdminUser {
  id: string;
  email: string;
  user_name: string | null;
  role: string;
  status: string;
  email_verified: boolean;
  approval_note: string | null;
  reviewed_at: string | null;
  reviewed_by: string | null;
  created_at: string;
  last_login_at: string | null;
  has_key: boolean;
  key_prefix: string | null;
  key_status: string | null;
  key_tier: string | null;
  usage_today_usd: number;
  usage_month_usd: number;
  usage_alltime_usd: number;
}
```

Replace with (drop `key_tier`):

```typescript
export interface AdminUser {
  id: string;
  email: string;
  user_name: string | null;
  role: string;
  status: string;
  email_verified: boolean;
  approval_note: string | null;
  reviewed_at: string | null;
  reviewed_by: string | null;
  created_at: string;
  last_login_at: string | null;
  has_key: boolean;
  key_prefix: string | null;
  key_status: string | null;
  usage_today_usd: number;
  usage_month_usd: number;
  usage_alltime_usd: number;
}
```

- [ ] **Step 2: Edit `AdminApiKey` (line ~94)**

Find:

```typescript
export interface AdminApiKey {
  user_id: string;
  user_name: string | null;
  key_prefix: string;
  tier: string;
  status: string;
  quota_daily_cost_usd: number;
  quota_monthly_cost_usd: number | null;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  usage_today_usd: number;
  usage_month_usd: number;
  notes: string | null;
}
```

Replace with (drop `tier`):

```typescript
export interface AdminApiKey {
  user_id: string;
  user_name: string | null;
  key_prefix: string;
  status: string;
  quota_daily_cost_usd: number;
  quota_monthly_cost_usd: number | null;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  usage_today_usd: number;
  usage_month_usd: number;
  notes: string | null;
}
```

- [ ] **Step 3: Edit `CreateApiKeyRequest` (line ~115)**

Find:

```typescript
export interface CreateApiKeyRequest {
  user_id: string;
  user_name?: string;
  tier?: string;
  quota_daily_cost_usd?: number;
  quota_monthly_cost_usd?: number | null;
  expires_at?: string | null;
  notes?: string | null;
}
```

Replace with:

```typescript
export interface CreateApiKeyRequest {
  user_id: string;
  user_name?: string;
  quota_daily_cost_usd?: number;
  quota_monthly_cost_usd?: number | null;
  expires_at?: string | null;
  notes?: string | null;
}
```

- [ ] **Step 4: Edit `CreateApiKeyResponse` (line ~125)**

Find:

```typescript
export interface CreateApiKeyResponse {
  api_key: string;
  user_id: string;
  key_prefix: string;
  tier: string;
  quota_daily_cost_usd: number;
  quota_monthly_cost_usd: number | null;
  expires_at: string | null;
  created_at: string;
  warning: string;
}
```

Replace with:

```typescript
export interface CreateApiKeyResponse {
  api_key: string;
  user_id: string;
  key_prefix: string;
  quota_daily_cost_usd: number;
  quota_monthly_cost_usd: number | null;
  expires_at: string | null;
  created_at: string;
  warning: string;
}
```

- [ ] **Step 5: Edit `listApiKeys(...)` — drop tier param (line ~137)**

Find:

```typescript
export async function listApiKeys(
  status?: string,
  tier?: string,
  limit = 100,
  offset = 0,
): Promise<ListApiKeysResponse> {
  const params = new URLSearchParams();
  if (status) params.set('status', status);
  if (tier) params.set('tier', tier);
  params.set('limit', String(limit));
  params.set('offset', String(offset));
  const resp = await fetchWithAuth(API_BASE, `/admin/api-keys?${params.toString()}`);
  return jsonOrThrow<ListApiKeysResponse>(resp);
}
```

Replace with:

```typescript
export async function listApiKeys(
  status?: string,
  limit = 100,
  offset = 0,
): Promise<ListApiKeysResponse> {
  const params = new URLSearchParams();
  if (status) params.set('status', status);
  params.set('limit', String(limit));
  params.set('offset', String(offset));
  const resp = await fetchWithAuth(API_BASE, `/admin/api-keys?${params.toString()}`);
  return jsonOrThrow<ListApiKeysResponse>(resp);
}
```

- [ ] **Step 6: Edit `createApiKeyAdmin(...)` body — drop tier (line ~152)**

Find:

```typescript
    body: JSON.stringify({
      user_id: data.user_id,
      user_name: data.user_name || null,
      tier: data.tier || 'free',
      quota_daily_cost_usd: data.quota_daily_cost_usd ?? 1000,
      quota_monthly_cost_usd: data.quota_monthly_cost_usd ?? null,
      expires_at: data.expires_at || null,
      notes: data.notes || null,
      metadata: null,
    }),
```

Replace with:

```typescript
    body: JSON.stringify({
      user_id: data.user_id,
      user_name: data.user_name || null,
      quota_daily_cost_usd: data.quota_daily_cost_usd ?? 1000,
      quota_monthly_cost_usd: data.quota_monthly_cost_usd ?? null,
      expires_at: data.expires_at || null,
      notes: data.notes || null,
      metadata: null,
    }),
```

- [ ] **Step 7: Edit `UserDetail` (line ~192)**

Find:

```typescript
export interface UserDetail {
  id: string;
  email: string;
  user_name: string | null;
  role: string;
  status: string;
  email_verified: boolean;
  created_at: string;
  last_login_at: string | null;
  has_key: boolean;
  key_prefix: string | null;
  key_tier: string | null;
  quota_daily_usd: number | null;
  quota_monthly_usd: number | null;
```

Replace with (drop `key_tier`):

```typescript
export interface UserDetail {
  id: string;
  email: string;
  user_name: string | null;
  role: string;
  status: string;
  email_verified: boolean;
  created_at: string;
  last_login_at: string | null;
  has_key: boolean;
  key_prefix: string | null;
  quota_daily_usd: number | null;
  quota_monthly_usd: number | null;
```

- [ ] **Step 8: Edit `UpdateUserData` (line ~219)**

Find:

```typescript
export interface UpdateUserData {
  role?: string;
  tier?: string;
  status?: string;
  quota_daily_cost_usd?: number;
  quota_monthly_cost_usd?: number;
}
```

Replace with:

```typescript
export interface UpdateUserData {
  role?: string;
  status?: string;
  quota_daily_cost_usd?: number;
  quota_monthly_cost_usd?: number;
}
```

- [ ] **Step 9: Edit `frontend/src/lib/api/auth.ts` — `LoginResponse`**

```bash
grep -n 'tier' frontend/src/lib/api/auth.ts
```

If the field exists, find it (typically a `tier?: string` or `tier: string` line in `LoginResponse` or a related interface) and delete it. If `grep` returns nothing, this step is a no-op.

- [ ] **Step 10: Verify**

```bash
grep -n 'tier' frontend/src/lib/api/admin.ts frontend/src/lib/api/auth.ts
```

Expected: zero matches.

- [ ] **Step 11: Commit**

```bash
git add frontend/src/lib/api/admin.ts frontend/src/lib/api/auth.ts
git commit -m "refactor(frontend/api): drop tier from admin and auth types"
```

---

## Task 10: Frontend AuthProvider — drop tier; fix ROLE_RANK

**Files:**
- Modify: `frontend/src/components/providers/AuthProvider.tsx`

- [ ] **Step 1: Edit `User` interface (line ~7)**

Find:

```typescript
interface User {
  id: string;
  email: string;
  user_name?: string | null;
  tier: string;
  role: string;
  is_admin: boolean;
}
```

Replace with:

```typescript
interface User {
  id: string;
  email: string;
  user_name?: string | null;
  role: string;
  is_admin: boolean;
}
```

- [ ] **Step 2: Edit `ROLE_RANK` to match backend (line ~29)**

Find:

```typescript
const ROLE_RANK: Record<string, number> = {
  free: 0,
  internal: 1,
  admin: 2,
};
```

Replace with (match `serving/config/settings.py:172` exactly):

```typescript
const ROLE_RANK: Record<string, number> = {
  free: 0,
  pro: 1,
  internal: 2,
  admin: 3,
};
```

- [ ] **Step 3: Edit `refreshUser` setState (line ~52)**

Find:

```typescript
        user: {
          id: me.id,
          email: me.email,
          user_name: me.user_name,
          tier: me.tier,
          role: me.role || 'free',
          is_admin: me.is_admin,
        },
```

Replace with (drop `tier:` line):

```typescript
        user: {
          id: me.id,
          email: me.email,
          user_name: me.user_name,
          role: me.role || 'free',
          is_admin: me.is_admin,
        },
```

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

## Task 11: Frontend admin page — drop tier UI; remove enterprise; add pro role option

**Files:**
- Modify: `frontend/src/app/dashboard/admin/page.tsx`

- [ ] **Step 1: Drop `editTier` state (line ~377)**

Find:

```typescript
  const [editRole, setEditRole] = useState('');
  const [editTier, setEditTier] = useState('');
  const [editQuota, setEditQuota] = useState('');
```

Replace with:

```typescript
  const [editRole, setEditRole] = useState('');
  const [editQuota, setEditQuota] = useState('');
```

- [ ] **Step 2: Drop `setEditTier` from `toggleDetail` (line ~593)**

Find:

```typescript
      setEditRole(d.role || 'free');
      setEditTier(d.key_tier || 'free');
      setEditQuota(d.quota_daily_usd?.toString() || '100');
```

Replace with:

```typescript
      setEditRole(d.role || 'free');
      setEditQuota(d.quota_daily_usd?.toString() || '100');
```

- [ ] **Step 3: Drop tier diff from `doSave` (line ~676)**

Find:

```typescript
      if (editRole !== (detail.role || 'free')) u.role = editRole;
      if (editTier !== (detail.key_tier || 'free')) u.tier = editTier;
      if (editQuota !== (detail.quota_daily_usd?.toString() || '100'))
        u.quota_daily_cost_usd = Number(editQuota);
```

Replace with:

```typescript
      if (editRole !== (detail.role || 'free')) u.role = editRole;
      if (editQuota !== (detail.quota_daily_usd?.toString() || '100'))
        u.quota_daily_cost_usd = Number(editQuota);
```

- [ ] **Step 4: Drop `key_tier` badge in row list (line ~1004)**

Find:

```typescript
                              {u.has_key && u.key_tier && u.key_tier !== 'free' && (
                                <span className="font-semibold uppercase text-[10px] text-blue-600">
                                  {u.key_tier}
                                </span>
                              )}
```

Delete those four lines entirely.

- [ ] **Step 5: Add `pro` to role dropdown (line ~1179)**

Find:

```typescript
                                      <select
                                        value={editRole}
                                        onChange={(e) => setEditRole(e.target.value)}
                                        className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-[13px]"
                                      >
                                        <option value="free">free</option>
                                        <option value="internal">internal</option>
                                        <option value="admin">admin</option>
                                      </select>
```

Replace with:

```typescript
                                      <select
                                        value={editRole}
                                        onChange={(e) => setEditRole(e.target.value)}
                                        className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-[13px]"
                                      >
                                        <option value="free">free</option>
                                        <option value="pro">pro</option>
                                        <option value="internal">internal</option>
                                        <option value="admin">admin</option>
                                      </select>
```

- [ ] **Step 6: Drop the entire Tier dropdown block (line ~1184–1199)**

Find the JSX block:

```typescript
                                    {detail.has_key && (
                                      <>
                                        <div>
                                          <div className="text-[11px] font-medium text-gray-500 mb-1">
                                            Tier
                                          </div>
                                          <select
                                            value={editTier}
                                            onChange={(e) => setEditTier(e.target.value)}
                                            className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-[13px]"
                                          >
                                            <option value="free">free</option>
                                            <option value="pro">pro</option>
                                            <option value="enterprise">enterprise</option>
                                          </select>
                                        </div>
                                        <div>
                                          <div className="text-[11px] font-medium text-gray-500 mb-1">
                                            Daily quota
                                          </div>
```

The `Tier` `<div>...</div>` block (Tier label + select wrapping `<div>`) needs to go, but the `Daily quota` block must stay inside the `{detail.has_key && (<>...</>)}` fragment. Replace the entire matched region with:

```typescript
                                    {detail.has_key && (
                                      <>
                                        <div>
                                          <div className="text-[11px] font-medium text-gray-500 mb-1">
                                            Daily quota
                                          </div>
```

(That is: keep the `{detail.has_key && (<>` opener, drop the Tier `<div>...</div>`, leave the Daily quota `<div>` and everything below it untouched.)

- [ ] **Step 7: Verify**

```bash
grep -n 'tier\|enterprise\|editTier\|key_tier' frontend/src/app/dashboard/admin/page.tsx
```

Expected: zero matches.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/app/dashboard/admin/page.tsx
git commit -m "refactor(admin/page): drop tier UI; remove enterprise; add pro role option"
```

---

## Task 12: Tests — update fixtures and assertions

**Files:**
- Modify: `test/servers/test_admin_api.py`
- Modify: `test/integration/test_database_integration.py`

- [ ] **Step 1: Edit `test/servers/test_admin_api.py`**

Run grep to enumerate every `tier` in the file:

```bash
grep -n 'tier' test/servers/test_admin_api.py
```

For each match, apply this rule:

- In a request body JSON like `{"user_id": "alice", "tier": "pro", ...}` — drop the `"tier": "..."` key.
- In a `connection.fetch.side_effect` row dict like `{..., "tier": "pro", ...}` — drop the `"tier": "..."` key.
- In an assertion `assert body["updated_fields"] == ["tier", "quota_daily_cost_usd"]` — drop `"tier"` from the list (becomes `["quota_daily_cost_usd"]`).
- In the test `test_update_api_key_not_found` body `{"tier": "pro"}` — replace with `{"quota_daily_cost_usd": 100}` so the request still has a valid field that triggers the 404 path.

Concretely:

  - Line ~93: `json={"user_id": "alice", "tier": "pro", "quota_daily_cost_usd": 500},` → `json={"user_id": "alice", "quota_daily_cost_usd": 500},`
  - Line ~143: drop the `"tier": "pro",` line.
  - Line ~183: drop the `"tier": "pro",` line.
  - Line ~221: `json={"tier": "enterprise", "quota_daily_cost_usd": 200},` → `json={"quota_daily_cost_usd": 200},`
  - Line ~226: `assert body["updated_fields"] == ["tier", "quota_daily_cost_usd"]` → `assert body["updated_fields"] == ["quota_daily_cost_usd"]`
  - Line ~250: `json={"tier": "pro"},` → `json={"quota_daily_cost_usd": 100},`

- [ ] **Step 2: Edit `test/integration/test_database_integration.py` (line ~129–142)**

Find:

```python
        await conn.execute(
            """
            INSERT INTO api_keys (
                key_hash, key_prefix, user_id, user_name, quota_daily_cost_usd, tier
            ) VALUES ($1, $2, $3, $4, $5, $6)
            """,
            key_hash,
            key_prefix,
            user_id,
            "Integration User",
            Decimal("1000.00"),
            "enterprise",
        )
```

Replace with:

```python
        await conn.execute(
            """
            INSERT INTO api_keys (
                key_hash, key_prefix, user_id, user_name, quota_daily_cost_usd
            ) VALUES ($1, $2, $3, $4, $5)
            """,
            key_hash,
            key_prefix,
            user_id,
            "Integration User",
            Decimal("1000.00"),
        )
```

- [ ] **Step 3: Verify**

```bash
grep -rn 'enterprise\|"tier"' test/ 2>/dev/null
grep -rn 'tier' test/servers/test_admin_api.py test/integration/test_database_integration.py
```

Expected: zero matches except possibly unrelated occurrences in other test files (handle if any surface).

- [ ] **Step 4: Sweep any other test files for stale tier refs**

```bash
grep -rn 'tier\|enterprise' test/ 2>/dev/null | grep -v __pycache__ | grep -v 'codex\|claude_token'
```

For each match: if the test asserts on or supplies a `tier` field for an api-key/user payload, remove it. If the file is unrelated (e.g., codex/claude subscription tier), leave it. Document any such file in the commit message.

- [ ] **Step 5: Run the affected tests**

```bash
uv run pytest test/servers/test_admin_api.py test/integration/test_database_integration.py -x
```

Expected: all pass. If any fail, read the error and fix in the same task.

- [ ] **Step 6: Commit**

```bash
git add test/servers/test_admin_api.py test/integration/test_database_integration.py
git commit -m "test: drop tier from admin and integration test fixtures"
```

---

## Task 13: Full sweep — confirm enterprise gone, run lint + tests

**Files:**
- None modified directly; this task verifies the refactor.

- [ ] **Step 1: Confirm `enterprise` gone (except unrelated upstream fields)**

```bash
grep -rn 'enterprise' serving/ frontend/src/ test/ 2>/dev/null | grep -v __pycache__ | grep -v node_modules
```

Expected: only the `serving/adapters/claude_token.py` comment remains:

```
serving/adapters/claude_token.py:82:    plan: str = "pro"  # pro / max / team / enterprise
```

That is fine — it documents the upstream Claude API's `plan` field and is unrelated. Any other matches must be cleaned up before continuing.

- [ ] **Step 2: Confirm `signup_default_tier` gone**

```bash
grep -rn 'signup_default_tier' serving/ frontend/src/ test/ 2>/dev/null | grep -v __pycache__
```

Expected: zero matches.

- [ ] **Step 3: Confirm `key_tier`/`api_keys.*tier`/`u.tier`/`tier=` gone in app code**

```bash
grep -rn 'key_tier\|k\.tier\|api_keys.*tier\|"tier"\|tier=' serving/ frontend/src/ 2>/dev/null \
  | grep -v __pycache__ | grep -v node_modules \
  | grep -v 'codex_token\|claude_token\|selected_tier'
```

Expected: zero matches. Anything left should be reviewed and either justified (subscription/router internals) or fixed.

- [ ] **Step 4: Run ruff (project lint check per CLAUDE.md)**

```bash
uv run ruff format --check .
```

Expected: pass. If it reports formatting issues introduced by edits, run `uv run ruff format .` and commit the formatting fix as a separate commit.

- [ ] **Step 5: Run ruff lint**

```bash
uv run ruff check .
```

Expected: pass. Fix any new lint errors caused by removed imports (e.g., if `Field` is no longer used in a file, remove the unused import).

- [ ] **Step 6: Run full backend test suite**

```bash
uv run pytest -x
```

Expected: all pass. Investigate and fix any failure caused by the refactor (likely missing `tier` references in tests not covered above).

- [ ] **Step 7: Frontend type check + build**

```bash
cd frontend && npm run typecheck && npm run lint && cd ..
```

Expected: pass. Fix any TypeScript errors (likely references to removed fields).

- [ ] **Step 8: Commit any formatting-only or follow-up fixes**

```bash
git status
# if any changes:
git add -A
git commit -m "chore: lint/format follow-up after tier removal"
```

---

## Task 14: Push branch, open PR, run review

**Files:**
- None modified.

- [ ] **Step 1: Push branch**

```bash
git push -u origin jason/claude/unify-tier-role
```

- [ ] **Step 2: Open PR to dev**

```bash
gh pr create --base dev --title "refactor: unify user tier and role; remove enterprise" --body "$(cat <<'EOF'
## Summary
- Drop `api_keys.tier` column and field; `users.role` is now the single source of truth for billing/access classification.
- Remove the unused `enterprise` value from schemas, UI, and tests.
- Fix frontend `ROLE_RANK` to match backend (adds missing `pro` rank); surface `pro` in the admin role dropdown.

Spec: [docs/superpowers/specs/2026-05-02-unify-tier-role-design.md](docs/superpowers/specs/2026-05-02-unify-tier-role-design.md)

## Test plan
- [ ] `uv run ruff format --check .` passes
- [ ] `uv run pytest` passes
- [ ] `cd frontend && npm run typecheck && npm run lint` passes
- [ ] Login + /me returns no `tier`; role still surfaces correctly
- [ ] Admin user list/detail and API-key CRUD work end-to-end on staging
- [ ] DB migration drops `api_keys.tier` cleanly on staging

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Capture PR URL**

The `gh pr create` output prints the URL. Record it; report back to the user.

- [ ] **Step 4: Review the diff and address any issues**

```bash
gh pr view --web   # or read the diff locally
git diff origin/dev...HEAD
```

Look for:
- Any `tier` reference still in the diff outside the unrelated codex/claude/router files.
- Missing test updates.
- Schema/UI mismatches.

If issues found, fix and push to the same branch.
