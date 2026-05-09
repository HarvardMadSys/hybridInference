---
name: debug
description: Diagnose and fix bugs in the hybridInference codebase using a structured reproduce-investigate-fix-verify workflow. Use when the user reports a bug, a test fails unexpectedly, or CI is red due to a code error (not infrastructure).
---

# Debug

Diagnose and fix bugs in hybridInference using a structured workflow: reproduce → investigate → fix → verify.

## Scope

- **What:** Runtime errors, incorrect routing, adapter failures, auth issues, test failures, CI failures caused by code bugs (not flaky infra).
- **Where:** Work directly in the current branch. Do **not** create a new branch or worktree unless the user asks.
- **Goal:** Minimal fix that resolves the issue without changing unrelated code.

## Repo layout (reference)

```
apps/backend/serving/     # FastAPI app — HTTP layer, SSE streaming, middleware
  adapters/                # LLM provider adapters (openai_compat, claude, gemini, openrouter, etc.)
  auth/                    # Signup policy, JWT/API-key auth
  config/                  # Settings (pydantic-settings), runtime settings
  observability/           # Metrics, alerts, logging
  servers/                 # App bootstrap, routers, middleware, SSE
  storage/                 # Database layer (postgres / D1)
apps/backend/routing/      # Routing engine — strategies, circuit breaker, EWMA health, provider registry
  routewise/               # Per-model routing config
apps/frontend/             # Next.js frontend
config/                    # YAML configs: models.yaml, routing.yaml, alerts.yaml, routewise.yaml
services/                  # llm-prober, freeinference-harness, alert-logger
tests/
  unit/                    # Pure unit tests (mocked adapters, routing logic, auth, config)
  api/                     # API-level tests per provider (test_openai_api, test_claude_api, etc.)
  integration/             # Tests hitting real DB or external services
  e2e/                     # End-to-end Makefile-driven tests (direct local, gateway routing, hybrid)
  external/                # Live server tests
```

## Step 1 — Reproduce

Reproduce the bug before doing anything else. **If you cannot reproduce, say so and stop — do not guess at fixes.**

### Test failure

Run the specific failing test:

```bash
uv run pytest -vv tests/path/to/test_file.py::test_function_name
```

Or the full suite:

```bash
make test-verbose
```

For database-dependent tests:

```bash
uv run pytest -vv -m dbtest
```

### Runtime / API error on staging

Verify the bug on staging (`https://staging.freeinference.org`) with the test account `admin@admin.com:admin`:

```bash
curl -s https://staging.freeinference.org/v1/chat/completions \
  -H "Authorization: Bearer <api-key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"<model>","messages":[{"role":"user","content":"hello"}]}'
```

### Docker service error

```bash
make ps                              # check running services
make logs s=backend                  # tail backend logs
make logs s=postgres                 # tail DB logs
```

### CI failure

```bash
gh run view <run-id> --log-failed
```

## Step 2 — Investigate

### 2a — Read the error carefully

Extract:

- **Exception type and message** — the actual error, not the traceback noise.
- **File and line number** — where the error originated.
- **Stack trace** — the call chain that led to the error.
- **Input / state** — what data / model / provider was being processed.

### 2b — Map the error to the codebase

Use this decision tree to find the right area:

| Error context | Look in |
|---|---|
| LLM provider returned unexpected response / timeout | `serving/adapters/` — find the adapter file for the provider (e.g. `claude.py`, `gemini.py`, `openrouter.py`) |
| Wrong provider selected / "all circuits open" | `routing/routers.py`, `routing/strategies.py`, `routing/health.py` |
| Model not found / routing config issue | `config/models.yaml`, `config/routing.yaml`, `config/routewise.yaml`, `routing/model_router_registry.py` |
| Auth failure (401/403, bad API key, JWT error) | `serving/auth/`, `serving/servers/auth.py`, `serving/config/settings.py` |
| SSE streaming interrupted / malformed chunks | `serving/stream.py`, `serving/servers/sse.py` |
| Database error (connection, query, migration) | `serving/storage/`, check `DB_BACKEND` env var (`postgres` vs `d1`) |
| Config / env var not picked up | `serving/config/settings.py` — check `get_settings()` cache (tests auto-clear via `conftest._reset_settings_cache`) |
| Middleware / CORS / rate-limit | `serving/servers/middleware/` |
| Alert not firing / wrong threshold | `config/alerts.yaml`, `serving/observability/alerts.py` |
| Frontend rendering / API mismatch | `apps/frontend/src/`, check browser console + network tab |

### 2c — Trace the code path

Starting from the error location, read backwards through the call chain:

1. Read the failing function — understand what it expects and what it got.
2. Read the caller — how was the input constructed?
3. Check data transformations — where could the data have diverged?

For routing bugs specifically, trace:
```
HTTP request → serving/servers/routers/ → routing/routers.py (select provider) → adapter (forward to provider) → response handling
```

### 2d — Check recent changes

```bash
git log --oneline -20 -- <failing-file>
git diff HEAD~5 -- <failing-file>
```

Also check if config files changed:

```bash
git diff HEAD~5 -- config/
```

### 2e — Check environment and config

- Compare `.env.example` with `.env` — are required vars set?
- Check the active config: `config/models.yaml` (model definitions), `config/routing.yaml` (routing strategy), `config/routewise.yaml` (per-model overrides).
- Settings are `lru_cache`-d — if a test changes env vars, ensure `get_settings.cache_clear()` is called (the top-level `conftest.py` does this automatically).

### 2f — Form a hypothesis

Write down a clear hypothesis before fixing:

> "The bug occurs because the Claude adapter in `serving/adapters/claude.py` doesn't handle empty `content` blocks when the model returns a `stop_reason` of `max_tokens`. This causes a `KeyError` when building the SSE chunk."

If you can't form a confident hypothesis, report what you found and ask the user for guidance.

## Step 3 — Fix

### 3a — Write the minimal fix

- Change **only** what is necessary to resolve the bug.
- Do not refactor, reformat, or "improve" surrounding code.
- Preserve existing behavior for all non-buggy code paths.

### 3b — Common fix patterns in this repo

| Root cause | Typical fix location |
|---|---|
| Provider returns unexpected response shape | Adapter in `serving/adapters/<provider>.py` — add guard in response parsing |
| `NoneType` / `AttributeError` in routing | `routing/routers.py` — null check on provider health or route config |
| Circuit breaker opens too aggressively | `routing/routers.py` or `config/routing.yaml` — tune `CIRCUIT_*` env vars or threshold logic |
| Auth token validation fails | `serving/servers/auth.py` or `serving/config/settings.py` — check key lookup / JWT decode |
| SSE stream breaks mid-response | `serving/stream.py` or `serving/servers/sse.py` — handle partial chunks / connection drops |
| Database query error | `serving/storage/` — check SQL, schema, or async connection handling |
| Config not reloading | `serving/config/settings.py` or `serving/config/runtime_settings.py` — check cache invalidation |
| Pydantic validation error | `serving/schemas.py` or adapter schemas — check field types / defaults |
| `asyncio` race condition | Add proper lock or serialize access in the affected module |

### 3c — Add a regression test

Place the test in the appropriate directory following existing patterns:

- **Adapter bug** → `tests/unit/adapters/` or `tests/api/test_<provider>_api.py`
- **Routing bug** → `tests/unit/routing/`
- **Auth bug** → `tests/unit/auth/`
- **Config bug** → `tests/unit/config/`
- **DB/storage bug** → `tests/integration/` (uses `dbtest` marker)

```python
def test_<bug_description>():
    # Regression: <short description of the original bug>
    ...
```

The test should be minimal and focused — it only needs to prove the bug is fixed.

Use existing fixtures from `tests/conftest.py` and subdirectory `conftest.py` files. For tests that need env var overrides, use `monkeypatch.setenv`.

## Step 4 — Verify

### 4a — Run the failing test

```bash
uv run pytest -vv tests/path/to/test_file.py::test_function_name
```

### 4b — Run the full test suite

```bash
make test
```

### 4c — Format and lint

```bash
make format
```

### 4d — Check for side effects

- Grep for other callers of the changed function.
- Grep for other uses of the changed data structure.
- If the fix touches an adapter, run that provider's API tests: `uv run pytest tests/api/test_<provider>_api.py`.
- If the fix touches routing, run all routing tests: `uv run pytest tests/unit/routing/`.

## Step 5 — Report

Provide a concise summary:

```
Bug: <one-line description>
Root cause: <why it happened>
Fix: <what you changed, file:line>
Verified: <test results>
```

If the fix is non-trivial or you're unsure about side effects, flag it for the user.

## Guardrails

- **Always reproduce first.** Never fix a bug you haven't seen happen.
- **Minimal changes only.** Do not refactor, reformat, or touch unrelated code.
- **Never skip tests.** If a test fails, fix the code — don't modify the test to pass.
- **Never commit unless asked.** Present the fix and let the user decide.
- **Stop if you can't reproduce.** Don't speculate about fixes for bugs you can't observe.
- **Don't suppress errors.** Fix the root cause, don't add blanket try/except or pass statements.
- **One bug per invocation.** If multiple bugs are found, fix the reported one and note the others for the user.
- **Test against staging** when applicable (`https://staging.freeinference.org`, account `admin@admin.com:admin`).
