# Staging Guide

This staging setup is fully Dockerized. The backend, frontend, database, and
observability services all run under
[docker-compose.staging.yml](../../../infrastructure/docker/docker-compose.staging.yml).

## Why this staging shape

- one command starts the full stack
- no per-worktree systemd unit is required
- the running services match the current checkout more closely
- SSH port forwarding stays simple and predictable

## Default ports

- Frontend: `3002`
- Backend API: `8000`
- PostgreSQL: `5433`
- pgAdmin: `5051` when the `admin` profile is enabled

## First-time server bootstrap

Run this once on a fresh staging server:

```bash
cd /home/to/dir/scripts/staging
chmod +x *.sh
./bootstrap_server.sh
newgrp docker
```

## Required env for a typical staging run

In `.env`, set at least:

```bash
DB_NAME=freeinference_db
DB_USER=postgres
DB_PASSWORD=...
JWT_SECRET_KEY=...
API_KEY_SECRET=...

NEXT_PUBLIC_DEPLOY_TARGET=staging
NEXT_PUBLIC_API_BASE=http://localhost:8000
BACKEND_PORT=8000
FRONTEND_PORT=3002
CORS_ALLOWED_ORIGINS=http://localhost:3002,http://localhost:3001,http://localhost:3000

USER_AUTH_ENABLED=1
SIGNUP_ENABLED=1
SIGNUP_REQUIRE_APPROVAL=0
SIGNUP_REQUIRE_EMAIL_VERIFICATION=0
```

If you want admin bootstrap on login, also set:

```bash
ADMIN_EMAILS=you@example.com
```

## Start staging

```bash
cd /path/to/dir
make staging-up
```

This command:

- pulls prebuilt infra images
- builds the current backend and frontend from the worktree
- starts the full staging stack with Docker Compose

You can still use [start_staging.sh](../../../scripts/staging/start_staging.sh)
directly, but `make staging-up` is the recommended day-to-day entry point.

## SSH forwarding

From your laptop:

```bash
ssh -L 3002:127.0.0.1:3002 -L 8000:127.0.0.1:8000 <user>@staging-internal
```

If using VScode-family IDE, setting forwarded ports in IDE GUI is more convenient.

Then open:

```text
http://localhost:3002
```

The staged frontend is built to talk to:

```text
http://localhost:8000
```

## Common operations

Restart the full staging stack:

```bash
make staging-build
```

Tail backend logs:

```bash
make staging-logs s=backend
```

Show staging container status:

```bash
make staging-ps
```

Restart a single staging service:

```bash
make staging-restart s=frontend
```

Reset the staging database completely:

```bash
docker compose -f infrastructure/docker/docker-compose.staging.yml --env-file .env down -v
make staging-up
```

## Notes for model monitor

- the monitor UI requires an `internal` or `admin` user
- `llm-prober` is not part of the staging compose file today, so direct-baseline
  panels may show an unavailable state
- routed traffic panels still work without `llm-prober`
