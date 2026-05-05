# Architecture Review — 2026-05-03

This document consolidates five per-subsystem architecture reviews of the FreeInference codebase into a single deep-dive. Scope covers the FastAPI gateway and adapters (`serving/`), the routing layer (`routing/` and `routing/routewise/`), the Next.js dashboard (`frontend/`), client-side tooling (`client/`, `freeinference-harness/`, `llm-prober/`), and the operational layer (deployment, scripts, CI, observability infra). Methodology was static review of code organization, request lifecycle, coupling, file-size hotspots, and failure-handling. Out of scope: dynamic profiling, security audit, model-quality benchmarks, and external dependencies beyond their integration shape.

## Executive Summary

- **Observability is gutted across the stack.** [serving/observability/metrics.py](serving/observability/metrics.py) is no-op shims, Prometheus scrape is disabled at [deploy/prometheus/prometheus.yml:33-40](deploy/prometheus/prometheus.yml#L33), only 3 alerts route (the rest blackhole), and there is no distributed tracing. See [serving/](#serving-fastapi-gateway-adapters-storage-observability) and [infrastructure](#infrastructure--config--scripts-deployment--ops).
- **`completions.py` is a 1030-line god-object** mixing auth, role gating, streaming, cost, logging, and routing observation in one file ([serving/servers/routers/completions.py:1030](serving/servers/routers/completions.py#L1030)). See [serving/](#serving-fastapi-gateway-adapters-storage-observability).
- **`RouteWiseRouter` is a 1188-line monolith** combining 3-tier classification, primal-dual LP, hedging, quota, concurrency, and predictor — with `_pending_decisions` keyed by request_id and no cleanup guard ([routing/routewise/router.py](routing/routewise/router.py)). See [routing/](#routing-router-executor-routewise).
- **The admin page is a 3343-line React monolith** with 32 useState calls ([frontend/src/app/dashboard/admin/page.tsx](frontend/src/app/dashboard/admin/page.tsx)). See [frontend/](#frontend-nextjs-dashboard--admin).
- **Cost increment and request logging are fire-and-forget** with no observability — if a user disconnects mid-stream, cost may be lost ([serving/servers/routers/completions.py:46-95](serving/servers/routers/completions.py#L46)). See [serving/](#serving-fastapi-gateway-adapters-storage-observability).
- **No schema migration framework.** Schema is applied by startup initialization code, with no rollback. See [infrastructure](#infrastructure--config--scripts-deployment--ops).
- **SSH-based git-reset deployment with no human gates or rollback automation.** Any green CI on `main` SSHes into prod and `git reset --hard`s. See [infrastructure](#infrastructure--config--scripts-deployment--ops).
- **Secrets sit unencrypted in `.env` files on disk** (API keys, DB passwords, JWT secret, Slack webhooks). See [infrastructure](#infrastructure--config--scripts-deployment--ops).
- **DB backups run nightly to S3 but are never restore-tested**; RTO/RPO undocumented. See [infrastructure](#infrastructure--config--scripts-deployment--ops).
- **`HybridClient.send_request()` is a `NotImplementedError` stub** in the load-test framework ([client/client.py:22-27](client/client.py#L22)); `llm-prober/` is an empty placeholder. See [client tooling](#client-tooling-client-freeinference-harness-llm-prober).
- **Two disjoint client tools with duplicated metrics and incompatible request models.** Black-box decoupling of `freeinference-harness/` is policy, not enforced. See [client tooling](#client-tooling-client-freeinference-harness-llm-prober).

## Subsystem Deep Dives

### serving/ (FastAPI gateway, adapters, storage, observability)

#### Top-Level Structure

`serving/` is well-organized into clear concerns:

- **`adapters/`** (19 files): Provider-specific request/response transformation layer (OpenAI, Anthropic, Gemini, OpenRouter, vLLM, SGLang).
- **`servers/`**: FastAPI application core
  - `routers/` (9 routes): HTTP endpoints for chat, embeddings, auth, health, admin
  - `middleware/` (4 modules): request logging, error handling, request ID, timeout
  - `app.py`, `bootstrap.py`, `auth.py`, `deps.py`, `registry.py`, `concurrency.py`
- **`storage/`**: `OperationalStore` & `LogStore` abstractions backed by PostgreSQL implementations.
- **`observability/`**: Metrics (currently no-op shims).
- **`config/`, `utils/`, `auth/`, `admin/`**: Configuration, JWT, email, rate limiting, admin endpoints.

#### Request Lifecycle

A typical `POST /v1/chat/completions` request:

1. **Entry** ([serving/servers/routers/completions.py:144](serving/servers/routers/completions.py#L144)): FastAPI route handler receives request, auth header.
2. **Auth** ([serving/servers/auth.py:78](serving/servers/auth.py#L78)): `verify_api_key()` dependency checks API key hash against `OperationalStore`, returns `user_ctx` with `user_id`, `role`.
3. **Concurrency gate** ([serving/servers/concurrency.py:144](serving/servers/concurrency.py#L144)): `enforce_user_concurrency` acquires per-user slot (role-sticky capacity).
4. **Routing** ([serving/servers/routers/completions.py:201-348](serving/servers/routers/completions.py#L201)): Route executor selects adapter(s) via `router_exec.routes[model]`, checks role-based access.
5. **Adapter dispatch** ([serving/servers/routers/completions.py:350+](serving/servers/routers/completions.py#L350)): Selected adapter (e.g., `OpenAICompatAdapter`) transforms request, calls upstream.
6. **Streaming** ([serving/adapters/http.py:116-262](serving/adapters/http.py#L116)): For streaming responses, SSE/NDJSON parser yields chunks.
7. **Cost tracking** ([serving/servers/routers/completions.py:683](serving/servers/routers/completions.py#L683), [serving/servers/routers/completions.py:691](serving/servers/routers/completions.py#L691)): Fire-and-forget background task calls `op_store.increment_user_cost()`.
8. **Logging** ([serving/servers/routers/completions.py:46-69](serving/servers/routers/completions.py#L46)): Fire-and-forget background task calls `log_store.log_request()` with full metadata.
9. **Response**: Serialized back to client; status + latency + tokens emitted to structured logs.

**Concern**: Cost increment and logging both run async without `await`. If the user disconnects before streaming completes, cost may not be tracked (see [serving/servers/routers/completions.py:95](serving/servers/routers/completions.py#L95)).

#### Adapter Abstraction

Strong abstraction via `BaseAdapter` ([serving/adapters/base.py:1-100](serving/adapters/base.py#L1)):

- All adapters extend `BaseAdapter` with common interface: `generate()`, `stream()`, `stream_delta()`.
- `ModelConfig` ([serving/adapters/base.py:48](serving/adapters/base.py#L48)) is the canonical config object shared across all adapters.
- Provider variability is centralized via [serving/registry.py:20-29](serving/registry.py#L20), which imports all adapter classes and instantiates based on YAML `kind` field.
- No provider branching in routers — route handlers are provider-agnostic, config-driven.

**Concern**: `OpenAICompatAdapter` is 782 lines ([serving/adapters/openai_compat.py](serving/adapters/openai_compat.py)), containing extensive profile-based usage normalization logic. This file has grown to absorb multiple provider quirks (DeepSeek cache semantics, GLM `function_call` format, Qwen structured output). Good encapsulation, but approaching the limit of single-responsibility.

#### Storage / Persistence

Storage architecture:

- **`OperationalStore`** (users, API keys, sessions, tokens): PostgreSQL.
- **`LogStore`** (request logs, hourly stats): PostgreSQL.

Schema shape: [serving/storage/postgres_operational.py](serving/storage/postgres_operational.py) handles operational tables. [serving/storage/postgres_log.py](serving/storage/postgres_log.py) maintains hourly bucketed logs. Stores have immutable column allowlists ([serving/storage/base.py:36-60](serving/storage/base.py#L36)) to prevent SQL injection in dynamic UPDATE paths.

**Concerns**:

- No foreign keys between operational & log stores — accidental orphaning is silent.

#### Observability

**Metrics** ([serving/observability/metrics.py](serving/observability/metrics.py)): All metrics are currently no-op shims (`prometheus-client` removed). ~40 metric symbols remain as stubs for backward compatibility. Callers still emit labels (`model`, `provider`, `status_code`) but they're discarded.

**Structured logging** via `get_logger()` ([serving/utils/logging.py](serving/utils/logging.py)):

- `RequestLogMiddleware` ([serving/servers/middleware/request_log.py:22](serving/servers/middleware/request_log.py#L22)) emits per-request logs with method, path, status, duration_ms, model, provider, IP.
- Routers add contextual logs (role violations, concurrency rejection, routing decisions).
- Request context ([serving/utils/context.py](serving/utils/context.py)) uses `contextvars` for trace propagation; populated with request_id, provider, auth_key_hash.

**Gaps**:

- No distributed tracing (no OpenTelemetry, W3C trace context headers).
- No span-level latency breakdown (time in auth, routing, adapter, vs. upstream).
- Logging is structured but metrics are gone — no alerting on error rates, latency percentiles, or provider health.

#### Coupling and Layering

**Clean separations**:

- Routers are provider-agnostic, config-driven.
- Auth concerns isolated in [serving/servers/auth.py](serving/servers/auth.py).
- Storage layer abstracted behind store protocols.
- Adapters have minimal dependency on router logic.

**Coupling issues**:

- Router ↔ Storage: `verify_api_key` ([serving/servers/auth.py:78](serving/servers/auth.py#L78)) directly reads from `OperationalStore` (user context, quota check).
- Router ↔ Cost calculation: `_schedule_cost_increment` ([serving/servers/routers/completions.py:72](serving/servers/routers/completions.py#L72)) mixed with response logic; cost calculation logic in `storage/utils.py:calculate_cost()` but pricing extracted in router.
- Router ↔ Routing metadata: `_record_routing_observation` passes RouteWise-specific data through structured dicts with no schema validation (`routing_info` keys expected by callers, [serving/servers/routers/completions.py:110+](serving/servers/routers/completions.py#L110)).
- Adapter ↔ profile logic: `profiles.py` (imported from `OpenAICompatAdapter`) centralizes provider quirks, but if a new adapter needs profile-specific behavior, it must either import `profiles.py` or duplicate logic.

#### Files > 800 Lines

| File | Lines | Responsibility |
|------|-------|----------------|
| [serving/storage/postgres_operational.py](serving/storage/postgres_operational.py) | 1499 | All user/key/session CRUD for Postgres |
| [serving/storage/database.py](serving/storage/database.py) | 1096 | Postgres connection pooling, migration runner |
| [serving/servers/routers/completions.py](serving/servers/routers/completions.py) | 1030 | Chat completions endpoint with routing, streaming, cost tracking, logging |
| [serving/servers/routers/user_routes.py](serving/servers/routers/user_routes.py) | 865 | User profile, API key mgmt, quota reads |
| [serving/schemas_admin.py](serving/schemas_admin.py) | 825 | Admin-facing Pydantic schemas |
| [serving/adapters/openai_compat.py](serving/adapters/openai_compat.py) | 782 | Generic OpenAI-compatible provider adapter |

`completions.py` (1030 lines) is the critical hotspot: mixing auth checks, role-based gating, streaming logic, error handling, cost calculation, routing observation recording, and background task scheduling.

#### Top Architectural Concerns

1. Fire-and-forget cost/logging without observability ([serving/servers/routers/completions.py:46-95](serving/servers/routers/completions.py#L46)).
2. Metrics removed, no alerting on inference quality ([serving/observability/metrics.py](serving/observability/metrics.py)).
3. Router centralization and single-responsibility creep ([serving/servers/routers/completions.py](serving/servers/routers/completions.py), 1030 lines).
4. No schema validation on routing metadata ([serving/servers/routers/completions.py:110-130](serving/servers/routers/completions.py#L110), `routing_info` dict).

### routing/ (router, executor, RouteWise)

#### Top-Level Structure

**Base layer** (`routing/`):

- [routing/routers.py](routing/routers.py) (830 lines) — Core: `BaseRouter` abstract class with circuit breaker, EWMA health tracking, `FixedRouter` and `RouteWiseRouter` peer.
- [routing/manager.py](routing/manager.py) (134 lines) — `RoutingManager` applies config-driven weights via `FixedRatioStrategy`.
- [routing/executor.py](routing/executor.py) (17 lines) — Backward-compatibility alias for `FixedRouter` (coupling artifact).
- [routing/health.py](routing/health.py) (90 lines) — Async `/health` polling monitor for local deployments.
- [routing/config.py](routing/config.py) (135 lines) — Pydantic schema + env var expansion for `routing.yaml`.
- [routing/model_router_registry.py](routing/model_router_registry.py) (99 lines) — Per-model router dispatch + canary rollout logic.
- [routing/strategies.py](routing/strategies.py) (48 lines) — Minimal: only `FixedRatioStrategy`.

**Advanced layer** (`routing/routewise/`):

- [routing/routewise/router.py](routing/routewise/router.py) (1188 lines) — The problem child: `RouteWiseRouter` extends `BaseRouter` with primal-dual LP-based selection, quota/concurrency/API tier classification, hedging, latency profiling.
- [routing/routewise/hedging.py](routing/routewise/hedging.py) (536 lines) — `SMART_ECONOMIC` hedge threshold and `HedgedAdapter` (races primary vs delayed backup).
- [routing/routewise/latency.py](routing/routewise/latency.py) (257 lines) — `ProviderProfile` stores EWMA latency samples, CDFs for LP constraints.
- [routing/routewise/config.py](routing/routewise/config.py) (218 lines), [routing/routewise/lp_solver.py](routing/routewise/lp_solver.py) (201 lines), [routing/routewise/predictor.py](routing/routewise/predictor.py) (197 lines), [routing/routewise/concurrency.py](routing/routewise/concurrency.py) (102 lines), [routing/routewise/quota.py](routing/routewise/quota.py) (93 lines).

#### Routing Decision Flow

Request entry ([serving/servers/routers/completions.py:359-363](serving/servers/routers/completions.py#L359)):

1. Dependency injection provides `router_exec` (FixedRouter singleton) and `model_router_registry`.
2. Registry dispatch: `active_router = model_router_registry.get_router(model)`.
3. Router invokes `stream_chat_completion()` or `chat_completion()`.

Inside routers ([routing/routers.py:384-513](routing/routers.py#L384), `routewise/router.py:_select_adapter`):

- **`FixedRouter`**: Weighted random selection with circuit breaker gating ([routing/routers.py:583-636](routing/routers.py#L583)).
- **`RouteWiseRouter`**: Classifies adapters into S_C, S_Q, S_A, then primal-dual threshold logic.

Fallback chain ([routing/routers.py:405-447](routing/routers.py#L405)):

- Primary fails → iterate remaining adapters (weight > 0) in route order.
- Pin mode: no fallback.
- Once streaming chunks flow: no fallback.

#### Strategies & Policies

**Configuration sources**:

- [config/models.yaml](config/models.yaml): Routes as adapter lists with weights; `subscription_type` field per adapter.
- [config/routing.yaml](config/routing.yaml): `routing_strategy` ("fixed" only), `routing_parameter.local_fraction`, health interval, timeout.
- Env: `CIRCUIT_FAILURE_THRESHOLD`, `CIRCUIT_COOLDOWN_SECONDS`, `CIRCUIT_MIN_AVAILABILITY`, `ROUTER_HEALTH_EWMA_ALPHA`.

**Critical gap**: No built-in per-request-attribute routing (e.g., user tier → local vs remote). Tier-based selection lives in [serving/servers/routers/completions.py](serving/servers/routers/completions.py).

#### State Management

- **Per-endpoint health** ([routing/routers.py:146-170](routing/routers.py#L146)): `_ProviderHealth` (EWMA), `_CircuitBreaker` (3-state FSM).
- **RouteWise-specific state** ([routing/routewise/router.py:101-150](routing/routewise/router.py#L101)): adapter classification, latency profiles, quota mgr, concurrency mgr, predictor, `_pending_decisions`, LP solver cache.
- **No persistence**: All state in-memory, lost on restart. Quota shadows reset daily.

#### Coupling with `serving/`

- **FastAPI gateway dependency** ([serving/servers/routers/completions.py](serving/servers/routers/completions.py)): Router via `Depends(get_router)` → `AppServices.fixed_router` (singleton).
- **Adapter interface**: Routers work with abstract `BaseAdapter` instances populated by registry.
- **Metrics coupling** ([routing/routers.py:24-34](routing/routers.py#L24)): Hard imports from `serving.observability.metrics`.
- **Request context** ([routing/routers.py:341](routing/routers.py#L341), [routing/routers.py:366](routing/routers.py#L366)): Uses `serving.utils.context.push()`.

**Decoupling feasibility**: Routing core could run as a separate service if adapters were remotely called. Current tight coupling via in-process `BaseAdapter` prevents this.

#### Failure Handling

- **Circuit breaking** ([routing/routers.py:178-257](routing/routers.py#L178)).
- **Fallback** ([routing/routers.py:405-447](routing/routers.py#L405), [routing/routers.py:478-505](routing/routers.py#L478)).

**Cascading failure risks**:

1. No circuit state sharing across routes.
2. RouteWise slot leak risk if adapter crashes mid-stream.
3. Health monitor only local.
4. No rate limit backoff (429 not respected).

**Hedging** ([routing/routewise/hedging.py](routing/routewise/hedging.py)): `RouteWiseRouter` races primary vs delayed backup. Risk: if both fail, second error shadows first.

#### Top Architectural Concerns

1. **`RouteWiseRouter` complexity & observability burden** ([routing/routewise/router.py](routing/routewise/router.py), 1188 lines; `routewise/*` ~2000 lines).
   - Pending decisions dict keyed by `request_id` couples request lifecycle to router; no cleanup guard.
   - Shadow hedging decision log grows unbounded.
   - Risk: Hard to debug user complaints; observation recording only post-request; dropped requests lose telemetry.
2. **State machine consistency: circuit breaker + EWMA health**.
   - EWMA can lag (alpha=0.2); availability threshold may not reflect recent degradation until N requests accumulate.
   - Risk: Slow detection of cascading failures.
3. **Coupling of three metrics systems**.
   - Circuit state, EWMA health, fallbacks scattered.
   - `FixedRouter` does not emit selection metric.
   - Risk: Inconsistent metric coverage; canary gating hides traffic.
4. **Configuration expressiveness gap**.
   - `routing.yaml` only supports "fixed" with `local_fraction`.
   - `RouteWiseRouter` config in `models.yaml` under per-adapter `subscription_type` plus separate env vars.
   - No way to switch strategies per model via config alone.
5. **Request-scoped state lifecycle**.
   - `_pending_decisions[request_id]` relies on caller to invoke `record_observation()`.
   - No callback guard; if observation never recorded, dict entry leaks.
   - Risk: Memory leak under high throughput.

#### Large Files

- [routing/routewise/router.py](routing/routewise/router.py) (1188 lines): Combines 3-tier classification, primal-dual, LP, hedging, quota/concurrency.
- [routing/routers.py](routing/routers.py) (830 lines): `BaseRouter` + `FixedRouter` + health + circuit breaker + metrics.

### frontend/ (Next.js dashboard + admin)

#### Tech Stack

- Next.js 15.5 (React 18) with TypeScript 5.3
- React Context API + React Query (TanStack Query 5.17)
- Tailwind CSS 3.4 + PostCSS
- React Hook Form 7.49 + Zod 3.22
- Next.js App Router (file-based)

#### Top-Level Directory Structure

`frontend/src/`:

- `app/` — Next.js app router pages (layout, page, login, signup, dashboard with admin/playground/settings)
- `components/` — providers, features, ui, landing
- `lib/` — `api/`, `hooks/`, `schemas/`, `utils/`
- `config/`, `styles/`

#### API Layer

**Centralized HTTP client**: [frontend/src/lib/api/client.ts](frontend/src/lib/api/client.ts) (173 lines)

- Single point for auth token management (sessionStorage-based JWT).
- Token refresh logic with race-condition protection ([frontend/src/lib/api/client.ts:24-51](frontend/src/lib/api/client.ts#L24)).
- Error parsing and standardization via `jsonOrThrow<T>()` ([frontend/src/lib/api/client.ts:85-173](frontend/src/lib/api/client.ts#L85)).

**API modules by domain**:

- [frontend/src/lib/api/auth.ts](frontend/src/lib/api/auth.ts) — login, logout, password reset
- [frontend/src/lib/api/user.ts](frontend/src/lib/api/user.ts) — user profile, API keys, usage stats, recent requests (200 lines)
- [frontend/src/lib/api/admin.ts](frontend/src/lib/api/admin.ts) — user management, auditing, broadcasts, analytics (768 lines)

**Coupling observation**: Playground page (820 lines) directly calls `fetchWithAuth()` for streaming chat ([frontend/src/app/dashboard/playground/page.tsx:271](frontend/src/app/dashboard/playground/page.tsx#L271)), bypassing the API layer.

**Auth flow**: JWT in sessionStorage, token refresh on 401 via `/auth/refresh` with `credentials: 'include'` (cookies); `AuthProvider` (103 lines) manages session state.

#### State Management

**Hybrid React Context + React Query**:

- Auth state: Context-only ([frontend/src/components/providers/AuthProvider.tsx:42-95](frontend/src/components/providers/AuthProvider.tsx#L42)), no Query cache.
- Data fetching: React Query with custom hooks (`useModels`, `useApiKey`, `useRecentRequests`, `useUsage`).
- Local UI state: `useState` scattered (158 grep matches).

**Problem areas**:

- **Admin page (3343 lines)**: 32 individual `useState` calls for modal/form/sort/filter/pagination.
- **Playground page (820 lines)**: `useState` for session UI + `useRef` for streaming buffer management.
- **`SettingsTab` (283 lines)**: Toast state managed manually with `setTimeout` cleanup. Not using `ToastProvider`.

#### Auth Flow

1. JWT access token in sessionStorage.
2. Refresh token in HTTP-only cookie (`credentials: 'include'`).
3. App mount: `AuthProvider` calls `getMe()` to validate session.
4. On 401: auto-refresh via `/auth/refresh`, retry request.
5. Role-based access via `hasRole()` helper.

**Boundary**: `ProtectedRoute` component wraps dashboard pages.

**Observations**:

- Refresh token in HTTP-only cookie — secure.
- Access token in sessionStorage — vulnerable to XSS but expected for SPA.
- No token expiry handling in UI (assumes auto-refresh handles it).
- `refreshUser()` can race if called multiple times during init.

#### Admin vs User-Facing Split

- Role check via `is_admin` boolean in User object.
- `hasRole()` utility for role rank: `free < pro < internal < admin`.
- Admin routes under `/dashboard/admin/`.
- Admin API methods in [frontend/src/lib/api/admin.ts](frontend/src/lib/api/admin.ts).

**Files**:

- User dashboard: [frontend/src/app/dashboard/page.tsx](frontend/src/app/dashboard/page.tsx) (183 lines, clean)
- Admin panel: [frontend/src/app/dashboard/admin/page.tsx](frontend/src/app/dashboard/admin/page.tsx) (3343 lines, monolithic)
- Admin sub-components: `AnalyticsTab`, `ProviderPerformanceTab`, `SettingsTab` (271, 402, 283 lines, extracted but tightly coupled)

#### Files Over 500 Lines

| File | Lines | Issue |
|------|-------|-------|
| [frontend/src/app/dashboard/admin/page.tsx](frontend/src/app/dashboard/admin/page.tsx) | 3343 | Monolithic admin with 32 useState, 6+ tabs |
| [frontend/src/app/dashboard/playground/page.tsx](frontend/src/app/dashboard/playground/page.tsx) | 820 | Chat UI with streaming |
| [frontend/src/lib/api/admin.ts](frontend/src/lib/api/admin.ts) | 768 | Large but appropriate (interface defs) |

#### Top Architectural Concerns

1. **Admin page monolith** (3343 lines).
2. **No state machine for complex workflows** (playground + admin).
3. **Auth state not cached by React Query**.
4. **Playground streaming bypasses API layer** ([frontend/src/app/dashboard/playground/page.tsx:271](frontend/src/app/dashboard/playground/page.tsx#L271)).
5. **Error handling complexity in API client** (`jsonOrThrow` uses pattern matching on error messages, 18+ conditional checks at [frontend/src/lib/api/client.ts:110-162](frontend/src/lib/api/client.ts#L110)).

**Testing**: Only 4 test files found; no component test infrastructure.

### client tooling (client/, freeinference-harness/, llm-prober/)

#### What Each Tool Does

**`client/`** (6 Python files, ~23KB):

- Load testing and benchmark framework.
- Internal developers, local/internal server.
- `HybridClient` orchestrates via `DataLoader` or `RequestGenerator`.
- `BenchmarkRunner` controls request rate, concurrency, duration.
- `MetricsCollector` aggregates latency, throughput, error rate, cost.
- `BurstGPTLoader`, `SplitwiseLoader` replay academic trace workloads.

**`freeinference-harness/`** (11 Python files + YAML configs, ~81KB):

- Black-box API regression testing.
- QA/CI validating FreeInference gateway stability.
- Intentionally standalone — does not import serving code ([freeinference-harness/PLAN.md:20](freeinference-harness/PLAN.md#L20)).
- Exercises HTTP API directly via OpenAI-compatible client and Anthropic SDK.
- 7 scenario types.
- Capability filtering per target.
- Detailed failure classification.

**`llm-prober/`**: empty directory (placeholder).

#### Overlap / Duplication

- Both collect request latency: [client/metrics.py:153-162](client/metrics.py#L153) and [freeinference-harness/runner.py:191](freeinference-harness/runner.py#L191) independently implement percentile calculation.
- Both define request structures: `client/` imports `serving.base.LLMRequest` while harness uses OpenAI dict format.
- Both support batched/concurrent execution, but harness is single-threaded per target.

#### Coupling with Server

**`client/` — tightly coupled**:

- Imports `serving.base.LLMRequest`, `LLMResponse` at module level ([client/client.py:3](client/client.py#L3), [client/runner.py:7](client/runner.py#L7), [client/loader.py:8](client/loader.py#L8), [client/base.py:6](client/base.py#L6)).
- `HybridClient.send_request()` is a stub — `NotImplementedError`.
- Cannot be deployed independently.

**`freeinference-harness/` — intentionally decoupled**:

- Zero imports from `serving/` or `routing/`.
- `httpx` + YAML config; black box.
- Explicit design principle ([freeinference-harness/README.md:6](freeinference-harness/README.md#L6), [freeinference-harness/PLAN.md:20](freeinference-harness/PLAN.md#L20)).
- Can be deployed as standalone package.

#### Configuration Approach

- **`client/`** — programmatic (Python code or `DataLoader` subclass).
- **`freeinference-harness/`** — declarative YAML (targets, scenarios, CLI-driven).

#### Top Architectural Concerns

1. **Incompatible request models**: `client/` uses `serving.base.LLMRequest`; harness uses OpenAI dict.
2. **Redundant, disjoint metrics**: latency percentile calc duplicated; divergent telemetry.
3. **Stub implementation in production path**: `HybridClient.send_request()` raises `NotImplementedError` ([client/client.py:22-27](client/client.py#L22)).
4. **Semantic separation unenforceable**: harness black-box principle has no automated enforcement.
5. **No shared CLI / orchestration**: two independent entry points; load + regression tests can't be combined.

### infrastructure / config / scripts (deployment + ops)

#### Deployment Topology

- **Production** (`freeinference.org`): systemd units on bare metal + Docker Compose with Nginx.
- **Staging** (`staging.freeinference.org`): Docker Compose stack via GitHub Actions SSH trigger.
- **systemd**: `hybrid_inference.service` (uvicorn:8080), `alertmanager.service`, `alert-logger.service`.
- **Docker Compose** ([deploy/docker/docker-compose.yml:16](deploy/docker/docker-compose.yml#L16)): postgres:16, backend, frontend, alertmanager, alert-logger.

#### Configuration Model

- `.env` file (not version-controlled).
- [config/models.yaml](config/models.yaml) — model registry with provider URLs, API keys via `${ENV_VAR}`, per-model pricing, weighted fallback.
- [config/routing.yaml](config/routing.yaml), [config/routewise.yaml](config/routewise.yaml).
- DB backend: PostgreSQL.
- Schema: initialized by PostgreSQL store code; Alembic migration coverage is a separate gap.
- Init in application code on startup.

#### Observability Stack — Sparse

- Alertmanager 0.31.1 receives alerts from disabled Prometheus ([deploy/prometheus/prometheus.yml:33-40](deploy/prometheus/prometheus.yml#L33)).
- `alert-logger` appends JSONL to `var/log/alert_history.jsonl`.
- Only 3 alerts routed ([deploy/alertmanager/alertmanager.yml:12-14](deploy/alertmanager/alertmanager.yml#L12)): `"ServiceDown|ServiceUnreachable|DatabaseDisconnected"`; rest blackholed.
- Nginx logs at `/var/log/nginx/{freeinference,staging}.{access,error}.log`.
- App logs JSON to `server.log` or stderr; no centralized aggregation.
- No Grafana dashboards, no metrics exporter, no APM.

#### Scripts/ Concerns

- `deploy_production.sh`, `deploy_staging.sh` — bash wrappers around `git reset` + `docker compose build`.
- [scripts/db/backup.sh](scripts/db/backup.sh) — daily 04:00 UTC, S3 with GFS rotation. No restore verification, no dry-run.
- Auth import scripts (`import_claude_auth.py`, `import_codex_auth.py`) — manual CLI on deploy.
- **Missing**: unified deployment CLI, runbook automation, canary/blue-green.

#### CI/CD (`.github/workflows/`)

- **PR**: lint, frontend checks, Docker build (cache only), tests without coverage.
- **Push to `main`/`dev`**: same lint + tests + codecov + security scan (gitleaks, pip-audit).
- **`deploy-staging.yml`**: triggers on dev-branch CI success; SSH to `/srv/hybridInference`, `git reset`, `make build`, health check.
- **`deploy.yml`**: same for `main` → production.
- **No pre-staging approval gates, no rollback automation**.
- Secrets in GitHub environment/org secrets, passed to SSH runner.

#### Dev Workflow

- Makefile-driven (`setup-dev`, `test`, `lint`, `check`, `all`).
- `uv` for dependency management; `ruff` for format/lint; `pytest` with markers.
- Pre-commit hooks (gitleaks, ruff, pydocstyle, eslint, prettier); backend excluded from ruff hooks.
- No database migrations; schema lives in SQL files in repo.

#### Top Architectural / Operational Concerns

1. **Monitoring gap**: Prometheus metrics endpoint disabled ([deploy/prometheus/prometheus.yml:33-40](deploy/prometheus/prometheus.yml#L33)), alerting local-only (blackhole default, 3 hard-coded alerts), no dashboards or external visibility. Production incidents invisible until human reports.
2. **Schema migration hazard**: Static SQL schema with no version control or rollback. Deployment scripts do not run migrations. Schema mismatch between code and DB → silent failures.
3. **Secrets in `.env` on disk**: production `.env` contains API keys, DB passwords, JWT secrets, Slack webhooks. No encryption at rest. Shared across multiple services.
4. **SSH-based deployment without approvals**: GitHub Actions SSH into production with `git reset --hard` on any main-branch CI pass. No human gate, no rollback automation.
5. **Backup & recovery untested**: [scripts/db/backup.sh](scripts/db/backup.sh) runs daily to S3 with GFS rotation, but no restore dry-runs or verification. RTO/RPO not documented.

## Cross-Cutting Themes

### 1. Observability gutted

**Subsystems**: serving, routing, infrastructure.

- [serving/observability/metrics.py](serving/observability/metrics.py) is no-op shims; ~40 metric symbols stubbed for backward compat; labels still emitted but discarded.
- Prometheus scrape disabled at [deploy/prometheus/prometheus.yml:33-40](deploy/prometheus/prometheus.yml#L33).
- Only 3 alerts route through Alertmanager ([deploy/alertmanager/alertmanager.yml:12-14](deploy/alertmanager/alertmanager.yml#L12)); rest blackholed.
- No distributed tracing (no OpenTelemetry, W3C trace context, span breakdown).
- No Grafana, no APM, no centralized log aggregation; app logs to `server.log` or stderr.
- Routing-layer telemetry inconsistent: `FixedRouter` doesn't emit selection metric; circuit/EWMA/fallback metrics scattered ([routing/routers.py:24-34](routing/routers.py#L24)).
- RouteWise dropped requests lose telemetry (observation only recorded post-request).
- Frontend has no error tracking integration.

### 2. God-objects in the request path

**Subsystems**: serving, routing, frontend, storage.

- [serving/servers/routers/completions.py](serving/servers/routers/completions.py) (1030 lines): auth, role gating, streaming, error, cost, logging, routing observation.
- [routing/routewise/router.py](routing/routewise/router.py) (1188 lines): 3-tier classification, primal-dual, LP, hedging, quota, concurrency.
- [serving/storage/postgres_operational.py](serving/storage/postgres_operational.py) (1499 lines) and [serving/storage/d1_operational.py](serving/storage/d1_operational.py) (1186 lines): all user/key/session CRUD in single files.
- [frontend/src/app/dashboard/admin/page.tsx](frontend/src/app/dashboard/admin/page.tsx) (3343 lines): 32 useState calls, 6+ tabs.
- [serving/adapters/openai_compat.py](serving/adapters/openai_compat.py) (782 lines): absorbing every provider quirk.

### 3. Fire-and-forget side effects

**Subsystems**: serving, routing, storage.

- Cost increment + request logging run async without await ([serving/servers/routers/completions.py:46-95](serving/servers/routers/completions.py#L46)). User disconnect mid-stream → cost may not be tracked.
- `RouteWiseRouter._pending_decisions[request_id]` relies on caller to invoke `record_observation()`; no callback guard, leak under throughput.
- RouteWise shadow hedging decision log grows unbounded.

### 4. Schema + deployment safety gap

**Subsystems**: storage, infrastructure.

- No migration framework; schema initialization still runs in application startup paths.
- No foreign keys between operational and log stores.
- SSH-deploy to prod via `git reset --hard` on any green main CI; no human gate, no rollback automation.
- Secrets in plaintext `.env` files on disk (API keys, DB passwords, JWT, Slack).
- Backups run nightly to S3 but never restore-tested; RTO/RPO undocumented.

### 5. Tooling fragmentation

**Subsystems**: client tooling.

- `client/` is tightly coupled to `serving/` via module-level imports ([client/client.py:3](client/client.py#L3) etc.) but `HybridClient.send_request()` is a `NotImplementedError` stub ([client/client.py:22-27](client/client.py#L22)).
- `freeinference-harness/` is policy-decoupled (black-box) but the rule has no automated enforcement.
- `llm-prober/` is an empty placeholder directory.
- Two disjoint percentile implementations ([client/metrics.py:153-162](client/metrics.py#L153), [freeinference-harness/runner.py:191](freeinference-harness/runner.py#L191)).
- Incompatible request models (`serving.base.LLMRequest` vs OpenAI dict).
- No shared CLI / orchestration between load testing and regression testing.

## Recommendations (Prioritized)

1. **Restore baseline metrics + alerting end-to-end.** Re-enable Prometheus scrape, wire real counters/histograms in [serving/observability/metrics.py](serving/observability/metrics.py), expand Alertmanager routes beyond the 3 hard-coded names, and add at least one external uptime/probe check. This is the highest leverage move because every other concern (silent shadow drift, fire-and-forget cost, RouteWise opacity, deploy regressions) is currently undetectable.
2. **Decompose `completions.py` and `routewise/router.py`.** Pull cost, logging, routing-observation, and role-gating into composable middleware/services; split RouteWise classification, LP, hedging, and quota into independently testable modules. The blast radius of either file is the entire request path; shrinking them unlocks unit-testability and reduces incident MTTR.
3. **Adopt a real DB migration framework and wire it into deploy.** Replace static [serving/storage/d1_schema.sql](serving/storage/d1_schema.sql) with versioned migrations (Alembic or equivalent), gate deploy on migration apply, and add a nightly restore-test of [scripts/db/backup.sh](scripts/db/backup.sh). Schema drift between code and DB is a silent-corruption class of bug; backups that have never been restored aren't backups.
4. **Add a deployment safety gate and rollback path.** Require human approval (or canary + auto-rollback) between green CI and SSH `git reset --hard` to prod; move secrets out of plaintext `.env` to a managed store. Today any `main` merge is a one-shot to production with no undo.
5. **Make shadow writes and pending-decision state observable, not silent.** Surface [serving/storage/dual_write.py:61-76](serving/storage/dual_write.py#L61) shadow failures as metrics + alerts, add a TTL/cleanup sweep for `_pending_decisions` in [routing/routewise/router.py](routing/routewise/router.py), and bound the hedging decision log. These are unbounded-leak / silent-divergence bugs waiting to happen at scale.
6. **Break up the admin page and unify frontend state.** Split [frontend/src/app/dashboard/admin/page.tsx](frontend/src/app/dashboard/admin/page.tsx) by tab into route segments, push auth state into React Query, and route the playground stream through the API layer instead of bypassing it ([frontend/src/app/dashboard/playground/page.tsx:271](frontend/src/app/dashboard/playground/page.tsx#L271)). 3343 lines + 32 useState is the frontend equivalent of `completions.py`.
7. **Consolidate client tooling around the harness contract.** Either delete `client/` and `llm-prober/` or rebuild `client/` on top of the harness's HTTP-only black-box pattern; share a single percentile/metrics implementation; add a lint rule enforcing harness's no-`serving`-imports rule. The current state has a `NotImplementedError` in a "production" load tester, an empty directory, and duplicated metrics — net negative.
