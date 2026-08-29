# Running a Non-Production Instance

A staging (or preview, or scratch) instance is the same stack as production,
started from the same `deploy/docker/docker-compose.yml`, on a machine you are
willing to break. This page covers the parts that differ from a production
deployment: what the compose file actually starts, how to keep it off the
network, how to get an admin account without opening a privilege-escalation
hole, and how to throw the database away.

For the production deployment procedure see [Deployment Guide](deployment.md); for a
zero-dependency tour of the routing engine see the
[Quickstart](router-tutorial.md).

## What the compose file starts

`deploy/docker/docker-compose.yml` defines five services:

| Service | Started by default | Notes |
|---|---|---|
| `backend` | yes | the FastAPI gateway |
| `frontend` | yes | the Next.js console |
| `postgres` | yes | `postgres:16` |
| `pgadmin` | no | Compose profile `admin` |
| `codex-oncall` | no | Compose profile `oncall`, see [Codex On-Call](codex-oncall.md) |

There are no metrics, tracing, or dashboard services in this file. If you want
observability, you run it yourself alongside the stack.

Profiles are opted into per invocation or through `.env`:

```bash
make up COMPOSE_PROFILES=admin
```

## Published ports

Every address below is where the port is published *on the host*; the
container-side port is fixed and is not what these variables change.

| Service | Default publish address | Override |
|---|---|---|
| `backend` | `127.0.0.1:8080` | `BACKEND_HOST`, `BACKEND_PORT` |
| `frontend` | `0.0.0.0:3001` | `FRONTEND_HOST`, `FRONTEND_PORT` |
| `postgres` | `127.0.0.1:5432` | `DB_PORT` |
| `pgadmin` (profile `admin`) | `127.0.0.1:5050` | `PGADMIN_PORT` |
| `codex-oncall` (profile `oncall`) | `127.0.0.1:8091` | `CODEX_ONCALL_PORT` |

Only `backend` and `frontend` take a host override. The other three have
`127.0.0.1` hard-coded in the Compose file, so their `*_PORT` variable moves the
port but never the bind address.

### The console default is not loopback — change it

The frontend is the one service whose default host is `0.0.0.0`, so out of the
box it answers on every interface of the machine. That is not only a console
exposure: `apps/frontend/next.config.js` rewrites `/v1/*`, `/anthropic/*`,
`/auth/*`, `/user/*`, `/admin/*`, `/health` and several `/internal/*` paths to
`BACKEND_INTERNAL_URL`. Anyone who can reach port 3001 can therefore reach the
API that the `127.0.0.1:8080` publish was meant to keep private.

If you intend to reach the instance over an SSH tunnel (the next section), pin
the console to loopback as well:

```bash
FRONTEND_HOST=127.0.0.1
```

If you instead want the instance reachable on a network, put it behind a
reverse proxy that terminates TLS and authenticates, and treat every rewritten
path above as publicly exposed.

## Configuration: which models it serves

There is no `config/models.yaml` or `config/routing.yaml` at the repository
root. The backend resolves each config file in this order
(`apps/backend/serving/config/distribution.py`):

1. an explicit environment variable — `MODELS_CONFIG_PATH`, `ROUTING_CONFIG_PATH`,
   `ALERTS_CONFIG_PATH`;
2. the `paths:` section of the distribution manifest named by
   `DISTRIBUTION_CONFIG_PATH`, but **only** when `DISTRIBUTION_CONFIG_MODE=active`
   — the default mode `dark` loads and validates the manifest, logs what it
   *would* change, and changes nothing;
3. the built-in fallbacks, which are `config/examples/models.openrouter.yaml`
   and `config/examples/routing.minimal.yaml`.

So a fresh clone with an empty `.env` starts with the shipped example registry
and needs no config variables at all. A staging instance that should mirror a
real deployment points at that deployment's overlay under
`distributions/<name>/config/`; the compose file mounts `distributions/`
read-only into the container, so the paths you name are container paths such as
`/app/distributions/<name>/config/models.yaml`.

Two of the three fall back silently when the file is missing — only a missing
model registry is loud, and its symptom is an empty `/v1/models` and a 404 for
every request. Check the backend log on startup.

## Environment

Compose refuses to start without `DB_NAME`, `DB_USER` and `DB_PASSWORD`
(they are declared `${VAR:?...}`). The gateway itself wants two more secrets:

```bash
DB_NAME=hybridinference
DB_USER=postgres
DB_PASSWORD=<generated>

# python -c "import secrets; print(secrets.token_urlsafe(48))"
JWT_SECRET_KEY=<generated>
API_KEY_SECRET=<generated>
```

Neither secret is enforced at startup: an empty `JWT_SECRET_KEY` or
`API_KEY_SECRET` logs one `critical` line and the gateway keeps going with
insecure tokens and insecure API-key hashing
(`apps/backend/serving/servers/app.py`). Generate both.

`CORS_ALLOWED_ORIGINS` already defaults to eight origins — ports 3000, 3001 and
3002 on both `http://localhost` and `http://127.0.0.1`, plus
`https://localhost:8443` and `https://127.0.0.1:8443`
(`Settings.cors_allowed_origins`) — so a tunnelled instance needs no CORS entry.
Add one only when you serve the console from some other origin.

`.env.example` is the full list; copy it and fill in what you need.

## Accounts, and how not to hand out admin

```{warning}
Do not combine `SIGNUP_ENABLED=1`, `SIGNUP_REQUIRE_EMAIL_VERIFICATION=0` and
`ADMIN_EMAILS` on an instance anyone else can reach. Together they are a
privilege-escalation recipe:

- with verification disabled, `POST /auth/signup` creates the account with
  `email_verified` set to the negation of `require_verification` — that is,
  already verified — without ever sending mail to the address
  (`apps/backend/serving/servers/routers/auth_routes.py`);
- on login *and* on every token refresh, the backend promotes any account whose
  address is listed in `ADMIN_EMAILS` from `free` to `admin`, with no check
  that the person signing up owns that address (same file, and
  `is_admin_email` in `apps/backend/serving/config/settings.py`).

So a stranger who guesses or reads your `ADMIN_EMAILS` value signs up with that
address and is an admin on their first login.
```

Pick one of these instead.

**Preferred — create the admin out of band and leave `ADMIN_EMAILS` unset.**
`ops/admin/create_admin.py` writes the row directly: it creates the account with
`role='admin'`, `status='active'`, `email_verified=TRUE`, or promotes an
existing account with the same address. Run it from the repository root once the
backend has started at least once (the backend creates the schema):

```bash
python ops/admin/create_admin.py --email you@example.com
```

Run it inside the project environment (`source .venv/bin/activate` after
`make setup-dev`) so the `serving` package is importable. It reads `DB_HOST`,
`DB_PORT`, `DB_NAME`, `DB_USER` and `DB_PASSWORD` from `.env`, and Postgres
publishes on `127.0.0.1:5432`, so it works from the host shell. Omit
`--password` and it prompts, keeping the password out of your shell history.
With this in place the instance can run with signup closed:

```bash
USER_AUTH_ENABLED=1
SIGNUP_ENABLED=0
```

**Alternative — keep signup open, but leave verification on.**
`SIGNUP_REQUIRE_EMAIL_VERIFICATION` defaults to `true`, and with it on an
account cannot log in until it has followed a link sent to the address, which
restores the ownership check that `ADMIN_EMAILS` itself does not perform. This
needs working SMTP; without it nobody can complete a signup.

`ADMIN_EMAILS` also picks the default recipients for signup approval mail. To
narrow the notification list without changing who holds the admin role, set
`SIGNUP_NOTIFY_EMAILS` (comma-separated); when it is empty, notifications fall
back to `ADMIN_EMAILS`
(`get_signup_notify_emails` in `apps/backend/serving/config/settings.py`).

```bash
SIGNUP_NOTIFY_EMAILS=you@example.com
```

Both `signup_enabled` and `signup_require_email_verification` can also be
flipped at runtime through the settings store, and the runtime value wins over
the environment. A `.env` line is the starting point, not a guarantee.

## Start it

```bash
make up          # start
make build       # rebuild images from the checkout, then start
```

`make up` and `make build` first create the external Docker volume
`hybridinference_postgres_data` if it does not exist.

## Reach it over an SSH tunnel

With both services pinned to loopback on the server, forward them from your
workstation:

```bash
ssh -L 3001:127.0.0.1:3001 -L 8080:127.0.0.1:8080 <user>@<your-server>
```

Then open `http://localhost:3001`. Keep the local end on `localhost` or
`127.0.0.1`: the refresh cookie is issued with the `Secure` flag by default
(`COOKIE_SECURE`), and browsers accept `Secure` cookies only over HTTPS or from
a loopback origin.

The console talks to the API at whatever `NEXT_PUBLIC_API_BASE` was baked in at
image build time — `http://localhost:8080` by default — which is why the tunnel
forwards 8080 as well. Changing it is a frontend rebuild, not a restart.

VS Code-family editors can manage the same forwards from their ports panel.

Check the stack answers:

```bash
curl -s http://localhost:8080/health
curl -s http://localhost:8080/v1/models
```

## Day-to-day

```bash
make ps                    # container status
make logs s=backend        # tail one service
make restart s=frontend    # restart one service
make build s=backend       # rebuild and restart one service
make down                  # stop everything, keep the data
```

## Reset the database

`postgres_data` is declared `external: true` in the compose file, which means
neither `docker compose down -v` nor `make down` (which passes no `--volumes` at
all) will delete it — a reset relying on either silently leaves every row in
place. Removing it takes an explicit step:

```bash
make down
docker volume rm hybridinference_postgres_data
make up
```

[Database](database.md#reset) is the canonical copy of this procedure, including
what the backend rebuilds afterwards.

`make up` recreates the empty volume, and the backend rebuilds the schema on
startup. Every account, API key and request log is gone, so re-create the admin
account afterwards.
