# Database

The gateway keeps request logs, accounts, API keys, its settings and every
runtime admin change in PostgreSQL (asyncpg). This page covers the connection,
the schema, what is stored, backups and pgAdmin.

A gateway started with `DB_ENABLED=false` still routes requests, but has no
request history, accounts, API keys or stored settings, and reads every setting
from the environment. How `/health` tells that apart from a database outage is
in [Health checks](deployment.md#health-checks).

## Connection settings

The backend reads these from `.env` or the process environment, because it
needs them before it can read anything from the database:

| Variable | Default | Notes |
|---|---|---|
| `DB_ENABLED` | `true` | `false`, `0` or `no` disables the database entirely |
| `DB_HOST` | `localhost` | Compose sets `postgres` inside the backend container |
| `DB_PORT` | `5432` | Under Compose, the host side of the port mapping; the container always uses 5432 |
| `DB_NAME` | `hybridinference` | Required by Compose (`DB_NAME must be set in .env file`) |
| `DB_USER` | `postgres` | Required by Compose |
| `DB_PASSWORD` | *(empty)* | Required by Compose |

Three settings that govern what is kept are stored in the database, under
**Privacy** in **Admin → Settings → Site & Setup** (the two
`ERASURE_FENCE_*` settings under **Advanced**):

| Setting | Default | Notes |
|---|---|---|
| `DB_STORE_FULL_CONTENT` | `false` | Whether prompts and responses are stored; applies after a backend restart. See [Request logging and privacy](#request-logging-and-privacy) |
| `ERASURE_FENCE_SECRET` | *(empty)* | Key for the records that keep deleted accounts deleted; can never change once set. See [Secrets for deleting accounts](#secrets-for-deleting-accounts) |
| `ERASURE_FENCE_PROTOCOL_READY` | `false` | Turns on permanent account deletion |

Each gateway process holds at most 18 connections: a general pool of 10, a pool
of 4 reserved for the lookups a request makes before it is served (key checks,
sessions, quotas), and 4 for alert history. Size `max_connections` for 18 per
gateway process, plus your other tools. A request that cannot get a
request-pool connection within 3 seconds gets a 503 with `Retry-After`. The
pool sizes and timeouts are in `apps/backend/serving/storage/pools.py`.

The bundled stack runs `postgres:16.16` (`-E UTF8 --locale=C.UTF-8`) and
publishes it on `127.0.0.1:${DB_PORT:-5432}` only. Use an SSH tunnel to reach it
from another machine rather than widening the binding.

## How the schema is created

The migrations in `apps/backend/migrations/` create and change every table. A
deploy runs them with the image it is about to start, before it restarts the
gateway; [Applying schema migrations](deployment.md#applying-schema-migrations)
lists the step for each deployment method.

```bash
make migrate                    # the bundled Compose stack

# The same by hand, from the repository root:
docker compose -f deploy/docker/docker-compose.yml --env-file .env up -d --no-recreate --wait postgres
docker compose -f deploy/docker/docker-compose.yml --env-file .env run --rm --no-deps backend python -m migrations upgrade

uv run python -m migrations upgrade   # a source checkout, from the repository root
```

The command uses the gateway's `DB_*` settings and does nothing when
`DB_ENABLED` is off. `python -m migrations status` shows the database's revision
next to the code's.

The gateway issues no DDL. At startup it compares the database's revision with
its own: with no revision or an older one, it refuses to start and names the
command above; with a newer one (after a deploy restored the previous image), it
starts with a warning as long as that schema still supports its code.
`0001_baseline` brings a database that predates versioning up to the current
baseline without preparation.

Each statement waits at most 3 seconds for a table lock. A long reader such as a
nightly `pg_dump` makes the migration roll back and exit with status 75, listing
the sessions holding locks; the running gateway keeps serving, and you can run
the command again later. Avoid the backup window for a release that changes the
schema. Only one migration runs at a time: a second run waits up to 60 seconds
for the first, then fails.

To change the schema, add a revision in `apps/backend/migrations/versions/`. A
revision must leave the previous release working, because that release keeps
serving while it runs and is what a failed deploy restores. Each revision
declares `compatibility = "expand"` (earlier code still works) or `"contract"`
(it does not, and it must raise the floor with `raise_compat_floor`). Never edit
or revert a revision that has run on a shared database; add a new one. Large
indexes on `api_logs` are built in the background instead
(`apps/backend/serving/storage/perf_metrics_index.py`), because building them in
a migration would block request logging. The design record is
`docs/agents/specs/2026-11-02-versioned-schema-migrations-design.zh.md`.

## What the tables hold

`\dt` in psql lists every table. By group:

| Group | Tables | Holds |
|---|---|---|
| Request history | `api_logs`, `api_stats_hourly`, `provider_hourly_*`, `request_volume_hourly`, `user_usage_daily*`, `daily_activity`, `admin_view_*` | One row per request (model, provider, tokens, latency, TTFT, status, cost), the hourly and daily rollups behind the dashboards, and cached results of the slowest admin views |
| Public statistics | `public_stats_snapshots`, `api_log_retained_*` | The snapshots behind `GET /public-stats`, and totals kept for requests pruned from `api_logs`, without account ids |
| Settings | `app_config`, `site_settings`, `config_*`, `routing_config_revisions`, `route_catalog` | Stored settings and documents, secrets included; runtime switches and the setup marker; the counters that carry changes to every process; and the previous release's catalog copy, kept for rollback. See [Settings and first-run setup](#settings-and-first-run-setup) |
| Accounts and sign-in | `users`, `projects`, `api_keys`, `account_quotas`, `auth_sessions`, `browser_session*`, `login_events`, `email_verification_tokens`, `password_reset_tokens`, `identity_auth_codes`, `phone_auth_*`, `user_two_factor`, `two_factor_recovery_codes`, `user_identities`, `sso_*`, `organizations`, `org_members` | Accounts, their projects and API keys with budgets and model allowlists, sessions, sign-in history, phone codes, authenticators and recovery codes, and single sign-on identities. `organizations` and `org_members` are no longer read and will be dropped |
| User tags | `user_tags`, `user_tag_members` | Named groups of accounts and the limits their members get |
| Admin | `admin_audit_log`, `signup_allowed_domains`, `site_updates`, `email_broadcast*` | Audited admin changes, signup policy, announcements and broadcasts |
| Routing overrides | `provider_definitions`, `provider_api_keys`, `provider_route_*`, `provider_weight_overrides`, `provider_env_key_min_roles`, `disabled_provider*`, `model_visibility_overrides`, `model_concurrency_exemptions` | What Admin changes about providers, routes and models at runtime; see [Runtime configuration from the admin console](configuration.md#runtime-configuration-from-the-admin-console) |
| Cost and quota | `user_daily_cost`, `api_key_daily_cost`, `spend_*`, `accounting_currency_migrations` | Daily spend per user and per key, project budget counters, the shared spend mode's reservations and settlements, and the currency migration's audit table |
| RouteWise | `routewise_probe_*` | Latency probe samples, and the lease that stops two workers probing at once |
| Responses API | `openai_responses` | Stored `/v1/responses` state when content storage is on, kept 30 days |
| Batch inference | `batch_*` | Batches with their requests, attempts, results and files until `BATCH_RETENTION_DAYS` after they finish, plus webhooks, per-model caps and scheduling state; see [Running batch inference](batches.md#running-batch-inference) |
| Geo analytics | `geo_hourly_*` | Hourly request and token counts per country; no addresses |
| GPU fleet metrics | `openinfra_*` | Host, GPU and inference-engine metrics that GPU hosts report for the public `/openinfra` page: live rows, per-second samples, rollups, request events and a record of archived weeks; see [OpenInfra](openinfra.md) |
| Agents | `agent_grants`, `agent_invite_tokens`, `agent_provisioned_accounts` | Short-lived grants this gateway mints for an external agent control plane, and the invitations and accounts it provisions |
| Account deletion | `erasure_fence` | Keyed fingerprints of permanently deleted accounts; see [Secrets for deleting accounts](#secrets-for-deleting-accounts) |

### Settings and first-run setup

- `app_config` has one row per stored setting: `key` (the environment-variable
  name), `value` (as that variable would spell it, such as `true` or `a,b,c`),
  `secret`, `source` (`admin`, `setup`, `env_import` or `generated`),
  `updated_at` and `updated_by`. Secrets are stored in plaintext; the admin API
  never returns them. A row wins over the environment even when empty. See
  [Settings stored in the database](configuration.md#settings-stored-in-the-database).
- `site_settings` holds the runtime switches and, once the first administrator
  exists, `setup_completed_at`. While setup is pending, its `setup_code` row
  holds the one-time code, so read access to the database is enough to create
  the first administrator.
- The setup administrator has no `users.email` and signs in with
  `users.login_name` (unique regardless of case). Accounts created by phone
  signup have `users.phone` and no email. Service accounts, which an
  administrator creates for API-key use, have no email and cannot sign in from
  a browser (`users.is_service_account`).

## Request logging and privacy

With `DB_STORE_FULL_CONTENT` at its default, `false`, the text is never written:
`api_logs.prompt`, `api_logs.response` and `api_logs.request_payload` are
`NULL`, and `/v1/responses` state is not stored. Token counts, cost, latency,
TTFT, the conversation's shape (`num_turns`, `num_user_turns`,
`num_tool_calls`) and a fingerprint of the newest user message
(`last_user_msg_chars`, `last_user_msg_entropy`, `last_user_msg_hash`) are
recorded either way, for the dashboards.

Turning it on stores full prompts and responses; weigh that against your users'
expectations. Rows stay bounded: a conversation is stored up to 4 Mi characters,
the rest of the body shares 64 Ki characters (smallest fields first), and a
refused request (a 4xx other than 499) keeps at most 64 Ki characters of each
part. `metadata.payload_truncated` marks a row that was cut.

### Which session a request belongs to

`api_logs.session_id` groups one conversation's requests. The gateway reads the
first session id the request carries:

| Read from | Sent by |
|---|---|
| `X-Session-ID` header | Anything using the gateway's own contract; always wins |
| `session-id` / `thread-id` headers (also spelled `session_id` / `conversation_id`) | Codex CLI |
| `x-session-affinity` / `x-opencode-session` headers | OpenCode and its Kilo Code fork |
| `metadata.session_id` or `client_metadata.session_id` in the body | Clients that label the session in the body; Codex uses `client_metadata` |
| `x-claude-code-session-id` header | Claude Code |
| `metadata.user_id` in the body | Claude Code, as a JSON object with a `session_id` (version 2.1.78 and later) or a `user_<hash>_account_<uuid>_session_<uuid>` string (earlier); `parent_session_id` is ignored |

Kilo Code sends no session id on its newer request path until it takes
OpenCode's fix (`sst/opencode#43188`), so those requests have no session.
`metadata.session_id_source` records which source was used. Session ids are
client-supplied labels: nothing is authorized, billed or rate-limited by them, a
value over 128 characters or with control characters is dropped, and they are
recorded whatever `DB_STORE_FULL_CONTENT` says. Recent Requests in the admin
console filters by session.

## Secrets for deleting accounts

When an admin permanently deletes a user, the gateway writes a keyed
fingerprint of the account to `erasure_fence`, so a request-log write already in
flight cannot bring the user's rows back.

`ERASURE_FENCE_SECRET` is the key. At its first start against a database the
gateway records a fingerprint of the key, and from then on it refuses to start
if the key differs. Left empty, the fence uses `API_KEY_SECRET`, which the
backend generates at that first start unless the environment supplies one. To
give the fence its own key, set `ERASURE_FENCE_SECRET` in `.env` before the
first start. Neither secret can change afterwards, and Admin refuses both.

An `ERASURE_FENCE_SECRET` added to the environment later is not imported, but
while it is set the backend uses it and refuses to start, so remove it. After a
mismatch (say, a backup restored beside different secrets), restore the original
value; never delete fence rows to get past it.

Leave `ERASURE_FENCE_PROTOCOL_READY` off until every process that writes
`api_logs` runs a version that checks the fence.

## Backup

```bash
make backup BACKUP=/private/path/gateway.dump
make restore-check BACKUP=/private/path/gateway.dump
```

Without `BACKUP`, the dump goes to `.ops/<distribution>/gateway.dump`, which
neither Git nor a Docker build includes. `make restore-check` never touches the
deployment's database server: it restores the dump into a throwaway PostgreSQL
container of the same image, runs this release's migrations on the copy, and
removes it. It adds a second server's CPU and I/O load while it runs, and refuses
to start unless the disk where Docker keeps volumes has about twice the
database's size plus 1 GiB free.

By hand, with `pg_dump` in custom format (never add `-t` to `docker exec`: a TTY
corrupts the binary stream):

```bash
docker exec hybridinference-postgres \
  pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > hybridinference-$(date +%F).dump
```

To restore over a running database, stop the backend first with
`docker stop hybridinference-backend` (`make down` would stop Postgres too):

```bash
docker exec -i hybridinference-postgres \
  pg_restore -U "$DB_USER" -d "$DB_NAME" --clean --if-exists < hybridinference-2026-01-01.dump
```

```{warning}
The database holds every setting in plaintext: provider keys, the SMTP
password, webhook URLs, and the `JWT_SECRET_KEY` and `API_KEY_SECRET` that sign
sessions and protect API keys, as well as user prompts if
`DB_STORE_FULL_CONTENT` was ever on. Anyone with a dump, a backup, or psql or
pgAdmin access can call your providers, decrypt users' stored API keys and sign
administrator tokens. Protect them as you would those credentials.
```

To delete all data and start over, see
[Resetting the stack](deployment.md#resetting-the-stack). The runnable example
(`make demo-reset DISTRIBUTION=example`) uses its own volume and cannot delete
`hybridinference_postgres_data`.

## Inspecting the database

```bash
docker exec -it hybridinference-postgres psql -U "$DB_USER" -d "$DB_NAME"
```

`\dt` lists the tables and `\d api_logs` the request log's columns. In `make ps`,
Postgres should show `127.0.0.1:...`; anything else means it listens beyond this
host.

## Optional: pgAdmin

The Compose file includes pgAdmin in the `admin` profile. Name the profile on
every command, including `make down`, or set `COMPOSE_PROFILES=admin` in
`.ops.env`: a plain `make up` skips pgAdmin, and a plain `make down` leaves it
running.

```bash
make up   COMPOSE_PROFILES=admin
make down COMPOSE_PROFILES=admin
```

The console serves pgAdmin at `/pgadmin/`. pgAdmin is on its own `pgadmin`
network, shared only with the console and the database, so the backend cannot
reach it. The Compose file pins `dpage/pgadmin4:9.19`; a volume that ran
`dpage/pgadmin4:8` is upgraded on first start and cannot go back.

### Authentication

Two checks protect pgAdmin, both on by default:

- The console's route handler (`apps/frontend/src/app/pgadmin/[[...path]]/route.ts`)
  admits only an admin session, verified by the backend, and refuses on any
  error. pgAdmin publishes no host port, so this is the only way in.
- pgAdmin's own login, `PGADMIN_EMAIL` and `PGADMIN_PASSWORD`, while
  `PGADMIN_CONFIG_SERVER_MODE` is `True` (the default). `False` means no login
  at all. Set the password before pgAdmin first starts: without one it refuses
  to set up its volume (see `docker logs hybridinference-pgadmin`).

pgAdmin keeps the login it first started with and ignores later changes. A
volume first started without a password signs in as `admin@local.dev` /
`admin`: change that password in pgAdmin. A volume first started with server
mode off has no login, so after turning server mode on, reset the volume (below).
Its master password prompt never appears: Compose pins
`PGADMIN_CONFIG_MASTER_PASSWORD_REQUIRED` to `"False"`.

For an SSH tunnel, `PGADMIN_PUBLISH=1` publishes pgAdmin on the host's loopback,
port `PGADMIN_PORT` (5050 unless set). Pass it on every command, as with the
profile. That port skips the console's check and is open to every account on the
host, so never combine it with server mode off.

```bash
make up COMPOSE_PROFILES=admin PGADMIN_PUBLISH=1
ssh -L 5050:127.0.0.1:5050 <user>@<your-gateway-host>
```

```{note}
pgAdmin and the agent web app (`/agents`) are served on the console's origin, so
a script either app serves runs with the authority of the console session open
in that browser. The proxies keep each app's cookies under its own path and
refuse cookies named like the console's, which stops an app from replacing the
console's session; only a separate origin would remove the shared authority.
```

### Registering the database

1. `Servers` → right-click → `Register` → `Server`.
2. **General**: any name.
3. **Connection**: host `postgres`, port `5432`, maintenance database `DB_NAME`,
   username `DB_USER`, password `DB_PASSWORD` (the values inside the container
   network, not the host port mapping).

### Resetting pgAdmin

Its saved connections live in `hybridinference_pgadmin_data`, an ordinary
project volume:

```bash
make down COMPOSE_PROFILES=admin
docker volume rm hybridinference_pgadmin_data     # destroys saved connections only
make up   COMPOSE_PROFILES=admin
```

### pgAdmin over mutual TLS

For a separately hosted pgAdmin behind a gateway that requires client
certificates, set these in the console container's runtime environment (the
Compose file does not pass them, so add them and the secret mounts in an
override; the root `.env` is not enough):

```text
PGADMIN_INTERNAL_URL=https://pgadmin-gateway.internal:8443
PGADMIN_MTLS_ENABLED=true
PGADMIN_MTLS_CA_FILE=/run/pgadmin-tls/ca.crt
PGADMIN_MTLS_CERT_FILE=/run/pgadmin-tls/client.crt
PGADMIN_MTLS_KEY_FILE=/run/pgadmin-tls/client.key
```

`PGADMIN_INTERNAL_URL` defaults to `http://pgadmin:80`. With mTLS on, the URL
must be HTTPS with a hostname matching the server certificate, and all three
files are required; any problem makes `/pgadmin/` answer 502, never a fallback
to HTTP. Values other than `true` or `false` fail closed. The files are paths in
the container, read for each new request: mount them read-only, readable by uid
1000 (the console does not run as root), for example group 1000 with mode
`0440` or a Kubernetes `fsGroup: 1000`. Keep all three in one Secret projection
so they change together, and coordinate certificate rotation with the gateway.
The gateway must enforce mTLS and restrict access to pgAdmin itself; the
console's check does not protect a pgAdmin exposed elsewhere.
