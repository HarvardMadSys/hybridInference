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

This starts 7 containers: backend (FastAPI), frontend (Next.js), PostgreSQL, Prometheus,
Alertmanager and Grafana. All ports bind to `127.0.0.1` only.

## Prerequisites

- Docker Engine 24+ and Docker Compose v2+
- User in the `docker` group (`sudo usermod -aG docker $USER`)
- Nginx on the host for SSL termination (not containerized)

## Service Architecture

```
Client ──▶ Cloudflare (CDN + DDoS) ──▶ Nginx (:443) ──┬──▶ backend  (:8080)
                                                        ├──▶ frontend (:3001)
                                                        ├──▶ grafana  (:3000)
                                                        └──▶ prometheus (:9090)

Docker internal network:
  backend ──▶ postgres (:5432)
  prometheus ──▶ backend (:8080/metrics)
  prometheus ──▶ alertmanager (:9093) ──▶ Slack #free-inference-alert
  grafana ──▶ prometheus (:9090), postgres (:5432)
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
| `GRAFANA_USER`, `GRAFANA_PASSWORD` | No | Grafana admin credentials (default: admin/admin) |

### Local GPU Endpoints

If you run local inference servers (sglang, vLLM) on the host or via SSH tunnels,
`config/models.yaml` references them as `host.docker.internal:<port>`. This DNS name
resolves to the host machine from inside Docker containers.

For bare-metal development without Docker, replace `host.docker.internal` with `localhost`.

## Nginx and HTTPS

Nginx runs on the host (not in Docker) to terminate TLS. An example configuration is
at `infrastructure/nginx/freeinference.conf`.

```bash
sudo cp infrastructure/nginx/freeinference.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/freeinference.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

This assumes:
- Backend: `127.0.0.1:8080`, Frontend: `127.0.0.1:3001`, Grafana: `127.0.0.1:3000`
- HTTPS certificates from Let's Encrypt

### Cloudflare

FreeInference runs behind Cloudflare. Key settings:
- **SSL/TLS mode**: Full (strict)
- **Caching**: Disabled for API paths (`/v1/*`)

## Subscription Account Management

If you use Claude or Codex subscription adapters (OAuth-based), accounts must be imported separately from the standard `.env` API keys.

Run these scripts from the project root on the **host machine**, not inside the backend container. They read credentials from the host user's home directory (`~/.claude/`, `~/.codex/`) and write into the project workspace under `var/data/`.

### Import Claude credentials

```bash
# After running `claude login` as the same host user
python scripts/import_claude_auth.py
```

### Import Codex credentials

```bash
# After running `codex --login` as the same host user
python scripts/import_codex_auth.py
```

### Check account health

```bash
python scripts/inspect_claude_accounts.py
```

This shows per-account state (`active`/`cooldown`/`revoked`/`disabled`), token expiry, and failure counts. Run this to diagnose subscription issues before checking server logs.

### Account state persistence

Account state (including `revoked` status) persists in `var/data/claude_accounts.json` across restarts. A revoked account (e.g., from an `invalid_grant` error) will stay revoked until you re-import fresh credentials.

### Verify Anthropic-native surface

If you are using Claude subscription routing, you can verify the Anthropic-compatible surface directly:

```bash
curl -s http://localhost:8080/anthropic/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: hyi-your-api-key" \
  -d '{
    "model": "claude-sonnet-4.6",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "Say hello"}]
  }'
```

This route uses the same shared Claude account pool as `/v1/chat/completions`, but it only accepts models routed through `provider: claude_sub`.

See `developer/configuration.md`, section `Subscription Adapters (Claude / Codex)`, for full setup details.

## Monitoring

### Health Checks

```bash
curl http://localhost:8080/health
# {"status":"healthy","routes_configured":17,"database_connected":true}
```

### Prometheus Metrics

Metrics at `http://localhost:9090`. Key metrics:
- `http_requests_total` — Total HTTP requests
- `http_request_duration_seconds` — Request latency
- `model_requests_total` — Requests per model

### Grafana Dashboards

Access at `https://<your-domain>/grafana/` (default login: admin/admin).

> **Security note**: The `/grafana/` path is currently public-facing behind Nginx with only
> Grafana's built-in login. Consider adding an IP allowlist or HTTP Basic Auth in the Nginx
> `location ^~ /grafana/` block for an extra layer of protection.

Dashboards are managed via the Grafana UI. To backup/restore:

```bash
# Export current dashboards from UI to repo
./infrastructure/grafana/export-dashboards.sh

# Import repo dashboards into a fresh Grafana instance
./infrastructure/grafana/import-dashboards.sh
```

### Alerting

Three active alert rules: `ServiceDown`, `ServiceUnreachable`, `DatabaseDisconnected`.
Alerts route to Slack `#free-inference-alert` via the allowlist in
`infrastructure/alertmanager/alertmanager.yml`. The Slack webhook URL is
read at runtime from `/etc/freeinference/slack-webhook-url`; see
`infrastructure/alertmanager/README.md` for setup.

## Database

PostgreSQL runs in Docker with data persisted to a named volume (`hybridinference_postgres_data`).

To access the database directly:

```bash
docker exec -it hybridinference-postgres psql -U $DB_USER -d $DB_NAME
```

For pgAdmin (optional):

```bash
# Start with admin profile
docker compose -f infrastructure/docker/docker-compose.yml --env-file .env --profile admin up -d
# Access at http://localhost:5050
```

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
docker compose -f infrastructure/docker/docker-compose.yml --env-file .env down -v
make up
```

> **Warning**: `-v` deletes all named volumes including the database.
