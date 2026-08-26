# AGENTS.md

This is an onboarding guide for any AI coding agent (Claude Code, Cursor, Codex,
etc.) working in this repository. Read this first.

## 1. About this project

HybridInference is a FastAPI gateway that routes LLM requests across local
inference servers (vLLM, SGLang, Ollama) and remote OpenAI-compatible providers
(DeepSeek, Zhipu, OpenRouter, Anthropic, Gemini, etc.).

- **Repo README:** [README.md](README.md)
- **Developer guide:** [docs/developer/](docs/developer/)

If you are working on a particular deployment, its hosts, accounts and
operational notes live in that deployment's overlay — see
`distributions/<name>/AGENTS.md`. They are deliberately not here: this file
ships with the source, and a URL or credential written into it is published to
everyone who clones the repository.

## 2. Repo map

```text
apps/
  backend/
    serving/      # FastAPI gateway: HTTP, SSE, adapters, auth, storage, observability
    routing/      # Routing engine: strategies, routers, health, circuit breaker
  frontend/       # Next.js web UI
config/           # YAML config: models, routing, alerts
services/         # Sidecar workers (present only where a deployment ships them)
tests/
  unit/           # Fast, mocked. Default in CI.
  api/            # Per-provider API surface tests.
  integration/    # Hits a real DB or external service. Marker: dbtest.
  e2e/            # Makefile-driven full-stack runs.
  external/       # Hits live servers. Marker: external.
ops/              # Operational tooling (deploy, setup, runtime, admin, perf, db, cloudflare)
deploy/           # Systemd units, Docker, observability manifests
distributions/      # Overlays, one directory each. A neutral checkout carries
                    # only example/; deployment overlays are absent, see 6.5.
  <name>/           # manifest + site config, branding, content, docs
    content/docs/   # that deployment's public doc-site source (Sphinx)
  example/          # public runnable router example; EXAMPLE_OVERLAY marks it
                    # as a teaching artifact, so `make up` never selects it
docs/
  developer/      # Developer guide (built into the internal doc site)
  agents/         # Agent-facing artifacts: specs/ and plans/
  superpowers/    # Additional design specs/ and plans/
  reviews/        # Code review records
```

## 3. Getting set up

Prerequisites: Python 3.10–3.13 (3.12 recommended) and [uv](https://github.com/astral-sh/uv).

```bash
make setup-dev
```

Or manually:

```bash
uv venv -p 3.12
source .venv/bin/activate
uv sync
```

The Makefile honors a `UV_RUN` override: `make lint UV_RUN="uv run --active"`.

## 4. Quality gates

| Command | What it does |
|---|---|
| `make format` | `ruff format .` then `ruff check --fix .` |
| `make lint`   | `ruff check --no-fix .` and `pydocstyle` |
| `make test`   | `pytest -q -m "not external and not dbtest"` |
| `make all`    | format + lint + test |

Pre-commit hooks are installed by `make setup-dev`.

**Before opening a PR:** run `make format` and ensure `make test` passes.

## 5. Workflow

- **Branch off `dev`**, never `main`.
- **Branch naming:** `<user>/<scope>/<feature-name>` (e.g. `jason/claude/add-x`).
- **Use a git worktree** rather than working in the main checkout. Worktree should be put in /tmp/claude/worktree/<feature-name>.
- **PRs target `dev`.**
- **Verify against a running deployment** before claiming done. If you are
  working on one, its staging host and test account are in its overlay guide
  (`distributions/<name>/AGENTS.md`) — never write credentials into this file.

## 6. Project-specific knowledge

### 6.1 Architecture in one diagram

```text
client → FastAPI gateway (serving/) → routing engine (routing/) → adapter → provider
```

For the full diagram (network layer, observability, storage), see
[docs/developer/architecture.md](docs/developer/architecture.md).

### 6.2 Key abstractions

- **Adapter** — provider-specific client. Lives in `apps/backend/serving/adapters/`.
  Dedicated adapters: `openai_compat` (generic OpenAI-compatible APIs — also
  serves local vLLM/SGLang/Ollama), `claude` (Claude via Google Vertex),
  `anthropic` (direct api.anthropic.com), `gemini`, `openrouter`. Local
  inference servers have no dedicated adapter — they route through
  `openai_compat`.
- **`provider` vs `endpoint_id`** — `provider` is a string label on `ModelConfig`
  identifying the API service (used in metrics labels, e.g. `"openai"`,
  `"anthropic"`). `endpoint_id` is the unique per-endpoint key
  (format `{model_id}:{location}`, minted by `registry._make_provider_id` —
  `local-<port>` / `local` for a host in `_LOCAL_HOSTS`, otherwise
  `<service>-api`, e.g. `glm-4.6:local-12003`, `glm-4.6:zai-api`) used for
  latency profiling and availability tracking. The suffix is not a reliable
  ownership signal: a gateway-owned server on a LAN address is stamped
  `<octet>-api`, and an admin-supplied `route_id` becomes the `endpoint_id`
  verbatim. The word "upstream" appears informally in code
  comments meaning "the remote API" but isn't a formal type.
- **Router** — `FixedRouter` in [apps/backend/routing/routers.py](apps/backend/routing/routers.py)
  does weighted random selection plus automatic fallback.
- **`routing/executor.py`** — backward-compatibility shim that re-exports
  `FixedRouter` as `RouteExecutor`. **Do not edit it** — edit `routers.py` instead.
- **Strategy** — two layers. The deployment-wide weight strategy
  (`FixedRatioStrategy` in
  [apps/backend/routing/strategies/weight.py](apps/backend/routing/strategies/weight.py))
  is applied by `RoutingManager` in
  [apps/backend/routing/manager.py](apps/backend/routing/manager.py) from
  `config/routing.yaml` (`default_router:`, formerly `routing_strategy:`).
  Per-model router selection (`fixed` / `routewise`) lives in the
  [apps/backend/routing/strategies/](apps/backend/routing/strategies/) package
  and is dispatched by
  [apps/backend/routing/model_router_registry.py](apps/backend/routing/model_router_registry.py)
  from each model's `router:` field in `config/models.yaml`.
- **Circuit breaker / EWMA health** — provider health tracking in
  [apps/backend/routing/](apps/backend/routing/).

### 6.3 Configuration files

| File | Owns |
|---|---|
| `distributions/<name>/config/models.yaml` | Model registry — a deployment's; per-model `router:` / `router_params:` (incl. RouteWise tuning). `config/examples/` has one to start from |
| `distributions/<name>/config/routing.yaml` | Local/remote split, health checks — a deployment's; the gateway starts without one |
| `distributions/<name>/config/alerts.yaml` | Alert rules — a deployment's, not the project's |

YAML supports env var interpolation: `${VAR}` and `${VAR:-default}`.

### 6.4 Tests: markers and tiers

| Tier | Path | Marker | In default `make test`? |
|---|---|---|---|
| Unit | `tests/unit/` | — | yes |
| API surface | `tests/api/` | — | yes |
| Integration (DB / external) | `tests/integration/` | `dbtest` | no |
| End-to-end | `tests/e2e/` | — (Makefile-driven) | no |
| Live external | `tests/external/` | `external` | no |

Opt in to excluded tiers explicitly: `pytest -m dbtest tests/integration/`.

### 6.5 Common gotchas

- Don't commit to `main` or `dev` directly — always branch + PR.
- ALWAYS use a git worktree for development — never work in the main checkout.
- SSE streaming lives in `apps/backend/serving/servers/`. Middleware order
  matters; new middleware that buffers responses will break streaming.
- Storage layer supports both Postgres and Cloudflare D1 — check
  `apps/backend/serving/storage/` for the active backend before assuming
  SQL dialect.
- Frontend is Next.js in `apps/frontend/` — its quality gates are separate
  from the Python `make` targets.

### 6.6 The cloud agent moved out

The cloud agent lives in
[freeinference-cloud-agent](https://github.com/HarvardMadSys/freeinference-cloud-agent).
Production cut over on 2026-08-07 and the H4 removal PR deleted the agent code
from this repository. **All agent work — features, fixes, deploy — happens in
that repository now**; the plan and manifest are in
[docs/agents/plans/2026-08-03-cloud-agent-repo-split.md](docs/agents/plans/2026-08-03-cloud-agent-repo-split.md).

What remains here is the gateway's side of the two service contracts:

- **Identity** — `servers/routers/identity.py` (`/v1/identity/*`).
- **Inference grants** — `serving/grants.py` + `serving/grant_auth.py`,
  `servers/routers/agent_grants.py` (`/internal/agent-grants*`), the
  `/internal/*` lookups (`servers/routers/internal_lookups.py`,
  `serving/model_catalog.py`), and `api_logs.agent_job_id` for cost
  attribution.
- **`/agents` routing** — the console proxies the path to the standalone web
  app via the `AGENT_WEB_INTERNAL_URL` / `AGENT_CONTROL_PLANE_INTERNAL_URL`
  build args (`apps/frontend/next.config.js` rewrites); without them the path
  is a 404.

The old `agent_*` tables stay in existing databases as read-only history
(decision DR5); nothing here creates, reads or migrates them.

## 7. Common tasks

Pointer table. Each row links to the canonical doc or skill — this guide does
not duplicate their content.

| Task | Where to look |
|---|---|
| Implement a feature | [.kilo/skills/impl-feat/SKILL.md](.kilo/skills/impl-feat/SKILL.md) |
| Debug a bug or test failure | [.kilo/skills/debug/SKILL.md](.kilo/skills/debug/SKILL.md) |
| Address PR review / fix CI | [.kilo/skills/check-pr/SKILL.md](.kilo/skills/check-pr/SKILL.md) |
| Add a new model | [docs/developer/adding-models.md](docs/developer/adding-models.md) |
| Add a local model (vLLM/SGLang/Ollama) | [docs/developer/add-local-model.md](docs/developer/add-local-model.md) |
