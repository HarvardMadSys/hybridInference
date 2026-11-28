# Installation

This page gets a gateway running, as the Docker stack or as a source checkout
you can edit. To see a gateway answer a request first, with no provider account,
API key or `.env`, try the [Quickstart](router-tutorial.md).

## What you need

| For | Requirement |
|---|---|
| The Docker stack | Docker Engine 24+ and Docker Compose v2+ |
| A source checkout | Python 3.11–3.13 (3.12 recommended) and [uv](https://github.com/astral-sh/uv) |
| Running the console outside Docker | Node.js 22 |

Linux or macOS.

## Running with Docker

```bash
git clone https://github.com/HarvardMadSys/hybridInference.git hybridinference
cd hybridinference
cp .env.example .env
```

Fill in `DB_USER` and `DB_PASSWORD` in `.env` (`DB_NAME` is already set);
Compose refuses to start without all three. Nothing else in `.env` is required:
provider keys, mail, proxy trust and the other settings are stored in the
database and set from the console. Then start the stack:

```bash
make up     # creates the external volume, then brings up Compose
make ps     # show the services and their health
curl -s http://localhost:8080/health
```

The backend listens on `127.0.0.1:8080`, the console on `127.0.0.1:3001` and
Postgres on `127.0.0.1:5432`. Read the [Deployment Guide](deployment.md) before
exposing them.

The database lives in the Docker volume
`hybridinference_postgres_data`, which `docker compose down -v` does not delete;
see [Resetting the stack](deployment.md#resetting-the-stack). After changing
`.env`, run `make up` again, not `make restart`: a container reads `.env` only
when it is created.

### First-run setup

On its first start against an empty database, the backend generates
`JWT_SECRET_KEY` (signs sign-in tokens) and `API_KEY_SECRET` (protects API
keys), stores them in the database, and logs a one-time setup code:

```bash
docker logs hybridinference-backend 2>&1 | grep 'setup code'
```

Open the console at `http://localhost:3001`; it sends you to its setup page.

1. Enter the setup code. It stays the same until setup is done and is logged
   at every start. Until then the backend refuses all sign-ups.
2. Create the administrator: a username and a password, no email. Pick a
   username that is hard to guess, not `admin`: sign-in attempts are counted
   per username, so anyone who knows it can lock the account out of new
   browsers. See [The first admin account](deployment.md#the-first-admin-account).
3. Fill in the required deployment settings. The gateway installs no sample
   models; missing configuration shows **Ask admin to configure**.
4. Add a provider and key, and create a model in **Admin → Routing**. Verify
   the route before making the model available to users. Aliases and other
   metadata are in the model catalog, **Admin → Routing → Models**. Follow
   the **Restart required** markers.

```{note}
Keep the database together with its secrets. `API_KEY_SECRET` can never change:
a new value would break every API key. Changing `JWT_SECRET_KEY` signs everyone
out. `ERASURE_FENCE_SECRET` takes `API_KEY_SECRET`'s value unless you set it in
`.env` before the first start, and cannot change afterwards either; see
[Database](database.md#secrets-for-deleting-accounts).
```

A gateway with `DB_ENABLED=false` has no setup step and reads every setting from
the environment, so with user authentication on it needs `JWT_SECRET_KEY` and
`API_KEY_SECRET` there. Only `DB_ENABLED=false` together with
`USER_AUTH_ENABLED=false` runs without them, as in Stage 1 of the
[Quickstart](router-tutorial.md).

## Development checkout (no Docker)

```bash
git clone https://github.com/HarvardMadSys/hybridInference.git hybridinference
cd hybridinference

make setup-dev
```

`make setup-dev` creates `.venv` on Python 3.12 (it refuses an existing `.venv`
on another minor version), installs the project editable with the `dev`
dependency group, and installs the pre-commit hooks. By hand:

```bash
uv venv -p 3.12
source .venv/bin/activate
uv sync --group dev
```

Run the gateway from the repository root, where it reads `.env`:

```bash
cp .env.example .env    # edit as above; a process started here reads it
uv run uvicorn serving.servers.app:app --no-proxy-headers --host 127.0.0.1 --port 8080
```

With a database, the setup code appears in this terminal. Run the console in a
second terminal:

```bash
cd apps/frontend
npm ci
BACKEND_INTERNAL_URL=http://127.0.0.1:8080 npm run dev -- --hostname 127.0.0.1
```

Open `http://localhost:3001`. The console forwards API paths to
`BACKEND_INTERNAL_URL`, whose default `http://backend:8080` exists only inside
Docker, and it does not read `.env`, so set it each time. Check the whole path
with `curl http://localhost:3001/health`.

To run only the database:

```bash
make docker-volumes
docker compose -f deploy/docker/docker-compose.yml --env-file .env up -d postgres
```

For a gateway without accounts or a database, set `DB_ENABLED=false` and
`USER_AUTH_ENABLED=false` in `.env`. It still routes requests; accounts, API
keys, request history and stored settings need the database.

## Configuration

`.env` holds only what the backend needs before it reaches the database and
what describes the containers: the `DB_*` connection, host ports and bind
addresses, and a few process and console values (see `.env.example`). Every
other setting lives in the database and is edited in **Admin → Settings**. An
environment value only seeds an empty database row at startup; after that the
stored value wins. See [Configuration](configuration.md).

Settings to review on a new deployment:

| Setting | Effect |
|---|---|
| `BASE_URL` | This gateway's public origin, used in signup and password-reset links. Set it behind a TLS proxy: the server ignores `X-Forwarded-*`, so a blank value builds `http://…` links |
| `FRONTEND_URL` | Console URL your users click in emails |
| `USER_AUTH_ENABLED` | `1` (default) requires an API key for inference; `0` allows anonymous inference but keeps account and admin sign-in. A runtime switch in **Admin → Settings → Access & Limits** |
| `DB_STORE_FULL_CONTENT` | `false` (default) stores no prompts or responses; see [Request logging and privacy](database.md#request-logging-and-privacy) |
| `TRUST_PROXY_HEADERS` and the other proxy settings | Which proxies may report client addresses; see [Trusted Proxies and Client IPs](trusted-proxies-and-client-ips.md) |
| `ADMIN_TOKEN` | Optional legacy bearer token for `/admin/*`; blank disables it |
| `PROVIDER_ROUTE_TYPES` | Route types Admin may add per provider, as comma-separated `provider=type[\|type]` entries (e.g. `chutes=quota,openrouter=concurrency\|on_demand`); blank allows all |

Set proxy trust in `.env` before the first start, or in Admin afterwards. Under
Compose the first start stores `TRUST_PROXY_HEADERS` and
`TRUST_CLOUDFLARE_HEADERS` as `0` when `.env` does not set them, and later
`.env` edits are ignored.

### Provider credentials

A model's `api_key`, `api_keys`, `base_url` and `provider_model_id` can each be
a `${VAR}` reference. It resolves to the stored setting of that name, else the
environment variable, else the gateway's registered default. Only a whole value
is substituted, so `${VAR:-default}` and `${VAR}` inside a longer string stay
literal.

```json
{
  "route": [
    {
      "kind": "openrouter",
      "base_url": "https://openrouter.ai/api/v1",
      "api_keys": ["${OPENROUTER_API_KEY}"]
    }
  ]
}
```

**Admin → Settings → Providers & Routing** lists every variable the model
catalog names, marks the ones a model needs, and stores their values; **Add
variable** adds a name it does not list yet. Restart the backend after changing
one. A provider key whose description mentions numbered extras, such as
`OPENROUTER_API_KEY`, also accepts `OPENROUTER_API_KEY1` to `OPENROUTER_API_KEY20`;
every one with a value joins the provider's key pool. Keys added in the
**Keys** section of **Admin → Providers** join the same pool without a restart.

## Verifying the install

```bash
curl -s http://localhost:8080/v1/models

curl -s http://localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${HYBRIDINFERENCE_API_KEY}" \
  -d '{"model":"<model-id>","messages":[{"role":"user","content":"Say hello."}]}'
```

Use a model id that `/v1/models` lists, and drop the `Authorization` header if
`USER_AUTH_ENABLED=0`. Any OpenAI client works with the gateway as its
`base_url`:

```python
from openai import OpenAI

client = OpenAI(api_key="<your-api-key>", base_url="http://localhost:8080/v1")

response = client.chat.completions.create(
    model="<model-id>",
    messages=[{"role": "user", "content": "Say hello."}],
)
print(response.choices[0].message.content)
```

In a development checkout, `make check` runs the linters and the default tests;
see [Contributing](contributing.md#quality-gates).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `required variable DB_USER is missing a value: DB_USER must be set in .env file` | Fill in `DB_USER` and `DB_PASSWORD` in `.env` |
| `env file ... .env not found` | `cp .env.example .env` before `make up` |
| `Authentication configuration incomplete: set JWT_SECRET_KEY, API_KEY_SECRET` | The backend could not read its settings from the database; an earlier `Configuration load failed after 3 attempts` line says why. Fix the connection; do not add secrets to `.env` |
| The setup page rejects the code | Copy the code from the last line `docker logs` prints. No code at all means setup is complete: sign in, or [reset the password](deployment.md#resetting-a-forgotten-password) |
| A banner says a setting is missing | Fill in the settings marked **Missing** in **Admin → Settings**. A provider key the catalog references is never required: until it is set, its route is skipped and the log names it |
| Signing in fails after a settings change | Reset the setting from the command line; see [Without the console](configuration.md#without-the-console) |
| Import errors when running from source | Run `make setup-dev`, then start with `uv run` or with `.venv` activated |
| Every request 404s and `/v1/models` is empty | No model is registered yet: create one in **Admin → Routing**. The startup log says whether the stored catalog loaded (`Registered N routes from the database route catalog`) or names its problem |
| Port already in use | Override `BACKEND_PORT`, `FRONTEND_PORT` or `DB_PORT` in `.env` |
