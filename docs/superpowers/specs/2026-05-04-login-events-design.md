# Login Events Audit Table

**Date:** 2026-05-04
**Status:** Approved (design)
**Author:** Juncheng Yang (with Claude)
**Issue:** #428

## Context

Today's login endpoint — public path **`POST /auth/login`** — defined as `@router.post("/login")` on the `/auth`-prefixed router in
[`apps/backend/serving/servers/routers/auth_routes.py:255`](../../../apps/backend/serving/servers/routers/auth_routes.py#L255)
persists three things on success:

- `users.last_login_at` (a single column, **overwritten** each login).
- An `auth_sessions` row (refresh-token bookkeeping; only the active session,
  not every attempt).
- A stdout `logger.info("User logged in: ...")` line.

Failed attempts (rate-limit 429, wrong password, missing user, unverified
email, etc.) leave **only** stdout traces. There is no persistent audit
trail and no way to answer questions like "is this email being brute-forced
right now?" or "how often does user X fail their login?".

## Goals

- A persistent `login_events` table that records every `POST /auth/login` outcome —
  success and the seven failure paths — with enough fields to answer the
  audit questions above.
- Always-on logging: no runtime toggle (a security audit log with gaps is
  worse than no audit log).
- Insertion is **awaited** so a failed write surfaces loudly, but it must
  not break login if the audit DB write itself fails (catch-and-log).
- Two admin-gated purge endpoints:
  - by-age, for retention.
  - by-user, for GDPR-style deletion.
- The existing `hard_delete_user` flow sweeps the user's `login_events`
  rows automatically (parity with how it already cleans `auth_sessions` /
  `api_keys`).

## Non-Goals

- Logging `/logout`, refresh-token rotations, signup, or password resets.
  Out of scope here; can be added in a follow-up if desired.
- A cluster-wide admin UI for browsing the events. Existing admin tooling
  + ad-hoc SQL is sufficient for now.
- Long-term retention enforcement (auto-purge cron). The admin endpoint
  exists; running it on a schedule is an operational concern.
- A "purge all" endpoint. Too easy to nuke the audit trail by mistake. An
  admin can use `?older_than_days=1` if they truly want a near-clean wipe.

## Design

### 1. Storage

A new table in the operational store
([`apps/backend/serving/storage/postgres_operational.py`](../../../apps/backend/serving/storage/postgres_operational.py)),
created in `_create_tables`:

```sql
CREATE TABLE IF NOT EXISTS login_events (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    user_id TEXT,                                  -- NULL when email doesn't resolve
    email TEXT NOT NULL,
    outcome TEXT NOT NULL
        CHECK (outcome IN ('success', 'failure')),
    failure_reason TEXT,                           -- machine code, NULL on success
    ip TEXT,
    user_agent TEXT
);

CREATE INDEX IF NOT EXISTS idx_login_events_user
    ON login_events (user_id, created_at DESC) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_login_events_email
    ON login_events (email, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_login_events_created
    ON login_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_login_events_failures
    ON login_events (created_at DESC) WHERE outcome = 'failure';
```

Operational-store table choice (vs. the `LogStore` where `api_logs` lives):
login events are *operational* data — they sit alongside `users`,
`api_keys`, and `auth_sessions`. They will be queried by ops/admin queries,
not by inference dashboards.

`failure_reason` vocabulary (machine codes):

| Code | When |
|---|---|
| `rate_limited` | Login rate limit hit before user lookup |
| `user_not_found` | Email doesn't match any user row |
| `invalid_password` | User exists, password wrong |
| `email_unverified` | User exists, password ok, email unverified |
| `account_pending_approval` | User awaiting admin approval |
| `account_rejected` | User registration was rejected |
| `account_inactive` | Status is anything other than `active` |

### 2. Storage interface

In `serving/storage/base.py`, add to `OperationalStore`:

```python
@abstractmethod
async def record_login_event(
    self,
    *,
    email: str,
    outcome: str,                # 'success' | 'failure'
    failure_reason: str | None,  # one of the codes above; None when outcome='success'
    user_id: str | None,
    ip: str | None,
    user_agent: str | None,
) -> None:
    """Insert one row into login_events. Best-effort for the caller —
    callers may catch + log on exception so audit failures don't break login."""

@abstractmethod
async def purge_login_events_older_than(self, days: int) -> int:
    """Delete login_events rows older than ``days``. Returns deleted count."""

@abstractmethod
async def purge_login_events_for_user(self, user_id: str) -> int:
    """Delete all login_events rows for ``user_id``. Returns deleted count."""
```

Implementations:

- `PostgresOperationalStore` — straightforward SQL.
- `CachedOperationalStore` — pass-through (no caching needed; writes are
  rare relative to API hits, and the read path is admin-only).
- `DualWriteOperationalStore` — writes go to both stores via the existing
  `_write_to_both` helper (matching `delete_user_sessions` etc.).
- `D1OperationalStore` — same shape, using D1's SQL dialect (Cloudflare).
  D1 lacks `INTERVAL`, so the by-age purge passes a precomputed cutoff
  timestamp instead.

### 3. Insertion sites in `POST /auth/login`

Inside
[`apps/backend/serving/servers/routers/auth_routes.py:255`](../../../apps/backend/serving/servers/routers/auth_routes.py#L255),
each rejection branch and the success branch insert one row.

A small local helper inside the `login` function keeps the call sites
short:

```python
async def _record(outcome: str, *, failure_reason: str | None, user_id: str | None) -> None:
    try:
        await op_store.record_login_event(
            email=body.email,
            outcome=outcome,
            failure_reason=failure_reason,
            user_id=user_id,
            ip=client_ip,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception:
        logger.exception(
            "login_event_write_failed",
            extra={
                "event": "login_event_write_failed",
                "outcome": outcome,
                "failure_reason": failure_reason,
            },
        )
```

Eight call sites (in execution order):

| Branch | Call |
|---|---|
| Rate-limit 429 (currently L277-284) | `_record("failure", failure_reason="rate_limited", user_id=None)` |
| `user_row is None` (L289) | `_record("failure", failure_reason="user_not_found", user_id=None)` |
| Wrong password (L293) | `_record("failure", failure_reason="invalid_password", user_id=user_row["id"])` |
| Email unverified 403 (L305) | `_record("failure", failure_reason="email_unverified", user_id=user_row["id"])` |
| Pending approval 403 (L312) | `_record("failure", failure_reason="account_pending_approval", user_id=user_row["id"])` |
| Rejected 403 (L318) | `_record("failure", failure_reason="account_rejected", user_id=user_row["id"])` |
| Status not active 403 (L324) | `_record("failure", failure_reason="account_inactive", user_id=user_row["id"])` |
| Success path (just before `update_user_last_login` at L331) | `_record("success", failure_reason=None, user_id=user_row["id"])` |

The `_record` call is **awaited** (audit-grade). The exception swallowing
ensures a DB hiccup on the audit insert can't break the login response —
errors land in stdout for ops to investigate. The login itself runs only
after the audit insert returns (or its exception is caught), so a
slow-running insert adds ~1 ms of latency on the login critical path.
Acceptable for an endpoint that's already doing bcrypt verification.

### 4. Admin purge endpoints

New file
[`apps/backend/serving/servers/routers/admin/login_events.py`](../../../apps/backend/serving/servers/routers/admin/login_events.py),
mounted under the existing `/admin` router, modeled on the existing
`admin/settings.py` and `admin/users.py` patterns.

```python
@router.delete("/login-events")
async def purge_login_events_endpoint(
    request: Request,
    older_than_days: int | None = Query(None, ge=1),
    user_id: str | None = Query(None, min_length=1),
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> dict[str, int]:
    if not op_store:
        raise HTTPException(500, "Database not configured")
    # Exactly one of the two filters must be provided.
    if (older_than_days is None) == (user_id is None):
        raise HTTPException(
            400,
            "Provide exactly one of `older_than_days` or `user_id`",
        )

    if older_than_days is not None:
        deleted = await op_store.purge_login_events_older_than(older_than_days)
        details = {"older_than_days": older_than_days, "deleted": deleted}
    else:
        deleted = await op_store.purge_login_events_for_user(user_id)
        details = {"user_id": user_id, "deleted": deleted}

    ip = get_client_ip(request)
    await log_admin_action(op_store, ip, "login_events.purge", None, details)
    return {"deleted": deleted}
```

Both purge variants are surfaced through the same endpoint (one HTTP
DELETE, two valid query shapes). Each call is recorded in the
`admin_audit_log` table via `log_admin_action`, matching the runtime-
settings PATCH and the user-delete flows.

The new router is registered in
[`apps/backend/serving/servers/routers/admin/__init__.py`](../../../apps/backend/serving/servers/routers/admin/__init__.py)
the same way `admin/settings.py` is included today.

### 5. User-deletion sweep

Extend `PostgresOperationalStore.hard_delete_user`
([currently at L530](../../../apps/backend/serving/storage/postgres_operational.py#L530))
to delete the user's `login_events` rows in the same transaction:

```python
        login_events_status = await conn.execute(
            "DELETE FROM login_events WHERE user_id = $1", user_id
        )
        # ...
        counts = {
            "api_keys": _row_count(keys_status),
            "auth_sessions": _row_count(sessions_status),
            "login_events": _row_count(login_events_status),
            "email_verification_tokens": _row_count(verif_status),
            "password_reset_tokens": _row_count(reset_status),
            "user_daily_cost": _row_count(cost_status),
            "admin_audit_log": _row_count(audit_status),
            "users": _row_count(user_status),
        }
```

The `D1OperationalStore` equivalent gets the matching extra DELETE.

## Affected components

| Component | Change |
|---|---|
| `serving/storage/postgres_operational.py` | New table + indexes in `_create_tables`; new methods (`record_login_event`, two purge methods); `hard_delete_user` extended |
| `serving/storage/d1_operational.py` | Same shape (table, methods, hard-delete extension) using D1 SQL dialect |
| `serving/storage/base.py` | Three new abstract methods on `OperationalStore` |
| `serving/storage/cache.py` | Three new pass-through methods on `CachedOperationalStore` |
| `serving/storage/dual_write.py` | Three new dual-write methods (writes to both, reads from primary) |
| `serving/servers/routers/auth_routes.py` | `_record` helper inside `login`; calls inserted at all 8 outcome paths |
| `serving/servers/routers/admin/login_events.py` | **New** — `DELETE /admin/login-events` |
| `serving/servers/routers/admin/__init__.py` | Include the new router |
| `tests/unit/storage/test_login_events.py` | **New** — table CRUD + purge methods (Postgres-fixture path) |
| `tests/servers/test_auth_routes.py` | Extend with assertions that each outcome path writes exactly one row with the expected `failure_reason` |
| `tests/servers/test_admin_login_events.py` | **New** — admin endpoint: by-age, by-user, both/neither → 400, admin auth, audit log emitted |
| `tests/integration/test_hard_delete_user_sweeps_login_events.py` | **New** — integration test that `hard_delete_user` removes the user's login_events |

## Testing

Unit:

- **Table creation**: `_create_tables` is idempotent (call it twice; no
  errors).
- **Insert paths**: 8 unit tests, one per outcome — each inserts a row
  with the right `outcome`, `failure_reason`, `user_id`, and `email`.
- **Insert failure swallowed**: `_record` catches an `Exception` from
  the store and the login response still returns the correct status
  code (401/403/429 for failures, 200 for success).
- **Purge by age**: insert N rows at varied timestamps; call
  `purge_login_events_older_than(7)`; verify only rows older than 7 days
  are gone, returns the deleted count.
- **Purge by user**: insert rows for two users; purge user A; user B's
  rows are intact, returns the deleted count.

API / integration:

- `DELETE /admin/login-events?older_than_days=7` returns
  `{"deleted": <n>}` and writes an admin-audit row.
- `DELETE /admin/login-events?user_id=u1` returns `{"deleted": <n>}`
  and writes an admin-audit row.
- `DELETE /admin/login-events` with no params → 400.
- `DELETE /admin/login-events?older_than_days=7&user_id=u1` → 400.
- `DELETE /admin/login-events` without admin auth → 401.
- `hard_delete_user` integration: insert login_events rows for the
  doomed user, run hard-delete, verify the count returned in the audit
  log includes `login_events: <n>` and the rows are gone.

## Risks / open questions

- **PII / privacy**: `email`, `ip`, and `user_agent` are stored in plain
  text. Acceptable per existing policy (`api_logs` already stores
  `metadata.remote_ip`; the user-row schema stores email plain). If a
  stricter policy emerges later, hash the email and drop UA.
- **Volume**: At ~10 logins/sec sustained (well above current load), this
  table grows ~860k rows/day. The retention purge endpoint exists to
  manage that. No automated cron in this PR.
- **Race with auth deletion**: A login attempt against an account being
  hard-deleted could insert a row after `hard_delete_user`'s sweep ran.
  Acceptable: the row is orphaned (`user_id` is no longer valid in
  `users`), but it's still a useful audit fact. No FK constraint on
  `user_id` to allow this orphaning.
- **D1 parity**: D1's SQL is SQLite-flavoured and lacks `INTERVAL`. The
  by-age purge passes a precomputed Python `datetime` ISO string instead
  of `NOW() - INTERVAL`. Implementation plan must verify this works
  end-to-end.
