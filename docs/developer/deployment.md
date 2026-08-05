# Deployment Guide

Guide for deploying HybridInference in production.

## Quick Start (Docker)

```bash
# 1. Clone and configure
git clone https://github.com/HarvardMadSys/hybridInference.git
cd hybridInference
cp .env.example .env
# Edit .env — fill in DB_PASSWORD, JWT_SECRET_KEY, API_KEY_SECRET, and provider API keys

# 2. Start all services
make up

# 3. Verify
make ps
curl http://localhost:8080/health
```

This starts 3 containers: backend (FastAPI), frontend (Next.js), and
PostgreSQL. Backend and PostgreSQL bind to `127.0.0.1` by default; the
frontend binds to `0.0.0.0` (override with the `FRONTEND_HOST` env var)
so it can be reached by Nginx on the host. pgAdmin is available but
requires the `admin` profile (see below).

## Prerequisites

- Docker Engine 24+ and Docker Compose v2+
- User in the `docker` group (`sudo usermod -aG docker $USER`)
- Nginx on the host for SSL termination (not containerized)

## Service Architecture

```
Client ──▶ Cloudflare (CDN + DDoS) ──▶ Nginx (:443) ──┬──▶ backend  (:8080)
                                                        └──▶ frontend (:3001)

Docker internal network:
  backend ──▶ postgres (:5432)
  backend ──▶ host.docker.internal (GPU SSH tunnels on host)
```

## Common Operations

All commands run from the project root via `make`:

```bash
make up                  # Start all services
make down                # Stop all services
make restart             # Restart all services
make restart s=backend   # Restart a single service
make ps                  # Show running services and health status
make logs                # Tail logs (all services)
make logs s=backend      # Tail logs for one service
make build               # Rebuild images and restart
make build s=frontend    # Rebuild one service
```

## Configuration

### Environment Variables

All secrets and configuration live in `.env` at the project root. See `.env.example` for
the full list with comments. Key variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Yes | PostgreSQL credentials |
| `JWT_SECRET_KEY` | Yes | JWT signing key (generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`) |
| `API_KEY_SECRET` | Yes | HMAC key for API key hashing |

### Local GPU Endpoints

If you run local inference servers (sglang, vLLM) on the host or via SSH tunnels,
`config/models.yaml` references them as `host.docker.internal:<port>`. This DNS name
resolves to the host machine from inside Docker containers.

For bare-metal development without Docker, replace `host.docker.internal` with `localhost`.

## Nginx and HTTPS

Nginx runs on the host (not in Docker) to terminate TLS. The example config
that used to ship under `deploy/nginx/` was removed from the
repo; write a host-level site config yourself, then:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

This assumes:
- Backend: `127.0.0.1:8080`, Frontend: `127.0.0.1:3001`
- HTTPS certificates from Let's Encrypt

### Cloudflare

Behind a CDN, two settings matter whichever one you use:
- **SSL/TLS mode**: full verification to the origin
- **Caching**: disabled for API paths (`/v1/*`) — streamed responses must not
  be cached, and a cached completion is served to the wrong user

## Monitoring

### Health Checks

```bash
curl http://localhost:8080/health
# {"status":"healthy","routes_configured":17,"database_connected":true}
```

### Alerting

The backend ships with an in-process alert engine that posts to Slack
directly. Configuration lives in `config/alerts.yaml`; rules and
thresholds are described in `apps/backend/serving/observability/`.
Set `ALERTS_ENABLED=true` and `SLACK_ALERTS_WEBHOOK_URL=...` in `.env`
to enable.

For asynchronous read-only Codex investigation and threaded Slack results, see
[Codex On-Call](codex-oncall.md). Keep the existing Slack webhook as
a fallback during rollout.

## Database

PostgreSQL runs in Docker with data persisted to a named volume (`hybridinference_postgres_data`).

To access the database directly:

```bash
docker exec -it hybridinference-postgres psql -U $DB_USER -d $DB_NAME
```

For pgAdmin (optional):

```bash
# Start with admin profile
docker compose -f deploy/docker/docker-compose.yml --env-file .env --profile admin up -d
# Access at http://localhost:5050
```

A deployment that wants pgAdmin reachable through the console instead sets the
profile in an env file the deploy reads, rather than passing the flag by hand —
`distributions/freeinference/deploy/compose.env` is the worked example. The
console then serves it at `/pgadmin/`, gated on an admin session by
`apps/frontend/src/app/pgadmin/[[...path]]/route.ts`.

Two things to know before relying on it:

- Whether pgAdmin **also** asks for a login is a per-host choice, and the two
  defaults disagree: the Compose service falls back to
  `PGADMIN_CONFIG_SERVER_MODE=False`, which serves it with no login at all,
  while `.env.example` suggests `True`, which turns pgAdmin's own login on.
  `True` is the safer of the two — it puts a second gate behind the console's.
  The route handler assumes it is the only one either way, and denies on every
  unexpected condition, including a backend it cannot reach.
- The deploy scripts do not rely on `--env-file` to carry that profile:
  Compose ignored `COMPOSE_PROFILES` there from 2.27.1 until the fix for
  [docker/compose#11856](https://github.com/docker/compose/issues/11856). They
  read the overlay themselves and export the union of it and whatever the
  host's `.env` selects, so a host that also runs `oncall` keeps it. Running
  Compose by hand is the exception — on an affected version a host `.env` that
  sets `COMPOSE_PROFILES` wins outright, so list every profile you want. The
  deploy scripts warn when pgAdmin ends up not running either way.

See [Database](database.md) for schema details.

## Troubleshooting

### Service won't start

```bash
make logs s=backend      # Check service-specific logs
make ps                  # Check health status
```

Common issues:
- Missing required env vars in `.env` → compose will error with `variable X is missing a value`
- Port already in use → check `ss -tlnp | grep <port>`
- Database connection failed → ensure postgres is healthy: `make ps`

### Rebuild after code changes

```bash
make build               # Rebuild all images
make build s=backend     # Rebuild just backend
```

### Full reset (preserves data)

```bash
make down && make up
```

### Full reset (destroy data)

```bash
docker compose -f deploy/docker/docker-compose.yml --env-file .env down -v
make up
```

> **Warning**: `-v` deletes all named volumes including the database.
