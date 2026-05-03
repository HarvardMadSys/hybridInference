# Broadcast Email — Admin Dashboard Feature Design

**Date:** 2026-04-30  
**Status:** Approved

---

## Overview

Add a "Broadcast Email" feature to the admin dashboard that lets admins compose and send bulk emails to filtered subsets of users. Supports predefined templates with variable substitution, custom free-form content, recipient filtering by role/status, preview with recipient count, test-send to admin, scheduled delivery, and full per-recipient send history.

---

## Requirements Summary

| Requirement | Decision |
|---|---|
| Email content | Predefined templates + custom free-form (both) |
| Recipients | Configurable — filter by role and/or status |
| Preview/confirmation | Recipient count preview + test email to admin + confirm modal |
| Scheduling | Immediate or scheduled for a future datetime |
| History | Full history with per-recipient sent/failed status |

---

## Data Model

### New table: `email_broadcasts`

One row per broadcast campaign.

```sql
CREATE TABLE IF NOT EXISTS email_broadcasts (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    body_html TEXT NOT NULL,
    body_text TEXT NOT NULL,
    template_key TEXT,                          -- null = custom
    template_vars JSONB NOT NULL DEFAULT '{}',
    target_roles TEXT[] NOT NULL DEFAULT '{}',  -- e.g. ['free','internal']
    target_statuses TEXT[] NOT NULL DEFAULT '{}', -- e.g. ['active']
    recipient_count INT NOT NULL DEFAULT 0,     -- snapshot at send time
    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled','sending','sent','failed','cancelled')),
    scheduled_at TIMESTAMPTZ,                   -- null = send immediately on creation
    created_by TEXT NOT NULL,                   -- admin user id
    created_at TIMESTAMPTZ DEFAULT NOW(),
    sent_at TIMESTAMPTZ
);
```

### New table: `email_broadcast_recipients`

One row per user per campaign.

```sql
CREATE TABLE IF NOT EXISTS email_broadcast_recipients (
    id BIGSERIAL PRIMARY KEY,
    broadcast_id TEXT NOT NULL REFERENCES email_broadcasts(id),
    user_id TEXT NOT NULL,
    email TEXT NOT NULL,                        -- snapshot at send time
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','sent','failed')),
    error TEXT,                                 -- error message on failure
    sent_at TIMESTAMPTZ,
    INDEX (broadcast_id),
    INDEX (broadcast_id, status)
);
```

---

## Backend Architecture

### New file: `serving/utils/email_scheduler.py`

Owns the APScheduler lifecycle and broadcast execution logic.

**Responsibilities:**
- Initialize `AsyncIOScheduler` and start/stop it via FastAPI `lifespan`
- On startup: query DB for `status='scheduled'` rows with `scheduled_at > now()`, re-register each as an APScheduler one-time job (restart recovery)
- If a `scheduled_at` is in the past at re-hydration time (server was down during the window), trigger immediately
- `schedule_broadcast(broadcast_id, run_at)` — adds a one-time DateTrigger job, job id = broadcast_id; when `run_at` is None (immediate), fires as a FastAPI `BackgroundTask` instead of scheduling
- `cancel_broadcast_job(broadcast_id)` — removes the APScheduler job if present
- `execute_broadcast(broadcast_id)` — the job function:
  1. Mark broadcast `sending`
  2. Resolve recipients from DB using `target_roles` + `target_statuses` filters, insert `email_broadcast_recipients` rows
  3. Send via existing `send_email()` in batches of 50 with short delay between batches
  4. Update each recipient row (`sent` or `failed` with error message)
  5. Mark broadcast `sent` (or `failed` if 100% of recipients failed), set `sent_at`

### New API endpoints (added to `serving/servers/routers/admin.py`)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/admin/broadcast-email/preview` | Admin token | Return recipient count + rendered preview (no send) |
| `POST` | `/admin/broadcast-email/test` | Admin token | Send test email to requesting admin's address |
| `POST` | `/admin/broadcast-email` | Admin token | Create & schedule (or fire immediately as a background task) a broadcast |
| `GET` | `/admin/broadcast-email` | Admin token | List all broadcasts (paginated, newest first) |
| `GET` | `/admin/broadcast-email/{id}` | Admin token | Detail: campaign info + per-recipient status (paginated) |
| `DELETE` | `/admin/broadcast-email/{id}` | Admin token | Cancel a `scheduled` broadcast |

### New schemas (added to `serving/schemas_admin.py`)

```python
class BroadcastPreviewRequest(BaseModel):
    template_key: str | None = None
    template_vars: dict = {}
    subject: str
    body_html: str
    body_text: str
    target_roles: list[str]
    target_statuses: list[str]

class BroadcastPreviewResponse(BaseModel):
    recipient_count: int
    rendered_subject: str
    rendered_body_html: str

class CreateBroadcastRequest(BroadcastPreviewRequest):
    scheduled_at: datetime | None = None   # None = send immediately

class CreateBroadcastResponse(BaseModel):
    id: str
    status: str
    recipient_count: int
    scheduled_at: datetime | None

class BroadcastListItem(BaseModel):
    id: str
    subject: str
    status: str
    recipient_count: int
    scheduled_at: datetime | None
    sent_at: datetime | None
    created_by: str
    created_at: datetime

class BroadcastRecipientItem(BaseModel):
    user_id: str
    email: str
    status: str
    error: str | None
    sent_at: datetime | None

class BroadcastDetailResponse(BaseModel):
    broadcast: BroadcastListItem
    recipients: list[BroadcastRecipientItem]
    total: int
```

### Predefined templates (added to `serving/utils/email.py`)

A dict `EMAIL_TEMPLATES` mapping key → `(subject_template, html_template, text_template)`:

| Key | Variables | Description |
|---|---|---|
| `maintenance` | `date`, `duration` | Scheduled downtime notice |
| `announcement` | `feature_name`, `description` | New feature or update |
| `quota_change` | `new_quota` | Quota adjustment notice |

Template rendering uses Python's `str.format_map()` with the provided `template_vars`.

---

## Frontend Architecture

### New tab: "Broadcast Email"

Added to the existing tab list in `frontend/src/app/dashboard/admin/page.tsx`, following the same tab pattern as existing sections.

### Composer panel

- **Template selector** — dropdown: "Custom" or one of the predefined template names
- When template selected: variable input fields rendered dynamically (e.g. `date`, `duration`)
- **Subject** field — pre-filled from template, editable
- **Body (HTML)** textarea — pre-filled from template, editable; plain text auto-generated from HTML on submission
- **Recipient filters** — two groups of checkboxes:
  - Roles: `free`, `internal`, `admin`
  - Statuses: `active`, `suspended`, `pending_approval`, `rejected`
- **Preview button** — calls `previewBroadcast()`, shows recipient count + rendered preview inline below the composer
- **"Send test email to me"** button — calls `sendTestEmail()`, toast on success/failure
- **Schedule toggle** — "Send now" vs "Schedule for later"; when scheduled: datetime picker
- **Send / Schedule button** — opens confirmation modal showing recipient count and scheduled time, then calls `createBroadcast()` on confirm

### History table (below composer)

- Columns: Subject, Status badge, Recipients, Scheduled/Sent at, Created by, Actions (Cancel for `scheduled`)
- Clicking a row opens a detail drawer showing per-recipient status table: Email, Status badge, Error, Sent at — paginated

### New API client functions (`frontend/src/lib/api/admin.ts`)

```typescript
previewBroadcast(req: BroadcastPreviewRequest): Promise<BroadcastPreviewResponse>
sendTestEmail(req: BroadcastPreviewRequest): Promise<void>
createBroadcast(req: CreateBroadcastRequest): Promise<CreateBroadcastResponse>
listBroadcasts(page: number, pageSize: number): Promise<{ items: BroadcastListItem[], total: number }>
getBroadcastDetail(id: string, page: number): Promise<BroadcastDetailResponse>
cancelBroadcast(id: string): Promise<void>
```

---

## Error Handling

### Per-recipient failures
- If `send_email()` raises for a recipient, mark that row `failed` with the error message and continue to the next
- Broadcast status: `sent` if ≥1 recipient succeeded; `failed` only if all recipients failed

### Scheduler restart recovery
- On FastAPI startup, re-hydrate all `status='scheduled'` broadcasts from DB into APScheduler
- If `scheduled_at` is in the past at re-hydration time, execute immediately

### API guards
- `preview` returns 0 count (not an error) when filters match no users — prevents accidental empty sends
- Test email always goes only to the requesting admin's address
- `DELETE` returns HTTP 409 if broadcast is not in `scheduled` status

### Cancellation
- `DELETE /admin/broadcast-email/{id}` sets status to `cancelled` in DB and removes the APScheduler job by broadcast id

---

## Testing

- **Unit tests:** template rendering (variable substitution, missing vars), recipient filter SQL logic
- **Integration tests:** preview endpoint (recipient count accuracy), create endpoint (DB row created, scheduler job added), cancel endpoint (409 on non-scheduled)
- **Email send:** mock `send_email()` at the boundary — no real SMTP in CI
- No end-to-end email delivery tests

---

## Files Changed / Created

| File | Change |
|---|---|
| `serving/storage/database.py` | Add `email_broadcasts` and `email_broadcast_recipients` tables |
| `serving/utils/email.py` | Add `EMAIL_TEMPLATES` dict and template rendering helper |
| `serving/utils/email_scheduler.py` | **New** — APScheduler setup, `execute_broadcast`, `schedule_broadcast`, `cancel_broadcast_job` |
| `serving/schemas_admin.py` | Add broadcast request/response schemas |
| `serving/servers/routers/admin.py` | Add 6 new broadcast endpoints |
| `serving/servers/app.py` | Wire APScheduler into FastAPI `lifespan` |
| `frontend/src/lib/api/admin.ts` | Add 6 new API client functions |
| `frontend/src/app/dashboard/admin/page.tsx` | Add "Broadcast Email" tab with composer + history |
