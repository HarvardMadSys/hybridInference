# Installation

How to get a HybridInference gateway running, either as the Docker stack or as
a source checkout you can edit.

If you would rather see a gateway answer a request before configuring anything,
start with the [Quickstart](router-tutorial.md). It runs a deterministic
fake provider and needs no provider account, no API key and no `.env` at all.
This page is the next step: your own deployment, with your own providers.

## What you need

| For | Requirement |
|---|---|
| The Docker stack | Docker Engine 24+ and Docker Compose v2+ |
| A source checkout | Python 3.10–3.13 (3.12 recommended, per `pyproject.toml`) and [uv](https://github.com/astral-sh/uv) |
| Running the console outside Docker | Node.js 22 (the version CI installs) |

Linux or macOS. Every dependency resolves from PyPI, so a plain `uv sync`
needs nothing but network access to the index.

## Quick start with Docker

```bash
git clone <repository-url> hybridinference
cd hybridinference
cp .env.example .env
```

Now edit `.env`. **`make up` aborts before starting anything unless these three
have values** — `deploy/docker/docker-compose.yml` declares them with Compose's
`:?` required form, and Compose fails at interpolation, not at runtime:

| Variable | How `.env.example` ships it |
|---|---|
| `DB_NAME` | already set to `hybridinference` — leave it or rename |
| `DB_USER` | **empty; you must fill it in** |
| `DB_PASSWORD` | **empty; you must fill it in** |

Two more are not enforced by Compose but should be set before anyone signs up:
`JWT_SECRET_KEY` and `API_KEY_SECRET`. Both ship empty, and
`apps/backend/serving/servers/app.py` logs a `CRITICAL` line and keeps running
with insecure tokens and insecure API-key hashing if they stay that way.
Generate each one:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Then start the stack:

```bash
make up     # creates the external volume, then brings up Compose
make ps     # show the services and their health
curl -s http://localhost:8080/health
```

`make up` runs the Makefile's `docker-volumes` target first, which creates the
Docker volume `hybridinference_postgres_data` if it does not exist. The volume
is declared `external: true` in the Compose file, so Compose itself never
creates or deletes it — see
[Deployment](deployment.md#resetting-the-stack) before you try to wipe the
database.

Three containers start:

| Service | Published on | Notes |
|---|---|---|
| `backend` | `127.0.0.1:8080` | FastAPI gateway; override the bind with `BACKEND_HOST` / `BACKEND_PORT` |
| `frontend` | `0.0.0.0:3001` | Next.js console; override with `FRONTEND_HOST` / `FRONTEND_PORT` |
| `postgres` | `127.0.0.1:5432` | override with `DB_PORT` |

pgAdmin and the Codex on-call relay are in the Compose file too but are gated
behind profiles, so nothing starts them unless you ask (see
[Deployment Guide](deployment.md)).

### Which models the fresh stack serves

A clone with no model registry of its own is not empty. When neither
`MODELS_CONFIG_PATH` nor an active distribution manifest names a file, the
backend falls back to the bundled reference registry
`config/examples/models.openrouter.yaml`, paired with
`config/examples/routing.minimal.yaml`
(`apps/backend/serving/config/distribution.py`, `_LEGACY_DEFAULTS`). That
registry routes through [OpenRouter](https://openrouter.ai) and needs exactly
one credential:

```bash
OPENROUTER_API_KEY=...   # in .env
```

Run `make up` again afterwards, not `make restart`: a container reads its
`env_file` when it is created, so `docker compose restart` leaves the old
environment in place, while `up` recreates the service whose configuration
changed. To point the gateway at your own registry instead, see
[Configuration](#configuration) below and
[Adding a New Model](adding-models.md).

## Development checkout (no Docker)

```bash
git clone <repository-url> hybridinference
cd hybridinference

make setup-dev
```

`make setup-dev` creates `.venv` on Python 3.12 (and refuses to continue if an
existing `.venv` is on a different minor version), installs the project
editable, syncs the `dev` dependency group and installs the pre-commit hooks.
To do it by hand:

```bash
uv venv -p 3.12
source .venv/bin/activate
uv sync --group dev
```

Run the gateway from the repository root — the backend packages live under
`apps/backend/`, which is why `PYTHONPATH` is set:

```bash
cp .env.example .env    # edit as above; a process started here reads it
PYTHONPATH=apps/backend uv run uvicorn serving.servers.app:app \
  --host 127.0.0.1 --port 8080
```

Run the console in a second terminal:

```bash
cd apps/frontend
npm ci
npm run dev            # listens on :3001
```

To develop against Postgres without running the whole stack, start just the
database container:

```bash
docker compose -f deploy/docker/docker-compose.yml --env-file .env up -d postgres
```

Or set `DB_ENABLED=false` in `.env` and run with no database at all: the gateway
still routes requests, and `/health` reports `"database_configured": false`.
Accounts, API keys and request history need the database.

## Configuration

### Environment variables

`.env` at the repository root is the single file. Two things read it:

- the backend process, when you start it from the repository root
  (`Settings.Config.env_file = ".env"` in
  `apps/backend/serving/config/settings.py`);
- the containers, because Compose is invoked with `--env-file .env` and the
  backend service also lists it as `env_file`.

Beyond the five required variables in the quick start (`DB_NAME`, `DB_USER`,
`DB_PASSWORD`, `JWT_SECRET_KEY`, `API_KEY_SECRET`) and the `OPENROUTER_API_KEY`
the default registry needs, everything in `.env.example` is optional. The ones you are most likely to want:

| Variable | Effect |
|---|---|
| `USER_AUTH_ENABLED` | `1` (default) requires an API key on `/v1/*`; `0` allows every request |
| `ADMIN_TOKEN` | bearer token for the `/admin/*` endpoints |
| `DB_ENABLED` | `false` runs the gateway with no database |
| `DB_STORE_FULL_CONTENT` | `false` (default) hashes prompts and responses instead of storing them |
| `FRONTEND_URL` | absolute URL your users click in verification and reset emails |
| `BASE_URL` | this gateway's own public origin; blank derives it from the request |
| `LOG_LEVEL`, `LOG_FORMAT` | logging verbosity and `json`/text output |
| `ALERTS_ENABLED`, `SLACK_ALERTS_WEBHOOK_URL` | in-process alerting, off by default |
| `TRUST_PROXY_HEADERS` | whether `X-Forwarded-For` / `X-Real-IP` are believed |

### Provider credentials

There is no fixed list of provider variables in the code. A model registry
interpolates `${VAR}` and `${VAR:-default}` when it is loaded
(`_expand_env_value` in `apps/backend/routing/config.py`), so the provider
credentials a deployment needs are exactly the variables its own registry names:

```yaml
route:
  - kind: openrouter
    base_url: https://openrouter.ai/api/v1
    api_keys:
      - ${OPENROUTER_API_KEY}
```

`.env.example` ships blank placeholders for the providers this project has
adapters or examples for; add your own names freely. The bundled default
registry needs only `OPENROUTER_API_KEY`.

### Where the config files live

There is no `config/models.yaml` or `config/routing.yaml` in this repository.
`resolve_config_path` in `apps/backend/serving/config/distribution.py` resolves
each config file in this order:

1. **an explicit environment variable** — `MODELS_CONFIG_PATH`,
   `ROUTING_CONFIG_PATH`, `ALERTS_CONFIG_PATH` (the older `MODELS_CONFIG` and
   `ROUTING_CONFIG` spellings are still accepted; the `*_CONFIG_PATH` name wins
   when both are set);
2. **a distribution manifest** — `DISTRIBUTION_CONFIG_PATH` pointing at a
   `distributions/<name>/distribution.yaml`, whose `paths:` section names the
   files. The manifest only takes effect with
   `DISTRIBUTION_CONFIG_MODE=active`; the default `dark` loads and validates it
   and logs what it *would* change while resolution stays as it was;
3. **the built-in defaults** — `config/examples/models.openrouter.yaml` and
   `config/examples/routing.minimal.yaml`. There is no default alerts file, and
   its absence means the built-in thresholds apply.

Paths are relative to the working directory (`/app` in the container). The
Compose file mounts both `config/` and `distributions/` read-only into the
backend, so editing either on the host and restarting the backend is enough —
no image rebuild.

See [Configuration](configuration.md) for what goes *inside* those files, and
[Quickstart](router-tutorial.md) for a worked overlay.

```{note}
A backend running directly on the host can reach a host inference server
through `localhost`. A backend in Docker must use a Docker-reachable address
such as `host.docker.internal`, written explicitly in the model registry;
HybridInference does not rewrite provider URLs.
```

## Verifying the install

```bash
make lint     # ruff format --check, ruff check, pydocstyle
make test     # pytest, excluding the external and dbtest tiers
make check    # lint + test
make format   # ruff format, then ruff check --fix --unsafe-fixes
```

End to end, against a running gateway:

```bash
curl -s http://localhost:8080/v1/models

curl -s http://localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${HYBRIDINFERENCE_API_KEY}" \
  -d '{"model":"<model-id>","messages":[{"role":"user","content":"Say hello."}]}'
```

Drop the `Authorization` header if you set `USER_AUTH_ENABLED=0`. Use a model id
that `/v1/models` actually listed.

## Documentation

These developer docs are MyST Markdown built with Sphinx from `docs/developer/`.
Building them locally and the checks that gate them are covered in
[Contributing](contributing.md#documentation).

## Troubleshooting

**`required variable DB_USER is missing a value: DB_USER must be set in .env
file`** — Compose stopped at interpolation. Fill in `DB_USER` and `DB_PASSWORD` in `.env`; see the table
above.

**`env file ... .env not found`** — the backend service reads `../../.env`
relative to `deploy/docker/`, i.e. `.env` at the repository root. `cp
.env.example .env` before `make up`.

**Import errors when running from source** — run from the repository root with
`PYTHONPATH=apps/backend`, and make sure the virtualenv is active (or use
`uv run`).

**Every request 404s with an empty `/v1/models`** — the registry loaded nothing.
On startup the backend logs either `Registered N routes from <path>` or an error
naming the registry path it could not find; compare that path against the
precedence list above.

**Port already in use** — override `BACKEND_PORT`, `FRONTEND_PORT` or `DB_PORT`
in `.env`.
