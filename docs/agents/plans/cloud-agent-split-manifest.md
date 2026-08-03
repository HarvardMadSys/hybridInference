# Cloud Agent Split — File Manifest

- **Status:** PRELIMINARY. Regenerate at freeze time (task A2/A3).
- **Basis:** `origin/dev @ 3f955493` (2026-08-03)
- **FREEZE_SHA:** _not yet declared_ — blocked on PRs #1147, #1148, #1167, #1168, #1170 (task A1)
- **Companion:** [2026-08-03-cloud-agent-repo-split.md](2026-08-03-cloud-agent-repo-split.md)

Regenerate with:

```bash
git ls-tree -r <FREEZE_SHA> --name-only \
  | grep -Ei '(agent[_-]|/agents?(/|$)|agent_jobs)' \
  | grep -vE '^docs/agents/'
```

Then diff against this file; every new path must be classified before Phase D starts.

## Legend

| Tag | Meaning |
|---|---|
| `move:host` | → new repo `host/cloud_agent_host/` (Phase D) |
| `move:control` | → new repo `backend/cloud_agent/` (Phase E) |
| `move:web` | → new repo `web/` (Phase F) |
| `move:deploy` | → new repo `deploy/` + `.github/workflows/` (D7) |
| `move:docs` | → new repo `docs/` (H5) |
| `stay:contract` | stays in gateway, becomes/serves a cross-service contract (Phase C) |
| `stay:edit-at-H4` | stays, but agent wiring is stripped at H4 |
| `stay:unrelated` | matched the grep but is NOT cloud agent — do not touch |

---

## move:host — execution core (Phase D)

| Path | Task |
|---|---|
| `apps/backend/serving/agent_jobs/sandbox.py` | D2 |
| `apps/backend/serving/agent_jobs/runtimes.py` | D2 |
| `apps/backend/serving/agent_jobs/egress.py` | D2 |
| `apps/backend/serving/agent_jobs/setup.py` | D3 |
| `apps/backend/serving/agent_jobs/workspace_paths.py` | D3 |
| `apps/backend/serving/agent_jobs/workspace_snapshot.py` | D3 |
| `apps/backend/serving/agent_jobs/patch_gate.py` | D4 ⚠️ see coordination note |
| `apps/backend/serving/agent_jobs/workspace_broker.py` | D5 |
| `apps/backend/serving/agent_jobs/workspace_browser.py` | D5 |
| `apps/backend/serving/agent_jobs/terminal_coordination.py` | D5 |
| `apps/backend/serving/agent_jobs/runner.py` | D6 |
| `apps/backend/serving/agent_jobs/workspace_broker_client.py` | D6 |
| `tests/unit/test_agent_sandbox.py` | D2 |
| `tests/unit/test_agent_runtimes.py` | D2 |
| `tests/unit/test_agent_egress.py` | D2 |
| `tests/fixtures/agent_runtime_streams/claude_code_stream_contract.jsonl` | D2 |
| `tests/unit/test_agent_setup.py` | D3 |
| `tests/unit/test_agent_workspace_snapshot.py` | D3 |
| `tests/unit/test_agent_patch_gate.py` | D4 |
| `tests/unit/test_agent_workspace_broker.py` | D5 |
| `tests/unit/test_agent_runner.py` | D6 |
| `tests/unit/test_agent_runner_worktree.py` | D6 |
| `tests/unit/test_agent_workspace_broker_client.py` | D6 |
| `tests/unit/test_agent_deploy_config.py` | D7 — imports `agent_jobs.runner.build_parser`, asserts on the compose file; moves with the deploy files |

## move:control — control plane (Phase E)

| Path | Task | Note |
|---|---|---|
| `apps/backend/serving/storage/agent_job_store.py` | E1 | 1,955 lines, 9 tables → Alembic baseline |
| `apps/backend/serving/schemas_agent_jobs.py` | E2 | |
| `apps/backend/serving/agent_jobs/tokens.py` | E3 | re-key to `AGENT_CONTROL_TOKEN_SECRET` |
| `apps/backend/serving/agent_jobs/entitlement.py` | E4 | reads plan/role from identity claims |
| `apps/backend/serving/agent_jobs/github_app.py` | E5 | |
| `apps/backend/serving/agent_jobs/source_control.py` | E5 | |
| `apps/backend/serving/agent_jobs/publisher.py` | E5 | |
| `apps/backend/serving/agent_jobs/publish_worker.py` | E5 | |
| `apps/backend/serving/servers/routers/agent_jobs.py` | E6 | 2,257 lines — **move whole, do not split** |
| `apps/backend/serving/servers/routers/admin/agent_runner_hosts.py` | E6 | from #1158 |
| `apps/backend/serving/agent_jobs/__init__.py` | E6 | package docstring only |
| `tests/unit/test_agent_job_tokens.py` | E3 | |
| `tests/unit/test_agent_entitlement.py` | E4 | |
| `tests/unit/test_agent_visible_models.py` | E4 | rewrite mocks to HTTP |
| `tests/unit/test_agent_github_app.py` | E5 | |
| `tests/unit/test_agent_source_control.py` | E5 | |
| `tests/unit/test_agent_publish_worker.py` | E5 | |
| `tests/integration/test_agent_publisher.py` | E5 | `dbtest` |
| `tests/integration/storage/test_agent_job_store.py` | E1 | `dbtest` |
| `tests/servers/test_agent_jobs_api.py` | E10 | |
| `tests/servers/test_admin_agent_runner_hosts.py` | E10 | |
| `tests/integration/servers/test_agent_jobs_lifecycle.py` | E10 | `dbtest` |

**Rewritten, not moved:** `apps/backend/serving/agent_jobs/visible_models.py` (E4) — replaced by an HTTP call to `GET {GATEWAY_BASE_URL}/v1/models`; public function signatures preserved.

## move:web — frontend (Phase F)

`apps/frontend/src/lib/api/agents.ts` (F2) · all 30 files under `apps/frontend/src/components/features/agents/` (F2) · `apps/frontend/src/app/agents/` — `page.tsx`, `layout.tsx`, `layout.test.tsx`, `[jobId]/page.tsx`, `archived/page.tsx`, `connected/page.tsx`, `connected/page.test.tsx`, `integrations/page.tsx` (F3)

## move:deploy — host deployment (D7)

`.github/workflows/agent-job-runner.yml` · `deploy/docker/Dockerfile.agent-runner` · `deploy/docker/Dockerfile.agent-sandbox` · `deploy/docker/docker-compose.agent-runner.yml` · `ops/deploy/agent_runner.sh`

## move:docs (H5)

`docs/developer/agent-sandbox-operations.md` → new repo `docs/operations.md`

## stay:contract — gateway keeps these (Phase C)

| Path | Why |
|---|---|
| `apps/backend/serving/agent_jobs/model_auth.py` | Grant verification seam. C6 adds the `agr` path; H4 drops legacy `ajt` and relocates the survivor to a gateway-owned module (e.g. `serving/grants.py`). |
| `apps/backend/serving/storage/log_schema.py:88,143,245` | `api_logs.agent_job_id` column + partial index. Billing attribution — never moves. |
| `apps/backend/serving/storage/postgres_log.py:310,324` | `get_agent_job_cost` / `get_agent_job_usage` — the budget enforcement C6 reuses and the data source for C7's usage endpoint. |
| `apps/backend/serving/storage/database.py:835,897` | `agent_job_id` in the api_logs INSERT (hand-duplicated with postgres_log.py — see the api_logs schema split-brain rule). |
| `apps/backend/serving/servers/routers/completions.py:598-601` | Propagates `agent_job_id` from the capability token into log metadata. |
| `apps/backend/serving/servers/routers/anthropic_messages.py:1075-1076` | Same, Anthropic surface. |
| `apps/backend/serving/servers/routers/embeddings.py:172` | Same, embeddings surface. |

## stay:edit-at-H4 — wiring stripped after cutover

| Path | What goes |
|---|---|
| `apps/backend/serving/servers/app.py:21,106` | `agent_jobs` router import + `include_router` |
| `apps/backend/serving/servers/bootstrap.py` (~783+) | `AgentJobStore` init, attempt reaper task, publish loop, GitHub/GitLab credential wiring |
| `apps/backend/serving/servers/deps.py:73,110,175-179` | `AgentJobStore` import, service slot, `get_agent_job_store` provider |
| `apps/backend/serving/servers/auth.py:15` | import of `agent_jobs.model_auth` → repoint at the gateway-owned grants module |

## stay:unrelated — matched the grep, NOT cloud agent

| Path | What it actually is |
|---|---|
| `services/freeinference-harness/src/freeinference_harness/agent_loop.py` | Benchmark harness: scripted agent-loop **protocol conformance** against the gateway/fake provider. Same issue number (#1041) in its docstring, different layer. |
| `services/freeinference-harness/src/freeinference_harness/agent_scripts.py` | Script definitions for the above. |
| `services/freeinference-harness/tests/test_agent_loop_openai.py` | Its tests. |
| `services/freeinference-harness/configs/**/agent-loop-*.yaml` (4 files) | Its scenario/target configs. |
| `tests/unit/storage/test_agent_name_from_prompt.py` | Tests `serving/storage/utils.py::agent_name_from_prompt` — parses *which coding agent* made an API call, for log analytics. Nothing to do with cloud agent jobs. |

---

## ⚠️ Coordination notes

1. **`ops/release/public_export.py:36`** hard-references `apps/backend/serving/agent_jobs/patch_gate.py`. `patch_gate.py` leaves in D4 and the whole package is deleted at H4, so the open-source export tool breaks at that point. Owner of the neutral-upstream split must either drop that reference or re-point it before H4 lands. This is the concrete instance of the "auth single-writer / no repo-wide sweeps over agent paths" coordination rule.

2. **`api_logs` INSERT is hand-duplicated** across `postgres_log.py` and `database.py`. Nothing in this split adds a column, but if Phase C ever needs one (e.g. `grant_id`), it must touch `log_schema.py` + BOTH INSERTs + the LogStore ABC + export.

3. **Attempt/lease fencing moves repos, budget enforcement does not.** After the split the gateway can no longer consult `AgentJobStore` to fence a model token; that is why C5 introduces a gateway-owned `agent_grants` table with explicit revoke. Any design change to grants must preserve: revoke-on-supersede (E9) and fail-closed-on-missing-budget (`model_auth.py`).
