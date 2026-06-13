# HybridInference Developer README

This guide is for contributors, maintainers, and operators working directly in the HybridInference repository.

## Prerequisites

- Python 3.10-3.13, with Python 3.12 recommended.
- [uv](https://github.com/astral-sh/uv) for Python dependency management.
- Node.js 22+ for the Next.js frontend.
- Docker Engine 24+ and Docker Compose v2+ for production-like local runs.
- Linux or macOS. Windows users should use WSL2.

## Setup

```bash
git clone --recurse-submodules https://github.com/HarvardMadSys/hybridInference.git
cd hybridInference

make setup-dev
```

Manual setup:

```bash
git submodule update --init --recursive
uv venv -p 3.12
source .venv/bin/activate
uv sync

cp .env.example .env
# Edit .env with local settings.
```

## Running Locally

Backend:

```bash
uvicorn serving.servers.app:app --host 0.0.0.0 --port 8080
```

Frontend:

```bash
cd apps/frontend
npm install
npm run dev
```

Production-like Docker stack:

```bash
cp .env.example .env
# Edit .env with database credentials, auth secrets, and provider keys.

make up
make ps
```

To start only PostgreSQL for local development:

```bash
docker compose -f deploy/docker/docker-compose.yml --env-file .env up -d postgres
```

## Quality Gates

```bash
make format   # ruff format . and ruff check --fix .
make lint     # ruff check --no-fix . and pydocstyle
make test     # pytest excluding external and dbtest markers
make all      # format, then run lint and test through make check
```

Run a specific test file:

```bash
uv run pytest tests/unit/routing/test_manager.py
```

## Test Tiers

- `tests/unit/`: fast mocked tests included in the default suite.
- `tests/api/`: API surface tests included in the default suite.
- `tests/integration/`: database or external-service tests, usually marked `dbtest`.
- `tests/e2e/`: Makefile-driven full-stack tests.
- `tests/external/`: live external-provider tests marked `external`.

Default test command:

```bash
pytest -q -m "not external and not dbtest"
```

## Repository Map

```text
apps/
  backend/
    serving/      # FastAPI gateway: HTTP, SSE, adapters, auth, storage, observability
    routing/      # Routing engine: strategies, routers, health, circuit breaker
    benchmark/    # Benchmark utilities
  frontend/       # Next.js web UI
config/           # YAML config: models, routing, routewise, alerts
services/         # status-monitor (+ -worker), freeinference-harness, alert-logger
tests/            # Unit, API, integration, e2e, external tests
ops/              # Operational tooling
deploy/           # Systemd units, Docker, observability manifests
docs/             # User docs, developer docs, agent specs/plans, reviews
```

## Architecture Entry Points

- Gateway app and HTTP/SSE handling: `apps/backend/serving/servers/`.
- Provider adapters: `apps/backend/serving/adapters/`.
- Routing engine: `apps/backend/routing/routers.py`, `apps/backend/routing/manager.py`, and `apps/backend/routing/strategies.py`.
- Storage backends: `apps/backend/serving/storage/`.
- Frontend dashboard: `apps/frontend/`.
- Architecture guide: [docs/developer/architecture.md](docs/developer/architecture.md).
- Routing guide: [docs/developer/routing.md](docs/developer/routing.md).

`apps/backend/routing/executor.py` is a compatibility shim that re-exports `FixedRouter` as `RouteExecutor`; edit `routers.py` for router behavior changes.

## Configuration Files

- `config/models.yaml`: required model registry.
- `config/routing.yaml`: optional local/remote split, routing strategy, and health checks.
- `config/routewise.yaml`: per-model routing overrides.
- `config/alerts.yaml`: alert rules.

YAML files support `${VAR}` and `${VAR:-default}` environment variable interpolation.

## Documentation

- User-facing docs source: `docs/free_inference/`.
- Developer docs source: `docs/developer/`.
- Public docs site: [doc.freeinference.org](https://doc.freeinference.org/).
- Developer docs site: [internaldoc.freeinference.org](https://internaldoc.freeinference.org/).

## Contribution Workflow

1. Branch from `dev`, not `main`.
2. Use a focused branch name such as `<user>/<scope>/<feature-name>`.
3. Make the smallest correct change that solves the problem.
4. Add or update tests for behavior changes.
5. Run `make format` and `make test` before opening a pull request.
6. Open pull requests against `dev`.

See [docs/developer/contributing.md](docs/developer/contributing.md) for more detail.
