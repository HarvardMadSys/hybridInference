# Quickstart

This quickstart takes a fresh clone of HybridInference through three stages on
one local gateway:

1. Prove the routing chain with a deterministic fake provider.
2. Add the Web and Admin Consoles, Postgres, accounts, API keys and request
   history to the same running Compose project.
3. Replace the fake provider with a local vLLM, SGLang or Ollama server.

The first two stages need no provider account, `.env` file, GPU, SMTP service
or paid API key, and CI runs their commands on every change that touches them.
To run the gateway from a source checkout against real models instead, see
[Installation](installation.md).

## What you need

- Docker Engine 24+ with Compose v2 (`docker compose version`) and a running
  daemon: `docker info` must succeed. On macOS, start Docker Desktop or Colima,
  and clone into a directory Docker may share with containers.
- Git, curl, GNU Make, and Python 3.11–3.13 (3.12 recommended). The smoke
  checks use only the standard library.
- Several GB of free disk.
- Free loopback ports `18080` (backend), `13001` (frontend) and `15432`
  (Postgres). Stage 1 uses only `18080`. To use other ports, see
  [Port already in use](#port-already-in-use).

Node.js is not needed: the frontend is built in Docker. A GPU is needed only
for a GPU-backed server in Stage 3.

## Stage 1: prove the router works

Clone the repository and start the example distribution:

```bash
git clone https://github.com/HarvardMadSys/hybridInference.git hybridinference
cd hybridinference

make up DISTRIBUTION=example
```

This starts two containers: `example-provider`, a deterministic
OpenAI-compatible upstream, and `backend`, the gateway. Postgres and the
frontend stay stopped, and accounts and authentication are off.

Run the automated check. It waits for startup and tests `/health`,
`/site-config`, `/v1/models` and a routed completion:

```bash
make smoke DISTRIBUTION=example
```

```text
EXAMPLE_SMOKE_OK
```

### Inspect the gateway yourself

```bash
curl -s localhost:18080/health
```

```json
{"status": "healthy"}
```

An anonymous caller sees only the status; an administrator also sees the route
count and the state of the database (see
[Health checks](deployment.md#health-checks)).

```bash
curl -s localhost:18080/v1/models
```

The response lists the public model id `example-chat`. Stage 1 has no
database, so the example hands the container its `MODEL_CATALOG` setting as an
environment seed, read from `distributions/example/config/models.json`. Normal
deployments configure the catalog in Admin. To try a change, edit the model's
`name` in that file, run `make up DISTRIBUTION=example` again and list the
models; restore the file before Stage 2.

Send a completion:

```bash
curl -s localhost:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"example-chat","messages":[{"role":"user","content":"Say hello."}]}'
```

The assistant replies `RUNNABLE_EXAMPLE_OK`. Only the bundled fake provider
sends that reply, so it proves the request went through the gateway's routing:
the client asked for `example-chat`, and the route translated that into the
provider's own model name.

With `"stream":true`, the same answer arrives as `chat.completion.chunk` events
ending with `data: [DONE]`:

```bash
curl -sN localhost:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"example-chat","messages":[{"role":"user","content":"Say hello."}],"stream":true}'
```

`curl -s localhost:18080/routing` lists each model's routes with their provider
label and weight. Only an administrator also sees each route's upstream URL;
see [API endpoints](routing.md#api-endpoints).

Leave the stack running and do not run `make down`: Stage 2 extends this
Compose project in place.

## Stage 2: continue into the Web and Admin Consoles

Add Postgres and the frontend, and recreate the backend with authentication
on:

```bash
make demo DISTRIBUTION=example
```

The bundled provider keeps running. Postgres starts with a volume of the
example's own, the backend is recreated against it, and the frontend starts
last. On its first start against the new database, the backend generates the
secrets that sign sessions and protect API keys, stores the example's settings,
and logs a one-time setup code:

```bash
docker logs hybridinference-example-backend 2>&1 | grep 'setup code'
```

The code stays the same until setup is done and is logged again at every
start. Open <http://localhost:13001/setup> (or your own `FRONTEND_PORT`):

1. Enter the setup code, then create the administrator with the username
   `admin` and a demo-only password of at least eight characters with an
   uppercase letter, a lowercase letter and a number. The account has no email
   address and signs in with the username. `admin` suits this loopback-only
   example and the full-stack check below; a real deployment should pick a name
   that is hard to guess (see
   [The first admin account](deployment.md#the-first-admin-account)).
2. The example needs none of the settings the next step offers: select **Save
   and continue**, then **Finish**. The Admin Console opens on its
   **Settings** tab, which holds the settings the gateway keeps in its
   database.
3. Open **API Keys**, create a key and copy it. **Show key** shows a saved key
   again later.
4. Open **Chat**, select `example-chat` and send a message. The reply is
   `RUNNABLE_EXAMPLE_OK`.

Others can now sign up at <http://localhost:13001/signup> as ordinary users;
email verification is off in this example. Your account is the administrator
because first-run setup created it. The Admin Console's **Providers** and
**Routing** tabs add providers, keys and models to a running gateway; see
[Runtime configuration from the admin console](configuration.md#runtime-configuration-from-the-admin-console).

The console and the API share one origin: the frontend forwards `/v1`,
`/auth`, `/user` and `/admin` on port `13001` to the backend
(`apps/frontend/next.config.js`). Send an authenticated request with your key:

```bash
export HYBRIDINFERENCE_API_KEY='<your copied key>'

curl -s localhost:13001/v1/chat/completions \
  -H "Authorization: Bearer ${HYBRIDINFERENCE_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"model":"example-chat","messages":[{"role":"user","content":"Say hello."}]}'
```

The request appears on the console's **Requests** page after a moment. The
**pgAdmin** link in the navigation is not part of this example and works only
if you deploy pgAdmin separately.

Run the full-stack check with your password:

```bash
EXAMPLE_DEMO_ADMIN_PASSWORD='<the same password>' \
make demo-smoke DISTRIBUTION=example
```

```text
EXAMPLE_FULL_SMOKE_OK
```

The check signs in as `admin`, completing first-run setup itself if you skipped
the browser steps. It sends normal and streaming requests through the console's
address, exercises the chat playground and the Admin API, confirms that a
non-admin user gets `403` from the Admin API, and checks the request history.
It then recreates the backend and confirms that the account, its session and
the API key still work, because their secrets live in the database. It prints
no secrets and does not reset the database.

## Stage 3: replace the fake provider with local inference

Start an OpenAI-compatible server (vLLM, SGLang, Ollama or similar) on the
host, listening on an address the backend container can reach, such as
`0.0.0.0:8000`; a server bound only to the host's `127.0.0.1` is not reachable.
Binding to `0.0.0.0` can expose an unauthenticated model server to your LAN, so
restrict the port with a host firewall, or bind a private interface Docker can
reach.

In the Admin Console, open **Settings**, then **Providers & Routing**, and set
these three settings under **Providers**:

| Setting | Value |
|---|---|
| `EXAMPLE_UPSTREAM_BASE_URL` | `http://host.docker.internal:8000/v1` |
| `EXAMPLE_UPSTREAM_API_KEY` | `local-placeholder` |
| `EXAMPLE_UPSTREAM_MODEL` | the model name your server serves |

Select **Save changes**. The model registry reads these settings only at
startup, so a **Restart pending** notice appears: select **Restart backend**
and wait for the page to reload (`make demo DISTRIBUTION=example` also works).
Clients still request `example-chat`; only the route behind it has changed, and
the **Routing** tab shows its new base URL.

Once Stage 2 has run, exporting `EXAMPLE_UPSTREAM_*` in your shell changes
nothing: a stored setting wins over the environment, and the Settings tab marks
such a variable **Environment ignored**. To return to the bundled provider,
select **Reset** on the three settings and restart the backend again.

`EXAMPLE_UPSTREAM_API_KEY` is the credential the gateway presents to the
provider, not the `HYBRIDINFERENCE_API_KEY` that clients present to the
gateway. Test the real model with **Chat** or the authenticated `curl` from
Stage 2. Do not run the smoke checks against it: they expect the fake
provider's fixed reply.

## What the example contains

The example is one [distribution](glossary.md#deployments):

```text
distributions/example/
├── EXAMPLE_OVERLAY
├── distribution.json
├── distribution.demo.json
├── config/
│   ├── models.json
│   └── routing.json
├── deploy/
│   ├── backend.env
│   ├── docker-compose.yml
│   └── docker-compose.demo.yml
├── fixtures/fake-openai-provider/
├── smoke.py
└── full_smoke.py
```

- `distribution.json` holds Stage 1's site settings, with public signup off,
  and `distribution.demo.json` the demo's, with signup on. Both stages seed
  the model catalog and the routing file from `config/`.
- `docker-compose.yml` adds the fake provider and starts the backend alone.
  `docker-compose.demo.yml` adds Postgres, the console and a database volume of
  the example's own; the first start copies the settings into that database,
  where they change from then on. Nothing in the example touches the
  `hybridinference_postgres_data` volume a real deployment uses.
- `EXAMPLE_OVERLAY` marks the directory as an example, so a plain `make up`
  never picks it; select it with `DISTRIBUTION=example`.

## Where to go next

- To build your own deployment, copy this directory, delete `EXAMPLE_OVERLAY`
  and the fake provider, replace every local-only name and secret, and read
  [Distribution customization](distribution-customization.md).
- [Configuration](configuration.md): settings and environment variables.
- [Adding a New Model](adding-models.md): a model entry and its `route` list.
- [Routing](routing.md): weights, fallback, circuit breaking, affinity and
  custom strategies.
- [Installation](installation.md): running from a source checkout against real
  providers.

## Stop, resume, and reset

After Stage 2 or 3, stop all four services and keep the account, API key,
settings and request history:

```bash
make demo-down DISTRIBUTION=example
```

`make demo DISTRIBUTION=example` resumes whichever stage you stopped at. To
stop the stack and delete the example's data, including the administrator, the
generated secrets and every stored setting:

```bash
make demo-reset DISTRIBUTION=example
```

The next `make demo` then starts with first-run setup again. `demo-reset`
cannot delete the production database volume. If you stop after Stage 1, use
`make down DISTRIBUTION=example`.

Once Stage 2 has started, do not use `make up DISTRIBUTION=example` to go back
to Stage 1: it applies only the Stage 1 Compose files and can leave a mix of
services. Run `make demo-reset DISTRIBUTION=example` and start again.

## Troubleshooting

### Port already in use

Pass the same overrides to every command:

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

Then use backend port `28080` in Stage 1 and frontend port `23001` afterwards.
The first `make demo` stores the site's links (`SITE_PUBLIC_BASE_URL`,
`BASE_URL` and `FRONTEND_URL`, built from `FRONTEND_PORT` in
`docker-compose.demo.yml`) in the example's database, so keep the same three
ports on every later `make demo` or `make demo-smoke`. A different
`FRONTEND_PORT` later does not change the stored links; edit them on the
Settings tab, or reset the example.

### The setup page rejects the code

Run the `docker logs` command from Stage 2 again and copy the code from the
last line; an earlier line may belong to an example database you have since
reset. Wrong codes are rate limited, but the right one is always accepted. If
the log has no setup code, setup is complete: sign in instead.

The administrator has no email address, so a forgotten password cannot be
reset by mail. Reset it from the command line:

```bash
docker exec -it hybridinference-example-backend \
  python -m serving.auth.reset_password admin
```

It asks for the new password twice and signs the account out everywhere;
`--generate` prints a strong password instead.

### Cannot connect to the Docker daemon

Start Docker Desktop or Colima. `docker info` must print a server section
before `make up` can work.

### Empty `/v1/models`

In Stage 1, check that you passed `DISTRIBUTION=example` and that the JSON
seeds are valid, run `make up DISTRIBUTION=example` again, and read
`make logs s=backend DISTRIBUTION=example`. In Stage 2, check the saved and
applied configuration in Admin (**Settings** and **Routing**); stored values
take precedence over the environment seeds.

### `make up` chose another distribution

Always pass `DISTRIBUTION=example` in this tutorial. Without it, `make up` uses
a real distribution if your checkout has one, never the example.

### Start over after a partial run

Run `make demo-reset DISTRIBUTION=example` and begin again at Stage 1. Do not
delete Docker volumes by broad name or prune unrelated projects.

### Inspect a failed service

`make logs DISTRIBUTION=example` follows every running service of the example
project. For one service:

```bash
make logs s=backend DISTRIBUTION=example
```

Replace `backend` with `frontend`, `postgres` or `example-provider`. Press
Ctrl-C to stop following.
