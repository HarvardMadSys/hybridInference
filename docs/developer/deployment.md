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
| `frontend` | built from `deploy/docker/Dockerfile.frontend` | `${FRONTEND_HOST:-127.0.0.1}:${FRONTEND_PORT:-3001}` |
| `postgres` | `postgres:16` | `127.0.0.1:${DB_PORT:-5432}` |

All three published ports default to loopback. A reverse proxy on the same
host can reach the console at `127.0.0.1:3001`. The containers also join one bridge
network defined in the same Compose file, on which the backend reaches the
database as `postgres:5432`. Only `DB_PORT` from `.env` reaches this file, and
only as the host half of the mapping (`127.0.0.1:${DB_PORT:-5432}:5432`); the
host-side bind address is hard-coded to loopback. `DB_HOST` is pinned to
`postgres` in the Compose file and is ignored under Compose — it matters only
for a backend started directly from source.

One more service exists in the same file but starts only when its profile is
named: `pgadmin` (profile `admin`).

`frontend` depends on `backend` with `condition:
service_started`, not `service_healthy` — deliberately, so that a backend
reporting unhealthy because its database logging is down does not stop the
console from starting.

### Putting it on the public internet

Nothing in the stack terminates TLS, and this repository ships no reverse-proxy
config to copy: certificates and the proxy in front of the two published ports
are yours to supply. A proxy running on the host can use `127.0.0.1:3001` for
the console or `127.0.0.1:8080` for direct gateway access. A proxy container on
the same Docker network can use `frontend:3001` or `backend:8080`.

If your proxy runs on another machine, set `FRONTEND_HOST` to a reachable host
interface in `.env`; `0.0.0.0` binds all IPv4 interfaces. Restrict access to the
intended proxy and run `make up` to recreate the port mapping. The console
forwards API routes as well as serving pages, so exposing its port also exposes
those routes. Set `BACKEND_HOST` separately only if direct gateway access is
needed.

Earlier releases defaulted the frontend host to `0.0.0.0`. Deployments that
relied on that default must set `FRONTEND_HOST` explicitly before upgrading.
See [Releases and upgrades](releases.md) for the upgrade checklist.

Which public paths the console serves itself and which it forwards to the
backend is a separate question, and the answer is in the console's own
`next.config.js` rather than in any proxy config. See
[The public path table](public-path-table.md).

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
| A distribution branding YAML | `make restart s=backend` — `/site-config` serves the validated snapshot loaded at backend startup |
| A file in the mounted site-assets directory | No image rebuild; replace the file in the deployment overlay |
| `AGENT_WEB_INTERNAL_URL` or `AGENT_CONTROL_PLANE_INTERNAL_URL` | `make up` — the `/agents` route handler reads them at runtime in the recreated frontend container |
| A true build-only `NEXT_PUBLIC_*` compatibility value | `make build s=frontend` — see below |
| Backend or frontend source | `make build`, or `make build s=<service>` |

Canonical console identity comes from the active distribution's versioned
branding YAML through `/site-config`, and `/agents` gets its two destinations
from server-only runtime environment. Neither change requires a frontend image
rebuild. Recreate the frontend with `make up` after changing its runtime
environment; restart the backend after changing the branding document it
loads.

The console requires a valid `/site-config` response before rendering normal
pages. A failed HTTP request, a three-second timeout, or invalid JSON/schema
shows a retryable configuration error instead of silently enabling build-time
feature defaults. Check the frontend logs and backend connectivity, then retry
the page after recovery. Valid neutral defaults and legacy documents remain
supported; operators do not need to add custom settings just to start the
example deployment.

The Compose file still exposes the old branding variables as build arguments
for source compatibility. The Dockerfile also retains the two legacy agent
arguments for explicit pre-W7 downstream build pipelines, but upstream Compose
does not populate them. Those are transition bridges, not the canonical
release path: neutral published images omit them. Values that genuinely remain
`NEXT_PUBLIC_*` build metadata or compatibility settings are compiled into the
browser bundle and still require `make build s=frontend`. See [The public path
table](public-path-table.md) for the distinction between runtime handlers and
legacy build-time rewrites.

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
[Adding a New Local Model](add-local-model.md).

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

### Marking monitor traffic

A monitor that drives real inference — hitting `/v1/chat/completions` on a
schedule to measure a backend end to end, rather than just polling `/health` —
would otherwise land in `api_logs`, skew the dashboards, and feed RouteWise's
online learning as if it were user demand. Send `X-Probe: synthetic` on those
requests to keep them out: a marked request is left out of `api_logs` (and its
rejections out of the rejection log), does not record a routing observation,
and carries an `X-Provider` response header naming the backend that answered,
so the monitor can confirm which route it exercised.

The marker is honoured **only from an authenticated internal- or admin-role API
key** — never a free/pro key, an agent-sandbox grant, or, importantly, an
anonymous caller on a deployment running with `USER_AUTH_ENABLED=0` (auth-off
hands every caller the admin role, which is not the same as holding a monitor
identity). From any other caller the header is ignored and the request is
logged like ordinary traffic. A deployment that needs probes without auth wants
an explicit mechanism — a shared secret, a source allowlist — not this header.

The marker is about noise, not access. It cannot keep a monitor's own auth
failures from tripping the repeated-auth-failure blocklist, because that
decision is made before the presented key is read — see [A monitor or service
account is suddenly getting
429s](#a-monitor-or-service-account-is-suddenly-getting-429s).

The one thing the marker never touches is billing: cost and quota are
incremented unconditionally on every surface, for trusted and untrusted callers
alike, so a probe cannot be used to obtain unmetered inference. To keep marked
traffic in `api_logs` after all — to see a monitor's real latency and spend in
the requests dashboard — turn on the `log_synthetic_probes` runtime setting;
the routing-observation and `X-Provider` behaviour is unchanged.

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

Two auth rules are worth knowing apart, because their defaults differ on
purpose:

- `rules.auth_failure_spike` is **off**. Bad keys are internet background
  noise, and a count of them names nothing to act on. The `auth_failure` log
  records are emitted regardless.
- `rules.auth_ip_blocked` is **on**. This one fires when the blocklist starts
  *refusing* a source — a discrete decision at a much higher threshold, naming
  an address. It is on because the source is sometimes the deployment's own;
  see the troubleshooting entry below.

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

- `required variable DB_NAME is missing a value: DB_NAME must be set in .env
  file` — Compose stopped at interpolation before starting anything. `DB_NAME`,
  `DB_USER` and `DB_PASSWORD` are declared required with the `${VAR:?message}`
  form, so the half after the colon is the Compose file's own text and the most
  greppable part of the line.
- Port already in use — override `BACKEND_PORT`, `FRONTEND_PORT` or `DB_PORT`.
- Database connection failed — check `make ps` for the `postgres` health status.

### A monitor or service account is suddenly getting 429s

`Too many authentication failures from this IP. Temporarily blocked.` is the
gateway's own abuse defense, not a provider error and not a quota. Once a source
accumulates `AUTH_FAILURE_BLOCK_THRESHOLD` failed authentications inside
`AUTH_FAILURE_BLOCK_WINDOW_SEC` (200 in a day, by default) it is refused for
`AUTH_FAILURE_BLOCK_DURATION_SEC` (a day).

The awkward case is a caller you own — a status monitor, a CI job, a service
account — whose key was rotated, revoked, or never reached its environment. It
retries on a schedule, crosses the threshold, and is then refused *ahead of the
key check*, which has two consequences worth internalising:

- **Repairing the credential does not lift the block.** The blocklist is
  consulted before the presented key is read, so a corrected key gets the same
  429 until the deadline passes.
- **The 429 hides the original error.** Whatever the caller reports after the
  block is in place says nothing about whether the underlying 401/403 was fixed.

Which caller is it? Turn on the `log_rejected_requests` admin setting and the
refusals land in Recent Requests as `ip_blocked` rows. Each row names the
account behind the key the caller presented — including a key that was revoked
or expired, which is what a stuck monitor is presenting — and labels it with the
credential's state (`revoked`, `expired`, `user_suspended`) beside the user. A
row with no user is a caller presenting a key this deployment never issued,
i.e. a scanner rather than something of yours.

To recover, first fix the credential, then clear the block:

```bash
# Which sources is this worker refusing?
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://localhost:8000/admin/auth-blocks

# Lift one. `ip` takes a raw address, or a bucket key exactly as listed
# (IPv6 sources are bucketed to their /64).
curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"ip": "203.0.113.7"}' \
  http://localhost:8000/admin/auth-blocks/clear
```

`cleared: false` means there was nothing to lift — it lapsed, or that bucket was
never blocked. Clearing grants no immunity: a caller still presenting a bad key
is blocked again on crossing the threshold. For a source that should never be
blocked at all, list it in `AUTH_FAILURE_BLOCK_EXEMPT_IPS` (comma-separated
addresses or CIDRs) instead.

The blocklist is per-process, in-memory state. On the single-process default
both endpoints are exact, and a restart also clears every block. Run multiple
workers and each holds its own counts, so a listing shows only the worker that
answered and clearing may take more than one call.

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
(`hybridinference_codex_oncall_data`) are ordinary local volumes, so a `down -v`
*would* remove those. Note that `make down` is a plain `docker compose down`
with no `--volumes`, so nothing here passes `-v` on your behalf — you have to
run `docker compose --profile admin --profile oncall down -v` yourself, or
remove the volumes by name as above.

### Rebuilding after code changes

```bash
make build               # all images
make build s=backend     # one service
```
