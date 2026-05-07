# Design: AGENTS.md — Agent Onboarding Guide

**Date:** 2026-05-03
**Status:** Draft

## Goal

Create a single tool-agnostic `AGENTS.md` at the repo root that helps any AI coding agent (Claude Code, Cursor, Codex, etc.) get productive in this codebase quickly — for both feature implementation and debugging tasks.

## Non-goals

- Not a replacement for `CLAUDE2.md`. AGENTS.md does not reference, fold in, or duplicate `CLAUDE2.md`.
- Not a tutorial on the project's domain (LLM inference) — assumes the reader already understands LLM gateways.
- Not a duplicate of existing developer docs. Recipes are pointer-style.

## Audience

Any AI coding agent dropped into this repo. The doc must be useful without privileged tooling (no Skill tool assumed, no plugin assumed). Human developers who want to know what their agent is reading are a secondary audience.

## Location

Repo root: `AGENTS.md`. Sits alongside `README.md` and `CLAUDE2.md`.

## Structure

Eight sections, each scaled to its content. Total length: ~300–500 lines.

```
1. About this project          — orientation (1 short paragraph + key links)
2. Repo map                    — annotated tree, what lives where
3. Getting set up              — clone → uv sync → make all
4. Quality gates               — ruff, tests, what CI runs
5. Workflow                    — branch naming, worktrees, PR flow, staging
6. Project-specific knowledge  — architecture, abstractions, configs, tests, gotchas
7. Common tasks                — pointer table to canonical docs/skills
8. Where to look next          — developer docs, internal doc site
```

## Section content

### 1. About this project

3–4 sentences. HybridInference is a FastAPI gateway that routes LLM requests across local servers (vLLM/SGLang/Ollama) and remote OpenAI-compatible providers. Production: freeinference.org. Staging: staging.freeinference.org. Links to README, public docs (https://doc.freeinference.org/), and internal docs (https://internaldoc.freeinference.org/).

### 2. Repo map

Annotated top-level tree. Source the layout from `.kilo/skills/debug/SKILL.md` (already accurate). Entries to include:

- `apps/backend/{serving,routing,benchmark}/`
- `apps/frontend/`
- `config/` (models, routing, routewise, alerts)
- `services/` (llm-prober, freeinference-harness, alert-logger)
- `tests/{unit,api,integration,e2e,external}/`
- `ops/`
- `deploy/`
- `docs/{agents,developer,user,reviews}/`

### 3. Getting set up

- `make setup-dev` is the preferred path.
- Manual: `uv venv -p 3.12 && uv sync`.
- Python: 3.10–3.13 supported, 3.12 recommended.
- Mention `UV_RUN ?= uv run` override pattern from the Makefile.

### 4. Quality gates

- `make format` — runs `ruff format .` then `ruff check --fix .`.
- `make lint` — `ruff check --no-fix` and `pydocstyle`.
- `make test` — runs pytest with `-m "not external and not dbtest"`.
- `make all` — format + lint + test.
- Pre-commit hooks installed by `make setup-dev`.
- Required before opening a PR: `make format` and a passing `make test`.

### 5. Workflow

- Branch off `dev`, never `main`.
- Branch naming: `<user>/<scope>/<feature-name>` (e.g., `jason/claude/add-x`).
- Use a git worktree, not the main checkout.
- PRs target `dev`. Staging deploys from `dev`.
- Verify changes against staging before claiming done.
- Staging test account: `admin@admin.com` / `admin`.

### 6. Project-specific knowledge

The densest section. Six sub-sections.

#### 6.1 Architecture in one diagram

Four-layer mental model: client → FastAPI gateway (`serving/`) → routing engine (`routing/`) → adapters (`serving/adapters/`) → providers. Reference `docs/developer/architecture.md` for the full diagram.

#### 6.2 Key abstractions

One line each:

- **Adapter** — provider-specific client. Lives in `apps/backend/serving/adapters/`. Examples: `openai_compat`, `claude`, `gemini`, `openrouter`, `ollama`, `vllm`, `sglang`.
- **Provider vs upstream** — clarify the distinction (a provider is a logical destination; an upstream is a concrete endpoint within that provider). Verify the exact terminology against the codebase before publishing.
- **Router** — `FixedRouter` in `routing/routers.py` does weighted random selection plus fallback.
- **`routing/executor.py`** — backward-compatibility shim that re-exports `FixedRouter` as `RouteExecutor`. Don't edit it; edit `routing/routers.py` instead.
- **Strategy** — decision layer in `routing/manager.py` and `routing/strategies.py`. Reads `config/routing.yaml` and computes weights.
- **Circuit breaker / EWMA health** — provider health tracking in `routing/`.

#### 6.3 Configuration files

What each owns:

- `config/models.yaml` — model registry (required).
- `config/routing.yaml` — local/remote split, health checks (optional).
- `config/routewise.yaml` — per-model routing overrides.
- `config/alerts.yaml` — alert rules.
- Env var interpolation: `${VAR}` and `${VAR:-default}` work in YAML.

#### 6.4 Tests: markers and tiers

- `tests/unit/` — fast, mocked, default in CI.
- `tests/api/` — per-provider API surface tests.
- `tests/integration/` — needs DB or external services. Marker: `dbtest`.
- `tests/e2e/` — Makefile-driven full-stack runs.
- `tests/external/` — hits live servers. Marker: `external`.
- `make test` excludes `external` and `dbtest` by default. Opt in explicitly when needed.

#### 6.5 Common gotchas

- Don't commit to `main` or `dev` directly — branch + PR.
- Don't edit `routing/executor.py` (compat shim) — edit `routing/routers.py`.
- SSE streaming lives in `apps/backend/serving/servers/` — middleware order matters.
- Storage layer supports both Postgres and Cloudflare D1 — check `serving/storage/` for the active backend before assuming SQL dialect.
- Frontend is Next.js in `apps/frontend/` — separate quality gates from the Python backend.

### 7. Common tasks

Pointer table. Each row links to the canonical doc/skill. No content duplication.

| Task | Where to look |
|---|---|
| Implement a feature | `.kilo/skills/impl-feat/SKILL.md` |
| Debug a bug or test failure | `.kilo/skills/debug/SKILL.md` |
| Address PR review / fix CI | `.kilo/skills/check-pr/SKILL.md` |
| Add a new model | `docs/developer/adding-models.md` |
| Add a local model (vLLM/SGLang/Ollama) | `docs/developer/add-local-model.md` |
| Touch routing logic | `docs/developer/routing.md` |
| Touch configuration | `docs/developer/configuration.md` |
| Database / storage changes | `docs/developer/database.md` |
| Deployment / staging / prod | `docs/developer/deployment.md`, `docs/developer/staging.md` |
| OpenRouter / FreeInference internals | `docs/developer/openrouter.md`, `docs/developer/freeinference.md` |
| Past designs / specs | `docs/agents/specs/` |
| Past plans | `docs/agents/plans/` |

### 8. Where to look next

- Public docs: https://doc.freeinference.org/
- Internal/developer docs: https://internaldoc.freeinference.org/
- Full developer guide index: `docs/developer/index.rst`
- Code review records: `docs/reviews/`

## Out of scope

- Sphinx integration. AGENTS.md is plain Markdown at the repo root and is not built into the doc site.
- Renaming `CLAUDE2.md`. Keep it as-is.
- Updating any of the linked docs. AGENTS.md is purely additive.

## Acceptance criteria

- File exists at repo root: `AGENTS.md`.
- All eight sections present.
- All linked paths in section 7 resolve to existing files at the time of writing.
- Section 6.2 terminology ("provider vs upstream") matches codebase usage — verify before publishing.
- Doc reads end-to-end without referencing `CLAUDE2.md` or assuming any specific agent platform.
- Total length 300–500 lines.

## Risks

- **Drift.** Section 6 mentions specific files (`routing/routers.py`, `routing/executor.py`). If those move or get renamed, AGENTS.md goes stale silently. Mitigation: keep file references minimal in section 6.5 (gotchas) where they matter most, and rely on pointer-style links in section 7 elsewhere.
- **Provider vs upstream terminology.** The doc must use whichever the codebase uses. Implementation step should grep the codebase to confirm before writing 6.2.
- **Test marker accuracy.** The `external` and `dbtest` markers were stated in `.kilo/skills/debug/SKILL.md`; confirm against `pyproject.toml` or `pytest.ini` during implementation.
