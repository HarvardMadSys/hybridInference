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

The layout is in [docs/developer/contributing.md](docs/developer/contributing.md),
under "Repository layout". Two things it does not tell you:

- Deployment overlays under `distributions/` move out of this repository before
  publication. `example/` is the one that remains.
- `distributions/example/` is marked `EXAMPLE_OVERLAY`, and that marker is what
  excludes it from automatic distribution discovery. Select it explicitly
  with `make up DISTRIBUTION=example` to run the local tutorial.

## 3. Getting set up

Prerequisites: Python 3.10–3.13 (3.12 recommended) and [uv](https://github.com/astral-sh/uv).

```bash
make setup-dev
```

The Makefile honors a `UV_RUN` override: `make lint UV_RUN="uv run --active"`.

## 4. Quality gates

`make format`, `make lint`, `make test`, `make all`. Run `make help` for the
full target list.

[docs/developer/contributing.md](docs/developer/contributing.md) is the
canonical reference for the gates and the test tiers.

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
- **Router / HybridRouter abstraction** — the agreed design uses one common
  router contract with independent strategy implementations. Reuse
  [RouterProtocol](apps/backend/routing/protocols.py) as that contract; do not
  create a duplicate interface merely to name it `HybridRouter`. `FixedRouter`
  in [routers.py](apps/backend/routing/routers.py) and `RouteWiseRouter` are
  peer implementations, both able to select across local and cloud candidates.
  Future Greedy/Nimbus routers belong at the same level. Each implementation
  owns its routing, retry and feedback flow; shared helpers need not impose one
  execution loop on every strategy. A separate `RouteWisePolicy` is not required.
  **Naming distinction:** the existing concrete
  [routing.hybrid.HybridRouter](apps/backend/routing/hybrid.py) composes a policy
  with backends; it is not the common interface in the design. Its `FixedPolicy`
  / `BackendSelection` can remain internal or compatibility implementation
  details. Preserve public imports if names change.
  The current bootstrap sends mixed `router: fixed` models through that
  concrete composition and single-domain models through the shared FixedRouter.
  `router: routewise` directly returns RouteWiseRouter. Returning a concrete
  implementation through the common contract is valid; it does not by itself
  prove that the target backend execution boundary is wired.
- **Backend** — [backends.py](apps/backend/routing/backends.py) provides callable
  inference capability in one of two roles. A `LeafBackend` executes the single
  endpoint it is bound to: no resampling, no cross-endpoint fallback, and it
  requires the wrapped router to declare `supports_exact_dispatch` because
  requesting the controls is not the same as having them honored. A
  `TreeBackend` is the entry to a routing subtree and delegates to a scoped
  internal router; `LocalBackend` and `CloudBackend` are pools in that sense.
  The instructions are explicit and distinct
  ([dispatch.py](apps/backend/routing/dispatch.py)): `ExecuteEndpoint` binds one
  endpoint, `DelegatePool` grants selection inside a named pool, and a mismatch
  is refused before any upstream I/O as a composition error -- never recorded as
  an attempted provider. A pool accepts an in-scope `ExecuteEndpoint` as well, as
  a compatibility capability for the existing wrappers. Neither role duplicates
  adapter implementations or shared state.
  **Preserve Fixed and RouteWise request behavior.** RouteWise keeps its full
  candidate pool, reservations, re-solving, hedging, learning and lifecycle,
  using `llm_routewise.core` for algorithm primitives. Do not force it through
  the concrete composition router, a Fixed domain split, or a cloud-only pool.
  `RouteWiseCloudBackend` is an optional scoped compatibility wrapper, not the
  architectural home of RouteWise, and need not be removed for this design.
  Local/cloud ownership is independent of `on_demand`, `quota` and `concurrency`.
  A local GPU deployment can be a concurrency candidate when explicitly
  configured; do not recreate capacity pools per backend or model binding.
  Keep model `router` / `router_params`, defaults, registry caching, aliases and
  Admin switching. Verify in-flight requests and feedback retain their original
  owner across updates; preserving configuration alone is not proof of this.
  Execution scopes currently come from the composition root
  ([hybrid_composition.py](apps/backend/serving/servers/hybrid_composition.py)).
  Explicit `local_scope` / `local_ownership` are supported; bootstrap currently
  uses the hostname default, which can classify an owned LAN or cluster DNS
  endpoint as remote. Do not infer a RouteWise resource type from that domain.
  See
  [docs/agents/specs/2026-09-12-hybrid-routing-abstraction-design.zh.md](docs/agents/specs/2026-09-12-hybrid-routing-abstraction-design.zh.md)
  for the agreed router/execution boundary, current implementation differences
  and behavior-preservation contract. That document revision changes no runtime
  code and does not claim the target integration has been completed.
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

YAML supports env var interpolation, in two dialects: the model registry
substitutes only a whole `${VAR}` value, while the routing and alert files also
handle `${VAR:-default}` and variables embedded in longer strings.

### 6.4 Tests: markers and tiers

| Tier | Path | Marker | In default `make test`? |
|---|---|---|---|
| Unit | `tests/unit/` | — | yes |
| API surface | `tests/api/` | — | yes |
| Integration (DB / external) | `tests/integration/` | `dbtest` | no |
| Live external | `tests/external/` | `external` | no |

Opt in to excluded tiers explicitly: `pytest -m dbtest tests/integration/`.

### 6.5 Common gotchas

- Don't commit to `main` or `dev` directly — always branch + PR.
- ALWAYS use a git worktree for development — never work in the main checkout.
- SSE streaming lives in `apps/backend/serving/servers/`. Middleware order
  matters; new middleware that buffers responses will break streaming.
- Storage layer is Postgres (asyncpg), in `apps/backend/serving/storage/`.
  Cloudflare D1 support was removed in #460; `DB_BACKEND` and `DB_DUAL_WRITE`
  are read by no code. There is one SQL dialect — write Postgres.
- Alerting has two permanent, independent paths (decision: issue #1103).
  Backend gateway alerts go through `alert_slack()`
  (`apps/backend/serving/observability/alerts.py`) to a Slack webhook;
  status-monitor alerts go through the status monitor and alert control plane
  workers owned and deployed by the external
  [freeInference repository](https://github.com/HarvardMadSys/freeInference).
  Do not wire backend producers to that control plane.
- Frontend is Next.js in `apps/frontend/` — its quality gates are separate
  from the Python `make` targets.

### 6.6 The cloud agent moved out

The cloud agent lives in
[hybridInference-cloud-agent](https://github.com/HarvardMadSys/hybridInference-cloud-agent).
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
  server-only runtime environment (`apps/frontend/src/app/agents/` route
  handler); without either value the path is a 404. The values never enter the
  browser bundle.

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
| Run / operate the cloud agent | `docs/operations.md` in [hybridInference-cloud-agent](https://github.com/HarvardMadSys/hybridInference-cloud-agent) |
