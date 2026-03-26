# User Roles & Permissions — Design Spec

> **Status:** Implemented.
> Quota management is out of scope — covered in a separate PR.

### Key Design Decisions

**Admin source-of-truth** — `users.role` in the database is the sole
authority for permission checks. `ADMIN_EMAILS` serves as a bootstrap seed:
on login/refresh, if a user's email matches `ADMIN_EMAILS` **and** their role
is still `'free'` (the default), the system auto-promotes them to `admin`.
Users demoted to `internal` will **not** be re-promoted.
Edge case: demotion back to `'free'` while the email remains in `ADMIN_EMAILS`
will trigger re-promotion — remove the email from the env var to prevent this.

**`ADMIN_TOKEN` break-glass** — The `ADMIN_TOKEN` env var provides a
separate, DB-independent authentication path for `/admin/*` endpoints. This
is intentional: it serves as an emergency break-glass mechanism when the
database is unavailable or when no admin user exists yet. `ADMIN_TOKEN`
bypasses the DB role check entirely. Keep it rotated and treat it as a
root credential.

All normal permission checks read `users.role` from the database:

| Code path | Check |
|-----------|-------|
| `deps.py:require_admin()` | `users.role == 'admin'` (DB query) |
| `deps.py:verify_admin_access()` | `users.role == 'admin'` (DB query) |
| `playground.py` | `Depends(require_role("internal"))` |
| `health.py:model-activity` | `has_role(role, "internal")` |
| `internal.py:verify-grafana` | `has_role(role, "internal")` |

**Endpoint × role verification matrix:**

| Endpoint | free | internal | admin |
|----------|:----:|:--------:|:-----:|
| POST /v1/chat/completions | yes | yes | yes |
| GET /internal/playground/* | — | yes | yes |
| GET /health/model-activity | — | yes | yes |
| Nginx: /grafana/, /llm-prober/ | — | yes | yes |
| Nginx: /pgadmin/ | — | — | yes |
| ALL /admin/* | — | — | yes |

**`role` vs `tier` — orthogonal concepts:**

- **`role`** (on `users` table): permission level. Enum: `free | internal | admin`.
- **`tier`** (on `api_keys` table): billing/quota label. Enum: `free | pro | enterprise`. **Unchanged.**
- These are orthogonal. An `internal` role can have a `free` tier key.
- JWT carries both `role` and `tier` claims.
- No migration or renaming of `api_keys.tier`.

## 1. Role Hierarchy

Three roles, ordered by privilege level:

```
 admin  ⊃  internal  ⊃  free
```

Higher roles inherit all permissions of lower roles.

| Role | Who | Purpose |
|------|-----|---------|
| **admin** | Platform operators | Full control: user management, system config, everything |
| **internal** | Internal team members / platform builders | Dev tools (Grafana, prober, playground) + subscription models (Claude, Codex) |
| **free** | External / public users | Basic inference API with community models |

---

## 2. Permission Matrix

### 2a. Feature access

| Feature | free | internal | admin |
|---------|:----:|:--------:|:-----:|
| Inference API (`/v1/chat/completions`) | yes | yes | yes |
| Anthropic proxy (`/anthropic/v1/messages`) | yes | yes | yes |
| Embeddings API (`/v1/embeddings`) | yes | yes | yes |
| Dashboard — own usage stats | yes | yes | yes |
| Dashboard — settings / key management | yes | yes | yes |
| Grafana dashboards (`/grafana/`) | — | yes | yes |
| LLM Prober UI (`/llm-prober/`) | — | yes | yes |
| Playground (`/internal/playground/*`) | — | yes | yes |
| Model activity endpoint (`/health/model-activity`) | — | yes | yes |
| PgAdmin (`/pgadmin/`) | — | — | yes |
| Admin panel (`/admin/*`) | — | — | yes |
| User approval / rejection | — | — | yes |
| API key management (other users) | — | — | yes |

### 2b. Model access

| Model category | free | internal | admin |
|----------------|:----:|:--------:|:-----:|
| Community models (open-source, low-cost) | yes | yes | yes |
| Codex models (`gpt-5.x-codex`) | — | yes | yes |
| Claude sub models (`claude-opus-4.6`, `claude-sonnet-4.6`) | — | yes | yes |
| Future premium / experimental models | — | — | yes |

> **internal** unlocks access to subscription models (Claude, Codex) plus dev tools.
> **free** users only see community models — subscription models are hidden.

---

## 3. Schema

### 3a. Role stored on `users` table

```sql
ALTER TABLE users
ADD COLUMN role TEXT DEFAULT 'free'
    CHECK (role IN ('admin', 'internal', 'free'));
```

Role lives on `users`, not `api_keys`. Key regeneration does not affect role.

### 3b. Model access level

In `config/models.yaml`:
```yaml
required_role: internal   # minimum role to see/use this model
```

Default: `required_role: free` (visible to everyone).

Role ranking for access check:
```python
ROLE_RANK = {"free": 0, "internal": 1, "admin": 2}
```

---

## 4. Auth Flow

### 4a. JWT includes `role`

In `auth_routes.py` login and refresh handlers, read `users.role` from DB:

```python
access_token, jti = create_access_token(
    user_id=user_row["id"],
    email=user_row["email"],
    role=user_row["role"],
    session_id=session_id,
    is_admin=(user_row["role"] == "admin"),
)
```

### 4b. API key verification returns `role`

In `verify_api_key()`, join `users` table to get role:

```sql
SELECT k.id, k.user_id, k.user_name, k.quota_daily_cost_usd,
       u.email, u.role
FROM api_keys k
LEFT JOIN users u ON u.id = k.user_id
WHERE k.key_hash = $1 AND k.status = 'active'
```

User context returned:
```python
{"user_id": ..., "role": "internal", "is_admin": False, ...}
```

### 4c. Nginx subrequest supports internal role

`GET /internal/verify-grafana` checks `has_role(role, "internal")`.
PgAdmin stays admin-only via `GET /internal/verify-admin`.

---

## 5. Model Gating

```python
ROLE_RANK = {"free": 0, "internal": 1, "admin": 2}

user_role = (user_ctx or {}).get("role", "free")
user_rank = ROLE_RANK.get(user_role, 0)

for model_id, route in router_exec.routes.items():
    required = route.required_role or "free"
    if ROLE_RANK.get(required, 0) > user_rank:
        continue  # hide this model
```

### Model config example

```yaml
# config/models.yaml
models:
  - id: deepseek-chat
    required_role: free        # everyone can use

  - id: gpt-5.3-codex
    required_role: internal    # internal team + admin

  - id: claude-opus-4.6
    required_role: internal    # internal team + admin

  - id: some-experimental-model
    required_role: admin       # admin testing only
```

---

## 6. Frontend

### AuthProvider state

```typescript
user: {
  role: "free" | "internal" | "admin";
  is_admin: boolean;  // kept as convenience (role === "admin")
}
```

### Route visibility

| Page | Gate |
|------|------|
| `/dashboard` | Authenticated (no change) |
| `/dashboard/settings` | Authenticated (no change) |
| `/dashboard/playground` | `role >= internal` |
| `/dashboard/admin` | `role === admin` |
| Grafana link in nav | `role >= internal` |

### Helper

```typescript
const ROLE_RANK = { free: 0, internal: 1, admin: 2 };
const hasRole = (userRole: string, required: string) =>
  (ROLE_RANK[userRole] ?? 0) >= (ROLE_RANK[required] ?? 0);
```

---

## 7. Role Assignment

### On signup

New users default to `free`. No self-service role upgrade.

### Admin promotes users

Via `PATCH /admin/users/{user_id}`:

```json
{"role": "internal"}
```

Admin panel has a role dropdown in the user detail view.

### Bootstrap

`ADMIN_EMAILS` env var seeds initial admin(s). On first login, if the user's
email matches `ADMIN_EMAILS` and their DB role is still `free`, auto-promote
to `admin`.

---

## 8. Key Code Paths

| File | What it does |
|------|-------------|
| `serving/storage/database.py` | `role` column DDL, migration (internal_group/developer → internal) |
| `serving/config/settings.py` | `ROLE_RANK` constant, `has_role()` helper |
| `serving/servers/auth.py` | `verify_api_key()` joins `users.role`, returns in context |
| `serving/servers/deps.py` | `get_current_user()` includes `role`; `require_role(min_role)` dep |
| `serving/servers/routers/auth_routes.py` | JWT creation reads `users.role` |
| `serving/servers/routers/models.py` | `required_role` rank check for model visibility |
| `serving/servers/routers/completions.py` | Same rank check for request-time gating |
| `serving/servers/routers/anthropic_proxy.py` | Same rank check |
| `serving/servers/routers/internal.py` | `verify-grafana` checks `role >= internal` |
| `serving/servers/routers/admin.py` | Role update via `PATCH /admin/users/{user_id}` |
| `config/models.yaml` | `required_role` per model entry |
| `frontend/src/components/providers/AuthProvider.tsx` | `ROLE_RANK`, `hasRole()` helper |
| `frontend/src/app/dashboard/playground/page.tsx` | Gate on `role >= internal` |
| `frontend/src/app/dashboard/admin/page.tsx` | Role dropdown in user management |
