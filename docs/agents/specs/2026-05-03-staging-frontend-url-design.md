# Staging Approval Email Uses Production URL

**Date:** 2026-05-03
**Status:** Approved (Option A)
**Type:** Bug fix / config

## Problem

When a user signs up on https://staging.freeinference.org and an admin approves the registration, the approval email contains a **Log In** button that points to `https://freeinference.org/login` (production) instead of `https://staging.freeinference.org/login` (staging).

The same issue affects every email link helper in `serving/utils/email.py`:

| Helper | Link target | Source |
|---|---|---|
| `send_verification_email` | `{frontend_url}/verify-email` | `serving/utils/email.py:120` |
| `send_password_reset_email` | `{frontend_url}/reset-password` | `serving/utils/email.py:204` |
| `send_approval_email` | `{frontend_url}/login` | `serving/utils/email.py:261` |
| `send_new_registration_admin_email` | `{frontend_url}/dashboard/admin` | `serving/utils/email.py:364` |

All use `settings.frontend_url`, which defaults to `"https://freeinference.org"` (`serving/config/settings.py:94`). Pydantic reads it from the `FRONTEND_URL` env var.

## Root cause

Two issues:

1. The staging server's `.env` does not set `FRONTEND_URL`, so the backend falls back to the production default.
2. `.env.example` does not document `FRONTEND_URL` (or `BASE_URL`), so a deployer setting up a non-production environment has no signal that they need to override it.

## Decision

Option A — **config-only fix**. Keep the existing decoupled design (`base_url` = backend origin, `frontend_url` = where users click links). Make the env var discoverable.

Rejected alternatives:

- **Option B (derive frontend URL from request `Host`/`Origin`):** Couples backend and frontend hosts. The current design separates them on purpose so the backend can be served from a different hostname than the SPA. Auto-derivation would silently break that separation if it ever happens.
- **Option C (B + audit `base_url` plumbing):** Out of scope — verification/reset emails are not in the bug report and are not currently broken on staging in a user-visible way.

## Code change

Add to `.env.example`, near the existing SMTP / public URL section:

```bash
# Public URL of the frontend (used in email links: verify, reset, approval).
# Set this to the user-facing origin for the environment, e.g.:
#   FRONTEND_URL=https://staging.freeinference.org
FRONTEND_URL=https://freeinference.org

# Public URL of the backend API (used for validation in email helpers).
BASE_URL=https://freeinference.org
```

Implementation outcome: `BASE_URL` was not already documented in `.env.example`, so both `FRONTEND_URL` and `BASE_URL` were added.

## Deployment change (manual, by repo owner)

On the staging host:

```bash
# Edit /srv/hybridInference/.env, add or update:
FRONTEND_URL=https://staging.freeinference.org

# Rebuild and restart the backend container:
cd /srv/hybridInference
make build
```

The backend reads `.env` via `env_file: ../../.env` in `deploy/docker/docker-compose.yml`, so a container restart is required for the new value to take effect.

## Verification

After deploy:

1. Sign up a new user on https://staging.freeinference.org.
2. Approve the user from the staging admin panel.
3. Confirm the approval email's **Log In** button URL is `https://staging.freeinference.org/login`, not the production URL.

## Out of scope

- Refactoring `send_verification_email` / `send_password_reset_email` to actually use the `base_url` argument they already accept (the argument is currently used only for validation/logging — the link still uses `settings.frontend_url`). The current behavior is intentional per the docstrings.
- Adding a startup warning when `FRONTEND_URL` is unset / equals the production default in a non-production environment. Could be considered later.

## Files touched

- `.env.example` — add `FRONTEND_URL` (and possibly `BASE_URL`) with comment.

No Python code changes.
