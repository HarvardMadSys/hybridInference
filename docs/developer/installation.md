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

Linux or macOS.

## Running with Docker

```bash
git clone https://github.com/HarvardMadSys/hybridInference.git hybridinference
cd hybridinference
cp .env.example .env
```

Before the first `make up`, fill in three things in `.env`:

1. **The database login.** `DB_USER` and `DB_PASSWORD` ship empty, and
   `DB_NAME` is already set to `hybridinference`. Compose refuses to start
   until all three have values.
2. **Two secrets.** `JWT_SECRET_KEY` signs sign-in tokens and
   `API_KEY_SECRET` is the key the gateway hashes API keys with. The backend
   refuses to start without them. Generate each one separately:

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

3. **A credential for the models it serves.** Out of the box the gateway
   serves three example models through [OpenRouter](https://openrouter.ai),
   from `config/examples/models.openrouter.yaml`. Set `OPENROUTER_API_KEY` and
   they work. To serve your own models instead, see
   [Adding a New Model](adding-models.md).

Store the two secrets with the rest of your deployment's configuration and
keep them for good: reuse them on every restart and replica, and carry them
over on upgrades. Changing `JWT_SECRET_KEY` invalidates the access tokens
already issued, and changing `API_KEY_SECRET` makes every existing API key stop
working. The gateway never generates or rotates them for you.

```{note}
Only a gateway with no database and no accounts can run without the two
secrets: `DB_ENABLED=false` **and** `USER_AUTH_ENABLED=false`, as in Stage 1
of the [Quickstart](router-tutorial.md). `ADMIN_TOKEN` is optional; leaving it
blank turns off only the legacy admin-token access.
```

Then start the stack:

```bash
make up     # creates the external volume, then brings up Compose
make ps     # show the services and their health
curl -s http://localhost:8080/health
```

The first `make up` also creates the Docker volume
`hybridinference_postgres_data`, which holds the database. `docker compose down
-v` does not delete it; see [Resetting the stack](deployment.md#resetting-the-stack)
if you need a clean database.

If you change `.env` later, run `make up` again rather than `make restart`. A
container reads `.env` when it is created, so a restart keeps the old values.

Three containers start:

| Service | Published on | Notes |
|---|---|---|
| `backend` | `127.0.0.1:8080` | FastAPI gateway; override the bind with `BACKEND_HOST` / `BACKEND_PORT` |
| `frontend` | `127.0.0.1:3001` | Next.js console; override with `FRONTEND_HOST` / `FRONTEND_PORT` |
| `postgres` | `127.0.0.1:5432` | override with `DB_PORT` |

These ports are reachable from the host by default. A reverse proxy on the
same host can use the loopback address; access from another machine requires
an explicit host override. See [Deployment Guide](deployment.md) before
exposing the console, which also forwards API requests.

pgAdmin is in the Compose file too, behind the optional `admin` profile.

## Development checkout (no Docker)

```bash
git clone https://github.com/HarvardMadSys/hybridInference.git hybridinference
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

Run the gateway from the repository root, because the default configuration
paths are relative to it:

```bash
cp .env.example .env    # edit as above; a process started here reads it
uv run uvicorn serving.servers.app:app --no-proxy-headers --host 127.0.0.1 --port 8080
```

Run the console in a second terminal:

```bash
cd apps/frontend
npm ci
BACKEND_INTERNAL_URL=http://127.0.0.1:8080 npm run dev -- --hostname 127.0.0.1
```

Open `http://localhost:3001`. The console forwards API paths to the gateway,
and `BACKEND_INTERNAL_URL` tells it where that is. Its default,
`http://backend:8080`, is an address that only exists inside Docker, and the
console does not read the repository's `.env`, so set it each time you start
the console. Check the whole path with `curl http://localhost:3001/health`.

To develop against Postgres without running the whole stack, start just the
database container:

```bash
make docker-volumes
docker compose -f deploy/docker/docker-compose.yml --env-file .env up -d postgres
```

For a local gateway without accounts or a database, set both `DB_ENABLED=false`
and `USER_AUTH_ENABLED=false` in `.env`. It still routes requests, and `/health`
reports `"database_configured": false`. Accounts, API keys and request history
need the database and both authentication secrets.

## Configuration

### Environment variables

`.env` at the repository root is the single file. The backend reads it when
you start it from the repository root, and Compose passes it to the
containers.

Beyond the five required variables in the Docker steps above (`DB_NAME`, `DB_USER`,
`DB_PASSWORD`, `JWT_SECRET_KEY`, `API_KEY_SECRET`) and the `OPENROUTER_API_KEY`
the default registry needs, everything in `.env.example` is optional. The ones you are most likely to want:

| Variable | Effect |
|---|---|
| `USER_AUTH_ENABLED` | `1` (default) requires user API keys for inference requests; `0` allows anonymous inference but does not disable account login or admin authentication |
| `ADMIN_TOKEN` | optional legacy bearer token for the `/admin/*` endpoints; blank disables only this access path |
| `PROVIDER_ROUTE_TYPES` | route types the admin console may add per provider, as `provider=type[\|type]` entries separated by commas (e.g. `chutes=quota,openrouter=concurrency\|on_demand`); blank leaves every provider unrestricted |
| `DB_ENABLED` | `false` runs the gateway with no database |
| `DB_STORE_FULL_CONTENT` | `false` (default) does not store prompts or responses at all; `true` stores them in full. See [Request logging and privacy](database.md#request-logging-and-privacy) |
| `FRONTEND_URL` | absolute URL your users click in verification and reset emails |
| `BASE_URL` | this gateway's own public origin, used to build the absolute links in signup and password-reset emails. Set it: the server does not interpret `X-Forwarded-*`, so a blank value derives `http://…` from the request even behind a TLS proxy. See [Trusted Proxies and Client IPs](trusted-proxies-and-client-ips.md) |
| `LOG_LEVEL`, `LOG_FORMAT` | logging verbosity and `json`/text output |
| `ALERTS_ENABLED`, `SLACK_ALERTS_WEBHOOK_URL` | in-process alerting, off by default |
| `TRUST_PROXY_HEADERS`, `TRUSTED_PROXIES` | trust `X-Forwarded-For` from the proxies listed in `TRUSTED_PROXIES`; the flag has no effect while that list is empty |
| `TRUSTED_DIRECT_CLIENT_NETWORKS` | optional CIDRs for clients that connect directly from a private network; does not authorize forwarding headers |
| `TRUST_CLOUDFLARE_HEADERS`, `TRUSTED_CLOUDFLARE_NETWORKS` | trust Cloudflare's `CF-Connecting-IP` from the listed peers; also needs `TRUST_PROXY_HEADERS=1` |

[Trusted Proxies and Client IPs](trusted-proxies-and-client-ips.md) says which
of the proxy settings above to set for your setup.

### Provider credentials

A model registry names its own credentials. Its `api_key`, `api_keys`,
`base_url` and `provider_model_id` fields can each be written as `${VAR}`,
which is read from the environment when the registry loads. So the provider
keys a deployment needs are the variables its registry names, and you can call
them whatever you like.

```yaml
route:
  - kind: openrouter
    base_url: https://openrouter.ai/api/v1
    api_keys:
      - ${OPENROUTER_API_KEY}
```

Only a whole value is substituted: `${VAR:-default}` and `${VAR}` inside a
longer string are not. See [Configuration](configuration.md) for the details.

`.env.example` ships blank placeholders for the providers this project has
adapters or examples for; add your own names freely. The bundled default
registry needs only `OPENROUTER_API_KEY`.

Once a database is configured, keys can also be added from the admin console
(**Providers → Keys**) with no restart; they join the same pool as the keys the
registry names. See
[Runtime configuration from the admin console](configuration.md#runtime-configuration-from-the-admin-console).

### Where the config files live

The gateway finds its model registry, routing file and alert rules through
environment variables or a distribution's manifest, and falls back to the
examples under `config/examples/` when neither names one.
[How a gateway finds its config](configuration.md#how-a-gateway-finds-its-config)
has the rules, and the rest of that page covers what goes inside the files.

The Compose file mounts both `config/` and `distributions/` read-only into the
backend, so editing either on the host and restarting the backend is enough —
no image rebuild.

```{note}
A backend running directly on the host can reach a host inference server
through `localhost`. A backend in Docker must use a Docker-reachable address
such as `host.docker.internal`, written explicitly in the model registry;
HybridInference does not rewrite provider URLs.
```

## Verifying the install

Against a running gateway:

```bash
curl -s http://localhost:8080/v1/models

curl -s http://localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${HYBRIDINFERENCE_API_KEY}" \
  -d '{"model":"<model-id>","messages":[{"role":"user","content":"Say hello."}]}'
```

Drop the `Authorization` header if you set `USER_AUTH_ENABLED=0`. Use a model id
that `/v1/models` actually listed.

The same call from the OpenAI Python SDK — the gateway is the `base_url`, and
nothing else about an existing client changes:

```python
from openai import OpenAI

client = OpenAI(api_key="<your-api-key>", base_url="http://localhost:8080/v1")

response = client.chat.completions.create(
    model="<model-id>",
    messages=[{"role": "user", "content": "Say hello."}],
)
print(response.choices[0].message.content)
```

In a development checkout, `make check` runs the linters and the default test
suite; [Contributing](contributing.md#quality-gates) lists the other checks.

## Troubleshooting

**`required variable DB_USER is missing a value: DB_USER must be set in .env
file`** — Compose stopped before starting anything. Fill in `DB_USER` and
`DB_PASSWORD` in `.env`, as in step 1 above.

**`env file ... .env not found`** — create it with `cp .env.example .env`
before `make up`.

**Import errors when running from source** — run `make setup-dev` (or
`uv sync --group dev`) so the backend is installed into `.venv`, then start the
gateway with `uv run` or with `.venv` activated.

**Every request 404s with an empty `/v1/models`** — the registry loaded nothing.
On startup the backend logs either `Registered N routes from <path>` or an error
naming the registry path it could not find; compare that path against the
precedence list in [Configuration](configuration.md#how-a-gateway-finds-its-config).

**Port already in use** — override `BACKEND_PORT`, `FRONTEND_PORT` or `DB_PORT`
in `.env`.
