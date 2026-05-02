# Unify User Tier and Role; Remove Enterprise

**Date:** 2026-05-02
**Status:** Design approved, awaiting review

## Goal

Eliminate the duplicate `api_keys.tier` concept. Make `users.role` the single source of truth for billing/access classification. Remove the unused `enterprise` value everywhere.

## Background

The codebase currently has two parallel concepts:

- **`api_keys.tier`** (`free|pro|enterprise`) — billing metadata per API key. Never gates access. Set via admin endpoints, returned in some user/admin responses.
- **`users.role`** (`free|pro|internal|admin`) — access control per user. Drives every permission check, concurrency limit, and model gate.

The two share `free` and `pro`. `enterprise` exists only as a Pydantic regex pattern and is never consumed by any gating logic. Per-key tier override (a key with a tier different from its owner's role) has no observed use.

## Approach

**Drop `api_keys.tier` entirely. Use `users.role` as the single classification field.**

This collapses the duplicate concept, removes one column, drops `enterprise`, and matches actual codepath usage (gating already only reads role).

## Backend changes

### DB migration (`serving/storage/database.py`)

- Drop `tier` column from `api_keys` table. SQLite ≥3.35 supports `ALTER TABLE api_keys DROP COLUMN tier`. Add a migration step gated by column-existence check (similar pattern to existing role-column migrations around lines 518–625).
- No change to `users.role` CHECK constraint (already `free|pro|internal|admin`).

### Schemas (`serving/schemas_admin.py`, `serving/schemas_auth.py`)

Remove the `tier` field from:

- `ApiKeyCreate`, `ApiKeyUpdate`, `ApiKey` (admin-facing API key models)
- `AdminApiKey`
- `AdminUser.key_tier`
- `LoginResponse.tier`

Drop all `^(free|pro|enterprise)$` regex patterns. The role pattern stays.

### Routes

- **`serving/servers/routers/auth_routes.py`** (login, google auth): stop populating `tier` in responses.
- **`serving/servers/routers/admin.py`**: remove `tier` from API key create/update payloads and responses; remove the tier-filter docstring at line 288.
- **`serving/servers/routers/user_routes.py`**: remove `tier` from any user-facing API key views.

### Config (`serving/config/settings.py`)

- Rename env-backed setting `signup_default_tier` → `signup_default_role`. New signups get this value written to `users.role`.
- Default value: `'free'`. Validate against `VALID_ROLES`.
- Update the signup code path to call the new name.

### Unrelated, leave alone

- **`serving/adapters/claude_token.py:82`** — comment about Claude API's `plan` field that may be `"enterprise"`. This is an external upstream API value, not our tier, not our concern.

## Frontend changes

### Types

- **`frontend/src/lib/api/auth.ts`**: drop `tier` from `LoginResponse`.
- **`frontend/src/lib/api/admin.ts`**: drop `tier` from `AdminApiKey`, drop `key_tier` from `AdminUser`.

### AuthProvider (`frontend/src/components/providers/AuthProvider.tsx`)

- Drop `tier` from `User`.
- Fix existing bug: `ROLE_RANK` is missing `pro`. Update to `{ free: 0, pro: 1, internal: 2, admin: 3 }` to match backend `serving/config/settings.py:172`.

### Admin UI (`frontend/src/app/dashboard/admin/page.tsx`)

- Remove tier column and tier dropdown from API key create/edit forms.
- Remove `enterprise` option from any user-role dropdown (line ~1197).
- Where the UI displays per-key tier, replace with the owning user's role (or just drop the column).

## Tests

- **`test/servers/test_admin_api.py:221`**: drop `tier="enterprise"` cases; remove tier from API key create/update test payloads.
- **`test/integration/test_database_integration.py`**: drop tier fixtures from API key creation paths.
- Verify any other tests that assert on `tier` in API responses; update them.

## Models config

`config/models.yaml` uses `required_role`, not tier. No change.

## Out of scope

- Concurrency limits (`serving/servers/concurrency.py`) — already keyed on role.
- Email broadcasts `email_broadcasts.target_roles` — already role-based.
- Per-key tier override — no evidence of use; gone with the column.
- Renaming `users.role` to anything else — keep the name.

## Migration / rollout notes

- The DB migration is destructive (drops a column). Once deployed, old API responses that included `tier` will no longer have it; clients must already tolerate missing fields, or the frontend types must be updated in lockstep with the backend deploy. Since backend and frontend ship from the same repo and the same deploy, ship together.
- No data backfill needed — `api_keys.tier` was never read for gating, so dropping it is silent for behavior.

## Workflow (per CLAUDE.md)

1. `git pull origin/dev`
2. Create worktree on branch `jason/claude/unify-tier-role`
3. Subagent executes the plan
4. Review the diff; fix issues
5. `uv run ruff format --check .`
6. Open PR to `dev`; return link

## Success criteria

- `api_keys.tier` column gone; no code reads or writes it.
- No occurrence of the literal string `enterprise` in `serving/`, `frontend/`, or `test/` (except the unrelated `claude_token.py` comment).
- All tests pass.
- Login, signup, admin user/key management, and per-role gating all still work end-to-end against staging.
- Frontend `ROLE_RANK` matches backend `ROLE_RANK`.
