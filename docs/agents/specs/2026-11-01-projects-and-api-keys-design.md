# Projects, multiple API keys and hierarchical limits

Status: implemented through P2b (several keys per account, per-key attribution,
per-key limits, projects). P3 (teams and project policies) is a draft and is
the open part of this record. For how spend is charged and shown, see
[Accounting](../../developer/accounting.md).

## 1. Model

The hierarchy is **account → project → API key**.

- An **API key** is a revocable credential. It has a name, an optional model
  allowlist and optional daily and monthly budgets. Its limits can only be
  tighter than its project's and its account's.
- A **project** belongs to one account and is the unit of budget, concurrency,
  model access and usage attribution. Every account has a `Default` project
  that cannot be archived. A key belongs to one project and keeps its
  `user_id`, so per-user mechanisms (concurrency, deny list, suspension) work
  unchanged.
- The **account** carries the ceilings the platform grants: the daily and
  monthly quota and the concurrency cap. The quota record is `account_quotas`
  (one row per account). Each unrevoked key also carries a copy, and every
  write of the account quota updates the record and the copies in one
  transaction. A daily quota left NULL in the record is unset, and the
  presented key's copy applies.

Each limit level is optional. The effective limit is the minimum over all
levels, so lowering a parent never rewrites its children. Model access narrows
at each level:
`role-visible − user deny list ∩ project.allowed ∩ key.allowed`, where a NULL
allowlist means no restriction at that level.

IDs are opaque and globally unique (`proj_…`; keys keep their `BIGSERIAL` id
and unique `key_prefix`), so item routes need not nest under the account.

An earlier design put a hidden personal organization between account and
project. It was removed because it duplicated what the account already held
(quota, concurrency cap, status) and needed code to keep the two equal. If
teams arrive (P3), a team can own projects next to accounts.

## 2. Data

| Object | Holds |
|---|---|
| `projects` | `user_id` (owner), `name`, `is_default`, `status` (`active` / `archived`), `budget_daily_usd`, `budget_monthly_usd`, `max_concurrent_requests`, `allowed_models`, `settings` JSONB (unused until P3) |
| `api_keys` | `project_id`, `name`, `allowed_models`, `budget_daily_usd`, `budget_monthly_usd`, `revoked_at`, plus the account quota copies |
| `account_quotas` | The account's daily and monthly quota |
| `user_daily_cost` | Account spend per day |
| `api_key_daily_cost` | Key spend per day (`key_id`, `day`, `user_id`) |
| `spend_counters` | Project spend, scope id = project id, period `d:YYYY-MM-DD` or `m:YYYY-MM` (UTC) |
| `api_logs.key_id`, `api_logs.project_id` | Attribution of each logged request |

Rules:

- A project name is unique per account among active projects, and an account
  has one Default project (partial unique indexes).
- Safety ceilings, not product limits: 100 active keys
  (`MAX_ACTIVE_API_KEYS_PER_ACCOUNT`) and 100 active projects
  (`_MAX_PROJECTS`).
- Archiving a project revokes its keys.
- Hard delete removes the account's projects, key and project counters, and
  placeholder rows in the same transaction as the rest of the account.
- History from before attribution stays attributed to the account only.
  Per-key and per-project views start at the day each shipped, because rows
  cannot be attributed with certainty after a key was regenerated.
- `organizations`, `org_members`, `projects.org_id` and `api_keys.org_id` still
  exist, written with placeholder values (`'org_p_' || user_id`), because the
  previous release enforces an organization budget and its startup backfill
  would otherwise move keys. Nothing in the current release reads them. A
  contract revision, shipped once this release is the oldest one running,
  drops them with the three compatibility triggers and moves the two unique
  indexes onto `projects.user_id`.

## 3. Request path

1. The auth lookup (cached 30 s for listings, uncached for the quota gate)
   joins key, project, account quota and spend in one query. A key budget
   adds a lateral read of `api_key_daily_cost` only when the key has one, so
   keys without limits cost nothing extra. A budget change applies on the key's
   next request.
2. The model check applies the effective allowlist (key, project, account).
3. Concurrency takes the user slot, plus a shared project slot when the project
   has a cap.
4. The quota gate checks key daily, key monthly, project daily, project
   monthly, then the account's daily and monthly quota. The first failure is
   reported, so the error names the narrowest scope that ran out. The check
   uses a reservation estimate priced from the prompt and the allowed output
   at the model's prices (`quota.estimate_request_cost`, capped at
   `MAX_RESERVED_REQUEST_COST_USD`), with a flat minimum of
   `ESTIMATED_REQUEST_COST_USD`.
5. Settlement adds the cost to the account, key and project counters. The key
   counter write is best effort: a failure is logged and under-counts the key,
   and the account quota remains the hard cap.
6. The log row records `key_id` and `project_id`.

A key's limits apply exactly when the request is attributed to that key:

- Requests made with the key (chat completions, messages, embeddings) and
  batches, which run under the key that created them. When a key's budget
  refuses a batch, only that key's batches pause (reason `key_budget`) and are
  checked again within an hour, so a raised budget takes effect. The
  account's other keys keep running.
- Requests sent with `X-On-Behalf-Of` and agent grants are attributed to no
  key. They are charged to the named end user, and grants resolve their
  project from the account's key.

A key can overspend by what its in-flight requests cost, at most the
concurrency cap times one request's estimate. Rotation keeps the key row and
id (new secret, same name, limits, spend and history), so the old secret stops
working at the commit. Rotation with a grace period was dropped on purpose: a
user regenerates mostly after a suspected leak. To switch without downtime,
create a new key and delete the old one.

## 4. Errors

A refused budget keeps the existing 429 body and headers and adds `code:
"budget_exceeded"`, `scope` (`key`, `project` or `account`), `window` (`day` or
`month`), `quota_usd`, `spent_usd`, `remaining_usd`, `reset_at`, `retry_after`
and a message naming the scope. A key refusal also carries `key_prefix`.

A model outside a key's or project's allowlist is answered exactly like an
unknown model (`404 model_not_found` on OpenAI routes, the equivalent on the
Anthropic route), and the key's own `/v1/models` omits it. A leaked limited key
therefore learns nothing about the models the account can reach. The server
logs the reason (`outside_key`, `outside_project`).

## 5. API

Console, session auth. Keys are addressed by `key_prefix`, projects by id. Every
route answers 404, not 403, for another account's object, and writes are
audited.

| Route | Behavior |
|---|---|
| `POST /user/api-keys` | Creates an additional key `{name?, project_id?}`, in the Default project when none is given. 409 only at the safety ceiling |
| `GET /user/api-keys/all` | Lists keys with name, project, limits and spend today and this month |
| `PATCH /user/api-keys/{key_prefix}` | Name, `budget_daily_usd`, `budget_monthly_usd`, `allowed_models`. An omitted field is unchanged, `null` clears it |
| `POST /user/api-keys/{key_prefix}/regenerate` | Rotates that key in place |
| `POST /user/api-keys/{key_prefix}/reveal` | Shows one key's full value, audited |
| `DELETE /user/api-keys/{key_prefix}` | Revokes an active key, removes a revoked one |
| `GET/POST /user/projects`, `GET/PATCH /user/projects/{id}`, `POST /user/projects/{id}/archive` | Project CRUD with budgets, concurrency cap and allowlist |
| `GET /v1/key` (API key auth) | Describes the calling key: project, models, budgets, spend, effective concurrency cap |
| `GET /v1/models` | Filtered to the effective allowlist |

Limit validation on write:

- A budget is at least 0, at most 1,000,000, with at most four decimals. A
  budget of 0 pauses the key without revoking it.
- A budget is not compared with its parents when written. The narrower one
  applies at run time, and the console shows the account quota beside the key
  budget so a moot budget is visible.
- An allowlist holds canonical model ids (aliases are resolved when written).
  An id the account cannot use, or an empty list, is rejected with 422. `null`
  means all models.

The legacy `POST /user/api-keys/regenerate` rotates the only active key and
answers 409 when the account has several. `GET /user/api-keys` returns the
newest active key.

The console has a key detail page (`/dashboard/keys/{key_prefix}`) with spend
against budgets and the limits form, and a project list and editor.

## 6. Concurrency of writes

Row locks alone cannot keep the account quota identical on every key. Under
READ COMMITTED, an `UPDATE api_keys WHERE user_id = $1` that waited on a row
lock re-checks only the rows in its snapshot, so a key inserted meanwhile keeps
its old quota or status. So every write that adds a key or changes all of an
account's keys (create, rotate, admin update and revoke, suspension, deletion)
takes a transaction-scoped advisory lock on the account first. A write that
spans accounts (the per-role quota bulk apply) takes one all-accounts lock
exclusively, and per-account writers hold it in shared mode before the account
lock, so the two cannot deadlock. A single global lock is used because one lock
per account could exhaust the shared lock table. The legacy regenerate picks its
"only key" inside the same locked transaction.
`tests/integration/storage/test_postgres_api_keys.py` checks each interleaving.

## 7. Open points

- **402 or 429 for an exhausted budget.** SDKs retry 429 automatically, which
  is pointless until the window resets. Switching to 402 would break clients
  that match on 429.
- **Default limits of a new project.** It inherits from the account today
  (NULL). Should it start with explicit values?
- **Running batches when a key is revoked.** Batches store the key's hash, so a
  regenerated key's unfinished batches fail. Binding batches to the key id
  would fix that, since the id now survives rotation.
- **Key expiry.** Several store checks treat `status = 'active'` as live and
  ignore `expires_at`: the key ceiling count, the legacy regenerate's "only
  key" check, the listing and `get_active_key_by_account`. The auth lookup does
  check expiry. Only an administrator can set `expires_at`. If expiry becomes
  a product feature, those checks need `status = 'active' AND (expires_at IS
  NULL OR expires_at > NOW())`.

## 8. P3: teams and project policies (draft)

Not built. Intended scope:

- **Teams.** A team owns projects next to accounts, with members, invitations
  and roles. Departing members' keys are revoked and the team keeps its
  projects. The requirements are to be set when this is picked up.
- **Project policies.** `projects.settings` already exists. Two fields are
  planned:
  - `allow_external_providers` (default true). False restricts routing to
    `self_hosted` routes, so prompts never leave owned GPUs. It filters
    candidates on the route's `deployment_type` before the router (Fixed or
    RouteWise) chooses, and RouteWise must receive the filtered set. If no
    candidate is left, the request fails as a model that is not available.
  - `store_request_content`: NULL uses the deployment default, otherwise it
    maps onto the request log's existing switch for storing full content.
- **Grants scoped to a project**, so an agent grant can name the project it
  charges.
- **Budget alerts** through the existing webhook and notification sinks.
- **Per-key concurrency caps and RPM/TPM limits**, if usage shows they are
  needed. Concurrency caps exist today at account and project level only.
- **Per-project routing policy.**

Rejected for now: a reservation ledger for in-flight cost (the concurrency cap
bounds overshoot), Redis for counters (every limiter and cache is in-process;
adopt it everywhere at once), and backfilling `api_logs` attribution.
