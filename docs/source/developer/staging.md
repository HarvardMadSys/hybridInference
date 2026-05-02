# Staging Guide

This staging setup is fully Dockerized. The backend, frontend, database, and
observability services all run under the same
`infrastructure/docker/docker-compose.yml` that production uses.

## Why this staging shape

- one command starts the full stack
- no per-worktree systemd unit is required
- the running services match the current checkout more closely
- SSH port forwarding stays simple and predictable

## Default ports

These are the ports exposed by the production compose file:

- Backend API: `8080` (bound to `127.0.0.1`)
- Frontend: `3001` (bound to `0.0.0.0`)
- PostgreSQL: `${DB_PORT:-5432}` (bound to `127.0.0.1`)
- pgAdmin: `5050` when the `admin` profile is enabled (bound to `127.0.0.1`)

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
NEXT_PUBLIC_API_BASE=http://localhost:8080
CORS_ALLOWED_ORIGINS=http://localhost:3001,http://localhost:3000

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
make build
```

This command:

- ensures Docker volumes exist
- builds the current backend and frontend from the worktree
- starts the full stack with Docker Compose

## SSH forwarding

From your laptop:

```bash
ssh -L 3001:127.0.0.1:3001 -L 8080:127.0.0.1:8080 <user>@staging-internal
```

If using VScode-family IDE, setting forwarded ports in IDE GUI is more convenient.

Then open:

```text
http://localhost:3001
```

The staged frontend is built to talk to:

```text
http://localhost:8080
```

## Common operations

Rebuild images and restart all services:

```bash
make build
```

Rebuild and restart a single service:

```bash
make build s=backend
```

Tail backend logs:

```bash
make logs s=backend
```

Show container status:

```bash
make ps
```

Restart a single service:

```bash
make restart s=frontend
```

Reset the database completely:

```bash
docker compose -f infrastructure/docker/docker-compose.yml --env-file .env down -v
make up
```

## Notes for model monitor

- the monitor UI requires an `internal` or `admin` user
- `llm-prober` is not part of the staging compose file today, so direct-baseline
  panels may show an unavailable state
- routed traffic panels still work without `llm-prober`
