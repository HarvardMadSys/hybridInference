# Router Tutorial

This tutorial takes one local HybridInference deployment through three stages:

1. prove the routing chain with a deterministic fake provider;
2. continue the same running Compose project into the Web Console, Admin
   Console, Postgres, accounts, API keys, and request history;
3. replace the fake provider with a local OpenAI-compatible vLLM, SGLang, or
   Ollama server.

You do not choose between a router-only product and a full-stack product. The
small router is the first checkpoint on one path, so you see a successful
request before adding the parts that have more ways to fail.

No provider account, host `.env`, GPU, SMTP service, or paid API key is needed
for the first two stages. CI runs the same `make up` → `make smoke` →
`make demo` → `make demo-smoke` transition whenever this contract changes.

## What you need

- Docker Engine 24+ with Compose v2. Check with `docker compose version`.
- A running Docker daemon. On macOS, start Docker Desktop or Colima; `docker
  info` must succeed.
- Git, curl, GNU Make, and Python 3.10–3.13 (3.12 recommended). The smoke
  clients use only the Python standard library, so there is no `pip install`
  step.
- Several GB of free disk for the backend, frontend, Postgres image, and local
  database volume.
- These loopback ports free: backend `18080`, frontend `13001`, and Postgres
  `15432`. See [Port already in use](#port-already-in-use) to override them.

Node.js is not required because the frontend is built in Docker. A GPU is only
needed if you continue to Stage 3 with a GPU-backed local server.

## Stage 1: prove the router works

Clone the repository and start the runnable distribution:

```bash
git clone https://github.com/HarvardMadSys/hybridInference.git
cd hybridInference

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
in `distributions/example/config/models.yaml`.

That file is mounted read-only into the backend; it is not baked into the
image. To prove the reload path, temporarily change the model's `name` to
`Reloaded Example Chat`, run
`make restart s=backend DISTRIBUTION=example`, and list the models again. The
new label appears without an image rebuild. Restore `Runnable Example Chat`
and restart the backend once more before continuing.

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

That fixed reply proves the request passed through HybridInference routing to
the bundled upstream. Clients address `example-chat`; the route maps it to the
upstream's own model id.

Streaming uses the same endpoint:

```bash
curl -sN localhost:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"example-chat","messages":[{"role":"user","content":"Say hello."}],"stream":true}'
```

Every `chat.completion.chunk` frame in one response repeats one completion id,
and the stream ends with the literal `data: [DONE]` sentinel.

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

Open <http://localhost:13001/signup> and complete the browser flow:

1. Sign up with `admin@local.dev`, a username, and a password with at least
   eight characters, uppercase, lowercase, and a number. Use a demo-only
   password that you do not reuse elsewhere, then accept the terms.
2. Select **Back to Login**, then sign in with the same email and password.
   Email verification is disabled for this loopback-only example.
3. On the dashboard, create an API key, then reveal and copy it for the API
   call below.
4. Open **API Playground**, select `example-chat`, and send a message. The reply
   is `RUNNABLE_EXAMPLE_OK`.
5. Open **Admin Console**. This account is an admin because its email matches
   the example's explicit `ADMIN_EMAILS` value.

The UI and API share one origin. Requests to `/v1`, `/auth`, `/user`, and
`/admin` on port `13001` are proxied by the frontend to the backend inside the
Compose network.

Use the copied key for a normal authenticated request through that origin:

```bash
export HYBRIDINFERENCE_API_KEY='<your copied key>'

curl -s localhost:13001/v1/chat/completions \
  -H "Authorization: Bearer ${HYBRIDINFERENCE_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"model":"example-chat","messages":[{"role":"user","content":"Say hello."}]}'
```

Now return to the dashboard's **Recent Requests** section. This API-key
completion appears in history once asynchronous logging completes. The
Playground is a separate UI routing proof and deliberately bypasses the normal
completion logger, so do not use its message as the history check.

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

The check logs into the existing account, creates or reuses an API key, calls
normal and streaming completions through the frontend origin, exercises the
Playground and Admin APIs, proves a second local user receives `403` from the
Admin API, verifies request history, recreates the backend, and then proves the
same account, refresh-cookie session, and API key still work. It does not print
any secret and does not reset the database.

## Stage 3: replace the fake provider with local inference

Start an OpenAI-compatible vLLM, SGLang, Ollama, or similar server on the host.
It must listen on a Docker-reachable address such as `0.0.0.0:8000`; a server
bound only to host `127.0.0.1` is not reachable from the backend container.
Binding to `0.0.0.0` can expose an unauthenticated model server to your LAN, so
restrict the port with a host firewall or bind a Docker-reachable private
interface instead when your runtime supports it.

Then point the same public model at that server:

```bash
export EXAMPLE_UPSTREAM_BASE_URL=http://host.docker.internal:8000/v1
export EXAMPLE_UPSTREAM_API_KEY=local-placeholder
export EXAMPLE_UPSTREAM_MODEL='<served-model-name>'
make demo DISTRIBUTION=example
```

`make demo` recreates the backend so the new upstream settings take effect,
while preserving the account, API key, frontend, and Postgres volume. Clients
and the Playground still request `example-chat`; only the route behind it has
changed.

`EXAMPLE_UPSTREAM_API_KEY` is the credential the gateway presents to that
provider. It is not the `HYBRIDINFERENCE_API_KEY` minted in Stage 2, which is
the client credential presented to the gateway.

Use the Playground or the authenticated `curl` from Stage 2 to test the real
model. Do not run either bundled smoke against it: both checks deliberately
assert the fake provider's exact sentinel, which a real model should not be
expected to produce.

## What the example contains

The example is one distribution directory:

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

The two Compose overlays follow the same progression. The first adds the fake
provider and makes the backend-only start deterministic. The second adds the
database/frontend settings and an example-scoped database volume. Nothing in
the example uses the production `hybridinference_postgres_data` volume.

`EXAMPLE_OVERLAY` marks the directory as a teaching artifact, so bare
distribution discovery does not mistake it for a real deployment. It is not a
Stage 1/Stage 2 switch; only the explicit `demo` targets add the third Compose
layer.

To create a project distribution, copy this shape, remove the teaching marker
and fake provider, replace every local-only identity and secret, and add the
deployment controls your environment needs. See [Configuration](configuration.md),
[Adding Models](adding-models.md), and [Routing](routing.md) for the relevant
contracts.

## Stop, resume, and reset

After Stage 2 or Stage 3, stop all four services while keeping the account,
API key, and request history:

```bash
make demo-down DISTRIBUTION=example
```

Resume Stage 2 with `make demo DISTRIBUTION=example`. To resume Stage 3, first
restore the three `EXAMPLE_UPSTREAM_*` exports from that stage, then run the
same command; those shell overrides are not stored in the database. To stop
the stack and delete this example project's data:

```bash
make demo-reset DISTRIBUTION=example
```

`demo-reset` is destructive for the example data, but it cannot delete the
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
EXAMPLE_DEMO_ADMIN_PASSWORD='<the password from signup>' \
make demo-smoke DISTRIBUTION=example
```

Then use backend port `28080` in Stage 1 and frontend port `23001` in Stages 2
and 3. The published site identity follows the frontend override. Keep the
same three port assignments on every later `make demo` or `make demo-smoke`,
including the Stage 3 command; both commands can recreate containers.

### Cannot connect to the Docker daemon

Start Docker Desktop or Colima first. `docker info` must print a server section
before `make up` can work.

### `make up` chose another distribution

Always pass `DISTRIBUTION=example` while following this tutorial. A bare
`make up` may discover a real deployment overlay in your checkout; the example
marker deliberately prevents the tutorial from being selected implicitly.

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
