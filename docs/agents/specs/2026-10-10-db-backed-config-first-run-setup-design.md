# Database-Backed Configuration and First-Run Setup

**Date:** 2026-10-10
**Status:** Approved for implementation

## Problem

Almost every gateway setting is an environment variable: provider API keys,
SMTP, Slack webhooks, JWT and API-key secrets, proxy trust, routing knobs. A new
deployment has to fill in a long `.env` before anything works, an operator has
to edit that file and recreate the container to change anything, and the first
administrator is whoever signs up with an address listed in `ADMIN_EMAILS`.

We want:

1. Application configuration stored in Postgres and edited from the admin
   console, not from `.env`.
2. A first-run flow: on a fresh deployment the console opens a setup page, the
   operator creates an administrator account **without an email address**, then
   fills in the settings the deployment needs.
3. When a setting the deployment requires is missing (for example, a release
   adds a new required setting), signed-in users see a warning to contact the
   administrator, and administrators see what to fix.

## Decisions

| Question | Decision |
|---|---|
| Where secrets live | **Plaintext in Postgres.** `JWT_SECRET_KEY` and `API_KEY_SECRET` are generated into the database on first boot. The environment keeps only the database connection and deployment wiring (below). The admin API never returns a secret's value. |
| Who may create the first admin | Whoever presents the **one-time setup code** that the backend prints in its startup log. |
| Settings read only at startup | The console marks them *restart required* and offers a **Restart backend** button that exits the process so Docker (`restart: unless-stopped`) or systemd (`Restart=on-failure`) starts it again. |
| Precedence | **Database row → environment → built-in default.** A database row wins even when its value is empty. |
| Existing deployments | On boot, every registered setting that has a non-empty environment value and no database row is **imported** into the database. Nothing is lost on upgrade, and the variable can then be deleted from `.env`. |
| Database-free mode | `DB_ENABLED=false` (the example router distribution) keeps today's behavior: environment and defaults only, no setup flow. |

Security consequence of the first row, accepted by the owner: anyone who can
read the database (a dump, a backup, pgAdmin) can read provider keys, decrypt
users' stored API keys (they are Fernet-encrypted with a key derived from
`API_KEY_SECRET`) and mint admin JWTs. Provider keys in `provider_api_keys` are
already stored this way.

## What stays in the environment

Only values needed before the database is reachable, or that describe the
container/network topology rather than the application:

- **Database connection:** `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`,
  `DB_PASSWORD`, `DB_ENABLED`.
- **Config file locations** (container paths set by distribution overlays):
  `MODELS_CONFIG_PATH`/`MODELS_CONFIG`, `ROUTING_CONFIG_PATH`/`ROUTING_CONFIG`,
  `ALERTS_CONFIG_PATH`, `DISTRIBUTION_CONFIG_PATH`, `DISTRIBUTION_CONFIG_MODE`.
- **Process wiring:** `LOG_FILE`, `BACKEND_EXTENSIONS` (imports code at boot),
  `GEOIP_COUNTRY_DB`, `GEOIP_COUNTRY_PROVIDER`, `WEB_CONCURRENCY`,
  `UVICORN_WORKERS`, `GUNICORN_WORKERS`, `REFRESH_TOKEN_COOKIE_NAME` (the console
  container reads the same name).
- **Compose/infra only** (never read by the app): `BACKEND_HOST`,
  `BACKEND_PORT`, `FRONTEND_HOST`, `FRONTEND_PORT`, `PGADMIN_*`, `BUILD_SHA`,
  `BUILD_TIMESTAMP`, `BACKEND_ENV_FILE`, `COMPOSE_PROFILES`,
  `AGENT_NETWORK_NAME`, `BACKEND_IMAGE`, `COMPOSE_PROJECT_NAME`.
- **Console (Next.js):** build-time `NEXT_PUBLIC_*`, `BACKEND_INTERNAL_URL`,
  `SITE_UI_DIR`, `SITE_UI_API`; container runtime `AGENT_WEB_INTERNAL_URL`,
  `AGENT_CONTROL_PLANE_INTERNAL_URL`, `AGENT_PUBLIC_URL`, `SITE_ASSETS_DIR`,
  `PGADMIN_INTERNAL_URL`.

Everything else the backend reads moves to the database, including
`LOG_LEVEL`/`LOG_FORMAT` (re-applied once the database values load), CORS
origins (now evaluated per request), the routing/stream tuning constants that
were frozen at import, RAG, identity, Slack, SMTP and proxy trust.

`ERASURE_FENCE_SECRET` moves too. It still falls back to `API_KEY_SECRET` when
unset, and both are **immutable once set** (the console refuses to change
them): changing `API_KEY_SECRET` invalidates every user API key, and changing
the fence secret re-opens erased accounts' logs.

## Data model

### `app_config`

```sql
CREATE TABLE IF NOT EXISTS app_config (
    key         TEXT PRIMARY KEY,          -- canonical env-var name, e.g. SMTP_PASSWORD
    value       TEXT NOT NULL,             -- raw string, same format as the env var
    secret      BOOLEAN NOT NULL DEFAULT FALSE,
    source      TEXT NOT NULL DEFAULT 'admin',  -- admin | setup | env_import | generated
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by  TEXT
);
```

Values are stored in their environment-variable string form (`true`, `8`,
`a,b,c`), so the import is lossless and one coercion path serves both sources.

### `users`

- `email` becomes nullable (catalog-gated `ALTER ... DROP NOT NULL`, following
  the role-constraint migration pattern). Postgres `UNIQUE` admits several
  `NULL`s.
- New column `login_name TEXT`, unique case-insensitively:
  `CREATE UNIQUE INDEX IF NOT EXISTS idx_users_login_name ON users (lower(login_name)) WHERE login_name IS NOT NULL`.
  It is deliberately not called `username`: the existing `user_name` column is
  the free-form display name, and two columns one underscore apart invite bugs.
- Both copies of the users DDL (`storage/database.py` and
  `storage/postgres_operational.py`) change so a fresh database matches.

### Setup marker

`site_settings` row `setup_completed_at` (ISO timestamp). Once written it is
never cleared by the application.

## Configuration registry

`apps/backend/serving/config/app_config.py` (plus helper modules as needed)
holds one entry per setting:

| Field | Meaning |
|---|---|
| `key` | Canonical env-var name (`SMTP_PASSWORD`). Legacy aliases (`MODELS_CONFIG`) are not keys. |
| `category` | One of `general`, `security`, `signup`, `email`, `providers`, `routing`, `network`, `alerts`, `integrations`, `privacy`. |
| `description` | Operator-facing text (taken from the comments in `settings.py`/`.env.example`). |
| `type` | `str`, `text` (multi-line, e.g. PEM), `int`, `float`, `bool`, `list` (comma-separated string). |
| `field` | The `Settings` attribute it overlays, or `None` for values read through `config_value()`. |
| `secret` | Never returned by the API; audit entries record only that it changed. |
| `required` | `False`, `True`, or a predicate over the effective configuration. |
| `restart_required` | The value is captured at startup, so a change applies only after a restart. |
| `setup` | Shown on the first-run configuration step. |
| `immutable` | Cannot be changed once set (`API_KEY_SECRET`, `ERASURE_FENCE_SECRET`). |
| `generated` | Generated on first boot when neither the database nor the environment has it (`JWT_SECRET_KEY`, `API_KEY_SECRET`). |

Entries come from four places:

1. **Static** — every `Settings` field and every scattered `os.getenv` the
   backend performs today, except the environment-only set above and the
   keys already managed by `RUNTIME_SETTINGS_REGISTRY` (those stay in the
   Settings tab; see *Runtime settings* below). A unit test fails when a
   `Settings` field is neither registered, environment-only, a runtime
   setting, nor in an explicit "unregistered" list with a reason.
2. **Discovered** — every `${VAR}` / `${VAR:-default}` reference in the active
   `models.yaml`, `routing.yaml` and `alerts.yaml`. Category `providers`
   (`alerts` for the alerts file). Secret when the name contains `KEY`,
   `TOKEN`, `SECRET`, `PASSWORD`, `WEBHOOK` or `PRIVATE`. Required when a
   models.yaml route that is not `optional: true` references it. Each carries
   `used_by` (model ids).
3. **Numbered provider keys** — `<BASE>2` … `<BASE>20` for the providers in
   `dynamic_keys._PROVIDER_ENV_KEY_VARS`, listed when present in the database or
   environment.
4. **Custom** — any `^[A-Z][A-Z0-9_]{1,127}$` name an administrator adds, for a
   models.yaml reference the registry cannot see. Never an environment-only
   name.

### Effective value and the overlay

- `config_value(key, default=None) -> str | None` is the one synchronous
  resolver: database row (if the service is initialized), then `os.environ`,
  then `default`. Every former `os.getenv` of a registered key, the four
  `${VAR}` expanders (`servers/registry.py`, `routing/config.py`,
  `observability/alert_config.py`, `admin/provider_definitions.py`) and
  `dynamic_keys` numbered scanning go through it.
- `Settings`-backed entries are overlaid **in place** on the existing
  `get_settings()` object (nine modules hold a reference to it): build a
  candidate `Settings` with the database values as init kwargs so every
  validator and derived field (`trusted_*_parsed`) recomputes, then copy the
  candidate's field values onto the live object. A database value that fails
  validation is skipped (logged, reported as `invalid` in the API) rather than
  stopping the gateway.
- Module-level constants that were read at import (routing affinity/prefill,
  Anthropic stream idle limits, upstream completion timeout, the timeout
  middleware, admin provider base URLs) become lazy reads, so they honor the
  database and, where read per call, apply live.
- A background loop re-reads `app_config` every 10 s and re-applies changes,
  so several processes converge. Admin writes also apply immediately in the
  process that served them.

### Boot sequence

1. `load_dotenv()`, `setup_logging()` (environment level/format), extensions.
2. **Config load** (only when the database is enabled), with the same 3×
   retry as the database logger, on a short-lived connection:
   create `app_config`; import environment values for registered keys with no
   row (`source='env_import'`); generate missing generated secrets; read all
   rows; overlay. Generation of `API_KEY_SECRET` is refused — startup fails
   with an explicit message — when the `api_keys` table exists and has rows:
   a new secret would silently invalidate every key. Unreachable database →
   continue on environment values, as today.

   An environment value that fails validation on its own (a field-level
   error) does not stop `get_settings()` before the database is read: that
   setting starts at its default, is listed by `invalid_environment()`, and is
   not imported. After the load, `require_valid_environment()` lets startup
   continue only when a valid stored value for it is in force, and otherwise
   fails naming the variable. The `DB_*` connection settings still fail at
   once.

   A rule between settings (`TRUST_X_REAL_IP` requires `TRUST_PROXY_HEADERS`,
   say) is not judged by `get_settings()` either: a stored value may replace
   either side. `require_valid_environment()` judges every rule against the
   configuration in force, stored values over the environment, and refuses
   startup if it still breaks one. So removing an environment variable whose
   value the database already holds does not stop the gateway. The import
   judges rules the same way: a value whose rule a stored value satisfies is
   imported (importing changes nothing in force), and neither side of a rule
   the configuration in force breaks is: which one is wrong is the operator's
   call. One function (`app_config._in_force`) computes the configuration in
   force for the overlay, the import and every write; a stored value that
   would break a rule holding without it is refused, so a rule the result
   still breaks is one only the environment breaks.
3. `validate_auth_secrets` (moved here from the lifespan, so it sees database
   values), re-apply `LOG_LEVEL`/`LOG_FORMAT`.
4. Database logger (now receiving the database-backed
   `DB_STORE_FULL_CONTENT` and fence secret), model registry with the resolver,
   and the rest of bootstrap as today.
5. After the operational store exists: import runtime-setting environment
   values (below), then `init_setup_state()`, then compute configuration health.

### Missing configuration ("health")

`missing` = registered or discovered entries whose `required` evaluates true
and whose effective value is empty. Initial required entries:

- `JWT_SECRET_KEY`, `API_KEY_SECRET` (normally satisfied by generation).
- `SMTP_USER` and `SMTP_PASSWORD` while public signup is enabled **and** email
  verification is required (runtime settings), since otherwise new users can
  never verify.
- Every discovered variable referenced by a non-optional models.yaml route.

A release that adds a required setting adds a registry entry with
`required=True`; deployments without it immediately report it as missing.

`pending_restart` = `restart_required` entries whose effective value differs
from the value the process booted with.

`get_config_health() -> ConfigHealth(missing, pending_restart)` is synchronous
and cached; it is recomputed after load, after every write, and by the refresh
loop.

A request for a model that was skipped at load because its credential was
missing keeps its 404. When an administrator makes it with their own
credentials, the message says the model is unavailable because the
deployment's configuration is incomplete; everyone else, a grant-scoped caller
included, gets the plain "not found" an unknown model gets. A skipped model
never reaches the role, visibility and grant gates, so a different message
would let any caller list models defined beyond what it may reach.

### Runtime settings

The 22 keys in `RUNTIME_SETTINGS_REGISTRY` already live in `site_settings`.
Two changes:

- **Environment import:** on boot, a key with no `site_settings` row and a
  non-empty environment variable of the upper-cased name is written to
  `site_settings` (`updated_by='env-import'`), so those variables can also be
  deleted from `.env`.
- **Bug fix:** `RuntimeSettings.get_cached` returned nothing once its 30 s TTL
  lapsed and nothing re-populated it, so `is_user_auth_enabled()` silently fell
  back to the environment value 30 s after boot and right after an admin edit
  (fail-open when the environment says auth is off). Add a refresh loop that
  reloads every registry key every 10 s with one query, and let `get_cached`
  return the last loaded value instead of expiring it.

## First-run setup

`setup_required` ⇔ the database is enabled **and** `setup_completed_at` is
absent **and** the `users` table is empty. On boot, a deployment that already
has users gets the marker immediately, so an upgrade never shows the setup
page (even one that has no admin and relies on `ADMIN_TOKEN`).

While setup is required:

- The first process to boot generates a setup code (12 characters from an
  unambiguous alphabet, shown as `XXXX-XXXX-XXXX`) and stores it in
  `site_settings` (`setup_code`), so every worker accepts the same code and a
  restart keeps it; setup deletes it. Each process keeps only its hash in
  memory and logs at WARNING:
  `First-run setup is pending. Open <console>/setup and enter setup code XXXX-XXXX-XXXX`.
- `POST /auth/signup` answers `503 {"detail": "This deployment has not been set up yet."}`
  so a stranger cannot create the first account (which would also complete
  setup by the rule above).
- The console redirects every route except `/setup` to `/setup`.

Creating the admin runs in one transaction under
`pg_advisory_xact_lock(hashtext('hybridinference:first-run-setup'))`, re-checks
that `users` is empty and the marker absent, inserts the user
(`email NULL`, `login_name`, Argon2 hash, `role='admin'`, `status='active'`,
`email_verified=TRUE`, `user_name` = display name or login name), writes the
marker, and audits `setup.admin_created`. Attempts are rate limited per IP
(10 per 15 minutes) and the code is compared in constant time.

### Email-less accounts

Only the setup admin has `login_name` and no email; signup is unchanged.

- `POST /auth/login` keeps its body shape; `email` becomes a plain string. A
  value containing `@` is an email (validated as today); otherwise it is a
  login name (`^[a-z0-9][a-z0-9_.-]{2,31}$`, case-insensitive).
- JWT `email` claim is `""` for such users; `get_current_user` stops requiring
  a non-empty claim (it already reads the email from the database).
- Admin identity strings (`admin_id`, `updated_by`, audit `admin`) are the
  email, else `login_name`.
- Every `email: str` response model that can carry such a user becomes
  `str | None`, and user-facing ones gain `login_name: str | None`.
- Nothing is mailed to a user without an email (broadcast recipients exclude
  them).
- Recovery: there is no email reset for this account, so ship a CLI that uses
  only the database environment variables:
  `python -m serving.auth.reset_password <login-name-or-email>` (prompts for,
  or with `--generate` prints, a new password and revokes the user's sessions).

## HTTP API

All bodies are JSON. Admin endpoints use `verify_admin_access`.

### `GET /auth/setup/status` (public)

```json
{"setup_required": true, "database_enabled": true}
```

### `POST /auth/setup/admin` (public)

Request:

```json
{"setup_code": "ABCD-EFGH-JKLM", "login_name": "admin", "password": "…", "display_name": "Ops"}
```

`display_name` is optional. The password follows the signup strength rules.
Success is `200` with the same body as `POST /auth/login` (`LoginResponse`) and
the same refresh cookie, so the browser is signed in. Errors: `409` setup
already complete, `403` wrong code, `422` invalid input, `429` rate limited,
`503` database unavailable.

### `GET /admin/config`

```json
{
  "categories": [{"id": "email", "label": "Email (SMTP)", "description": "…"}],
  "entries": [
    {
      "key": "SMTP_PASSWORD",
      "category": "email",
      "description": "…",
      "type": "str",
      "secret": true,
      "required": true,
      "missing": false,
      "is_set": true,
      "value": null,
      "default": null,
      "source": "database",
      "restart_required": false,
      "pending_restart": false,
      "environment_ignored": false,
      "immutable": false,
      "setup": true,
      "custom": false,
      "invalid": null,
      "used_by": [],
      "updated_at": "2026-10-10T12:00:00Z",
      "updated_by": "admin"
    }
  ],
  "missing": ["SMTP_PASSWORD"],
  "pending_restart": ["CORS_ALLOWED_ORIGINS"],
  "restart_supported": true
}
```

- `value` is the effective value in its typed form (`bool`, number, string;
  `list` as the comma-separated string) and is **always `null` for secrets**.
- `default` is `null` for secrets.
- `source`: `database` | `environment` | `default`.
- `environment_ignored`: the environment has a different non-empty value that
  the database row overrides.
- `invalid`: a validation message when the stored value could not be applied.

### `PATCH /admin/config`

Request `{"values": {"KEY": value, ...}, "secrets": {"CUSTOM_KEY": true}}`.
`value` is a JSON boolean for `bool`, a number for `int`/`float`, a string
otherwise. Validates the whole batch together (so cross-field rules such as
`TRUST_CLOUDFLARE_HEADERS` requiring `TRUST_PROXY_HEADERS` can be satisfied in
one save), writes it in one transaction, applies live entries, and returns the
full `GET /admin/config` body. The batch is judged by what it leaves in force,
compared with what is in force now: it is refused when a written value cannot
apply, when another stored value that applies now would not (its rule needs
the value being changed), or when it breaks a rule an environment value relies
on. What is already wrong never blocks an unrelated batch. `secrets` sets the secret flag for new custom
keys only. Errors: `400 {"detail": "KEY: reason"}`; `403` for an
environment-only name; `409` for an immutable key that is already set.

### `DELETE /admin/config/{key}`

Replaces the database row with a reset marker (`source='reset'`, no value):
the setting falls back to the environment, then its default, and keeps doing so
across restarts, since the marker keeps the boot-time import from storing the
environment value again. A generated secret replaces the marker only once the
environment no longer supplies one. Custom keys disappear. Returns the full
`GET /admin/config` body. `409` for immutable keys, and for a reset that the
batch rule above would refuse, or that would bring back an environment value
that is not valid on its own.

### `POST /admin/system/restart`

`202 {"restarting": true, "instance": "<id>"}` then, after the response is
sent, the process signals itself `SIGTERM`; uvicorn shuts down gracefully, and the process exits
with status 75 so systemd's `Restart=on-failure` also restarts it. A watchdog
forces the exit after 30 s. `409` when `restart_supported` is false: the
deployment has not declared `SUPERVISOR_RESTARTS_BACKEND=1` (set by the bundled
Compose file and systemd units beside their restart policy; `/.dockerenv` and
`INVOCATION_ID` do not show the policy), or several workers serve it
(`WEB_CONCURRENCY`/`UVICORN_WORKERS` above 1, or a worker `uvicorn --workers`
spawned). Audited. `instance` names the exiting process; `/health` reports
the answering process's `instance` too (random, new at every start), and the
console counts the restart done when a different one answers. Waiting to see
`/health` fail first misses a restart quicker than the one-second poll.

### `GET /site-config` additions (public, top level)

```json
"setup": {"required": false},
"configuration": {"incomplete": false}
```

Top-level keys pass through older consoles' parser. No setting names are
exposed publicly.

### Changed shapes

- `UserInfo` (login response, `/user/me`): `email: string | null`,
  new `login_name: string | null`.
- Admin user list/detail: `email: string | null`, `login_name: string | null`.

## Console

- **`/setup`**: step 1 *Create administrator* (setup code with a hint to read
  it from the backend log, username, password, confirm). Step 2 *Configure*:
  the `setup` and `missing` entries from `GET /admin/config`, grouped by
  category, plus the signup toggles (`signup_enabled`,
  `signup_require_email_verification`) via `PATCH /admin/settings/{key}`.
  Step 3 *Finish*: when anything is `pending_restart`, offer **Restart backend**
  (poll `/health` until a process other than the restarted `instance` answers),
  then a full-page navigation to the
  admin Configuration tab. Visiting `/setup` when setup is not required
  redirects to `/dashboard`.
- **Setup gate**: when site-config says `setup.required`, every route except
  `/setup` redirects there. After setup the console navigates with a full page
  load so the server-rendered site-config is fetched again.
- **Admin → Configuration tab** (`/dashboard/admin/configuration`): all
  entries grouped by category with search and a *missing only* filter;
  badges for Required, Missing, Secret, Restart required, Pending restart,
  From environment, Environment ignored, Invalid; editors by type (toggle,
  number, input, textarea); secrets are write-only (placeholder "Set" / "Not
  set", Replace, Clear); Reset (DELETE) reverts to environment/default; Add
  variable (name, value, secret); a pending-restart banner with a confirmed
  Restart button.
- **Configuration banner** (console chrome): when
  `configuration.incomplete`, administrators see "Required settings are
  missing" with a link to the Configuration tab; everyone else sees "This
  service is missing required configuration, so some features may not work.
  Please contact your administrator." Saving configuration calls
  `router.refresh()` so the banner updates.
- **Login** accepts "Email or username".
- User types treat `email` as optional and display `user_name || email ||
  login_name`.

## Non-goals

- Encrypting secrets at rest (declined above).
- Migrating `provider_api_keys` or route tables; the Provider Keys tab is
  unchanged (its "environment" keys are now resolved through the database
  first).
- Moving console (Next.js) build-time variables, which are inlined at build.
- Live reload of the model registry; credentials referenced by models.yaml
  are restart-required.
