# Audit attribution: actor, source, target (2026-10-25)

Status: implemented. Code: `apps/backend/serving/audit.py` (contract),
`apps/backend/serving/storage/audit_schema.py` (table, migration, insert).

## Problem

`admin_audit_log` had two overloaded columns and no actor:

- `admin_ip` held a client IP for some writers and the acting account's email
  or login name for others (writers passed `verify_admin_access`'s display
  string). Some writers put the actor into `details.admin_id`; others, such
  as the model concurrency toggle, kept only the address.
- `target_user_id` was a user id for most writers, but the provider toggle
  stored a provider name there and the RouteWise model settings a model id.
  The user filter and the hard-delete erasure read it as a user id.
- `delete_user` received the admin and dropped it from its row.

## Contract

Five nullable `TEXT` columns, added without a default:

| Column | Meaning |
|---|---|
| `actor_type` | `admin` (account signed in with a session, on an admin route), `admin_token` (the shared `ADMIN_TOKEN`), `user` (an account acting on itself), `anonymous` (registration), `bootstrap` (first-run setup), `system` |
| `actor_id` | The acting account's stable user id, for `admin` and `user` only. Never an email or login name. No foreign key: deleting an account neither cascades into nor is blocked by what it did. |
| `source_ip` | The client address `serving.utils.request_ip` trusted, or NULL when it had none. |
| `target_type`, `target_id` | The object acted on: `user`, `model`, `provider`, `provider_key`, `setting`, `config`, `alert_type`, `auth_block`, `signup_domain`, `site_update`, `broadcast`. |

Rules for new rows:

- `admin_ip` mirrors `source_ip` (`"unknown"` when NULL), so it means an
  address on every new row.
- `target_user_id` is set only for a `user` target. A row about an account's
  API key targets that account, so the user filter and erasure keep reaching
  it. A model's routes, weights, offload policy and RouteWise settings target
  the model; the route stays in `details`.
- The actor is built once per request, as a frozen `AuditActor`:
  `verify_admin_access` stores it on `request.state` (its return value is
  unchanged) and routes read it with `get_admin_audit_actor`, which answers
  500 rather than inventing one when nothing recorded it. Self-service routes
  use `user_audit_actor`. Nothing is process-wide state. Deferred work (an
  export stream, a post-commit callback) closes over the value resolved
  before it runs.
- Storage methods that audit inside their own transaction (`create_first_admin`,
  `delete_user`, `resume_user`, `hard_delete_user`) take `actor=`.

## Legacy rows and callers

- Old rows are not backfilled or re-typed: the attribution columns read NULL
  ("not recorded"), `admin_ip` and `target_user_id` are returned as stored,
  and readers must not guess from what the values look like.
- `log_admin_action(store, admin_ip, action, target_user_id, ...)` still works
  and writes the same kind of legacy row. Passing a legacy argument together
  with `actor` / `target` raises `ValueError`.
- `details.admin_id` and `details.actor`, where writers already put them, are
  kept as context.

## Migration

`ensure_admin_audit_log_schema` is shared by both schema builders. It is
catalog-gated: a settled database issues no DDL and takes no table lock. On
an upgrade the `ADD COLUMN` statements run under the bounded lock wait, and
rewrite no rows. The columns are required: `PostgresOperationalStore` turns a
lock timeout into `RequiredOperationalSchemaUnavailable`, and startup stops
rather than serve writes that would fail or lose their actor. There is no
old-format fallback.

## Writer inventory

| Writer | Actor | Target |
|---|---|---|
| admin users: approve, reject, update | admin / admin_token | user |
| admin users: delete, resume, hard delete (in-transaction) | admin / admin_token | user |
| admin API keys: create, update, revoke, hard delete | admin / admin_token | user (key owner) |
| models: concurrency, visibility, batch concurrency | admin / admin_token | model |
| routing weights, offload routes, provider routes (strategy, model, candidate, capabilities, state, delete, restore) | admin / admin_token | model |
| RouteWise model settings; probe run | admin / admin_token | model (canonical id) |
| RouteWise global settings; runtime settings | admin / admin_token | setting |
| app configuration update/reset; configuration backup/restore | admin / admin_token | config |
| provider disable/enable; provider definitions | admin / admin_token | provider |
| provider keys (DB and env) | admin / admin_token | provider_key |
| alert mute/unmute | admin / admin_token | alert_type |
| alert test, snooze; quota role apply; login-event purge; clear error requests; export; system restart; usage-insights settings | admin / admin_token | none |
| usage insights samples/analyze | admin / admin_token | user when scoped |
| auth block clear | admin / admin_token | auth_block |
| signup domain add/remove; site updates; broadcasts | admin / admin_token | signup_domain / site_update / broadcast |
| self-service: API key create/revoke/delete/regenerate, email and phone binding, account deletion | user | user (self) |
| email and phone registration | anonymous | user (new account) |
| first-run setup | bootstrap | user (new administrator) |

No background (`system`) writer exists today; the kind is reserved for one.

## Not in scope

Resolving `actor_id` to a current display name (the stable id is shown),
new audit events, and changes to authentication or routing.
