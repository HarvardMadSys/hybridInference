# Signup Domain Allowlist with Admin-Editable Approval Policy

**Date:** 2026-05-02
**Status:** Design approved, awaiting review

## Goal

Let admins configure a list of email domains whose signups auto-approve. Signups from any other domain land in `pending_approval` and require admin action. Replace the existing `SIGNUP_REQUIRE_APPROVAL` env var with this allowlist-driven policy, manageable from the admin dashboard.

## Background

The codebase already supports approval-gated signup:

- `users.status` includes `pending_approval` and `rejected` (`serving/storage/database.py:438-439`).
- Approval columns `approval_note`, `reviewed_at`, `reviewed_by` exist (`database.py:440-442`).
- `auth_routes.py:169` reads `SIGNUP_REQUIRE_APPROVAL` env to decide initial status. `1` → all signups pending; `0` → all auto-approve.
- Admin endpoints for approve/reject (`admin.py:966`, `admin.py:1033`) and notification email (`send_new_registration_admin_email` in `serving/email.py:347`) already wired.
- Email blocklist exists (`email_blocklist.py`) — static disposable-domain set.
- No allowlist concept. No admin UI for any signup policy. No app-settings table.

This proposal adds a per-deploy, runtime-editable allowlist that drives the same `pending_approval` flow already in place.

## Approach

**Dedicated `signup_allowed_domains` table.** Admin CRUD via new endpoints. New `Settings` tab in the admin dashboard hosts a `Signup Policy` section. Signup logic queries the table; empty list = all auto-approve. Match supports exact (`example.com`) and subdomain wildcard (`*.example.com`).

Alternative considered: generic `app_settings` JSONB key/value table. Rejected on YAGNI grounds — no second admin-editable scalar setting exists today; introduce a settings table when one appears.

## Backend changes

### DB migration (`serving/storage/database.py`)

Add new table alongside existing schema definitions. Follow the same in-place migration pattern used for prior column adds.

```sql
CREATE TABLE IF NOT EXISTS signup_allowed_domains (
    domain TEXT NOT NULL,
    is_wildcard BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by TEXT REFERENCES users(id),
    PRIMARY KEY (domain, is_wildcard)
);
```

Storage rules:

- `domain` always normalized: lowercase, trimmed, no leading `*.`.
- `is_wildcard=TRUE` when the user-supplied entry began with `*.`. The stored `domain` is the suffix (e.g., user enters `*.acme.io` → row `('acme.io', TRUE)`).
- Composite PK `(domain, is_wildcard)` lets the same suffix coexist as exact and wildcard entries.

### Match logic (`serving/auth/signup_policy.py`, new file)

```python
def is_domain_allowed(email: str, conn) -> bool:
    """True if email's domain is on the allowlist (exact or wildcard suffix)."""
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].strip().lower()
    if not domain:
        return False
    # exact
    row = conn.fetchone(
        "SELECT 1 FROM signup_allowed_domains WHERE domain=%s AND is_wildcard=FALSE",
        (domain,),
    )
    if row:
        return True
    # wildcard suffix: walk parent labels
    parts = domain.split(".")
    for i in range(1, len(parts) - 1):
        suffix = ".".join(parts[i:])
        row = conn.fetchone(
            "SELECT 1 FROM signup_allowed_domains WHERE domain=%s AND is_wildcard=TRUE",
            (suffix,),
        )
        if row:
            return True
    return False


def allowlist_is_empty(conn) -> bool:
    row = conn.fetchone("SELECT 1 FROM signup_allowed_domains LIMIT 1")
    return row is None
```

Cache `allowlist_is_empty` in-process with a short TTL (~30s) so the hot signup path avoids the DB query when the feature is unused.

### Signup flow (`serving/servers/routers/auth_routes.py:169-189`)

Replace the env-var branch with allowlist logic:

```python
if allowlist_is_empty(conn) or is_domain_allowed(email, conn):
    status = "active"
else:
    status = "pending_approval"
```

Keep the existing admin-notify call: when `status == "pending_approval"`, invoke `send_new_registration_admin_email`.

Email-verification logic (`SIGNUP_REQUIRE_EMAIL_VERIFICATION`) stays untouched and applies to both auto-approved and pending users.

### Env var removal

Delete all reads of `SIGNUP_REQUIRE_APPROVAL`. PR description must call out the migration:

- Deploys with `SIGNUP_REQUIRE_APPROVAL=0` (or unset): no behavioral change. Empty allowlist = all auto-approve.
- Deploys with `SIGNUP_REQUIRE_APPROVAL=1`: behavior changes from "all pending" to "all auto-approve" once the env var is removed. Operators who want to keep approval-gating must populate the allowlist with their trusted domains before/at deploy time. Domains outside the allowlist will then be gated. Strict-for-all (no auto-approve) is no longer supported; this is the intended trade-off (per design Q2 = option A, no master toggle). Operators relying on strict-for-all should populate only their internal corporate domain(s) — the result is functionally equivalent to the old strict mode for outside signups.

## Admin API endpoints (`serving/servers/routers/admin.py`)

All require admin auth; all mutations write to the existing `audit_log` table.

| Method | Path | Body / Params | Returns |
|---|---|---|---|
| GET | `/admin/signup-domains` | — | `[{domain, is_wildcard, created_at, created_by_email}]` |
| POST | `/admin/signup-domains` | `{domain: str}` | `201 {domain, is_wildcard, ...}` or `409` on duplicate |
| DELETE | `/admin/signup-domains/{domain}?wildcard=bool` | — | `204` |

### Validation (POST)

1. Strip whitespace, lowercase.
2. Reject empty.
3. If begins with `*.` → set `is_wildcard=TRUE`, strip prefix.
4. Reject if remainder contains `*`, `@`, whitespace.
5. Reject if remainder fails `^([a-z0-9-]+\.)+[a-z]{2,}$`.
6. Reject duplicate composite key.

### Audit log

- `signup_domain.add` — actor=admin id, target=domain, metadata=`{is_wildcard}`
- `signup_domain.remove` — actor=admin id, target=domain, metadata=`{is_wildcard}`

No bulk endpoints.

## Frontend changes

### New tab: `Settings` (`frontend/src/app/dashboard/admin/page.tsx`)

Append `Settings` to the tab list (after `Analytics`). Render `SettingsTab.tsx`.

### `frontend/src/app/dashboard/admin/SettingsTab.tsx` (new)

Sections:

- **Signup Policy**
  - Description: "Signups from listed domains auto-approve. Other domains require admin approval. Empty list = all signups auto-approve."
  - Form: text input + Add button. Hint: `example.com` or `*.example.com` for subdomains.
  - Table: `Domain | Type | Added | Action(Remove)`.
  - Empty state: "No allowed domains. All signups auto-approve."

Behavior:

- Fetch `/admin/signup-domains` on mount.
- POST on Add. Refetch on success. Toast on 4xx with server message.
- DELETE on Remove with confirm dialog.
- Client-side validation mirrors server (strip, lowercase, `*.` prefix detection, regex). Inline error if invalid.
- Domains immutable — to change, remove and re-add.

## Edge cases

- **Email-verification independence**: auto-approved users still receive verification email if `SIGNUP_REQUIRE_EMAIL_VERIFICATION=1`.
- **Pre-existing users**: only new signups consult the allowlist. Existing `pending_approval` users keep that status until an admin acts.
- **Domain extraction**: take substring after last `@`; lowercase; trim. Email format validated upstream by signup schema.
- **Duplicate add**: PK conflict → 409 with clear message.
- **Domain removal**: not retroactive. Already-active users remain active.
- **Disposable-domain blocklist**: runs BEFORE allowlist check. Blocked domains rejected regardless of allowlist entry.
- **Mixed case input**: normalized to lowercase before storage and match.
- **Subdomain wildcard scope**: `*.example.com` matches `a.example.com`, `a.b.example.com`. Does NOT match `example.com` itself — admin must add an exact entry too if they want both.

## Testing

### Unit (`test/unit/auth/test_signup_policy.py`, new)

- `is_domain_allowed` exact match — case-insensitive, leading/trailing whitespace.
- Wildcard match — matches one and many subdomain levels; does not match the bare suffix.
- No match returns False.
- Empty list returns False (caller handles "empty = allow").
- Malformed email (no `@`, empty domain) returns False.

### Integration (`test/integration/test_signup_flow.py`)

- Empty allowlist → signup status `active`.
- Allowlisted exact domain → `active`.
- Allowlisted wildcard match → `active`.
- Non-listed domain → `pending_approval`, admin notify email sent.
- Disposable domain blocked even if added to allowlist (block precedes allowlist).

### Admin API (`test/integration/test_admin_signup_domains.py`, new)

- Non-admin gets 403 on all three endpoints.
- POST validates malformed, duplicate, wildcard prefix.
- DELETE removes correct row including `wildcard` query param disambiguation.
- Audit log row written for add and remove.

### Frontend

- `SettingsTab` renders, fetches list, adds, removes.
- Error toast on server 4xx.
- Empty-state copy when list is empty.

## Out of scope

- Bulk import/export of domains.
- Per-domain notes, expiry, or owner.
- Domain-based role/tier auto-assignment.
- UI for editing the disposable-domain blocklist.
- Re-evaluating existing users against the allowlist.
- Master "require approval" toggle (rejected per Q2 = A).
