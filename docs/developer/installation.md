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

Before the first `make up`, fill in the database login in `.env`: `DB_USER`
and `DB_PASSWORD` ship empty, and `DB_NAME` is already set to
`hybridinference`. Compose refuses to start until all three have values.
Nothing else in `.env` is required. The gateway keeps its other settings —
provider keys, SMTP, proxy trust and the rest — in its database, and you fill
them in from the console after the first start.

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

`.env` keeps only the database connection and the container settings. If you
change one of those later, run `make up` again rather than `make restart`: a
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

### First-run setup

On its first start against an empty database the backend generates the two
secrets it cannot run without — `JWT_SECRET_KEY`, which signs sign-in tokens,
and `API_KEY_SECRET`, which protects API keys — and stores them in the
database. It then waits for an administrator: it creates a one-time setup
code, keeps it in the database, and prints it in its log:

```bash
docker logs hybridinference-backend 2>&1 | grep 'setup code'
```

Open the console at `http://localhost:3001`, which sends you to its setup page,
and:

1. Enter the setup code. It stays the same until setup is done, and the
   backend logs it again every time it starts, whatever `LOG_LEVEL` is set to.
   Until setup is done the backend also refuses sign-ups, so nobody else can
   claim the first account.
2. Create the administrator: a username and a password. The account has no
   email address; it signs in with the username. Pick a username that is hard
   to guess, not `admin`: sign-in allows five attempts per username in 15
   minutes, counted before the password is checked, so anyone who knows the
   username can lock the account out.
3. Fill in the settings the page lists. Out of the box the gateway serves the
   three example models in `config/examples/models.openrouter.yaml`, which all
   use [OpenRouter](https://openrouter.ai) (one tries a local Ollama server
   first), so `OPENROUTER_API_KEY` is listed as missing: set it and they work.
   To serve your own models instead, see [Adding a New Model](adding-models.md).
4. Finish. The model registry reads provider credentials when the backend
   starts, so the last step offers **Restart backend** when anything you saved
   needs it.

Everything set here can be changed later on the admin console's
**Configuration** tab; [Settings stored in the database](configuration.md#settings-stored-in-the-database)
explains how. [The first admin account](deployment.md#the-first-admin-account)
covers the other ways to create an administrator and how to reset its
password.

```{note}
Keep the database with its secrets. `API_KEY_SECRET` cannot be changed once it
is set, because a new value would make every existing API key stop working,
and changing `JWT_SECRET_KEY` signs every user out. `ERASURE_FENCE_SECRET`,
which protects the records of deleted accounts, takes `API_KEY_SECRET`'s value
unless you set it in `.env` before the first start, and it cannot change
afterwards either; see [Database](database.md#secrets-for-deleting-accounts).
A database dump carries these secrets and every provider key.
```

A gateway without a database (`DB_ENABLED=false`) has no setup step and no
stored settings: it reads every setting from the environment, and with user
authentication on it needs `JWT_SECRET_KEY` and `API_KEY_SECRET` there. Only a
gateway with no database and no accounts runs without them —
`DB_ENABLED=false` **and** `USER_AUTH_ENABLED=false`, as in Stage 1 of the
[Quickstart](router-tutorial.md).

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

With a database, the setup code of [First-run setup](#first-run-setup) appears
in this terminal's log.

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
reports `"database_configured": false`. Accounts, API keys, request history
and the settings stored in the database all need the database.

## Configuration

### Environment variables

`.env` at the repository root holds what the backend needs before it can reach
its database, and what describes the containers rather than the application:
the database connection (`DB_*`), host ports and bind addresses, the
config-file paths, and a few process and console values. `.env.example` lists
them. The backend reads `.env` when you start it from the repository root, and
Compose passes it to the containers.

Every other setting lives in the database and is edited on the admin console's
**Configuration** tab, which describes each one; the runtime switches, such as
`USER_AUTH_ENABLED`, are on the **Settings** tab. A value in the environment is
only a starting point: at startup the backend copies it into the database when
the database has none, and from then on the stored value wins. See
[Settings stored in the database](configuration.md#settings-stored-in-the-database).

The settings you are most likely to want:

| Setting | Effect |
|---|---|
| `USER_AUTH_ENABLED` | `1` (default) requires user API keys for inference requests; `0` allows anonymous inference but does not disable account login or admin authentication. On the **Settings** tab |
| `ADMIN_TOKEN` | optional legacy bearer token for the `/admin/*` endpoints; blank disables only this access path |
| `PROVIDER_ROUTE_TYPES` | route types the admin console may add per provider, as `provider=type[\|type]` entries separated by commas (e.g. `chutes=quota,openrouter=concurrency\|on_demand`); blank leaves every provider unrestricted |
| `DB_ENABLED` | `false` runs the gateway with no database, reading every setting from the environment. Environment only |
| `DB_STORE_FULL_CONTENT` | `false` (default) does not store prompts or responses at all; `true` stores them in full. See [Request logging and privacy](database.md#request-logging-and-privacy) |
| `FRONTEND_URL` | absolute URL your users click in verification and reset emails |
| `BASE_URL` | this gateway's own public origin, used to build the absolute links in signup and password-reset emails. Set it: the server does not interpret `X-Forwarded-*`, so a blank value derives `http://…` from the request even behind a TLS proxy. See [Trusted Proxies and Client IPs](trusted-proxies-and-client-ips.md) |
| `LOG_LEVEL`, `LOG_FORMAT` | logging verbosity and `json`/text output |
| `ALERTS_ENABLED`, `SLACK_ALERTS_WEBHOOK_URL` | in-process alerting, off by default |
| `TRUST_PROXY_HEADERS`, `TRUSTED_PROXIES` | trust `X-Forwarded-For` from the proxies listed in `TRUSTED_PROXIES`; the Cloudflare settings below also need the flag |
| `TRUSTED_DIRECT_CLIENT_NETWORKS` | optional CIDRs for clients that connect directly from a private network; does not authorize forwarding headers |
| `TRUST_CLOUDFLARE_HEADERS`, `TRUSTED_CLOUDFLARE_NETWORKS` | trust Cloudflare's `CF-Connecting-IP` from the listed peers; also needs `TRUST_PROXY_HEADERS=1` |

[Trusted Proxies and Client IPs](trusted-proxies-and-client-ips.md) says which
of the proxy settings above to set for your setup.

### Provider credentials

A model registry names its own credentials. Its `api_key`, `api_keys`,
`base_url` and `provider_model_id` fields can each be written as `${VAR}`,
which is resolved when the registry loads: the setting of that name stored in
the database, else the environment variable. So the provider keys a
deployment needs are the variables its registry names, and you can call them
whatever you like.

```yaml
route:
  - kind: openrouter
    base_url: https://openrouter.ai/api/v1
    api_keys:
      - ${OPENROUTER_API_KEY}
```

Only a whole value is substituted: `${VAR:-default}` and `${VAR}` inside a
longer string are not. See [Configuration](configuration.md) for the details.

The **Configuration** tab lists every variable the active registry names,
under **Providers**, marks the ones a model cannot load without, and stores
their values; the bundled default registry needs only `OPENROUTER_API_KEY`. A
name the tab does not list yet — for a registry you are about to switch to —
can be added with **Add variable**. The registry reads these values at
startup, so restart the backend after changing one. A gateway without a
database reads them from `.env` instead.

Keys can also be added from the admin console (**Providers → Keys**) with no
restart; they join the same pool as the keys the registry names. See
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
`DB_PASSWORD` in `.env`, as [above](#running-with-docker).

**`Authentication configuration incomplete: set JWT_SECRET_KEY, API_KEY_SECRET`**
at startup — the backend could not read its settings from the database and
fell back to the environment, which has no secrets. An earlier line,
`Configuration load failed after 3 attempts`, says why; fix the database
connection rather than adding secrets to `.env`.

**The setup page rejects the code** — copy it again from the last line the
`docker logs` command prints; an earlier line can belong to a database you
have since reset. Wrong codes are rate limited per client address, but the
right one is always accepted. If the log has no code at all, setup is already
complete; sign in, or reset the administrator's password as in
[The first admin account](deployment.md#the-first-admin-account).

**A required setting is missing** — signed-in users see a banner, and the
settings it means are marked **Missing** on the **Configuration** tab. Fill
them in there; a provider credential takes effect after **Restart backend**.
If the deployment leaves a model's credential unset on purpose, mark that
route `optional: true` in the model registry instead, so the variable is no
longer required.

**Signing in fails after a settings change** — change the setting back from
the command line, which needs no sign-in; see
[Without the console](configuration.md#without-the-console).

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
