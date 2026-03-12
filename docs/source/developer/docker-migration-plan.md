# Docker Migration Plan

**GitHub Issue**: #147
**Date**: 2026-03-12

## Overview

Migrate all services into a single `docker-compose.yml`. Host Nginx stays on the host (manages SSL/TLS).

### Target Architecture

```
Nginx (host, manages SSL)
  ├→ /v1, /auth, /user, /admin → backend:8080
  ├→ /grafana/                  → grafana:3000
  └→ /*                         → frontend:3001

docker-compose.yml:
  backend        FastAPI           (new Dockerfile)
  frontend       Next.js           (new Dockerfile)
  postgres       PostgreSQL 16     (existing, unchanged)
  prometheus     prom/prometheus   (from systemd → default service)
  alertmanager   prom/alertmanager (from systemd → default service)
  alert-logger   Python script     (new Dockerfile)
  grafana        grafana/grafana   (from apt → default service)
  pgadmin        dpage/pgadmin4    (existing, unchanged)
```

---

## New Files

### 1. `.dockerignore`

```dockerignore
.git
.venv
__pycache__
*.pyc

# Frontend build artifacts
frontend/node_modules
frontend/.next

# Data & logs (never bake into images)
var/
data/

# Dev/test
test/
tests/
docs/

# Binaries on host
prometheus
prometheus-*.linux-amd64/
alertmanager-*.linux-amd64/

# Rollback backup (created during migration)
infrastructure/docker/.rollback-backup/

# IDE
.vscode/
.idea/
*.swp
```

### 2. `infrastructure/docker/Dockerfile.backend`

```dockerfile
# ---- Stage 1: build ----
FROM python:3.12-slim AS builder

RUN pip install uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
# --no-install-project: only install dependencies, not the project itself.
# The project source is copied in the runtime stage and used via PYTHONPATH.
RUN uv sync --frozen --no-dev --no-install-project

# ---- Stage 2: runtime ----
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy virtualenv from builder
COPY --from=builder /app/.venv .venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy application source (only the packages needed at runtime)
COPY serving/ serving/
COPY routing/ routing/
COPY config/ config/

# config/ will be overridden by a read-only volume mount at runtime,
# but we copy a default so the image can run standalone for testing.

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["uvicorn", "serving.servers.app:app", \
     "--host", "0.0.0.0", "--port", "8080", "--no-server-header"]
```

**Key decisions:**
- Uses `uv sync --frozen --no-install-project` to install only dependencies (not the project itself). The project source is copied into the runtime stage and accessed via `PYTHONPATH=/app` (the default `WORKDIR`). This avoids needing `README.md` and the full source tree during the dependency install step.
- `README.md` is still copied to the builder because `pyproject.toml` references it; `uv` validates metadata even with `--no-install-project`.
- Only copies `serving/`, `routing/`, `config/` — no test code, no docs.
- `config/` is volume-mounted at runtime (read-only), so changes to `models.yaml` don't require a rebuild.
- `curl` installed for healthcheck only.

### 3. `infrastructure/docker/Dockerfile.frontend`

```dockerfile
# ---- Stage 1: dependencies ----
FROM node:22-alpine AS deps
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# ---- Stage 2: build ----
FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules node_modules
COPY frontend/ .
RUN npm run build

# ---- Stage 3: runtime ----
FROM node:22-alpine
WORKDIR /app

ENV NODE_ENV=production
ENV PORT=3001

# Copy standalone output (requires output: 'standalone' in next.config.js)
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static .next/static
# Copy public dir if it exists in the future
# COPY --from=builder /app/public public

EXPOSE 3001

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q --spider http://localhost:3001/ || exit 1

CMD ["node", "server.js"]
```

**Key decisions:**
- 3-stage build: deps → build → runtime. Final image is ~150 MB (vs ~1 GB without standalone).
- `output: 'standalone'` must be added to `next.config.js` (see modifications below).
- No `public/` directory exists currently; commented out COPY line for future use.

### 4. `infrastructure/docker/Dockerfile.alert-logger`

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY infrastructure/alertmanager/alert_logger.py .
EXPOSE 5001
CMD ["python", "alert_logger.py", "--port", "5001", "--bind", "0.0.0.0", "--log-dir", "/data"]
```

**Key decisions:**
- Zero external dependencies — just stdlib.
- `--bind 0.0.0.0` is passed only in the Docker CMD; the host source file defaults to `127.0.0.1`. This means no host-side change is needed and rollback is clean.
- Logs to `/data/` inside container, mapped to a named volume.
- `--log-dir /data` overrides the default path resolution (which uses `__file__` relative paths).

### 5. `infrastructure/grafana/provisioning/datasources/datasource.yaml`

UIDs are pinned to match the existing `grafana.db` so dashboards keep working after migration.

```yaml
apiVersion: 1
datasources:
  - name: prometheus
    uid: fexk65coxgoaof
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    editable: false

  - name: freeinference Postgres
    uid: af043ano31dz4f
    type: grafana-postgresql-datasource
    access: proxy
    url: postgres:5432
    editable: false
    jsonData:
      database: ${DB_NAME}
      sslmode: disable
    secureJsonData:
      user: ${DB_USER}
      password: ${DB_PASSWORD}
```

### 6. `infrastructure/grafana/provisioning/dashboards/dashboards.yaml`

```yaml
apiVersion: 1
providers: []
```

Intentionally empty — dashboards are managed via UI and persisted in `grafana_data` volume (not loaded from files). See "Dashboard Sync Strategy" below.

### 7. `infrastructure/grafana/export-dashboards.sh`

Script to export dashboards from running Grafana back to the repo:

```bash
bash infrastructure/grafana/export-dashboards.sh
```

Calls the Grafana API, exports all dashboards as JSON to `infrastructure/grafana/dashboards/`.

---

## Dashboard Sync Strategy

Grafana does not support bidirectional file sync natively. Our approach:

- **Source of truth**: Grafana UI (persisted in `grafana_data` volume)
- **Edit dashboards**: directly in Grafana UI, changes persist immediately
- **Sync to repo**: `bash infrastructure/grafana/export-dashboards.sh`, then `git commit`
- **Restore from repo**: `bash infrastructure/grafana/import-dashboards.sh` (disaster recovery / cold-start)
- **Initial migration**: `grafana.db` is copied to the Docker volume, so existing dashboards carry over automatically
- **File naming**: `<uid>__<slug>.json` — UID is immutable, slug for readability
- **Stale cleanup**: export script automatically removes JSON files for dashboards deleted in UI
- **Repo JSON** (`infrastructure/grafana/dashboards/`) serves as version-controlled backup, not auto-loaded by Grafana

---

## Modified Files

### 1. `frontend/next.config.js`

```diff
  const nextConfig = {
    reactStrictMode: true,
    swcMinify: true,
+   output: 'standalone',
  };
```

Required for the 3-stage Docker build. No effect on non-Docker development (`npm run dev` still works).

### 2. `infrastructure/alertmanager/alert_logger.py` — NO CHANGE

The host file stays as-is (`127.0.0.1`). The Docker container overrides the bind address
via a `--bind` CLI argument (see Dockerfile and the source change below).

### 3. `infrastructure/prometheus/prometheus.yml`

```diff
  scrape_configs:
    - job_name: app
      static_configs:
-       - targets: ["localhost:8080"]
+       - targets: ["backend:8080"]

  alerting:
    alertmanagers:
      - static_configs:
-         - targets: ["localhost:9093"]
+         - targets: ["alertmanager:9093"]
```

Docker service names replace `localhost` — containers are on the same bridge network.

### 4. `infrastructure/alertmanager/alertmanager.yml`

```diff
  - name: alert-logger
    webhook_configs:
-     - url: 'http://127.0.0.1:5001/alerts'
+     - url: 'http://alert-logger:5001/alerts'
```

### 5. `config/models.yaml` — localhost → host.docker.internal

These are local GPU endpoints accessed via SSH tunnel on the host. From inside Docker, we reach the host via `host.docker.internal`.

```diff
  # glm-4.7-flash local endpoint
-       base_url: "http://localhost:8004/v1"
+       base_url: "http://host.docker.internal:8004/v1"

  # qwen3-coder-30b local endpoint
-       base_url: "http://localhost:8003"
+       base_url: "http://host.docker.internal:8003"

  # minimax-m2 local endpoint
-       base_url: "http://localhost:12004"
+       base_url: "http://host.docker.internal:12004"

  # bge-m3 embedding endpoint (gpu-host-8 via SSH tunnel)
-       base_url: "http://localhost:8005"
+       base_url: "http://host.docker.internal:8005"
```

**Note:** Commented-out models (glm-4.6 on `localhost:12003`) are left as-is since they're inactive.

### 6. `config/routing.yaml` — same localhost → host.docker.internal

```diff
  local_deployment:
-   - endpoint: http://localhost:8003
+   - endpoint: http://host.docker.internal:8003
      models:
        - qwen3-coder-30b
-   - endpoint: http://localhost:12003
+   - endpoint: http://host.docker.internal:12003
      models:
        - glm-4.6
-   - endpoint: http://localhost:12004
+   - endpoint: http://host.docker.internal:12004
      models:
        - minimax-m2
-   - endpoint: http://localhost:8004/v1
+   - endpoint: http://host.docker.internal:8004/v1
      models:
        - glm-4.7-flash
```

### 7. `infrastructure/docker/docker-compose.yml` — Full Rewrite

```yaml
# Docker Compose configuration for hybridInference
#
# Usage:
#   Start all:    docker compose -f infrastructure/docker/docker-compose.yml up -d
#   With pgAdmin: docker compose -f infrastructure/docker/docker-compose.yml --profile admin up -d
#   Stop all:     docker compose -f infrastructure/docker/docker-compose.yml down
#   Rebuild:      docker compose -f infrastructure/docker/docker-compose.yml up -d --build

# Pin the project name so volume names are deterministic regardless of CWD.
# Volumes will always be: hybridinference_postgres_data, hybridinference_prometheus_data, etc.
name: hybridinference

services:
  # ===== Application =====

  backend:
    build:
      context: ../..
      dockerfile: infrastructure/docker/Dockerfile.backend
    container_name: hybridinference-backend
    restart: unless-stopped
    env_file: ../../.env
    environment:
      DB_HOST: postgres
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - ../../config:/app/config:ro
    ports:
      - "127.0.0.1:8080:8080"
    depends_on:
      postgres:
        condition: service_healthy
    networks:
      - hybridinference

  frontend:
    build:
      context: ../..
      dockerfile: infrastructure/docker/Dockerfile.frontend
    container_name: hybridinference-frontend
    restart: unless-stopped
    environment:
      NODE_ENV: production
      PORT: "3001"
    ports:
      - "127.0.0.1:3001:3001"
    networks:
      - hybridinference

  # ===== Database =====

  postgres:
    image: postgres:16
    container_name: hybridinference-postgres
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${DB_NAME:?DB_NAME must be set in .env file}
      POSTGRES_USER: ${DB_USER:?DB_USER must be set in .env file}
      POSTGRES_PASSWORD: ${DB_PASSWORD:?DB_PASSWORD must be set in .env file}
      POSTGRES_INITDB_ARGS: "-E UTF8 --locale=C.UTF-8"
    ports:
      - "127.0.0.1:${DB_PORT:-5432}:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USER} -d ${DB_NAME}"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s
    command: >
      postgres
      -c shared_buffers=256MB
      -c max_connections=200
      -c effective_cache_size=1GB
      -c maintenance_work_mem=64MB
      -c checkpoint_completion_target=0.9
      -c wal_buffers=16MB
      -c default_statistics_target=100
      -c random_page_cost=1.1
      -c effective_io_concurrency=200
      -c work_mem=4MB
      -c min_wal_size=1GB
      -c max_wal_size=4GB
    networks:
      - hybridinference

  pgadmin:
    image: dpage/pgadmin4:8
    container_name: hybridinference-pgadmin
    restart: unless-stopped
    environment:
      PGADMIN_DEFAULT_EMAIL: ${PGADMIN_EMAIL:-admin@local.dev}
      PGADMIN_DEFAULT_PASSWORD: ${PGADMIN_PASSWORD:-admin}
      PGADMIN_CONFIG_SERVER_MODE: "${PGADMIN_CONFIG_SERVER_MODE:-False}"
      PGADMIN_CONFIG_MASTER_PASSWORD_REQUIRED: "False"
    ports:
      - "127.0.0.1:${PGADMIN_PORT:-5050}:80"
    volumes:
      - pgadmin_data:/var/lib/pgadmin
    depends_on:
      postgres:
        condition: service_healthy
    networks:
      - hybridinference
    profiles:
      - admin

  # ===== Observability =====

  prometheus:
    image: prom/prometheus:latest
    container_name: hybridinference-prometheus
    restart: unless-stopped
    command:
      - "--config.file=/etc/prometheus/prometheus.yml"
      - "--storage.tsdb.path=/prometheus"
      - "--storage.tsdb.retention.time=30d"
      - "--web.enable-lifecycle"
      - "--web.console.libraries=/usr/share/prometheus/console_libraries"
      - "--web.console.templates=/usr/share/prometheus/consoles"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    ports:
      - "127.0.0.1:9090:9090"
    volumes:
      - ../prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ../prometheus/rules:/etc/prometheus/rules:ro
      - prometheus_data:/prometheus
    networks:
      - hybridinference

  alertmanager:
    image: prom/alertmanager:latest
    container_name: hybridinference-alertmanager
    restart: unless-stopped
    command:
      - "--config.file=/etc/alertmanager/alertmanager.yml"
      - "--storage.path=/alertmanager"
    ports:
      - "127.0.0.1:9093:9093"
    volumes:
      - ../alertmanager/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro
      - alertmanager_data:/alertmanager
    networks:
      - hybridinference

  alert-logger:
    build:
      context: ../..
      dockerfile: infrastructure/docker/Dockerfile.alert-logger
    container_name: hybridinference-alert-logger
    restart: unless-stopped
    volumes:
      - alert_log_data:/data
    networks:
      - hybridinference

  grafana:
    image: grafana/grafana:12.1.1
    container_name: hybridinference-grafana
    restart: unless-stopped
    env_file: ../../.env
    environment:
      GF_SERVER_ROOT_URL: "https://freeinference.org/grafana/"
      GF_SERVER_SERVE_FROM_SUB_PATH: "true"
      GF_SECURITY_ADMIN_USER: ${GRAFANA_USER:-admin}
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_PASSWORD:-admin}
    ports:
      - "127.0.0.1:3000:3000"
    volumes:
      - grafana_data:/var/lib/grafana
      - ../grafana/provisioning:/etc/grafana/provisioning:ro
    depends_on:
      - prometheus
    networks:
      - hybridinference

volumes:
  postgres_data:
    driver: local
  pgadmin_data:
    driver: local
  prometheus_data:
    driver: local
  alertmanager_data:
    driver: local
  alert_log_data:
    driver: local
  grafana_data:
    driver: local

networks:
  hybridinference:
    driver: bridge
```

**Key changes from existing docker-compose.yml:**
- `version: '3.8'` removed (deprecated in modern Docker Compose).
- Added top-level `name: hybridinference` to pin the project name. This makes volume names deterministic (`hybridinference_prometheus_data`, etc.) regardless of which directory you run `docker compose` from.
- `prometheus`, `alertmanager`, `grafana` promoted from `profiles: [observability]` to **default** (always-on).
- Added `backend`, `frontend`, `alert-logger` with `build` directives.
- Added `alertmanager` service (was missing — previously ran via systemd).
- All host ports bound to `127.0.0.1` only (Nginx handles public traffic).
- `--web.enable-lifecycle` added to Prometheus for hot-reload support.
- Grafana uses pinned version `12.1.1` (not `latest`) and sub-path config for `/grafana/`.
- `extra_hosts: host.docker.internal` on `backend` and `prometheus` for SSH tunnel access.
- `env_file: ../../.env` on backend to load API keys and DB credentials.
- `config/` mounted read-only into backend — no image rebuild needed for model changes.
- New volumes: `alertmanager_data`, `alert_log_data`.

---

## Network & Security

| Aspect | Design |
|--------|--------|
| Inter-service | All on `hybridinference` bridge, resolved by service name |
| Host ports | All bound to `127.0.0.1` — no direct public access |
| Public access | Nginx (host) reverse-proxies to `127.0.0.1:{8080,3000,3001}` |
| GPU endpoints | Backend reaches host SSH tunnels via `host.docker.internal` |
| Nginx config | **No changes needed** — already proxies to `127.0.0.1` ports |

---

## Data Migration (Phase 2, step 3)

| Data | Source | Target | Ownership |
|------|--------|--------|-----------|
| PostgreSQL | `postgres_data` volume (existing) | Same volume, untouched | `999:999` (postgres) |
| Prometheus TSDB | `var/prometheus/` | `prometheus_data` volume | `chown -R 65534:65534` |
| Alertmanager state | `var/alertmanager/` | `alertmanager_data` volume | `chown -R 65534:65534` |
| Alert history | `var/log/alert_history.jsonl` | `alert_log_data` volume | default (root) |
| Grafana DB | `/var/lib/grafana/grafana.db` | `grafana_data` volume | `chown -R 472:0` |

**Migration script** (run during Phase 3):

Uses `docker compose run` with temporary containers to copy data directly into
the correct volumes. This avoids hardcoding volume names (which depend on the
Compose project name) and avoids needing `sudo` to access host volume paths.

```bash
#!/bin/bash
set -euo pipefail

COMPOSE="docker compose -f infrastructure/docker/docker-compose.yml"
PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# 1. Create containers + volumes (without starting them)
$COMPOSE up --no-start

# 2. Copy Prometheus TSDB (prometheus container has prometheus_data:/prometheus)
docker compose -f infrastructure/docker/docker-compose.yml \
    run --rm --no-deps \
    -v "$PROJECT_ROOT/var/prometheus:/source:ro" \
    --entrypoint sh prometheus \
    -c "cp -a /source/. /prometheus/"
# Prometheus image runs as nobody (65534) — data is already owned correctly.

# 3. Copy Alertmanager state (alertmanager container has alertmanager_data:/alertmanager)
docker compose -f infrastructure/docker/docker-compose.yml \
    run --rm --no-deps \
    -v "$PROJECT_ROOT/var/alertmanager:/source:ro" \
    --entrypoint sh alertmanager \
    -c "cp -a /source/. /alertmanager/"

# 4. Copy alert history (alert-logger container has alert_log_data:/data)
docker compose -f infrastructure/docker/docker-compose.yml \
    run --rm --no-deps \
    -v "$PROJECT_ROOT/var/log:/source:ro" \
    --entrypoint sh alert-logger \
    -c "cp /source/alert_history.jsonl /data/"

# 5. Copy Grafana data (grafana container has grafana_data:/var/lib/grafana)
#    Must run as root to copy, then chown to grafana user (472).
docker compose -f infrastructure/docker/docker-compose.yml \
    run --rm --no-deps --user root \
    -v "/var/lib/grafana:/source:ro" \
    --entrypoint sh grafana \
    -c "cp -a /source/. /var/lib/grafana/ && chown -R 472:0 /var/lib/grafana/"

echo "Data migration complete. Run: $COMPOSE up -d"
```

---

## Execution Phases

### Phase 1: Prepare (zero downtime, can be merged to main)

These changes are safe to apply while systemd services are still running:

1. Create `.dockerignore`
2. Create `Dockerfile.backend`, `Dockerfile.frontend`, `Dockerfile.alert-logger`
3. Create Grafana provisioning files
4. Add `output: 'standalone'` to `frontend/next.config.js`
5. Add `--bind` CLI argument to `alert_logger.py` (backward-compatible, defaults to `127.0.0.1`)
6. Rewrite `docker-compose.yml` (no effect until `docker compose up`)
7. Build & test images locally: `docker compose build`

### Phase 2: Switchover (~30s downtime)

Config changes and service cutover happen atomically. No window where
systemd services could restart and read Docker-specific configs.

```bash
# 0. Create a rollback savepoint (BEFORE modifying any config)
#    Plain file copy — no git gymnastics, no ambiguity.
ROLLBACK_DIR="infrastructure/docker/.rollback-backup"
mkdir -p "$ROLLBACK_DIR"
cp config/models.yaml "$ROLLBACK_DIR/"
cp config/routing.yaml "$ROLLBACK_DIR/"
cp infrastructure/prometheus/prometheus.yml "$ROLLBACK_DIR/"
cp infrastructure/alertmanager/alertmanager.yml "$ROLLBACK_DIR/"
echo "Backup saved to $ROLLBACK_DIR/"

# 1. Stop ALL systemd services first
sudo systemctl stop hybrid_inference freeinference-frontend \
    prometheus alertmanager alert-logger grafana-server

# 2. Now apply config changes (services are stopped, no race condition)
#    - prometheus.yml: localhost:8080 → backend:8080, localhost:9093 → alertmanager:9093
#    - alertmanager.yml: 127.0.0.1:5001 → alert-logger:5001
#    - config/models.yaml: localhost → host.docker.internal (active routes only)
#    - config/routing.yaml: localhost → host.docker.internal
#    (These changes are detailed in the "Modified Files" section above)

# 3. Run data migration script
bash infrastructure/docker/migrate-data.sh

# 4. Start Docker stack
docker compose -f infrastructure/docker/docker-compose.yml up -d
```

### Phase 3: Cleanup (after 24h verification)

```bash
sudo systemctl disable hybrid_inference freeinference-frontend \
    prometheus alertmanager alert-logger grafana-server
# After full verification:
# sudo apt remove grafana
```

---

## Verification

```bash
# Service status
docker compose -f infrastructure/docker/docker-compose.yml ps

# Backend API
curl -s http://127.0.0.1:8080/health
curl -s http://127.0.0.1:8080/v1/models | head

# Frontend
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3001/

# Prometheus targets
curl -s http://127.0.0.1:9090/api/v1/targets | python3 -m json.tool | head

# Grafana (sub-path)
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3000/grafana/login

# Via Nginx (public)
curl -s https://freeinference.org/v1/models | head
curl -s -o /dev/null -w "%{http_code}" https://freeinference.org/grafana/login

# GPU endpoint (SSH tunnel via host.docker.internal)
docker compose -f infrastructure/docker/docker-compose.yml \
    exec backend curl -s http://host.docker.internal:8005/health
```

---

## Rollback

If anything goes wrong after cutover:

```bash
# 1. Stop Docker stack
docker compose -f infrastructure/docker/docker-compose.yml down

# 2. Restore config files from the backup created in Phase 2 step 0.
ROLLBACK_DIR="infrastructure/docker/.rollback-backup"
cp "$ROLLBACK_DIR/models.yaml" config/models.yaml
cp "$ROLLBACK_DIR/routing.yaml" config/routing.yaml
cp "$ROLLBACK_DIR/prometheus.yml" infrastructure/prometheus/prometheus.yml
cp "$ROLLBACK_DIR/alertmanager.yml" infrastructure/alertmanager/alertmanager.yml

# 3. Restart systemd services
sudo systemctl start hybrid_inference freeinference-frontend \
    prometheus alertmanager alert-logger grafana-server
```

- PostgreSQL data is on the original named volume — completely unaffected.
- Config files are restored from plain file copies in `infrastructure/docker/.rollback-backup/`. No git operations involved — works regardless of working tree state.
- After successful migration (Phase 3 done, 24h verified), clean up: `rm -rf infrastructure/docker/.rollback-backup/`.
