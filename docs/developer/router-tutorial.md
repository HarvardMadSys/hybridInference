# Quickstart

This is the first thing to do with a fresh clone of HybridInference. It takes
one local gateway through three stages:

1. prove the routing chain with a deterministic fake provider;
2. continue the same running Compose project into the Web Console, Admin
   Console, Postgres, accounts, API keys, and request history;
3. replace the fake provider with a local OpenAI-compatible vLLM, SGLang, or
   Ollama server.

Each stage builds on the one before, so you see one request succeed before
adding the parts that have more ways to fail.

No provider account, host `.env`, GPU, SMTP service, or paid API key is needed
for the first two stages. CI runs the same commands on every change that
touches them.

If you would rather run the gateway from a source checkout without Docker, and
against real models, see [Installation](installation.md) — but come back here
first if you have never seen this gateway serve a request.

## What you need

- Docker Engine 24+ with Compose v2. Check with `docker compose version`.
- A running Docker daemon. On macOS, start Docker Desktop or Colima; `docker
  info` must succeed, and the directory you clone into must be one Docker is
  allowed to share into containers.
- Git, curl, GNU Make, and Python 3.10–3.13 (3.12 recommended). The smoke
  clients use only the Python standard library, so there is no `pip install`
  step.
- Several GB of free disk for the backend, frontend, Postgres image, and local
  database volume.
- These loopback ports free: backend `18080`, frontend `13001`, and Postgres
  `15432`. Only `18080` is used in Stage 1 — `make up` on this overlay starts
  the backend with `--no-deps`, so the frontend and Postgres stay stopped until
  Stage 2. See [Port already in use](#port-already-in-use) to override them.

Node.js is not required because the frontend is built in Docker. A GPU is only
needed if you continue to Stage 3 with a GPU-backed local server.

## Stage 1: prove the router works

Clone the repository and start the runnable distribution:

```bash
git clone https://github.com/HarvardMadSys/hybridInference.git hybridinference
cd hybridinference

make up DISTRIBUTION=example
```

Two containers start: `example-provider`, a deterministic OpenAI-compatible
upstream, and `backend`, the HybridInference gateway. Postgres and the frontend
remain stopped; accounts and authentication are disabled at this checkpoint.

Run the automated check:

```bash
make smoke DISTRIBUTION=example
```

```text
EXAMPLE_SMOKE_OK
```

The smoke waits for startup and verifies `/health`, `/site-config`,
`/v1/models`, and a routed completion.

### Inspect the gateway yourself

Check health:

```bash
curl -s localhost:18080/health
```

```json
{
  "status": "healthy",
  "routes_configured": 1,
  "database_configured": false,
  "database_connected": false
}
```

List models:

```bash
curl -s localhost:18080/v1/models
```

The response contains the public model id `example-chat`. Its registry entry is
in `distributions/example/config/models.yaml`, which the backend finds through
the distribution manifest `distributions/example/distribution.yaml` — see
[Configuration](configuration.md) for how that resolution works.

The file is mounted into the container rather than built into the image, so
an edit only needs a backend restart. To try it, change the model's `name`
from `Runnable Example Chat` to `Reloaded Example Chat`, run
`make restart s=backend DISTRIBUTION=example`, and list the models again. Change
it back and restart once more before continuing.

Send a completion:

```bash
curl -s localhost:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"example-chat","messages":[{"role":"user","content":"Say hello."}]}'
```

The assistant content is:

```text
RUNNABLE_EXAMPLE_OK
```

Only the bundled fake provider sends that reply, so seeing it means the request
went through the gateway's routing. Clients ask for `example-chat`; the route
translates that into the provider's own model name.

Streaming uses the same endpoint:

```bash
curl -sN localhost:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"example-chat","messages":[{"role":"user","content":"Say hello."}],"stream":true}'
```

The answer arrives as a series of `chat.completion.chunk` events and ends with
the line `data: [DONE]`.

To see the route the gateway just used, ask it:

```bash
curl -s localhost:18080/routing
```

This is an unauthenticated endpoint that reports each model's upstream base
URLs and weights. That is what you want on a laptop and not what you want on a
public host — see the warning in [Routing](routing.md#api-endpoints).

Do not run `make down` here. Stage 2 extends this same Compose project in
place.

## Stage 2: continue into the Web and Admin Consoles

Add Postgres and the frontend, and recreate the backend with authentication
enabled:

```bash
make demo DISTRIBUTION=example
```

This is an in-place transition, not a second deployment. The bundled provider
keeps running in the existing project and network. Postgres is added with an
example-owned persistent volume, the backend is recreated against it, and the
frontend is added last.

The new database has no account yet. On its first start the backend generates
the secrets that sign sessions and protect API keys, stores the example's
settings, and prints a one-time setup code in its log:

```bash
docker logs hybridinference-example-backend 2>&1 | grep 'setup code'
```

`make logs s=backend DISTRIBUTION=example` shows the same line; press Ctrl-C
to stop following it. The code stays the same until setup is done, and the
backend prints it again every time it starts.

Open <http://localhost:13001/setup> — or your own `FRONTEND_PORT` if you
overrode it — and complete the browser flow:

1. Enter the setup code, then create the administrator with the username
   `admin` and a password with at least eight characters, uppercase,
   lowercase, and a number. Use a demo-only password that you do not reuse
   elsewhere. The account has no email address; it signs in with the
   username. `admin` suits this loopback-only example, where the full-stack
   check below expects it; a real deployment should pick a name that is hard
   to guess, as [The first admin account](deployment.md#the-first-admin-account)
   explains.
2. The next step offers the settings a deployment usually fills in. The
   example needs none of them: select **Save and continue**, then **Finish**.
   The Admin Console opens on its **Configuration** tab, which lists every
   setting the gateway keeps in its database, the example's own values among
   them; see
   [Settings stored in the database](configuration.md#settings-stored-in-the-database).
3. On the dashboard, create an API key, then reveal and copy it for the API
   call below.
4. Open **API Playground**, select `example-chat`, and send a message. The reply
   is `RUNNABLE_EXAMPLE_OK`.
5. Open **Admin Console** again. This account is an administrator because
   first-run setup created it. Anyone else can now sign up at
   <http://localhost:13001/signup> as an ordinary user; email verification is
   disabled for this loopback-only example.

The **Providers** and **Routing** tabs are where a provider, a key or a model
is added to a running gateway;
[Runtime configuration from the admin console](configuration.md#runtime-configuration-from-the-admin-console)
walks through them.

The UI and API share one origin. Requests to `/v1`, `/auth`, `/user`, and
`/admin` on port `13001` are rewritten by the frontend to the backend inside
the Compose network (`apps/frontend/next.config.js`).

Use the copied key for a normal authenticated request through that origin:

```bash
export HYBRIDINFERENCE_API_KEY='<your copied key>'

curl -s localhost:13001/v1/chat/completions \
  -H "Authorization: Bearer ${HYBRIDINFERENCE_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"model":"example-chat","messages":[{"role":"user","content":"Say hello."}]}'
```

Now return to the dashboard's **Recent Requests** section. The request you just
sent appears there after a moment. Playground messages are not recorded in
this history, so use an API request like this one when you check it.

The dashboard may also render cards for optional services such as Agents or
pgAdmin. They are not part of this example and their routes are unavailable
unless you deploy those services separately.

Use the password you chose above to run the deterministic full-stack check:

```bash
EXAMPLE_DEMO_ADMIN_PASSWORD='<the same password>' \
make demo-smoke DISTRIBUTION=example
```

```text
EXAMPLE_FULL_SMOKE_OK
```

The check signs in as `admin`. If you skipped the browser steps, it completes
first-run setup itself, reading the setup code from the backend's log. It
reuses or creates an API key and sends normal and streaming requests through
the console's address. It also exercises the Playground and the Admin API,
confirms that a second, non-admin user gets `403` from the Admin API, and
checks the request history. Finally it recreates the backend and confirms that
the account, its sign-in session and the API key still work: the secrets
behind them live in the database, not in the container. It prints no secrets
and does not reset the database.

## Stage 3: replace the fake provider with local inference

Start an OpenAI-compatible vLLM, SGLang, Ollama, or similar server on the host.
It must listen on a Docker-reachable address such as `0.0.0.0:8000`; a server
bound only to host `127.0.0.1` is not reachable from the backend container.
Binding to `0.0.0.0` can expose an unauthenticated model server to your LAN, so
restrict the port with a host firewall or bind a Docker-reachable private
interface instead when your runtime supports it.

Then point the same public model at that server. The model's route reads its
upstream from three settings, which Stage 2 stored in the example's database.
In the Admin Console, open **Configuration**, find them under **Providers**,
and set:

| Setting | Value |
|---|---|
| `EXAMPLE_UPSTREAM_BASE_URL` | `http://host.docker.internal:8000/v1` |
| `EXAMPLE_UPSTREAM_API_KEY` | `local-placeholder` |
| `EXAMPLE_UPSTREAM_MODEL` | the model name your server serves |

Select **Save**. The model registry reads these settings only when the backend
starts, so a **Restart pending** notice appears: select **Restart backend** and
wait for the page to reload. (`make demo DISTRIBUTION=example` has the same
effect, by recreating the backend container.) The account, API key, frontend,
and Postgres volume are untouched. Clients and the Playground still request
`example-chat`; only the route behind it has changed.
`curl -s localhost:18080/routing` now reports the new `base_url` (substitute
your own `BACKEND_PORT` here too if you overrode it).

Once Stage 2 has started, exporting `EXAMPLE_UPSTREAM_*` in your shell does not
change the route: a stored setting wins over the environment, and the
Configuration tab marks such a variable **Environment ignored**. To return to
the bundled provider, select **Reset** on the three settings and restart the
backend again.

`EXAMPLE_UPSTREAM_API_KEY` is the credential the gateway presents to that
provider. It is not the `HYBRIDINFERENCE_API_KEY` minted in Stage 2, which is
the client credential presented to the gateway.

Use the Playground or the authenticated `curl` from Stage 2 to test the real
model. Do not run either bundled smoke check against it: both expect the fake
provider's fixed reply, which a real model will not produce.

## What the example contains

The example is one [distribution](glossary.md#deployments) — a directory of
deployment files that the gateway reads instead of anything built into the
source:

```text
distributions/example/
├── EXAMPLE_OVERLAY
├── distribution.yaml
├── distribution.demo.yaml
├── config/
│   ├── models.yaml
│   └── routing.yaml
├── deploy/
│   ├── backend.env
│   ├── docker-compose.yml
│   └── docker-compose.demo.yml
├── fixtures/fake-openai-provider/
├── smoke.py
└── full_smoke.py
```

`distribution.yaml` describes the Stage 1 capability with public signup off.
`distribution.demo.yaml` describes the same distribution after auth and the
frontend are enabled. Both reuse the same model and routing files; they do not
duplicate a registry.

The two Compose files follow the same progression. The first adds the fake
provider and starts the backend alone. The second adds the database and
console settings and a database volume of the example's own; its first start
copies those settings into that database, which is where they change from then
on. Nothing in the example touches the `hybridinference_postgres_data` volume
a real deployment uses.

The `EXAMPLE_OVERLAY` file marks the directory as an example, so a plain
`make up` never picks it by accident; you select it with
`DISTRIBUTION=example`. It has nothing to do with which stage you are in.

## Where to go next

- To build your own deployment, copy this directory, delete `EXAMPLE_OVERLAY`
  and the fake provider, replace every local-only name and secret, and see
  [Distribution customization](distribution-customization.md) for what else a
  distribution can set.
- [Configuration](configuration.md) — settings, environment variables, and how
  a deployment supplies its own files.
- [Adding a New Model](adding-models.md) — the model registry entry and its `route:`
  list.
- [Routing](routing.md) — weighted selection, fallback, circuit breaking,
  session affinity, and how to add your own routing strategy.
- [Installation](installation.md) — running the gateway from a source checkout
  against real providers.

## Stop, resume, and reset

After Stage 2 or Stage 3, stop all four services while keeping the account,
API key, and request history:

```bash
make demo-down DISTRIBUTION=example
```

Resume with `make demo DISTRIBUTION=example`. Stage 3's upstream settings are
stored in the database too, so the same command resumes whichever stage you
stopped at. To stop the stack and delete this example project's data:

```bash
make demo-reset DISTRIBUTION=example
```

`demo-reset` is destructive for the example data — the administrator, the
generated secrets and every stored setting go with it, and the next
`make demo` starts with first-run setup again — but it cannot delete the
production database volume. If you intentionally stop after Stage 1 instead,
use `make down DISTRIBUTION=example`.

Once Stage 2 has started, do not use plain `make up DISTRIBUTION=example` to
try to downgrade the running project: that command only applies the Stage 1
Compose inputs and can leave the full-stack services in a mixed state. Use
`make demo-reset DISTRIBUTION=example`, then begin again at Stage 1.

## Troubleshooting

### Port already in use

Repeat the same overrides on each command in the linear journey:

```bash
BACKEND_PORT=28080 FRONTEND_PORT=23001 DB_PORT=25432 \
make up DISTRIBUTION=example

BACKEND_PORT=28080 make smoke DISTRIBUTION=example

BACKEND_PORT=28080 FRONTEND_PORT=23001 DB_PORT=25432 \
make demo DISTRIBUTION=example

BACKEND_PORT=28080 FRONTEND_PORT=23001 DB_PORT=25432 \
EXAMPLE_DEMO_ADMIN_PASSWORD='<the password from setup>' \
make demo-smoke DISTRIBUTION=example
```

Then use backend port `28080` in Stage 1 and frontend port `23001` in Stages 2
and 3. The links the site generates follow the same ports:
`docker-compose.demo.yml` builds `SITE_PUBLIC_BASE_URL`, `BASE_URL` and
`FRONTEND_URL` from `FRONTEND_PORT`, and the first `make demo` stores them in
the example's database. Keep the same three ports on every later `make demo`
or `make demo-smoke`, because both commands can recreate containers. A
different `FRONTEND_PORT` later does not reach the stored addresses; change
them on the Configuration tab, or reset the example and start again.

### The setup page rejects the code

Run the `docker logs` command from Stage 2 again and copy the code from the
last line. That is the current code, even when an earlier line shows the code
of an example database you have since reset. Wrong codes are rate limited,
but the right one is always accepted. If the log has no setup code at all,
setup is already complete, so sign in instead.

The administrator has no email address, so a forgotten password cannot be
reset by mail. Reset it from the command line instead:

```bash
docker exec -it hybridinference-example-backend \
  python -m serving.auth.reset_password admin
```

It asks for the new password twice and signs the account out everywhere;
`--generate` prints a strong password instead of asking.

### Cannot connect to the Docker daemon

Start Docker Desktop or Colima first. `docker info` must print a server section
before `make up` can work.

### Empty `/v1/models` and `routes_configured: 0`

The backend could not read the example's config. The most common cause on
macOS is a checkout outside Docker's shared file paths: the `distributions/`
bind mount then resolves to an empty directory inside the VM, the manifest at
`/app/distributions/example/distribution.yaml` is missing, and
`make logs s=backend DISTRIBUTION=example` says so explicitly. Move the
checkout under a shared path (or add yours in Docker Desktop's *File sharing*
settings) and run `make up DISTRIBUTION=example` again.

### `make up` chose another distribution

Always pass `DISTRIBUTION=example` while following this tutorial. Without it,
`make up` uses a real distribution if your checkout has one, and never the
example.

### Start over after a partial run

Use `make demo-reset DISTRIBUTION=example`, then begin again at Stage 1. Do not
delete Docker volumes by broad name or prune unrelated projects.

### Inspect a failed service

`make logs DISTRIBUTION=example` sees the running services in the shared
Compose project at every stage. To inspect one service after Stage 2, use:

```bash
make logs s=backend DISTRIBUTION=example
```

Replace `backend` with `frontend`, `postgres`, or `example-provider` as needed.
