# Runnable local distribution example

This directory is a complete, neutral HybridInference teaching distribution.
It starts small enough to prove the routing chain deterministically, then grows
the same running Compose project into a local Web/Admin Console with Postgres
and authentication. No provider account, host `.env`, GPU, SMTP service, or
paid API key is required.

For the complete walkthrough, including the browser flow and a local
vLLM/SGLang/Ollama endpoint, read the
[Router Tutorial](../../docs/developer/router-tutorial.md).

## Stage 1: prove the router works

From the repository root:

```bash
make up DISTRIBUTION=example
make smoke DISTRIBUTION=example
```

This starts only the deterministic `example-provider` and the backend. Postgres,
the frontend, accounts, and API-key authentication remain disabled. A successful
smoke prints:

```text
EXAMPLE_SMOKE_OK
```

Do not stop the project before Stage 2.

## Stage 2: continue into the Web/Admin Console

Extend the running project in place:

```bash
make demo DISTRIBUTION=example
```

`make demo` keeps the provider, starts an example-owned Postgres database,
recreates the backend with authentication enabled, and starts the frontend. The
same project now contains:

```text
example-provider + postgres + backend + frontend
```

Open <http://localhost:13001>. The normal UI supports signup, login, API-key
creation, the Playground, request history, and the application Admin Console.
The configured first-admin address is `admin@local.dev`; it becomes an admin on
its first successful login. Email verification is deliberately disabled for
this loopback-only demo.

Sign up and log in through the UI first with a demo-only password that you do
not reuse elsewhere, then use the same password for the full smoke:

```bash
EXAMPLE_DEMO_ADMIN_PASSWORD='<the same password>' \
make demo-smoke DISTRIBUTION=example
```

The full smoke uses the real auth and API routes, verifies the Admin Console and
Playground backends, checks that a non-admin user gets `403`, and proves the
account, refresh-cookie session, and API key survive an ordinary backend
recreation. Success prints:

```text
EXAMPLE_FULL_SMOKE_OK
```

These checked-in defaults are intentionally local-only. They are not production
credentials or a production security configuration.

## Stage 3: use local inference

The public gateway model remains `example-chat`; only its upstream changes:

```bash
export EXAMPLE_UPSTREAM_BASE_URL=http://host.docker.internal:8000/v1
export EXAMPLE_UPSTREAM_API_KEY=local-placeholder
export EXAMPLE_UPSTREAM_MODEL='<served-model-name>'
make demo DISTRIBUTION=example
```

This works with an OpenAI-compatible vLLM, SGLang, Ollama, or similar server.
The host server must listen on a Docker-reachable address such as `0.0.0.0`.
That bind may expose an unauthenticated model server to the LAN; use a host
firewall or a Docker-reachable private interface when possible.
The deterministic smoke expects the bundled provider's fixed response, so use a
normal completion rather than `make demo-smoke` after selecting a real model.
`EXAMPLE_UPSTREAM_API_KEY` is the gateway-to-provider credential; it is separate
from the HybridInference user API key you create in the Web Console.

The dashboard may show cards for optional Agents or pgAdmin services. This
example does not start either service, so those routes remain unavailable.

## Optional: learn quota reporting and quota-aware routing

The example also includes `quota_extension.py`, an explicitly enabled backend
extension that reads a simulated account's daily request usage. The fixture's
`--quota-limit` option enables its `/usage` endpoint and an in-memory counter;
without it the original example provider behaves as before. No real provider
account, cookie, paid key or GPU is involved.

Follow [Quota reporting](../../docs/developer/distribution-customization.md#quota-reporting)
for the complete three-terminal walkthrough using
`config/examples/models.routewise.quota.yaml`. It demonstrates the same source
feeding the admin quota API and RouteWise, a priced fallback on cold start or
exhaustion, and how to replace the simulation with an authorized data source.
The simulation is not production accounting and resets when its process
restarts or at UTC midnight. It is not enabled by `make up` or `make demo`;
the existing three-stage tutorial and its smoke checks are unchanged.

## Stop, restart, and reset

Stop the full example while keeping its account, key, and history:

```bash
make demo-down DISTRIBUTION=example
```

Run `make demo DISTRIBUTION=example` again to resume Stage 2. To resume Stage
3, re-export its three `EXAMPLE_UPSTREAM_*` values first; shell overrides are
not persisted. To remove this example project's data and return to a fresh
state:

```bash
make demo-reset DISTRIBUTION=example
```

The reset is destructive, but it cannot remove the production
`hybridinference_postgres_data` volume.

If you stop after Stage 1, the original command is still available:

```bash
make down DISTRIBUTION=example
```

After Stage 2 has started, do not use plain `make up DISTRIBUTION=example` as a
downgrade. Reset the example and begin again at Stage 1; otherwise the running
frontend/Postgres and the Stage 1 backend inputs can form a mixed stack.

At every stage, `make logs DISTRIBUTION=example` sees the services running in
the shared Compose project. Add `s=backend`, `s=frontend`, or `s=postgres` to
focus on one service.

## Ports and isolation

Defaults are loopback-only:

| Service | Host port |
| --- | ---: |
| Backend | `18080` |
| Frontend | `13001` |
| Postgres | `15432` |

The example has its own Compose project, parameterized container identities,
and a project-scoped database volume. A host-local `.env` is ignored. CI can
override all published ports and identities without colliding with another
HybridInference stack.

## Directory contents

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
├── frontend/site-ui/
├── quota_extension.py
├── smoke.py
└── full_smoke.py
```

`EXAMPLE_OVERLAY` only marks this directory as a teaching artifact so bare
distribution discovery does not select it. It is not an auth/full-stack mode
flag. A real project should copy the distribution shape, remove the teaching
marker and fake provider, replace every local-only credential and identity,
and add its own deployment controls.

`frontend/site-ui/` is the smallest useful Site UI module: it replaces the home
page and nothing else. `make demo` does not use it; the Stage 2 frontend is the
standard image. The **Site UI Containers** CI job compiles it, and
[Your public pages](../../docs/developer/distribution-customization.md#your-public-pages)
shows how to build it into a frontend image yourself.
