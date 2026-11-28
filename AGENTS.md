# AGENTS.md

Onboarding guide for AI coding agents (Claude Code, Cursor, Codex and others)
working in this repository. Read it first.

## 1. About this project

HybridInference is a FastAPI gateway that routes LLM requests across local
inference servers (vLLM, SGLang, Ollama) and remote OpenAI-compatible providers
(DeepSeek, Zhipu, OpenRouter, Anthropic, Gemini and others). Start with the
[README](README.md) and the [developer guide](docs/developer/).

A deployment's hosts, accounts and operational notes belong in its overlay
guide, `distributions/<name>/AGENTS.md`. Never add them here: this file ships
with the source, so any URL or credential in it is published to everyone who
clones the repository.

## 2. Repo map

[contributing.md](docs/developer/contributing.md#repository-layout) describes
the layout. Deployment overlays live in their own repositories;
`distributions/example/` is the only one here, and its `EXAMPLE_OVERLAY` marker
keeps automatic discovery from selecting it. Run the local tutorial with
`make up DISTRIBUTION=example`.

## 3. Getting set up

You need Python 3.11–3.13 (3.12 recommended) and
[uv](https://github.com/astral-sh/uv). `make setup-dev` creates the environment
and installs the pre-commit hooks. Python `make` targets run through `uv run`;
override it with `UV_RUN`, for example `make lint UV_RUN="uv run --active"`.

## 4. Quality gates

The gates are `make format`, `make lint`, `make test` and `make all`
(`make help` lists every target);
[contributing.md](docs/developer/contributing.md#quality-gates) says what each
runs and describes the test tiers.

Before you open a PR, run `make format` and make sure `make test` passes. If the
change touches storage, SQL, the schema or a database test, `make test-db-local`
must pass too. It runs the `dbtest` tier against one shared local PostgreSQL
container (Docker), in a database of this worktree's own; re-runs reuse it, and
the full tier takes about 15 seconds.

Other agents use that container at the same time. **Never run
`make test-db-local-down`**: it discards every worktree's databases and breaks
their runs. To start this worktree's databases afresh, for example after
switching to an older branch, run `make test-db-local-reset`.

## 5. Workflow

- Branch off `dev`, never `main`, and target PRs at `dev`. Never commit to
  `main` or `dev` directly.
- Name branches `<user>/<scope>/<feature-name>`, for example
  `jason/claude/add-x`.
- Always work in a git worktree in `/tmp/claude/worktree/<feature-name>`, never
  in the main checkout.
- Verify runtime changes against a running deployment before you claim live
  acceptance; a local or example deployment is enough when appropriate. For a
  specific deployment, follow its `distributions/<name>/AGENTS.md`, and never
  write its credentials here. Documentation and skill changes need content and
  link checks, not a deployment.

## 6. Project-specific knowledge

### 6.1 Architecture in one diagram

```text
client → FastAPI gateway (serving/) → routing engine (routing/) → adapter → provider
```

[architecture.md](docs/developer/architecture.md) has the full picture.

### 6.2 Key abstractions

[Architecture](docs/developer/architecture.md) explains adapters, `provider`
versus `endpoint_id`, and the two kinds of strategy.
[Routing](docs/developer/routing.md#router-types-and-execution-boundaries) explains routers,
leaves and pools, and the
[composable hybrid routing design](docs/agents/specs/2026-09-14-composable-hybrid-routing-design.zh.md)
is their agreed contract, with the opt-in wiring and deferred work. The rules:

- Adapters live in `apps/backend/serving/adapters/`. Local vLLM, SGLang and
  Ollama servers use `openai_compat`; they need no adapter of their own.
- Do not infer ownership from an `endpoint_id` (`{model_id}:{location}`, such
  as `glm-4.6:local-12003` or `glm-4.6:zai-api`, minted by
  `registry._make_provider_id`). A gateway-owned server on a LAN address gets
  `<octet>-api`, and an admin-supplied `route_id` becomes the `endpoint_id`
  unchanged.
- `RouterProtocol` ([protocols.py](apps/backend/routing/protocols.py)) is the
  one common router contract; do not create a duplicate interface just to name
  it `HybridRouter`. `FixedRouter` ([routers.py](apps/backend/routing/routers.py))
  and `RouteWiseRouter` are peer implementations that both select across local
  and cloud candidates, and future Greedy or Nimbus routers belong beside them.
  Each owns its routing, retry and feedback flow: do not force one shared
  execution loop on every strategy, and RouteWise needs no separate
  `RouteWisePolicy`.
- [routing.hybrid.HybridRouter](apps/backend/routing/hybrid.py) is a concrete
  composition of a policy with backends, not the common interface. Bootstrap
  uses it only for a `router: fixed` model with both local and cloud candidates
  and `router_params.hybrid_composition: true`; other Fixed models keep the
  shared `FixedRouter`. Its `FixedPolicy` and `BackendSelection` can stay
  internal, but keep public imports working if you rename them. It still
  differs from `FixedRouter` in affinity, re-selection after a rejected primary
  claim, and fallback circuit timing; resolve those before enabling it by
  default.
- Every attempt executes through a `LeafBackend`
  ([backends.py](apps/backend/routing/backends.py)): Fixed chat and stream,
  primary and fallback, RouteWise decisions and both hedge legs, and RouteWise's
  latency probe. A leaf runs one bound adapter with the adapter's call
  signatures and keeps no queue, reservation, health record, failed-attempt
  sample or `_routing` block. That state stays with the router that chose the
  endpoint, and a second copy would double-count it. A `TreeBackend` (pools
  such as `LocalBackend` and `CloudBackend`) delegates to a scoped router.
- [dispatch.py](apps/backend/routing/dispatch.py) keeps two instructions
  distinct: `ExecuteEndpoint` runs one endpoint through an `EndpointBinding`
  that carries the resolved adapter, and `DelegatePool` grants selection inside
  a named pool. A mismatch is refused before any upstream I/O as a composition
  error, never recorded as an attempted provider.
- Preserve Fixed and RouteWise request behavior. `router: routewise` returns
  `RouteWiseRouter` directly, which by itself does not prove the target backend
  execution boundary is wired. RouteWise keeps its full candidate pool,
  reservations, re-solving, hedging, learning and lifecycle, with
  `llm_routewise.core` for algorithm primitives. Do not force it through the
  composition router, a Fixed domain split or a cloud-only pool.
  `RouteWiseCloudBackend` is an optional scoped compatibility wrapper, not
  RouteWise's architectural home, and need not be removed.
- Local/cloud ownership is independent of the `provider_type` values
  `on_demand`, `quota` and `concurrency`; an explicitly configured local GPU
  deployment can be a concurrency candidate. Do not recreate capacity pools per
  backend or model binding. Execution scopes currently come from the
  composition root, [hybrid_composition.py](apps/backend/serving/servers/hybrid_composition.py):
  an explicit `local_scope` or `local_ownership`, otherwise each route's
  `deployment_type` (`self_hosted` makes a LAN or cluster-DNS endpoint local,
  `provider_api` keeps a loopback proxy in the cloud scope, `auto` keeps the
  hostname default). Outbound admission uses the same classification; endpoint
  IDs and RouteWise resource types do not change.
- Keep model `router` / `router_params`, defaults, registry caching, aliases
  and Admin switching. Verify that in-flight requests and feedback keep their
  original owner across updates; preserved configuration alone does not prove
  it.
- The deployment-wide weight strategy (`FixedRatioStrategy`, applied by
  `RoutingManager` in [manager.py](apps/backend/routing/manager.py) from the
  `ROUTING_POLICY` document when its `default_router` is `fixed`) is separate
  from per-model router selection (each model's `router` in `MODEL_CATALOG`,
  dispatched by [model_router_registry.py](apps/backend/routing/model_router_registry.py)).
  The circuit breaker and EWMA health tracking live in
  [endpoint_health.py](apps/backend/routing/endpoint_health.py).

### 6.3 Configuration

Application configuration lives in PostgreSQL and is edited through Admin.
There are no runtime model, routing, alert, distribution or branding YAML
files, and an unconfigured gateway shows “Ask admin to configure” instead of
picking example models or a provider.

| Configuration | Owner |
|---|---|
| Model catalog, optional routing policy, alert rules, site identity and branding, single sign-on providers | `app_config`: `MODEL_CATALOG`, `ROUTING_POLICY`, `ALERT_RULES`, `SITE_CONFIGURATION`, `SITE_BRANDING`, `SSO_PROVIDERS`, as validated JSON |
| Provider keys, live routes, weights and model overrides | Admin Providers/Routing and the operational store |
| Database connection and infrastructure wiring | `.env` and Docker Compose; persistent lifecycle choices in `.ops.env` |

Documents refer to credentials as `${NAME}`, stored as separate secret
settings. A setting resolves from its database row, then the environment seed,
then the registered default. Catalog, routing-policy and alert-rule changes
need a restart; Admin route edits synchronize across processes, and each
process reports whether it applied them. Preserve request ownership and
feedback across those updates. Old YAML is imported only by an explicit
`python -m serving.config.manage import-legacy`, never at startup; keep the old
files and `route_catalog` rows through the rollback window. See
[the accepted design](docs/agents/specs/2026-11-08-database-only-configuration.md)
and [configuration.md](docs/developer/configuration.md).

### 6.4 Tests: markers and tiers

`make test` skips tests marked `dbtest` (need PostgreSQL; mostly
`tests/integration/`) and `external` (call live servers; `tests/external/`); opt
in to those explicitly. For `dbtest`, use `make test-db-local`
(`ARGS="tests/integration/test_x.py"` narrows it; the `dbtest` marker is always
kept). To run anything else against that database, first run
`eval "$(ops/dev/test-db-local.sh env)"` in the same worktree.

### 6.5 Common gotchas

- SSE streaming lives in `apps/backend/serving/servers/`. Middleware order
  matters; middleware that buffers responses breaks streaming.
- Storage is PostgreSQL (asyncpg) in `apps/backend/serving/storage/`, with one
  SQL dialect: write Postgres. `DB_BACKEND` and `DB_DUAL_WRITE` are read by no
  code.
- The schema comes only from the migrations in `apps/backend/migrations/`; the
  service issues no DDL, and a test enforces it. A schema change is a new
  revision that keeps the previous release working, never an edit to the frozen
  `baseline/` (its digest is pinned). See the
  [versioned schema migrations design](docs/agents/specs/2026-11-02-versioned-schema-migrations-design.zh.md).
- Alerting has two permanent, independent paths (issue #1103). Backend gateway
  alerts go through `alert_slack()`
  (`apps/backend/serving/observability/alerts.py`) to one configured Slack or
  WeCom webhook. Status-monitor alerts go through the status monitor and alert
  control plane workers that the external
  [freeInference repository](https://github.com/HarvardMadSys/freeInference)
  owns and deploys. Do not wire backend producers to that control plane.
- The frontend is Next.js in `apps/frontend/`, with its own
  [quality gates](docs/developer/contributing.md#frontend).

### 6.6 The cloud agent

The cloud agent lives in
[cloud-agent](https://github.com/HarvardMadSys/cloud-agent). **Do all agent
work there: features, fixes and deployment.** This repository keeps only the
gateway's side of its contracts (backend paths relative to
`apps/backend/serving/`):

- Identity: `servers/routers/identity.py` (`/v1/identity/*`).
- Inference grants: `grants.py`, `grant_auth.py`,
  `servers/routers/agent_grants.py` (`/internal/agent-grants*`), the
  `/internal/*` lookups (`servers/routers/internal_lookups.py`,
  `model_catalog.py`), and `api_logs.agent_job_id` for cost attribution.
- `/agents`: the console route handler in `apps/frontend/src/app/agents/`
  proxies it to the standalone web app through the server-only runtime
  variables `AGENT_WEB_INTERNAL_URL` and `AGENT_CONTROL_PLANE_INTERNAL_URL`,
  which never enter the browser bundle. Without either, the path is a 404.

The agent's old tables (`agent_jobs`, `agent_threads` and the rest) may remain
in existing databases as read-only history; nothing here creates, reads or
migrates them. `agent_grants` belongs to the grant contract: the migrations
create it, and `storage/postgres_operational.py` reads and writes it.
[test_no_agent_tables.py](tests/unit/storage/test_no_agent_tables.py) lists
every agent table the gateway still creates, and why.

## 7. Common tasks

Repository skills live only in `.agents/skills/`; use and edit them there.
Every skill follows these rules:

- Reuse a suitable task worktree or create one (section 5), preserve other
  work, and run the gates in section 4.
- The user's instructions decide whether you commit, publish comments, push or
  open a PR.
- Merging and deploying need authorization for that action. Neither a skill
  name nor a green CI result grants it, and a request to monitor is not
  permission to merge.
- Never force-push or bypass branch protection.

| Task | Where to look |
|---|---|
| Implement a feature | [.agents/skills/impl-feat/SKILL.md](.agents/skills/impl-feat/SKILL.md) |
| Debug a bug or test failure | [.agents/skills/debug/SKILL.md](.agents/skills/debug/SKILL.md) |
| Address PR review / fix CI | [.agents/skills/check-pr/SKILL.md](.agents/skills/check-pr/SKILL.md) |
| Follow one PR to readiness | [.agents/skills/babysit-pr/SKILL.md](.agents/skills/babysit-pr/SKILL.md) |
| Add a new model | [docs/developer/adding-models.md](docs/developer/adding-models.md) |
| Add a local model (vLLM/SGLang/Ollama) | [docs/developer/add-local-model.md](docs/developer/add-local-model.md) |
| Run or operate the cloud agent | `docs/developer/operations.md` in [cloud-agent](https://github.com/HarvardMadSys/cloud-agent) |

## 8. Local subsystem guidance

A subsystem with a lasting ownership boundary may have its own `AGENTS.md`.
Inside it, follow this guide and every ancestor guide on the path; a nested
guide adds to its ancestors and never overrides repository-wide workflow or
quality rules. Add one only where ownership, invariants other code relies on, a
staged migration, specialized verification or repeated cross-boundary mistakes
justify a local contract; not every package needs one. A subsystem guide states
what it owns and does not own, its invariants, cross-boundary edit
expectations, verification categories and pointers to design records. It does
not duplicate architecture specs, plans, file maps or call graphs.

Design records are kept only while they govern live work.
[`docs/agents/specs/`](docs/agents/specs/) holds accepted designs that this
guide, a subsystem guide or the developer guide points to, and
[`docs/agents/plans/`](docs/agents/plans/) holds plans still being carried out.
Delete a record once its work has shipped, been abandoned or been superseded;
git history keeps it. Verification evidence belongs in the pull request. A
nested guide replaces none of these records.

The current implementation is not automatically the target architecture. A
guide for a subsystem under revision must say so and name the record that
governs it; read that record before broad changes. When an accepted
architecture changes, update or replace its record in the same change. Update a
subsystem guide only when its ownership, invariants, edit rules or verification
contract change.

### Subsystem guidance index

| Path | Purpose |
|---|---|
| [`apps/backend/routing/`](apps/backend/routing/AGENTS.md) | Pilot guidance for the routing ownership boundary |
