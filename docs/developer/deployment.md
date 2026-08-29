# Deployment Guide

Running HybridInference as a long-lived deployment: what starts, how to operate
it, and how to reset it without losing (or accidentally keeping) data.

First-time setup — cloning, filling in `.env`, and the first `make up` — is in
[Installation](installation.md#quick-start-with-docker). This page assumes the
stack already comes up.

## What the stack is

`make up` starts three containers from `deploy/docker/docker-compose.yml`:

| Service | Image / build | Published on |
|---|---|---|
| `backend` | built from `deploy/docker/Dockerfile.backend` | `${BACKEND_HOST:-127.0.0.1}:${BACKEND_PORT:-8080}` |
| `frontend` | built from `deploy/docker/Dockerfile.frontend` | `${FRONTEND_HOST:-0.0.0.0}:${FRONTEND_PORT:-3001}` |
| `postgres` | `postgres:16` | `127.0.0.1:${DB_PORT:-5432}` |

The frontend defaults to `0.0.0.0` so a reverse proxy on the host can reach it;
the backend and the database default to loopback. All three join one bridge
network defined in the same Compose file, on which the backend reaches the
database as `postgres:5432` — `DB_HOST`/`DB_PORT` from `.env` control only the
host-side port mapping, because the Compose file pins the container-internal
values.

Two more services exist in the same file but start only when their profile is
named: `pgadmin` (profile `admin`) and `codex-oncall` (profile `oncall`).

`frontend` and `codex-oncall` depend on `backend` with `condition:
service_started`, not `service_healthy` — deliberately, so that a backend
reporting unhealthy because its database logging is down does not stop the
console from starting.

### Putting it on the public internet

Nothing in the stack terminates TLS, and this repository ships no reverse-proxy
config to copy: certificates and the proxy in front of the two published ports
are yours to supply. Point it at `${BACKEND_HOST}:${BACKEND_PORT}` and
`${FRONTEND_HOST}:${FRONTEND_PORT}`.

Which public paths the console serves itself and which it forwards to the
backend is a separate question, and the answer is in the console's own
`next.config.js` rather than in any proxy config. See
[Edge and console routing](edge-and-console-routing.md).

## Everyday operations

All from the repository root:

```bash
make up                  # start everything
make down                # stop everything (data survives; see below)
make restart             # restart everything
make restart s=backend   # restart one service
make ps                  # services and health status
make logs                # tail all logs
make logs s=backend      # tail one service
make build               # rebuild images and restart
make build s=frontend    # rebuild one service
```

`make up` and `make build` first run the `docker-volumes` target, which creates
the external volume `hybridinference_postgres_data` when it is missing.

To start an optional profile, pass it on the `make` command line — a variable
set there is exported into the environment of the recipe, and a shell variable
outranks every `--env-file` in Compose:

```bash
make up COMPOSE_PROFILES=admin
```

`COMPOSE_PROFILES` is a comma-separated list, so `admin,oncall` starts both. It
can also be set in `.env` (as `.env.example` notes), but the command line is the
form to reach for when you want certainty about which profiles are active.

### What a change actually requires

Three different answers, and picking the wrong one looks like the change not
taking effect:

| You changed | Do this |
|---|---|
| A value in `.env` | `make up` — a container reads its `env_file` when it is *created*, so `docker compose restart` keeps the old environment |
| A model registry or routing YAML | `make restart s=backend` — `config/` and `distributions/` are bind-mounted read-only, so no rebuild is needed |
| A `NEXT_PUBLIC_*` or `AGENT_*` console value | `make build s=frontend` — see below |
| Backend or frontend source | `make build`, or `make build s=<service>` |

The console's identity and its `/agents` rewrites are Next.js **build args**
(`deploy/docker/docker-compose.yml`, `frontend.build.args`), and Next resolves
`rewrites()` at build time into `.next/routes-manifest.json`. Changing any
`NEXT_PUBLIC_*` value or the `AGENT_*` URLs therefore takes a `make build
s=frontend`; a value supplied only at container start is read by nothing, and
the symptom is the old pages continuing to serve while `docker inspect` shows
the new value. Adopting or rolling back those settings is therefore a rebuild,
not a restart.

## Configuration

### Environment

Everything is in `.env` at the repository root; `.env.example` is the annotated
list. Compose is invoked with `--env-file .env` and the backend service also
loads it as `env_file`. The variables Compose itself requires, and the two
secrets you should not leave blank, are in
[Installation](installation.md#quick-start-with-docker).

### Config file resolution

There is no `config/models.yaml` or `config/routing.yaml` in this repository.
`resolve_config_path` (`apps/backend/serving/config/distribution.py`) picks each
file by precedence: an explicit `MODELS_CONFIG_PATH` / `ROUTING_CONFIG_PATH` /
`ALERTS_CONFIG_PATH`, then an active distribution manifest, then the built-in
defaults under `config/examples/`. The full rules, including why
`DISTRIBUTION_CONFIG_MODE` defaults to `dark`, are in
[Installation](installation.md#where-the-config-files-live).

Note that the Compose file passes these through explicitly:

```yaml
ROUTING_CONFIG_PATH: ${ROUTING_CONFIG_PATH-}
MODELS_CONFIG_PATH: ${MODELS_CONFIG_PATH-}
DISTRIBUTION_CONFIG_PATH: ${DISTRIBUTION_CONFIG_PATH-}
```

An `--env-file` alone does not put a variable into a container's environment;
these lines are what carry it in. Losing one silently swaps a deployment's
routing map or alert thresholds for the defaults — which is why tests pin them.

### Local inference servers

The backend container reaches servers on the host through
`host.docker.internal`, which the Compose file wires with
`extra_hosts: host.docker.internal:host-gateway`. Write that address explicitly
in the model registry:

```yaml
route:
  - kind: openai_compat
    base_url: http://host.docker.internal:8001/v1
```

For a backend running directly on the host, use `localhost` instead. The gateway
never rewrites provider URLs. See
[Adding a local model](add-local-model.md).

## Health checks

```bash
curl -s http://localhost:8080/health
```

```json
{
  "status": "healthy",
  "routes_configured": 3,
  "database_configured": true,
  "database_connected": true,
  "stores": {
    "operational_store": {"status": "ok", "backend": "postgres", "cache": "in_memory"},
    "log_store": {"status": "ok", "backend": "postgres"}
  }
}
```

- `routes_configured` counts published route entries — one per model id in the
  active registry, plus one per alias. It is whatever *your* registry defines.
- `database_configured` distinguishes "this deployment asked for no database"
  from "the database is down": with `DB_ENABLED=false` it is `false` and the
  status is still `healthy`; with a database configured but unreachable at
  startup, `/health` answers **503** with `"reason":
  "database_unavailable_at_startup"`.
- `status` becomes `degraded` — still HTTP 200 — when one configured store is
  down but the other is serving. That shape is deliberate: the container
  `HEALTHCHECK` uses `curl -f /health`, so returning 503 for partial degradation
  would tear down backends that are still answering requests.

Use `/health/ready` for a strict readiness probe: it applies AND-logic across
configured stores and returns 503 unless every one of them is up.
`/health/deep` additionally reports per-endpoint health.

## Alerting

The backend has an in-process alert engine that posts to a Slack webhook. It is
off unless you turn it on:

```bash
ALERTS_ENABLED=true
SLACK_ALERTS_WEBHOOK_URL=https://hooks.slack.com/services/...
```

If `SLACK_ALERTS_WEBHOOK_URL` is empty it falls back to `SLACK_WEBHOOK_URL`, so
one webhook can serve both code paths.

Rules and thresholds are a deployment's own; this repository ships no alerts
file. Point `ALERTS_CONFIG_PATH` at yours, or leave it unset and the built-in
thresholds apply. The rule types and evaluation live in
`apps/backend/serving/observability/`.

For asynchronous read-only Codex investigation of alerts, see
[Codex On-Call](codex-oncall.md).

## Database

PostgreSQL 16 runs in the `postgres` service with its data in the Docker volume
`hybridinference_postgres_data`. A psql shell:

```bash
docker exec -it hybridinference-postgres psql -U "${DB_USER}" -d "${DB_NAME}"
```

Schema details are in [Database](database.md).

### pgAdmin (optional)

```bash
make up COMPOSE_PROFILES=admin
```

pgAdmin then listens on `127.0.0.1:5050` with `SCRIPT_NAME=/pgadmin`, so an SSH
tunnel to that port is enough to reach it. The console can also proxy it at
`/pgadmin/`, gated on an admin session by
`apps/frontend/src/app/pgadmin/[[...path]]/route.ts`, which denies on every
unexpected condition, including a backend it cannot reach.

One thing to get right: whether pgAdmin *also* asks for its own login is set by
`PGADMIN_CONFIG_SERVER_MODE`, and the two defaults disagree. The Compose service
falls back to `False`, which serves pgAdmin with no login at all; `.env.example`
suggests `True`, which turns pgAdmin's own login on behind the console's gate.
`True` is the safer of the two.

## Troubleshooting

### A service will not start

```bash
make logs s=backend
make ps
```

- `variable X is missing a value` — Compose stopped at interpolation before
  starting anything. `DB_NAME`, `DB_USER` and `DB_PASSWORD` are declared
  required.
- Port already in use — override `BACKEND_PORT`, `FRONTEND_PORT` or `DB_PORT`.
- Database connection failed — check `make ps` for the `postgres` health status.

### Resetting the stack

**Stop and start, keeping data:**

```bash
make down && make up
```

**Destroy the database and start clean.** `docker compose down -v` does *not* do
this. `postgres_data` is declared `external: true` in
`deploy/docker/docker-compose.yml`, and Compose never removes an external
volume — `down -v` returns success and leaves it fully intact, so `make up`
comes back on exactly the same data. Remove it by name:

```bash
make down
docker volume rm hybridinference_postgres_data
make up   # docker-volumes recreates it empty; Postgres re-initialises
```

```{warning}
`docker volume rm` is irreversible and takes every account, API key and request
log with it. Take a `pg_dump` first if any of it matters.
```

pgAdmin's own volume (`hybridinference_pgadmin_data`) and the on-call relay's
(`hybridinference_codex_oncall_data`) are ordinary local volumes, so
`docker compose ... down -v` does remove those.

### Rebuilding after code changes

```bash
make build               # all images
make build s=backend     # one service
```
