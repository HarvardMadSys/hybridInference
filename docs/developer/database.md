# Database

The gateway uses PostgreSQL for request logs, user accounts, API keys, and every
admin/runtime setting that outlives a restart. This page covers what is stored,
how the schema comes into existence, and how to back it up, restore it, and reset
it.

## Is a database required?

No. `DB_ENABLED` defaults to `true`, but a gateway started with `DB_ENABLED=false`
routes requests normally — it simply has no request history, no user accounts, no
API-key issuance, and no admin surfaces backed by stored settings.

The distinction matters when reading `/health`, because the two "no database"
states are not the same answer:

```json
{"status": "healthy",   "routes_configured": 3, "database_configured": false, "database_connected": false}
{"status": "unhealthy", "reason": "database_unavailable_at_startup", "database_configured": true, "database_connected": false}
```

The first is a deployment that asked for no database. The second asked for one
and did not get it, and reports unhealthy so a load balancer takes it out of
rotation.

## Connection settings

Read by `Settings` in `apps/backend/serving/config/settings.py`, from `.env` or
the process environment.

| Variable | Default | Notes |
|---|---|---|
| `DB_ENABLED` | `true` | Any of `false` / `0` / `no` disables the database entirely. |
| `DB_HOST` | `localhost` | The Compose stack overrides this to `postgres` inside the backend container. |
| `DB_PORT` | `5432` | In Compose this is the **host** port mapping; the container always talks to 5432. |
| `DB_NAME` | `hybridinference` | Required by Compose (`DB_NAME must be set in .env file`). |
| `DB_USER` | `postgres` | Required by Compose. |
| `DB_PASSWORD` | *(empty)* | Required by Compose. |
| `DB_STORE_FULL_CONTENT` | `false` | Whether prompts and responses are stored verbatim. See [Request logging and privacy](#request-logging-and-privacy). |

The bundled stack (`deploy/docker/docker-compose.yml`) runs `postgres:16`,
initialised with `-E UTF8 --locale=C.UTF-8`, and publishes it on
`127.0.0.1:${DB_PORT:-5432}` — loopback only. Reach it from another machine with
an SSH tunnel, not by widening that binding.

## How the schema is created

There is no migration tool in this repository — no Alembic, no SQL migration
directory. The schema is created and migrated **by the application at startup**,
idempotently. Every statement is `CREATE TABLE IF NOT EXISTS`, so the builders
below overlap harmlessly where two of them define the same table:

| Code | Creates |
|---|---|
| `ensure_api_logs_schema` in `apps/backend/serving/storage/log_schema.py` | `api_logs` and `api_stats_hourly`, with their columns and indexes |
| `DatabaseLogger._create_tables` in `apps/backend/serving/storage/database.py` | Calls the above, then the auth/admin tables |
| `PostgresOperationalStore.initialize` in `apps/backend/serving/storage/postgres_operational.py` | The operational tables (settings, overrides, provider registry) |
| `ResponseStore.initialize` in `apps/backend/serving/storage/responses_store.py` | `openai_responses` |
| `apps/backend/serving/grants.py` and `apps/backend/serving/admin/geo_demand_rollup.py` | `agent_grants`; the `geo_hourly_*` rollup tables |

Pointing a gateway at an empty database is therefore all the "migration" there
is: start it and the tables appear. Booting this revision against a blank
`postgres:16` database creates 33 tables in `public`.

Two properties are worth knowing before you operate this:

**Schema DDL is gated on the Postgres system catalogs.** Each startup reads `pg_attribute` / `pg_indexes`
first and issues only the `ALTER`/`CREATE INDEX` statements that are actually
missing, so a steady-state restart takes no strong table locks. This matters
because `ALTER TABLE` acquires `ACCESS EXCLUSIVE` *before* Postgres evaluates
`IF NOT EXISTS`, and a queued exclusive lock parks every reader behind it.

**A migration that cannot get its lock is deferred, not fatal.** The DDL phase
runs under a 3-second `lock_timeout` and raises `SchemaLockUnavailable` rather
than waiting; the caller retries it in the background and startup proceeds. The
usual lock holder is a long-running `pg_dump`, which can hold `ACCESS SHARE` over
`api_logs` for hours. If you take backups on a schedule, expect an occasional
deferred-migration line in the log after a deploy that adds a column.

If you are contributing a column to `api_logs`, add it in `log_schema.py` only.
That module exists because the DDL was once duplicated across two builders, they
drifted, and the builder that actually runs at boot never created the new
columns.

## What the tables hold

| Group | Tables | Holds |
|---|---|---|
| Request history | `api_logs`, `api_stats_hourly`, `provider_hourly_stats` | One row per request (model, provider, tokens, latency, TTFT, status, cost) plus hourly rollups used by the dashboards |
| Accounts and auth | `users`, `api_keys`, `auth_sessions`, `login_events`, `email_verification_tokens`, `password_reset_tokens`, `identity_auth_codes` | User records, hashed API keys and their quotas, refresh sessions, sign-in history |
| Admin actions | `admin_audit_log`, `signup_allowed_domains`, `site_settings`, `site_updates`, `email_broadcasts`, `email_broadcast_recipients` | Audited admin changes, signup policy, runtime settings, announcements, broadcast delivery state |
| Runtime routing overrides | `provider_definitions`, `provider_api_keys`, `provider_route_configs`, `provider_route_candidates`, `provider_weight_overrides`, `disabled_providers`, `disabled_provider_env_keys`, `provider_env_key_min_roles`, `model_visibility_overrides`, `model_concurrency_exemptions` | Everything the admin UI can change about routing without editing YAML |
| Cost and quota | `user_daily_cost` | Per-user daily spend used for quota enforcement |
| RouteWise | `routewise_probe_samples`, `routewise_probe_leases` | Latency probe samples and the lease that stops two workers probing at once |
| Responses API | `openai_responses` | Stored `/v1/responses` state, when content storage is enabled |
| Geo analytics | `geo_hourly_coverage`, `geo_hourly_demand` | Hourly per-country request and token counts; aggregate only, no IP addresses stored |
| Agent grants | `agent_grants` | Short-lived, model-scoped capabilities this gateway mints for an external agent control plane |

`\dt` on a live database is the authoritative list; the code paths above are the
authoritative definition.

## Request logging and privacy

`DB_STORE_FULL_CONTENT` defaults to `false`, and that default is not a redaction
of the stored text — the text is never written. With it off, `api_logs.prompt`,
`api_logs.response` and `api_logs.request_payload` are inserted as `NULL`, and
`/v1/responses` state is not persisted.

Derived, non-content columns are recorded either way, because the dashboards read
them instead of de-TOASTing payloads: token counts, cost, latency and TTFT, the
conversation shape (`num_turns`, `num_user_turns`, `num_tool_calls`), and a
fingerprint of the newest user message (`last_user_msg_chars`,
`last_user_msg_entropy`, `last_user_msg_hash`).

Turning it on stores full prompts and responses. Weigh that against your users'
expectations before you do.

## Backup

`pg_dump` in custom format, straight out of the container:

```bash
docker exec hybridinference-postgres \
  pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > hybridinference-$(date +%F).dump
```

Do not add `-t` to `docker exec`: allocating a TTY corrupts the binary stream.

Restore into an existing, running database:

```bash
docker exec -i hybridinference-postgres \
  pg_restore -U "$DB_USER" -d "$DB_NAME" --clean --if-exists < hybridinference-2026-01-01.dump
```

Stop the backend (`make down`, or `docker stop hybridinference-backend`) before
restoring over a live database, and remember that the backup contains hashed API
keys and — if `DB_STORE_FULL_CONTENT` was ever on — user prompt content. Store it
accordingly.

## Reset

Deleting the database is more awkward than it looks, because the Postgres volume
is declared `external:` in `deploy/docker/docker-compose.yml`:

```yaml
volumes:
  postgres_data:
    external: true
    name: hybridinference_postgres_data
```

**`docker compose down --volumes` does not delete an external volume.** Nor does
`make down`, which passes no `--volumes` at all. A reset that relies on either
silently leaves every row in place. Delete the volume by name:

```bash
make down
docker volume rm hybridinference_postgres_data    # destroys all data
make up                                           # recreates an empty volume
```

`make up` depends on the `docker-volumes` target, which recreates the named
volume if it is missing, so the stack comes back on an empty database and the
startup initialisers rebuild the schema. Take a dump first if there is any chance
you want the data back.

The runnable example (`make demo-reset DISTRIBUTION=example`) uses its own,
example-scoped volume and cannot delete `hybridinference_postgres_data`.

## Inspecting the database

```bash
docker exec -it hybridinference-postgres psql -U "$DB_USER" -d "$DB_NAME"
```

Useful starting points: `\dt` for the table list and `\d api_logs` for the request
log's columns. `make ps` shows the published bindings — Postgres and pgAdmin
should both read `127.0.0.1:...`; anything else means the database is listening
beyond this host.

## Optional: pgAdmin

The Compose stack ships a pgAdmin service for people who prefer a GUI. It is
**profile-gated** — nothing starts it unless the `admin` profile is named — and
it is entirely optional; `psql` above does everything.

### Starting and restarting it

The profile must be on *every* Compose command, not just the first:

```bash
make up   COMPOSE_PROFILES=admin
make down COMPOSE_PROFILES=admin
```

Omitting it does not fail loudly, it just does the wrong thing in both
directions: a plain `make up` starts every other service and skips pgAdmin, and a
plain `make down` leaves the pgAdmin container running while removing everything
around it (Compose then reports the network as still in use). Pass
`COMPOSE_PROFILES=admin` on both halves of a restart.

### Authentication

Two independent gates, and by default neither is pgAdmin's own login:

- `PGADMIN_CONFIG_SERVER_MODE` defaults to `False`, which serves pgAdmin with **no
  login of its own**. Set it to `True` in `.env` and pgAdmin asks for
  `PGADMIN_EMAIL` / `PGADMIN_PASSWORD` (which themselves default to
  `admin@local.dev` / `admin` — change them before enabling this).
- Reached through the console at `/pgadmin/`, the request is gated on an admin
  session by a Next.js route handler
  (`apps/frontend/src/app/pgadmin/[[...path]]/route.ts`), which asks the backend
  to verify the caller. Reached directly on its published port
  (`127.0.0.1:${PGADMIN_PORT:-5050}`, loopback only), that gate does not apply —
  use an SSH tunnel:

  ```bash
  ssh -L 5050:127.0.0.1:5050 <user>@<your-gateway-host>
  ```

pgAdmin's *master password* prompt never appears:
`PGADMIN_CONFIG_MASTER_PASSWORD_REQUIRED` is pinned to `"False"` in the Compose
file with no environment variable to change it.

### Registering the database

1. `Servers` → right-click → `Register` → `Server`.
2. **General**: any name.
3. **Connection**: host `postgres`, port `5432`, maintenance database `DB_NAME`,
   username `DB_USER`, password `DB_PASSWORD` — the container-internal values,
   not the host port mapping.

### Resetting pgAdmin

Its saved connections live in `hybridinference_pgadmin_data`, an ordinary
project-local volume (not external, unlike the Postgres one):

```bash
make down COMPOSE_PROFILES=admin
docker volume rm hybridinference_pgadmin_data     # destroys saved connections only
make up   COMPOSE_PROFILES=admin
```
