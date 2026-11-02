# Projects, multiple API keys and hierarchical limits (2026-11-01)

Status: P1a (several keys per account) implemented; see §16 for what was
built and where it departs from the original P1 plan. P1b, P2 and P3 are a
draft for discussion. Baseline: `dev` at `09a0dbcff`.

## 1. Summary

Today an account owns at most one active API key, and every policy (daily
quota, concurrency, model access) and every usage figure is keyed by
`user_id`. This document proposes a three-level resource model,
**Organization → Project → API key**:

- An **API key** is a credential that can be revoked at any time. It has a
  name, a model allowlist and optional limits, and those limits can only be
  *tighter* than its project's.
- A **project** is the unit of budget, concurrency, model access and usage
  attribution, and later of routing and logging policy.
- An **organization** owns projects and carries the ceilings the platform
  grants (today's per-user quota). In this iteration every user gets one hidden
  *personal* organization, which the console does not show. Teams later become
  "an organization with more than one member", with no ownership migration.

The work ships in phases. P1a unlocks several named keys per account (built;
§16), and P1b attributes usage to each key. P2 adds organizations and projects
with enforced daily and monthly limits at every level. P3 adds teams and the
HybridInference-specific project policies.

## 2. Current state

### 2.1 What already exists and this design builds on

| Capability | Where |
|---|---|
| Auth lookup cached for 30 s, invalidated on write | `CachedOperationalStore`, `apps/backend/serving/storage/cache.py` |
| Identity and today's spend read in one query (#185) | `get_auth_context_with_spend`, `apps/backend/serving/storage/postgres_operational.py` |
| Daily spend counter, incremented when a cost settles | `user_daily_cost`; `CostTracker`, `apps/backend/serving/servers/routers/completions_cost.py` |
| One quota gate shared by the API-key path and agent grants | `apps/backend/serving/quota.py` |
| Per-role default quota, applied in bulk by admins | `docs/agents/specs/2026-05-05-per-role-daily-quota-design.md` |
| Per-user in-flight cap (role default, `users.max_concurrent_requests`) | `apps/backend/serving/servers/concurrency.py` |
| Per-user model deny list, role-gated provider keys | `apps/backend/serving/model_access.py`; `provider_api_keys.min_role` |
| Key history listing, revoke and delete by prefix, regenerate | `apps/backend/serving/servers/routers/user_routes.py` |
| Structured audit (`actor`, `AuditTarget`) | `apps/backend/serving/audit.py` |
| Daily per-user usage rollup | `apps/backend/serving/storage/user_usage_rollup.py` |
| Startup DDL that checks the catalog first, waits at most `lock_timeout` for locks and defers on contention; background fills under advisory locks | `apps/backend/serving/storage/log_schema.py` (`apply_column_migrations`); `docs/developer/database.md` |
| Route locality (`deployment_type: self_hosted / provider_api / auto`) | `apps/backend/serving/servers/hybrid_composition.py` |

### 2.2 Gaps

| Gap | Evidence |
|---|---|
| **One active key per user.** It is enforced by partial unique indexes, and `POST /user/api-keys` returns 409 when a key already exists | `idx_api_keys_user_unique`, `idx_api_keys_account_active_unique` in `postgres_operational.py`; `user_routes.py:create_api_key` |
| The quota lookup for agent grants *depends on* that 1:1 rule to find "the" limit | module docstring of `serving/quota.py` |
| The limit sits on the key row, but the spend it is checked against is per user | `api_keys.quota_daily_cost_usd` vs `user_daily_cost(user_id, day)` |
| No monthly limit (`monthly_limit = None`) | `user_routes.py:get_usage` |
| Requests are attributed only to a user: `api_logs`, `user_daily_cost` and `user_usage_daily` have no key or project column | `log_schema.py` |
| Model access can only be narrowed per user (a deny list), never per key | `model_access.py` |
| Regenerate revokes the old key immediately, so rotation always causes downtime | `user_routes.py:regenerate_api_key` |
| The cost pre-check is a fixed $0.01, regardless of `max_tokens` or price | `serving/quota.py:ESTIMATED_REQUEST_COST_USD` |

## 3. Goals and non-goals

### Goals

1. A user can create, name, scope, rotate (with a grace period) and revoke
   several keys.
2. Every new request is attributed to `(org, project, key)`, and usage can be
   read by any of the three and by model.
3. Daily and calendar-monthly USD budgets, and in-flight concurrency caps, are
   enforced at org, project and key level. Errors name the scope that ran out.
4. Model access can be restricted per project and per key.
5. No extra database round-trip on the request path: the existing cached
   single-query auth lookup is extended, not added to.
6. Teams can be added later without migrating ownership.

### Non-goals (this iteration)

- Prepaid credits or invoicing. Budgets are quotas, not a ledger.
- Team UI, invitations and roles beyond `owner`. Only the data model is in
  scope; see P3.
- Per-endpoint read/write permissions (OpenAI's "Restricted" keys).
- RPM/TPM token buckets. Concurrency caps already exist and cover abuse; P3
  revisits.
- Backfilling key/project attribution into historical `api_logs` rows (§10).
- Cross-process consistency. State stays in-process, as `concurrency.py` and
  `InMemoryCache` are today.

## 4. Prior art

| Platform | Hierarchy | Where limits live | Key-level controls |
|---|---|---|---|
| OpenAI | Org → Project → Key / service account | Project: model allowlist, per-model rate limits, budget and alerts | All / Restricted (per endpoint) / Read-only |
| Anthropic | Org → Workspace → Key | Workspace: monthly spend, RPM / ITPM / OTPM, each ≤ the org's | Key bound to one workspace |
| OpenRouter | Account → Key | Key: credit limit with daily/weekly/monthly reset | Management-only provisioning keys |
| LiteLLM Proxy | User / Team → Virtual key | Key, team, user: `max_budget` + `budget_duration`, TPM/RPM | `models` allowlist, metadata |

What this design adopts: the **project is the policy boundary and the key is
disposable** (OpenAI, Anthropic); **a child never exceeds its parent**
(Anthropic); **keys may narrow limits** so a constrained key can be handed to a
script (OpenRouter, LiteLLM); **errors name the exhausted scope** (OpenRouter).

## 5. Model and invariants

```text
users ──N:M── org_members(role) ── organizations ──1:N── projects ──1:N── api_keys
                                     │ ceilings               │ limits ≤ org    │ limits ≤ project
                                     │ (platform-set)         │                 │
```

- **Organization.** Kinds: `personal` (created with the user), `team` (P3).
  Ceilings default from the owner's role through the existing per-role
  runtime settings. An admin can override them per org.
- **Project.** Belongs to one org. Each org has exactly one `Default` project,
  which cannot be archived.
- **API key.** Belongs to one project. It keeps its `user_id` (the creator),
  so per-user mechanisms (concurrency, deny list, verification, suspension)
  keep working unchanged.

Invariants:

1. `api_keys.org_id` always equals its project's `org_id`. It is denormalized
   for the auth query.
2. A limit written at a level must not exceed the limit at any ancestor that
   has one; this is validated on write. At run time the **effective limit is
   the minimum over all levels**, so lowering a parent never rewrites its
   children.
3. Model access narrows at each level:
   `effective = role-visible − user deny list ∩ project.allowed ∩ key.allowed`,
   where a `NULL` allowlist means "no restriction here".
4. Archiving a project revokes all of its keys.
5. IDs are opaque and globally unique (`org_…`, `proj_…`; keys keep their
   `BIGSERIAL` plus the existing unique `key_prefix`), so item routes need not
   nest under an org.

## 6. Data model

DDL follows the house pattern: `CREATE TABLE IF NOT EXISTS` for new tables, and
`apply_column_migrations` for new columns on existing tables. The latter checks
the catalog first and defers when it cannot get the lock in time, so it is safe
behind the nightly `pg_dump`.

### 6.1 New tables

```sql
CREATE TABLE IF NOT EXISTS organizations (
    id                      TEXT PRIMARY KEY,               -- 'org_…'
    name                    TEXT NOT NULL,
    kind                    TEXT NOT NULL CHECK (kind IN ('personal', 'team')),
    status                  TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'suspended')),
    -- NULL = derive from the owner's role (per-role runtime settings).
    budget_daily_usd        DECIMAL(12, 4),
    budget_monthly_usd      DECIMAL(12, 4),
    max_concurrent_requests INTEGER,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS org_members (
    org_id     TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_org_members_user ON org_members(user_id);

CREATE TABLE IF NOT EXISTS projects (
    id                      TEXT PRIMARY KEY,               -- 'proj_…'
    org_id                  TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name                    TEXT NOT NULL,
    description             TEXT,
    is_default              BOOLEAN NOT NULL DEFAULT FALSE,
    status                  TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'archived')),
    budget_daily_usd        DECIMAL(12, 4),
    budget_monthly_usd      DECIMAL(12, 4),
    max_concurrent_requests INTEGER,
    allowed_models          TEXT[],                         -- NULL = unrestricted
    settings                JSONB NOT NULL DEFAULT '{}'::jsonb,  -- §9, P3
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at             TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_org_name
    ON projects(org_id, lower(name)) WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_org_default
    ON projects(org_id) WHERE is_default;

-- Spend counters for every scope and window, generalizing user_daily_cost.
-- Scope ids are type-prefixed and globally unique, so no scope_type column.
CREATE TABLE IF NOT EXISTS spend_counters (
    scope_id        TEXT NOT NULL,          -- 'org_…' | 'proj_…' | 'key:<id>'
    period          TEXT NOT NULL,          -- 'd:2026-11-01' | 'm:2026-11'  (UTC)
    cost_usd        DECIMAL(14, 6) NOT NULL DEFAULT 0,
    requests        INTEGER NOT NULL DEFAULT 0,
    last_request_at TIMESTAMPTZ,
    PRIMARY KEY (scope_id, period)
);
```

**`spend_counters` instead of more columns on `user_daily_cost`.** One
settlement becomes a single multi-row `INSERT … ON CONFLICT DO UPDATE` of six
rows (three scopes × two windows). The auth query reads them with one
`LEFT JOIN … ON scope_id = ANY(…) AND period = ANY(…)`. `user_daily_cost` is
still written during the transition, because admin pages and `quota.py` read it
(§10).

### 6.2 Columns added to existing tables

```sql
-- api_keys
org_id                  TEXT        -- denormalized from the project
project_id              TEXT
name                    TEXT
allowed_models          TEXT[]
budget_daily_usd        DECIMAL(12, 4)
budget_monthly_usd      DECIMAL(12, 4)
max_concurrent_requests INTEGER
revoked_at              TIMESTAMPTZ
rotated_from_id         BIGINT
-- index: (project_id) WHERE status = 'active'

-- api_logs (in log_schema.py only, per docs/developer/database.md)
key_id      BIGINT
project_id  TEXT
-- partial indexes: (key_id, timestamp DESC), (project_id, timestamp DESC) WHERE NOT NULL
```

`api_keys.quota_daily_cost_usd` / `quota_monthly_cost_usd` are no longer
written after P2. Their values migrate to the org (§10), and the read path
falls back to them only while `organizations.budget_*` is unset.

**Why limit columns rather than a generic `limit_policies` table:** the set of
limits is small and stable. Typed columns let the auth query fetch the whole
chain in one join. Revisit if per-model budgets arrive.

### 6.3 Usage rollup

Add `key_usage_daily(day, key_id, model_id, requests, prompt_tokens,
completion_tokens, cost_usd)`. It is filled by the same mechanism as
`user_usage_rollup.fill_days` (coverage rows, advisory lock, hourly job), and
project and org totals are aggregated from it. Hard delete removes its rows in
the same transaction as `purge_user` (§12).

## 7. Request path

### 7.1 Flow

```text
1. auth lookup (CachedOperationalStore, 30 s)   key ⨝ project ⨝ org ⨝ user ⨝ spend_counters
2. model check                                  effective allowlist (§5, invariant 3)
3. concurrency                                  acquire slots: user (today), + project, + key if capped
4. quota.check                                  every level × {day, month}; first failure wins
5. route + execute                              unchanged; P3 honours project routing policy
6. CostTracker settle                           +user_daily_cost (transition) +spend_counters ×6
7. log                                          api_logs.key_id / project_id
```

The auth lookup **replaces** the current query; it is not added beside it, so
the request path still makes one round-trip on a cache miss and none on a hit.
Edits to a project or an org are rare and call the existing
`invalidate_auth_caches()`; edits to a key go through the existing
write-through invalidation.

### 7.2 Quota check

`serving/quota.py` stays the single gate for both entry points. Its inputs
change from one `(quota, spent)` pair to a list of
`(scope, window, limit, spent)` entries:

```python
@dataclass(frozen=True, slots=True)
class Budget:
    scope: Literal["key", "project", "org"]
    window: Literal["day", "month"]
    limit_usd: float
    spent_usd: float

def check(budgets: Sequence[Budget], estimate_usd: float) -> None:
    """Raise QuotaExceeded for the first budget the estimate does not fit."""
```

The check runs in order key → project → org, so the error names the
narrowest scope that ran out.

**Estimate.** The fixed $0.01 becomes
`price_in × estimated_prompt_tokens + price_out × min(max_tokens, model max output)`.
It is priced at the most expensive route candidate, because routing happens
after admission, and is $0 when every route is free. No reservation ledger is
added: overspend from concurrent requests is already bounded by the in-flight
cap (≤ cap × one request's maximum cost). That bound will be measured (§11)
before anything heavier is built.

**Agent grants** carry no key. In P2 they charge the org and its Default
project, which keeps today's "the user's quota governs how much" semantics.
Letting a grant name a project is P3.

### 7.3 Errors

The existing 429 body from `quota.exceeded_payload` is kept so clients do not
break. P2 adds two fields:

```json
{
  "error": "Daily cost quota exceeded",
  "code": "budget_exceeded",
  "scope": "project",
  "window": "month",
  "quota_usd": 50.0,
  "spent_usd": 50.12,
  "remaining_usd": 0,
  "reset_at": "2026-12-01T00:00:00+00:00",
  "retry_after": 1209600,
  "message": "Project 'eval-pipeline' reached its monthly budget of $50.00."
}
```

Model denials keep today's status and add `"code": "model_not_allowed"` and the
`scope` that excluded the model. A concurrency rejection adds `scope`. Whether
an exhausted budget should move to 402 is listed in §15.

## 8. API surface

### 8.1 Console API (existing `/user` router, session auth)

| Method & path | Phase | Purpose |
|---|---|---|
| `GET /user/api-keys/all` | P1 (extended) | Adds `id`, `name`, `project_id` and limits to each row |
| `POST /user/api-keys` | P1 (changed) | Creates an *additional* key `{name, project_id?}` up to the cap; no more 409 |
| `PATCH /user/api-keys/{key_prefix}` | P1 | Rename (P1); allowlist and limits (P2) |
| `POST /user/api-keys/{key_prefix}/rotate` | P1 | New key with the same settings; the old key expires after `grace_seconds` (0 – 7 days, default 24 h) |
| `DELETE /user/api-keys/{key_prefix}` | unchanged | Revoke if active, remove if already revoked |
| `POST /user/api-keys/regenerate` | deprecated | Same as `rotate` on the oldest active key with `grace_seconds = 0` |
| `GET /user/usage` | P1 (extended) | Adds `?key_prefix=`, `?project_id=`, `group_by=day,model,key,project` |
| `GET/POST /user/projects` | P2 | List (with spend against limits) and create |
| `GET/PATCH /user/projects/{id}` | P2 | Details and effective limits; update name, limits, allowlist |
| `POST /user/projects/{id}/archive` | P2 | Archive and revoke its keys |

Keys stay addressed by `key_prefix`, which is already unique and already used
by the console. Every route checks the owner and answers 404, not 403, for
another account's object (OWASP API Security Top 10 2023, API1: Broken Object
Level Authorization). Writes are audited with new `AuditTarget` types `api_key`
and `project`.

### 8.2 Inference plane (API-key auth)

| Method & path | Phase | Purpose |
|---|---|---|
| `GET /v1/key` | P2 | Describes the calling key: project, models, limits, remaining budget, reset times (as OpenRouter's `GET /api/v1/key` does) |
| `GET /v1/models` | P2 | Filtered to the key's effective allowlist |

### 8.3 Admin

- `PATCH /admin/orgs/{id}`: ceilings and suspension.
- Per-role bulk apply is retargeted from `api_keys.quota_daily_cost_usd` to
  personal orgs whose owner has that role.
- Existing `/admin/api-keys/*` routes keep working; responses gain
  `project_id` and `name`.

## 9. Key lifecycle and project policies

- **Caps** (runtime settings): 25 active keys per project, 20 active projects
  per org.
- **Signup** creates the user, the personal org, an `owner` membership and the
  Default project in one transaction, then mints the first key as today.
- **Rotation** copies the name (with a date suffix), allowlist and limits, sets
  `rotated_from_id`, and sets the old key's `expires_at = now + grace`. Both
  keys work during the grace period. Expiry fires no write, so a cached auth
  row can outlive the deadline by up to the 30 s TTL. P1 bounds the cache
  entry by the key's remaining lifetime, as `get_key_owner_for_audit` already
  does with `expires_in_sec`.
- **Revocation** is unchanged in behavior; it now also stamps `revoked_at`.
- **Batches** record the `key_hash` that created them (`routers/batches.py`).
  They will also record `project_id` so their cost lands on the right project.
  What happens to a running batch when its key is revoked follows the existing
  batch semantics, to be confirmed in P1 review.
- **Upstream key affinity** (`auth_key_hash` in `completions.py`) is
  unaffected: more keys per user just means more affinity buckets.

### Project settings (schema in P2, behavior in P3)

```python
class ProjectSettings(BaseModel):
    allow_external_providers: bool = True   # False → only self_hosted routes
    store_request_content: bool | None = None  # None → deployment default
```

- `allow_external_providers = False` guarantees that prompts never leave
  self-hosted GPUs. It filters candidates on the route's existing
  `deployment_type` before the router (Fixed or RouteWise) chooses. If no
  candidate is left, the request fails with `model_not_allowed`. RouteWise must
  receive the filtered candidate set, not filter after it decides.
- `store_request_content` maps onto the request log's existing switch for
  storing full content.

## 10. Migration

Every step is idempotent and follows the current startup pattern.

| Step | What | Reversible |
|---|---|---|
| **M1 Expand** | Create the tables in §6.1; add the columns in §6.2 with `apply_column_migrations`. Nullable and without defaults, so `api_logs` (millions of rows) takes a metadata-only change | Yes |
| **M2 Backfill** | Background job under an advisory lock, in batches: for each `users` row, create a personal org, an owner membership and a Default project; set `org_id`, `project_id` and `name = 'default'` on its keys; copy the active key's `quota_daily_cost_usd` to `organizations.budget_daily_usd` **only when it differs from the role default**, so later per-role changes still take effect for everyone else; seed `spend_counters` for today and this month from `user_daily_cost`. New signups create the rows inline | Yes |
| **M3 Switch** | Runtime setting `api_keys_multi_enabled` (in `app_config`, hot-reloadable) turns on the new auth query, settlement writes and console routes. Off falls back to today's path | Yes (setting) |
| **M4 Contract** | Once M2 reports complete and M3 has been stable for a week: drop `idx_api_keys_user_unique` and `idx_api_keys_account_active_unique` (`DROP INDEX CONCURRENTLY`) and replace them with the per-project cap; stop writing `api_keys.quota_*`; rewrite the docstring of `quota.py` | No |

Historical `api_logs` rows are **not** backfilled. Once a key has been
regenerated, past rows cannot be attributed to a key with certainty, and
rewriting millions of rows is not worth it. History stays attributed to users;
per-key and per-project views start at cut-over, and the console says so.

## 11. Phases

| Phase | Scope | Exit criteria |
|---|---|---|
| **P1a Several keys per account** (built, §16) | Retire the one-key indexes; per-account cap; key names; per-key rotation; account-level quota kept on every live key; console list with revoked keys folded away | Full unit, `dbtest` and frontend suites green; concurrent creates never pass the cap |
| **P1b Attribution** | `api_logs.key_id`; `key_usage_daily`; per-key usage in the console; rotation with a grace period | Per-key usage reconciles with `api_logs` within 0.1% |
| **P2 Projects and limits** | Organizations and projects (M1–M4, which now also move the account quota off the key rows onto the personal org); projects API and UI; daily and monthly budgets at key/project/org; `spend_counters`; the estimate in §7.2; scoped error fields; allowlists; per-project/per-key concurrency caps; `GET /v1/key`; filtered `/v1/models`; admin org endpoints; retargeted per-role bulk apply | Concurrency test: N in-flight requests never overshoot a budget by more than N × the maximum estimate error. Monthly limit enforced and shown |
| **P3 Teams and policies** | `team` orgs, invitations, roles, org switcher; project routing and logging policies; budget alerts through the existing webhook/notification sinks; RPM/TPM if the metrics in §11.1 show a need; grants scoped to projects | Separate spec |

### 11.1 Observability

Low-cardinality metrics: `tenant_rejections_total{reason, scope}`,
`quota_estimate_ratio` (histogram of actual cost over the estimate, at
settlement), and `auth_cache_requests_total{result}`. Per-key and per-project
numbers come from `key_usage_daily`, never from metric labels.

## 12. Hard delete and privacy

Hard delete already purges a user's rows in one transaction and fences the
account (`log_schema.py`, `user_usage_rollup.purge_user`). The same transaction
must also remove the personal org (which cascades to members and projects), the
user's `spend_counters` and their `key_usage_daily` rows. For a team org (P3), a
departing member's keys are revoked and their projects stay with the org.
`dbtest` covers this.

## 13. Testing

- **Unit:** effective-limit resolution (minimum over levels, `NULL`);
  allowlist narrowing; validation on write against ancestors; the estimate for
  free routes, paid routes and `max_tokens = None`.
- **`dbtest` tier** (`make test-db-local`): M1–M4 against fixtures with
  legacy keys (`account_id IS NULL`), a regenerated key history and a
  custom-quota user; running everything twice is a no-op; each user's daily
  spend and effective quota are the same before and after; hard delete leaves
  no org, project, counter or rollup rows.
- **API:** another account's `key_prefix`/`project_id` returns 404; rotation
  grace; the legacy `regenerate` path; agent grants charge the Default project.
- **Live:** verify against a local or example deployment
  (`make up DISTRIBUTION=example`) before claiming acceptance, per `AGENTS.md`.

## 14. Alternatives considered

| Alternative | Why not |
|---|---|
| Key-level limits only, no projects (OpenRouter) | Users with several workloads end up rebuilding projects out of naming conventions, and routing/logging policy has nowhere to live |
| Show organizations in the console now | A concept most users do not need yet; a hidden personal org gives the same forward compatibility |
| Reservation ledger for in-flight cost | The concurrency cap already bounds overshoot; build it only if `quota_estimate_ratio` shows a need |
| Backfill historical `api_logs` attribution | Ambiguous after regeneration and costly at this table's size |
| Extend `user_daily_cost` with key/project columns | Changes the primary key of a hot table, still has no monthly window, and has to be read three times |
| Redis for counters now | Every limiter and cache today is in-process; adopt it everywhere at once, not here alone |

## 15. Open questions

1. **Showing a key again.** `api_keys.api_key_encrypted` lets the console show
   an active key's full value again. OpenAI and Anthropic show a key only once,
   and keeping a reversible copy makes a database leak worse. Keep it for
   existing keys, but default new keys to show-once?
2. **402 vs 429 for an exhausted budget.** OpenAI SDKs retry 429 automatically,
   which is pointless until the window resets. OpenRouter uses 402. Switching
   would break clients that match on 429 today.
3. **Default project limits.** Should a new project inherit from the org
   (`NULL`, as proposed) or start with explicit values?
4. **Caps.** Are 25 keys per project and 20 projects per org right for current
   users?
5. **Batch on key revocation.** Should revoking a key cancel that key's
   running batches, or let them finish?
6. **Making the key cap a runtime setting.** P1a ships it as a constant (10).

## 16. P1a as built

P1a delivers several keys per account without the organization and project
tables. Moving the account quota onto a personal org (the original P1 step 1)
needs a deferrable users/org column read on the auth path, a backfill and a
rollback story. All of that belongs with the P2 migration, which has to move
it anyway.

**The quota stays on the key rows, as an account-level value.** Every
unrevoked key of an account carries the same `quota_daily_cost_usd` /
`quota_monthly_cost_usd`. The store keeps it that way, so the auth lookup can
keep reading the quota from the presented key:

- `create_key(inherit_account_quota=True)`, which a user's own create uses,
  copies the account's quota onto the new key. Only a first key takes the role
  default.
- `create_key(inherit_account_quota=False)`, which the admin path uses, makes
  the given quota the account's and writes it to the user's other unrevoked
  keys in the same transaction.
- `update_key(user_id)` (admin) already wrote every unrevoked key.
  `apply_role_quota` and its preview now cover suspended keys as well, so a
  resumed key cannot come back with an older limit.
- Reads made per user rather than per key take one key row per account
  (`LATERAL … LIMIT 1`): the grant quota lookup, the users-at-quota alert, the
  admin summary and the admin user list.

**Schema.** The two partial unique indexes are dropped at startup. The drop is
catalog-gated and deferrable: while a held table postpones it, a second key is
refused with the old 409. The new `api_keys.name` column has its own deferrable
migration. Until it lands, keys list as unnamed and naming one answers 503
(`AccountSchemaPending`). Neither copy of the DDL creates the indexes any more.

**Cap.** `MAX_ACTIVE_API_KEYS_PER_ACCOUNT = 10`
(`serving/servers/routers/user_routes.py`). The count and the insert run under
a per-user transaction-scoped advisory lock, so concurrent creates cannot pass
it.

**API.**

| Method & path | Behavior |
|---|---|
| `POST /user/api-keys` | Optional body `{name}`; 409 at the cap |
| `GET /user/api-keys/all` | Adds `name` per key and `max_active_keys` |
| `PATCH /user/api-keys/{key_prefix}` | Rename; a blank name clears it; 404 for a revoked or foreign key |
| `POST /user/api-keys/{key_prefix}/regenerate` | Rotates that key: revoke and reissue in one transaction, keeping its name, expiry, notes, metadata and the account quota |
| `POST /user/api-keys/regenerate` | Legacy; rotates the only active key, 409 when there are several |
| `GET /user/api-keys` | Legacy; the newest active key |

**Behavior changes worth knowing.**

- Regenerating a key no longer resets a custom quota to the role default.
- Everything that used to read "the" key reads the newest active one: the
  console batch credential, the admin user detail, `GET /user/api-keys`.

**Console.** Create stays available until the cap, asks for an optional name
and shows the active count. Each active key has Rename, Regenerate and Delete.
Revoked keys are folded under "Show revoked keys (n)".

**Not in P1a:** per-key usage attribution, rotation with a grace period, and
admin endpoints addressed by key (they remain account-level). Those are P1b and
later.

## References

- OpenAI Help Center: [Assign API key permissions](https://help.openai.com/en/articles/8867743-assign-api-key-permissions); [Managing projects in the API platform](https://help.openai.com/articles/9186755)
- Anthropic: [Workspaces](https://platform.claude.com/docs/en/manage-claude/workspaces)
- OpenRouter: [Provisioning API keys](https://openrouter.ai/docs/features/provisioning-api-keys); [Limits](https://openrouter.ai/docs/api/reference/limits)
- LiteLLM: [Budgets, rate limits](https://docs.litellm.ai/docs/proxy/users)
- OWASP: [API Security Top 10 (2023), API1 Broken Object Level Authorization](https://owasp.org/API-Security/editions/2023/en/0xa1-broken-object-level-authorization/)
