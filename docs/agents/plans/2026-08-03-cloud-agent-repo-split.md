# Cloud Agent Repo Split — Fine-Grained Execution Plan

- **Date:** 2026-08-03
- **Owner:** Murphy (decisions, reviews, cutover approval)
- **Executors:** Glen + AI coding agents (each task below is written to be handed to one agent session)
- **Decision source:** Murphy ↔ Juncheng Slack DM, 2026-07-31 — decouple cloud agent into its own service + repo, share login only, separate agent admin dashboard, dynamic host pool, open-source the result. Glen finishes in ~2 months.
- **Source repo:** `HarvardMadSys/hybridInference` (private; history is NOT a publication artifact)
- **Target repo:** `HarvardMadSys/freeinference-cloud-agent` (created 2026-07-31, currently empty, will be open-sourced)
- **FREEZE_SHA:** `764a6f97477504deb4542e1c28c3128ff9601c94` (`dev`, 2026-08-03) — declared, A1/A2 complete
- **Inventory basis:** the freeze commit. File-level authority is [cloud-agent-split-manifest.md](cloud-agent-split-manifest.md).

---

## 0. Context block — prepend to EVERY task prompt

> **System overview.** HybridInference is a FastAPI LLM gateway (`apps/backend/serving/`) with a Next.js frontend (`apps/frontend/`). The Cloud Agent feature (issue #1041) lets users run coding agents (Claude Code, Codex, Kilo, …) in sandboxed containers against their GitHub/GitLab repos, with results published as PRs. It is currently embedded in the gateway monorepo. We are extracting it into `HarvardMadSys/freeinference-cloud-agent` as an independent service that depends on HybridInference only through two HTTP contracts: **identity** (login/SSO) and **inference grants** (model-call tokens + budget).
>
> **Current coupling points (verified 2026-08-03):**
> - `apps/backend/serving/servers/auth.py:15` imports `serving.agent_jobs.model_auth` (gateway → agent reverse dependency).
> - `apps/backend/serving/agent_jobs/tokens.py` derives worker/model token keys from the gateway's `API_KEY_SECRET` (line ~45).
> - `apps/backend/serving/agent_jobs/model_auth.py` enforces per-job budget from the billing ledger (`api_logs`, keyed by `agent_job_id`), fail-closed, and checks attempt fencing via `AgentJobStore`.
> - `apps/backend/serving/servers/bootstrap.py` (~line 783+) conditionally wires `AgentJobStore`, the attempt reaper, and the publish loop.
> - 9 Postgres tables created at startup by `apps/backend/serving/storage/agent_job_store.py`: `agent_threads`, `agent_jobs`, `agent_attempts`, `agent_job_events`, `agent_thread_messages`, `agent_job_artifacts`, `agent_repo_grants`, `agent_oauth_states`, `agent_gitlab_connections`.
> - Workspace broker is addressed by a single global `AGENT_WORKSPACE_BROKER_URL` (`workspace_broker_client.py:195`).
> - `servers/routers/agent_mcp.py` proxies MCP for the sandbox and authenticates with `model_auth.authenticate_agent_tool_call` — the **same** capability token and `AgentJobStore` fence as model calls, minus the budget check. It stays in the gateway; the grant work in Phase C must cover it.
> - Gateway JWTs have no `iss`/`aud`; the frontend keeps the access token in `sessionStorage` (no cookie session).
> - `#1158` (merged) added admin host switching: `apps/backend/serving/servers/routers/admin/agent_runner_hosts.py`. `#1170` (SSH relay so a runner host without a local gateway can reach the gateway; relay-not-proxy design) is the transport substrate for multi-host — do not reinvent it.
>
> **Global rules (non-negotiable):**
> 1. **Move-as-is.** Migration tasks change imports/paths/config ONLY. No renames, no splitting files, no "improvements". Refactors come after cutover.
> 2. **No shared signing secrets across repos.** Grant tokens are minted AND verified by the gateway (secret never leaves it). Control/worker tokens use the new repo's own secret.
> 3. **Contracts before parity.** Phase C lands in the old repo before Phase E claims parity.
> 4. **Freeze discipline.** After `FREEZE_SHA` is declared, agent-path changes in the old repo are limited to Phase C files. Repo-wide sweeps (renames, import re-orgs) must exclude agent paths until Phase D/E complete.
> 5. Old repo quality gates: `make format && make test`. New repo: `make check` (defined in B1). Every task ends green.
> 6. Old repo branches: `<user>/<scope>/<name>` off `dev`, PR → `dev`. New repo: same convention once B1 lands.
> 7. Do not commit secrets. The new repo will be public later; treat every commit as public.

**How to use this plan:** one task = one agent session = (usually) one PR. Paste Section 0 + the task block. `<FREEZE_SHA>` is `764a6f97477504deb4542e1c28c3128ff9601c94`. Tasks list explicit dependencies; anything not listed as a dependency can run in parallel. Effort: S ≤ half day, M ≈ 1 day, L ≈ 2–3 days.

---

## Decision register (Murphy resolves; blocking tasks noted)

| ID | Decision | Default / recommendation | Blocks |
|----|----------|--------------------------|--------|
| DR1 | License for new repo | Apache-2.0 | B1 |
| DR2 | GitHub App: reuse org App `4436561` or create a new one | Reuse; add new callback/setup URLs for agent domains. Rotate the key that leaked into Slack while at it | B4, E5 |
| DR3 | New Postgres: same instance new database (`cloud_agent`) vs new instance | Same staging instance, new database; prod decides at H3 | B4, E1 |
| DR4 | Domains | `agents.freeinference.org` / `agents.staging.freeinference.org`; API on same host under `/api` | B4, F6 |
| DR5 | Old job history | Leave in old DB; old UI read-only until H4, then gone. Only `agent_repo_grants` + `agent_gitlab_connections` migrate | H1 |
| DR6 | Identity token signing alg | RS256 (PyJWT + `cryptography`, JWKS-friendly) | C1 |
| DR7 | Plan/entitlement source of truth | Identity JWT carries `role`/`plan` claims; control plane enforces its own limits | C3, E4 |

---

## Phase A — Pre-freeze stabilization (old repo)

### A1. Land or close the 5 open agent PRs — ✅ **done 2026-08-03**
`#1147` egress allowlist proxy, `#1148` MCP via gateway, `#1167` runner-host lock order, `#1168` Kata provisioning, `#1170` remote runner SSH relay — **all merged**. They added 13 files to the manifest, including the egress proxy, the MCP proxy, and the remote-runner relay that Phase G builds on.

### A2. Declare the freeze — ✅ **done 2026-08-03**
- `FREEZE_SHA = 764a6f97477504deb4542e1c28c3128ff9601c94`.
- Add a short section to the old repo `CLAUDE.md`: agent paths (`apps/backend/serving/agent_jobs/`, `servers/routers/agent_jobs.py`, `servers/routers/admin/agent_runner_hosts.py`, `storage/agent_job_store.py`, `schemas_agent_jobs.py`, `apps/frontend/src/{app,components/features}/agents/`, `apps/frontend/src/lib/api/agents.ts`, agent deploy files) are **frozen except Phase C contract work**; new agent features go to the new repo.
- **Acceptance:** CLAUDE.md note merged to `dev`; `FREEZE_SHA` written into this plan and into B3's MIGRATION.md.

### A3. Generate the migration manifest (S)
- Repo: old. Deps: A2.
- At `<FREEZE_SHA>` run and save output to `docs/agents/plans/cloud-agent-split-manifest.txt`:
  ```bash
  git ls-tree -r <FREEZE_SHA> --name-only \
    | grep -Ei '(agent[_-]|/agents?(/|$)|agent_jobs)' \
    | grep -vE '^docs/agents/'
  ```
  Cross-check against the Appendix list; investigate any diff (new files since 3f955493 must be classified move/stay).
- **Acceptance:** manifest committed; every file tagged `move:host`, `move:control`, `move:web`, `move:deploy`, `stay:contract`, or `stay:delete-at-H4`.

---

## Phase B — New repo bootstrap

### B1. Scaffold (M)
- Repo: new (empty). Deps: DR1.
- Layout:
  ```text
  freeinference-cloud-agent/
  ├── backend/cloud_agent/        # control plane package (FastAPI)
  ├── host/cloud_agent_host/      # runner/host package
  ├── web/                        # Next.js app (F1)
  ├── contracts/                  # OpenAPI YAMLs (C-phase mirrors)
  ├── migrations/                 # Alembic
  ├── deploy/{control-plane,host,sandbox}/
  ├── tests/{unit,servers,integration}/
  ├── docs/
  ├── AGENTS.md  README.md  LICENSE  MIGRATION.md  Makefile  pyproject.toml
  ```
- Python 3.12 + `uv`; copy `ruff` config from old repo `pyproject.toml` unchanged. `Makefile` targets: `setup-dev`, `format`, `lint`, `test` (pytest `-m "not dbtest"`), `check` = format+lint+test. Branches: `main` (init commit) then `dev`. `AGENTS.md`: skeleton pointing at this plan.
- **Acceptance:** fresh clone → `make setup-dev && make check` passes (zero tests is OK); `dev` is default branch.

### B2. CI + secret scanning (M)
- Repo: new. Deps: B1.
- GitHub Actions: `backend-ci.yml` (uv, ruff, pytest, Python 3.12; mirror old repo's xdist flags `-n 4 --dist loadfile`), `web-ci.yml` (lint+test+build; activates when `web/` exists — gate on path), `secret-scan.yml` (gitleaks on push + PR). PR template with a "no secrets / move-as-is" checklist.
- **Acceptance:** CI green on a trivial PR; gitleaks catches a planted dummy key in a test PR (then revert).

### B3. MIGRATION.md (S)
- Repo: new. Deps: A3, B1.
- Contents: source repo + `<FREEZE_SHA>`, the A3 manifest table (path → destination), list of source PRs (#1041 lineage: #1123, #1134, #1136, #1145, #1147, #1148, #1149–#1170 as applicable), and the rule "history stays in the private repo; this repo starts from a clean snapshot".
- **Acceptance:** file merged; every manifest row has a destination.

### B4. ENVIRONMENT.md — secrets & infra checklist (M) — **mostly human**
- Repo: new. Deps: DR2, DR3, DR4.
- Enumerate every env var the new stack needs, who provisions it, and where it lives: `AGENT_DATABASE_URL`, `AGENT_CONTROL_TOKEN_SECRET` (new random), `AGENT_SESSION_SECRET` (new), `GATEWAY_BASE_URL`, `GATEWAY_JWKS_URL`, `GATEWAY_GRANT_DISPATCH_TOKEN`, GitHub App id/key/webhook secret, GitLab OAuth id/secret, staging domain + bore tunnel port on spark2, sandbox image registry. Explicitly note: gateway's `API_KEY_SECRET` and `JWT_SECRET_KEY` must NEVER appear in this repo's config.
- **Acceptance:** staging values provisioned (secrets in the deploy host env, not in git); doc merged.

---

## Phase C — Contracts in HybridInference (old repo; only permitted agent-area change during freeze)

> Design note for C4–C6: today, sandbox model tokens are fenced via `AgentJobStore` lookups. After the split the gateway has no job store, so the fence is replaced by a gateway-owned `agent_grants` table + an explicit revoke call from the control plane. Budget enforcement (ledger-based, fail-closed) already exists in `model_auth.py` — reuse it, keyed by the grant's job id.

### C1. Identity keys + JWKS endpoint (S)
- Repo: old. Deps: none (may start before freeze).
- RS256 keypair from env `IDENTITY_JWT_PRIVATE_KEY` (PEM; staging/prod provisioned, tests generate ephemeral). New router `serving/servers/routers/identity.py`: `GET /v1/identity/jwks` → JWKS with `kid`. No behavior change elsewhere.
- **Acceptance:** unit tests: JWKS shape, kid stability, 500-free when key unset (endpoint 404s cleanly).

### C2. One-time authorization code endpoint (M)
- Repo: old. Deps: C1.
- `POST /v1/identity/code` — auth: existing user bearer JWT. Body: `client_id` (must equal `cloud-agent`), `redirect_uri` (must exactly match one of env `IDENTITY_ALLOWED_REDIRECTS`, comma-separated), `code_challenge` (S256), `state` (opaque, ≤512B). Returns `{code}` — random 256-bit, stored server-side (new small table `identity_codes`: code_hash, user_id, challenge, redirect_uri, expires_at 60s, used_at) — single-use.
- **Acceptance:** tests: happy path, wrong client_id 400, unlisted redirect 400, expired/replayed code rejected at C3.

### C3. Token exchange endpoint (M)
- Repo: old. Deps: C2, DR6, DR7.
- `POST /v1/identity/token` — unauthenticated; body `{code, code_verifier, client_id, redirect_uri}`. Verifies S256(verifier)==challenge, single-use, expiry, redirect match. Returns RS256 JWT: `iss=<GATEWAY_BASE_URL>`, `aud=cloud-agent`, `sub=<user_id>`, `email`, `role`, `plan`, `exp=now+10min`, `kid` header. No refresh token (the agent BFF holds its own session).
- **Acceptance:** tests incl. JWKS round-trip verification; code replay → 400; verifier mismatch → 400.

### C4. Frontend authorize page (M)
- Repo: old. Deps: C2.
- `apps/frontend/src/app/authorize/page.tsx`: reads `client_id,redirect_uri,code_challenge,state` from query; if not logged in → existing login flow then back; calls `POST /v1/identity/code` with bearer token; redirects to `redirect_uri?code=...&state=...`. Reject on API error with visible message. Minimal UI ("Continue to Cloud Agent as <email>" + button).
- **Acceptance:** component test for param validation + redirect construction; manual staging check listed in PR description.

### C5. `agent_grants` table + mint/revoke endpoints (L)
- Repo: old. Deps: none technically, but design-review with Murphy before merge (this is the money boundary).
- New table (created in `agent_job_store` startup DDL for now — it stays gateway-side at H4, move DDL to gateway-owned module then): `agent_grants(grant_id ulid pk, user_id, external_job_id, external_attempt_id, allowed_models jsonb, budget_usd numeric not null, expires_at, revoked_at, created_at)`.
- `POST /internal/agent-grants` — auth: `Authorization: Bearer <GATEWAY_GRANT_DISPATCH_TOKEN>` (new env, constant-time compare). Body: user_id, external_job_id, external_attempt_id, allowed_models, **allowed_mcp** (server → tool allowlist), budget_usd, ttl_seconds. Returns `{grant_id, token}`. Token format: reuse `tokens.py` HMAC style, new prefix `agr`, new signing context, still derived from gateway `API_KEY_SECRET` (gateway mints AND verifies; secret never leaves).
- `allowed_mcp` is not optional polish: `agent_mcp.py` authenticates tool calls with the same token, so a grant that only describes models would either lock the sandbox out of MCP or leave tool access ungoverned.
- `POST /internal/agent-grants/{grant_id}/revoke` — same auth; sets `revoked_at`.
- **Acceptance:** unit tests for mint/verify/revoke/expiry; dispatch-token auth (401 on miss); no route reachable without the env set.

### C6. Grant verification path in model_auth (L)
- Repo: old. Deps: C5.
- `model_auth.authenticate_agent_model` accepts BOTH token kinds during transition: legacy `ajt` (unchanged behavior) and new `agr` → verify signature, load grant row, reject revoked/expired/model-not-allowed, then reuse the existing ledger budget check with the grant's `external_job_id` written to `api_logs.agent_job_id` (existing dashboards keep working). Fail-closed on missing budget, as today.
- `model_auth.authenticate_agent_tool_call` gets the same `agr` path **without** the budget check — a tool call buys no inference, which is why that function does not apply it today. Skipping this function would break MCP the moment grants replace `ajt`.
- **Acceptance:** all existing `test_agent_model_auth.py` and `test_agent_mcp.py` tests still pass (legacy path untouched); new tests for the `agr` path on both functions, incl. revoke-then-call → 401, budget exhaustion → 429 on the model path only, and a tool outside `allowed_mcp` refused.

### C7. Grant usage endpoint (S)
- Repo: old. Deps: C5.
- `GET /internal/agent-grants/{grant_id}/usage` (dispatch-token auth) → `{spent_usd, request_count}` summed from the ledger for that grant's job id. Used by the control plane UI later.
- **Acceptance:** unit test with seeded ledger rows.

### C8. contracts/ mirrors (S)
- Repo: new. Deps: C3, C5, C7.
- Write `contracts/identity.openapi.yaml` and `contracts/inference-grants.openapi.yaml` describing exactly what C1–C7 shipped (hand-written, small). These are the reference for E7/E9 and for Juncheng's review.
- **Acceptance:** YAMLs lint (`openapi-spec-validator` in CI).

---

## Phase D — Move the execution core (new repo `host/`)

> Pattern for every D/E move task: copy the listed files from `<FREEZE_SHA>` (`git show <FREEZE_SHA>:<path>`), place at destination, rewrite `from serving.agent_jobs.X` → `from cloud_agent_host.X` (or `cloud_agent.X` per destination), copy the listed tests, adjust test imports, run them. **Diff vs source must be imports/paths only** — reviewer checks with `git diff --stat` against pristine copies.

### D1. Host package skeleton (S)
- Deps: B1. Create `host/cloud_agent_host/__init__.py`, wire pytest paths, add `host` to CI matrix.

### D2. Sandbox + runtimes + egress (M)
- Deps: D1. Move `sandbox.py`, `runtimes.py`, `egress.py`, `egress_proxy.py`; tests `test_agent_sandbox.py`, `test_agent_runtimes.py`, `test_agent_egress.py`, `test_agent_egress_proxy.py`; fixture `tests/fixtures/agent_runtime_streams/claude_code_stream_contract.jsonl`.
- `egress_proxy.py` generates the Squid allowlist config on the runner host and `sandbox.py` imports `CANARY_HOST` from it, so they must move together or the import breaks.
- **Acceptance:** those 4 test files pass in new-repo CI.

### D3. Workspace setup + paths + snapshot (M)
- Deps: D1. Move `setup.py` (rename module to `workspace_setup.py` ONLY if the name collides with packaging; record in MIGRATION.md if so — this is the one sanctioned rename), `workspace_paths.py`, `workspace_snapshot.py`; tests `test_agent_setup.py`, `test_agent_workspace_snapshot.py`.

### D4. Patch gate (S)
- Deps: D1. Move `patch_gate.py` + `test_agent_patch_gate.py`.

### D5. Workspace broker + browser + terminal coordination (M)
- Deps: D3. Move `workspace_broker.py`, `workspace_browser.py`, `terminal_coordination.py`; tests `test_agent_workspace_broker.py` (+ terminal-related tests that live inside it).

### D6. Runner + broker client (L)
- Deps: D2–D5. Move `runner.py`, `workspace_broker_client.py`; tests `test_agent_runner.py`, `test_agent_runner_worktree.py`, `test_agent_workspace_broker_client.py`. The runner's gateway-facing URLs/token env names stay AS-IS for now (E9 re-points them).

### D7. Host deploy files (M)
- Deps: D6. Move `deploy/docker/Dockerfile.agent-runner`, `Dockerfile.agent-sandbox`, `Dockerfile.agent-egress-proxy`, `Dockerfile.agent-gateway-tunnel`, `agent-gateway-tunnel.sh`, `docker-compose.agent-runner.yml`, `docker-compose.agent-remote-runner.yml`, `ops/deploy/agent_runner.sh`, `ops/deploy/agent_remote_runner.sh`, `ops/setup/setup_kata_runtime.sh`, `.github/workflows/agent-job-runner.yml`, `tests/unit/ops/test_agent_runner_preflight.py` → new repo `deploy/host/` + `.github/workflows/` + `tests/`; update build contexts/paths.
- The remote-runner compose and tunnel are #1170 as merged — Phase G extends this, it does not replace it.
- **Acceptance:** `docker build` of both images succeeds in CI (or a documented dry-run if CI runners can't).

---

## Phase E — Move the control plane (new repo `backend/`)

### E1. Store + Alembic baseline (L)
- Deps: B1, DR3. Move `storage/agent_job_store.py` → `backend/cloud_agent/storage/store.py` unchanged, EXCEPT: delete startup `CREATE TABLE` execution and generate `migrations/0001_baseline.py` producing the IDENTICAL DDL for the 9 tables (copy the SQL verbatim; do not "normalize" types). `agent_grants` (C5) is NOT copied — it belongs to the gateway.
- Move `tests/integration/storage/test_agent_job_store.py` (marker `dbtest`).
- **Acceptance:** `alembic upgrade head` on a fresh Postgres → store integration tests pass with `-m dbtest`.

### E2. Schemas (S)
- Deps: B1. Move `schemas_agent_jobs.py` → `backend/cloud_agent/schemas.py`; move `test_agent_name_from_prompt.py` if it imports it.

### E3. Control tokens with new secret (S)
- Deps: B1. Move `tokens.py` → `backend/cloud_agent/tokens.py`; replace the `API_KEY_SECRET` derivation with env `AGENT_CONTROL_TOKEN_SECRET`; keep format/scopes identical. Move `test_agent_job_tokens.py`.
- **Note:** model-scope tokens disappear from this module at E9 (grants replace them); until then keep both scopes so moved tests pass.

### E4. Entitlement + visible models (M)
- Deps: E2, DR7. Move `entitlement.py` (reads `plan`/`role` from the identity claims instead of gateway user rows — smallest possible edit, flag every changed line in the PR). Replace `visible_models.py`'s gateway-internal calls with `GET {GATEWAY_BASE_URL}/v1/models` filtered by the grant's `allowed_models`; keep its public function signatures. Move `test_agent_entitlement.py`, `test_agent_visible_models.py` (adapt mocks to HTTP).

### E5. Source control + publishing (L)
- Deps: E1, DR2. Move `github_app.py`, `source_control.py`, `publisher.py`, `publish_worker.py`; tests `test_agent_github_app.py`, `test_agent_source_control.py`, `test_agent_publish_worker.py`, `tests/integration/test_agent_publisher.py` (dbtest marker as-is).

### E6. API router moved AS-IS + app shell (L)
- Deps: E1–E5. Move `servers/routers/agent_jobs.py` (2,257 lines) → `backend/cloud_agent/api/routes.py` **without splitting it**; move `servers/routers/admin/agent_runner_hosts.py` → `api/admin_hosts.py`. Create `backend/cloud_agent/app.py` (FastAPI, lifespan starts store + reaper + publish loop — port the ~40 lines of wiring from old `bootstrap.py:783+`) and `deps.py` (temporary local auth stub returning a fixed test user; replaced by E7). Keep every route path identical (`/v1/agent/...`, admin paths).
- **Acceptance:** app boots against migrated DB; `GET /healthz` added; route table diff vs old repo shows identical agent paths.

### E7. Identity adapter (M)
- Deps: E6, C1–C3. Replace the E6 auth stub: verify `Authorization: Bearer <identity JWT>` against `GATEWAY_JWKS_URL` (cache keys, honor `kid`), require `aud=cloud-agent`; upsert local `users(id, external_user_id unique, email, role, plan)` row; inject as the "current user" dependency with the same shape routes already expect.
- **Acceptance:** unit tests with a locally-generated RS256 keypair: valid/expired/wrong-aud/unknown-kid.

### E8. Session BFF endpoints (M)
- Deps: E7. `POST /v1/session/login` (body: identity JWT → sets HttpOnly SameSite=Lax cookie signed with `AGENT_SESSION_SECRET`, 7d), `POST /v1/session/logout`, `GET /v1/session/me`. Cookie auth accepted everywhere bearer identity JWT is (web uses cookies; workers keep bearer control tokens).
- **Acceptance:** cookie round-trip tests; `me` returns user; logout clears.

### E9. Grant client — sandbox credentials via gateway (M)
- Deps: E6, C5, C6. Where the old code minted `scope=model` tokens in-process, call `POST {GATEWAY_BASE_URL}/internal/agent-grants` with `GATEWAY_GRANT_DISPATCH_TOKEN` per attempt (allowed_models + budget from entitlement, ttl = lease horizon); inject returned token into the sandbox env exactly where the old token went. On attempt supersede/terminal state, call revoke. Remove `SCOPE_MODEL` minting from E3's module.
- **Acceptance:** unit tests with a mocked gateway; revoke fired on supersede (reaper test).

### E10. Parity test port (L)
- Deps: E6–E9. Port `tests/servers/test_agent_jobs_api.py`, `tests/servers/test_admin_agent_runner_hosts.py`, `tests/integration/servers/test_agent_jobs_lifecycle.py` (dbtest). Auth fixtures switch to identity-JWT/test-keypair. **Do not weaken assertions** — any test that can't pass unmodified indicates a parity break: stop and report, don't adapt the test.
- **Acceptance:** full new-repo suite green; count of ported vs skipped tests reported in PR (target: 0 skipped).

### E11. Single-host end-to-end smoke (M) — **human-assisted**
- Deps: D7, E10. `deploy/control-plane/docker-compose.yml` (API + Postgres) + host compose from D7 on one machine; run one real job end-to-end (clone → run → patch → publish to a scratch repo) against staging gateway grants.
- **Acceptance:** runbook `docs/smoke.md` written from what was actually executed; job reaches `succeeded` with a PR opened on the scratch repo.

---

## Phase F — Web (new repo `web/`)

### F1. Next.js scaffold (M)
- Deps: B2. Same Next.js major version as old repo; copy lint/test configs; `web-ci` activates.

### F2. Move API client + components (L)
- Deps: F1. Move `apps/frontend/src/lib/api/agents.ts` → `web/src/lib/api/agents.ts` (base URL from env; cookie credentials `include`; DELETE the sessionStorage token plumbing — session comes from E8 cookies). Move all of `components/features/agents/` (30 files incl. tests) as-is.
- **Acceptance:** component test suite passes; `rg sessionStorage web/` → no hits.

### F3. Move pages (M)
- Deps: F2. Move `app/agents/{page,layout}.tsx` + `[jobId]/`, `archived/`, `connected/`, `integrations/` to `web/src/app/` **as the root app** (`/` = task list; keep sub-route names). Login redirect: unauthenticated → gateway `/authorize?...` (C4) with PKCE, callback page exchanges code (C3) then `POST /v1/session/login` (E8).
- **Acceptance:** e2e-ish test with mocked BFF: unauth → redirect URL correct (challenge present); auth → task list renders.

### F4. Admin hosts UI (M)
- Deps: F3, E6 (admin routes). New `/admin/hosts` page: list hosts (status, slots, last heartbeat), actions wired to E10/G6 endpoints. Guard: `role=admin` from session. Plain table UI — match existing admin styling, no new design system.

### F5. Staging deploy (M) — **human-assisted**
- Deps: F3, B4, DR4. Deploy web + control plane to staging host; DNS `agents.staging.freeinference.org`; register redirect URI in gateway env (`IDENTITY_ALLOWED_REDIRECTS`).
- **Acceptance:** real login round-trip staging → gateway → back; create + run a job from the browser.

---

## Phase G — Dynamic host pool (new repo; builds on #1158 admin switching + #1170 relay)

### G1. `agent_hosts` table + store methods (M)
- Deps: E1. Migration `0002_hosts.py`: `agent_hosts(host_id ulid pk, name unique, status enum(enrolling,online,draining,disabled,offline), labels jsonb, capabilities jsonb, slots_total int, slots_used int, runner_version text, broker_url text, last_heartbeat_at, credential_hash, created_at)`; add `agent_attempts.host_id` (nullable FK) via `0003`. Store CRUD + heartbeat upsert + atomic slot claim/release (respect #1167's lock-order rule — read that PR's description first).

### G2. Enrollment (M)
- Deps: G1. Admin API `POST /admin/hosts/enroll-token` → one-time token (15 min TTL, hashed at rest). Host boot: `POST /v1/host/register {enroll_token, name, capabilities, slots_total, broker_url}` → host credential (HMAC token via E3 module, new scope `host`) + `host_id`; credential hash stored; token single-use.
- **Acceptance:** tests: replayed enroll token rejected; credential auths subsequent host calls.

### G3. Heartbeat + status machine (M)
- Deps: G2. `POST /v1/host/heartbeat` (credential-authed): slots, version, broker health. Reaper marks `offline` after 3 missed intervals; `enrolling→online` on first heartbeat. Status transitions table-driven and unit-tested; invalid transitions 409.

### G4. Host-aware claiming (M)
- Deps: G3. Claim endpoint takes host credential; scheduler filters: `status=online`, free slot, capabilities ⊇ job requirements (runtime tier, sandbox backend from job metadata); stamps `attempts.host_id`, increments `slots_used`; release on terminal state.
- **Acceptance:** tests: draining host receives no new claims; capability mismatch skipped; slots never negative (concurrency test).

### G5. Per-host broker routing (M)
- Deps: G4, D6. Every control-plane call through `workspace_broker_client` resolves `broker_url` from the attempt's host row instead of global `AGENT_WORKSPACE_BROKER_URL` (keep the env as single-host fallback when `host_id` is null, so E11 setups keep working).
- **Acceptance:** unit test: two fake hosts, terminal/files requests hit the right base URL.

### G6. Drain / remove / revoke (M)
- Deps: G4. Admin endpoints: `POST /admin/hosts/{id}/drain` (no new claims; existing attempts finish), `POST /admin/hosts/{id}/remove` (allowed only when `slots_used=0` unless `force=true` → revoke credential, mark disabled; forced removal relies on lease expiry to requeue in-flight attempts). Wire into F4 UI.
- **Acceptance:** tests for both paths; forced removal → attempt requeues exactly once (fencing regression test).

### G7. Host-loss chaos test (M)
- Deps: G5, G6. Integration test (dbtest): host stops heartbeating mid-attempt → offline → lease expires → attempt superseded → grant revoked (E9) → job re-claimed by second host → publish happens once. This is the "no duplicate PR" guarantee — assert on publish call count.

---

## Phase H — Cutover & removal

### H1. Connection-data migration script (M)
- Repo: new, `deploy/migrate/`. Deps: E1, DR5. Script exports ONLY `agent_repo_grants` + `agent_gitlab_connections` from the gateway DB and imports into the new DB (id-preserving, idempotent re-run, row-count + checksum report). Job/thread/event history intentionally left behind.
- **Acceptance:** rehearsal on staging copies; re-run is a no-op.

### H2. Staging cutover (M) — **human-led, runbook produced**
- Deps: E11, F5, G-phase, H1. Ordered runbook: announce → disable new-job creation on old stack (feature flag/env) → drain old runner → run H1 → point staging runner host at new control plane (re-enroll via G2) → smoke (E11 checklist) → old `/agents` UI shows a banner linking to the new domain. No proxy, no dual-write; old in-flight terminal sessions are closed deliberately (they are broker-memory state and non-migratable).
- **Acceptance:** runbook committed as `docs/cutover.md` with each step ticked + timestamps; rollback section tested at least once (flip staging back, then forward again).

### H3. Production cutover — **human-led (Murphy approval gate)**
- Deps: H2 + ≥1 week staging soak with real dogfood use. Same runbook + user announcement. Coordinate so this deploy shares nothing with the open-source-split's prod cutover (see Coordination rules).

### H4. Old-repo removal PR (L)
- Repo: old. Deps: H3. Delete `agent_jobs/` (EXCEPT `model_auth.py` grant path — relocate the surviving `agr` verification + `agent_grants` DDL into gateway-owned modules, e.g. `serving/grants.py`; delete legacy `ajt` model-token acceptance), `servers/routers/agent_jobs.py`, `admin/agent_runner_hosts.py`, `schemas_agent_jobs.py`, `storage/agent_job_store.py`, frontend `agents/` trees + `lib/api/agents.ts`, agent deploy files, the 25 agent test files. `bootstrap.py` loses store/reaper/publish wiring; `auth.py` import updated to the new grants module. `/agents` route → redirect to new domain.
- **Acceptance:** gateway boots with zero agent env vars and creates no agent tables (fresh-DB test asserts table absence, `agent_grants` + `identity_codes` excepted); full `make test` green; grep gate `rg 'agent_jobs' apps/` → no hits.

### H5. Post-cutover close-out (S)
- Old repo CLAUDE.md agent sections → pointer to new repo. `docs/developer/agent-sandbox-operations.md` moves to new repo `docs/operations.md`. New repo README gets architecture diagram + "powered by FreeInference" contract description. File the open-source-readiness issue (license headers, public CI, secret-history audit — trivial since history is clean by construction).

---

## Coordination rules with the open-source (neutral-upstream) split

1. **Deploy isolation:** the neutral split's prod cutover (manifest first run + alert label flip) rides alone; no agent change shares that deploy. H3 likewise rides alone.
2. **Freeze discipline:** between A2 and E10, repo-wide sweeps in the old repo (config extraction, import re-orgs, branding) must exclude agent paths — otherwise the A3 manifest chases a moving target.
3. **Auth single-writer:** C1–C6 and the open-source split's auth/IdP phases (P3/P4 of the 2026-06-18 epic draft) touch the same auth layer. One person (Murphy) owns sequencing; recommended order: agent contracts first, pluggable-IdP abstraction on top of them later. Never two concurrent PRs editing `auth.py`.

---

## Task prompt template (example: D2)

```text
[paste Section 0 — Context block]

TASK D2 — Move sandbox/runtimes/egress into the host package.
Repo: HarvardMadSys/freeinference-cloud-agent, branch off dev.
Source of truth: HarvardMadSys/hybridInference @ 764a6f97477504deb4542e1c28c3128ff9601c94.

Steps:
1. git show <FREEZE_SHA>:apps/backend/serving/agent_jobs/sandbox.py   > host/cloud_agent_host/sandbox.py   (same for runtimes.py, egress.py)
2. Rewrite imports: serving.agent_jobs.* → cloud_agent_host.*; serving.config.settings stays behind a new host/cloud_agent_host/settings.py shim if referenced (copy only the needed accessors).
3. Copy tests test_agent_sandbox.py, test_agent_runtimes.py, test_agent_egress.py → tests/unit/, plus fixture tests/fixtures/agent_runtime_streams/claude_code_stream_contract.jsonl. Fix imports only.
4. make check.

Guardrails: imports/paths-only diff; no renames, no logic edits, no dead-code cleanup. If something cannot move without a logic change, STOP and report instead of improvising.
Acceptance: the three test files pass in CI; git diff vs pristine copies shows only import/path lines.
```

---

## Milestone shape (8 weeks, Glen full-time + agent sessions)

| Week | Lands |
|---|---|
| 1 | A1–A3, B1–B4, C1 |
| 2 | C2–C8 (Murphy reviews C5/C6 design) |
| 3 | D1–D7 |
| 4 | E1–E6 |
| 5 | E7–E11 |
| 6 | F1–F5 |
| 7 | G1–G7 |
| 8 | H1–H2 staging cutover + soak start; H3–H5 the following week |

---

## Appendix — file inventory @ origin/dev 3f955493

**Backend package `apps/backend/serving/agent_jobs/` (21 files):** `__init__.py`, `egress.py`, `entitlement.py`, `github_app.py`, `model_auth.py`*, `patch_gate.py`, `publish_worker.py`, `publisher.py`, `runner.py`, `runtimes.py`, `sandbox.py`, `setup.py`, `source_control.py`, `terminal_coordination.py`, `tokens.py`, `visible_models.py`†, `workspace_broker.py`, `workspace_broker_client.py`, `workspace_browser.py`, `workspace_paths.py`, `workspace_snapshot.py`
(* stays in gateway as the grant-verification seam; † replaced by an HTTP call, not moved)

**Backend other:** `servers/routers/agent_jobs.py` (2,257 lines), `servers/routers/admin/agent_runner_hosts.py`, `schemas_agent_jobs.py`, `storage/agent_job_store.py` (1,955 lines; 9 tables), wiring in `servers/bootstrap.py`, import in `servers/auth.py:15`, attribution column `api_logs.agent_job_id`.

**Frontend:** `app/agents/{page,layout,layout.test}.tsx` + `[jobId]/ archived/ connected/ integrations/`; `components/features/agents/` (30 files); `lib/api/agents.ts`.

**Tests (25):** unit: `test_agent_{deploy_config,egress,entitlement,github_app,job_tokens,model_auth,patch_gate,publish_worker,runner,runner_worktree,runtimes,sandbox,setup,source_control,visible_models,workspace_broker,workspace_broker_client,workspace_snapshot}.py`, `unit/storage/test_agent_name_from_prompt.py`; servers: `test_agent_jobs_api.py`, `test_admin_agent_runner_hosts.py`; integration: `servers/test_agent_jobs_lifecycle.py`, `storage/test_agent_job_store.py`, `test_agent_publisher.py`; fixture: `agent_runtime_streams/claude_code_stream_contract.jsonl`.

**Deploy/ops/docs:** `.github/workflows/agent-job-runner.yml`, `deploy/docker/Dockerfile.agent-runner`, `deploy/docker/Dockerfile.agent-sandbox`, `deploy/docker/docker-compose.agent-runner.yml`, `ops/deploy/agent_runner.sh`, `docs/developer/agent-sandbox-operations.md`.

**Open PRs to resolve before freeze (A1):** #1147, #1148, #1167, #1168, #1170.
