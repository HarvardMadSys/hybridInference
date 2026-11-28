# Runnable local distribution example

A complete, neutral HybridInference distribution for learning. It needs no
provider account, host `.env`, GPU, SMTP service or paid API key. The
[Quickstart](../../docs/developer/router-tutorial.md) walks through it; this
page is a quick reference. Run every command from the repository root.

## Stage 1: prove the router works

```bash
make up DISTRIBUTION=example
make smoke DISTRIBUTION=example
```

This starts only the deterministic `example-provider` and the backend, with no
Postgres, frontend, accounts or API-key authentication, and prints
`EXAMPLE_SMOKE_OK`. Leave it running for Stage 2.

## Stage 2: the Web and Admin Console

```bash
make demo DISTRIBUTION=example
```

`make demo` keeps the provider, adds the frontend and a Postgres database of the
example's own, and recreates the backend with authentication. On first start
the backend generates its JWT and API-key secrets into that database, stores
the settings from `deploy/docker-compose.demo.yml` there, and logs a one-time
setup code:

```bash
docker logs hybridinference-example-backend 2>&1 | grep 'setup code'
```

Open <http://localhost:13001>, enter the code (reprinted at every start until
setup is done), and create the administrator with the username `admin` and a
demo-only password. Then run the full smoke with that password:

```bash
EXAMPLE_DEMO_ADMIN_PASSWORD='<the same password>' \
make demo-smoke DISTRIBUTION=example
```

It exercises the real auth and API routes, the Admin Console and Playground
backends and a non-admin `403`, checks that the account, session and API key
survive a backend recreation, and prints `EXAMPLE_FULL_SMOKE_OK`. If setup is
still pending, it completes it as `admin`, reading the code from the container
named by `EXAMPLE_BACKEND_CONTAINER_NAME` (or `--backend-container`).

The checked-in defaults are local-only, not production credentials or a
production security configuration. Email verification is off, public signups
are not administrators, and the optional Agents and pgAdmin services are not
started.

## Stage 3: use local inference

Stage 2 stored the three `EXAMPLE_UPSTREAM_*` settings in the example database,
where they outrank `deploy/backend.env` and your shell. In the Admin Console,
open **Settings → Providers** and set `EXAMPLE_UPSTREAM_BASE_URL` to
`http://host.docker.internal:8000/v1`, `EXAMPLE_UPSTREAM_API_KEY` to
`local-placeholder` and `EXAMPLE_UPSTREAM_MODEL` to the served model name. Then
select **Save** and **Restart backend**. Clients still request `example-chat`.
To make the change from a terminal, as CI does:

```bash
export EXAMPLE_UPSTREAM_BASE_URL=http://host.docker.internal:8000/v1
export EXAMPLE_UPSTREAM_API_KEY=local-placeholder
export EXAMPLE_UPSTREAM_MODEL='<served-model-name>'
EXAMPLE_DEMO_ADMIN_PASSWORD='<the admin password>' \
python3 distributions/example/full_smoke.py --configure-upstream
make demo DISTRIBUTION=example
```

`--configure-upstream` signs in as `admin` and stores the exported values
through the admin configuration API; `make demo` recreates the backend so the
model registry reads them.

The model server (vLLM, SGLang, Ollama or any OpenAI-compatible server) must
listen on an address Docker can reach, such as `0.0.0.0`. That can expose an
unauthenticated model server to your LAN, so use a host firewall or a private
interface. The smoke checks expect the bundled provider's fixed reply; test a
real model with a normal completion. `EXAMPLE_UPSTREAM_API_KEY` is the
gateway's credential for the provider, not a HybridInference API key.

## Stop, resume and reset

| Command | Effect |
|---|---|
| `make demo-down DISTRIBUTION=example` | Stops the full example and keeps its account, key, settings and history. `make demo DISTRIBUTION=example` resumes at the stage you stopped at. |
| `make demo-reset DISTRIBUTION=example` | Deletes the example's data, including the administrator, generated secrets and stored settings; the next `make demo` starts setup again. It cannot remove the production `hybridinference_postgres_data` volume. |
| `make down DISTRIBUTION=example` | Stops a project still at Stage 1. |

After Stage 2 has started, never go back with plain
`make up DISTRIBUTION=example`: the running frontend and Postgres and the Stage
1 backend inputs would form a mixed stack. Reset and start again at Stage 1.
`make logs DISTRIBUTION=example` follows every service; add `s=backend`,
`s=frontend` or `s=postgres` for one.

## Ports and isolation

Defaults are loopback-only: backend `18080`, frontend `13001`, Postgres
`15432`. The example has its own Compose project, container names and
project-scoped database volume, and ignores a host-local `.env`, so CI can
override every port and name without colliding with another HybridInference
stack.

## Optional quota reporting

`quota_extension.py` is an opt-in backend extension that reads a simulated
account's daily request usage from the fixture's `/usage` endpoint, which the
fixture's `--quota-limit` option enables.
[Quota reporting](../../docs/developer/backend-extensions.md#quota-reporting)
walks through it with `config/examples/models.routewise.quota.json`. The
simulation is not production accounting, resets when its process restarts or
at UTC midnight, and is not enabled by `make up` or `make demo`.

## Directory contents

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
├── frontend/site-ui/
├── quota_extension.py
├── smoke.py
└── full_smoke.py
```

`EXAMPLE_OVERLAY` only marks this directory as a teaching example, so bare
distribution discovery skips it; it is not an auth or stage flag. A real
project copies the shape, removes the marker and the fake provider, replaces
every local-only credential and identity, and adds its own deployment controls.

`frontend/site-ui/` is the smallest useful Site UI module: it replaces only the
home page. `make demo` does not use it (Stage 2 runs the standard frontend
image); the **Site UI Containers** CI job compiles it, and
[Site UI Modules](../../docs/developer/site-ui-modules.md) shows how to build it
into an image.
