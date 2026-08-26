# Cloud Agent Split — File Manifest

> **Publication note (2026-08-26):** References below to
> `ops/release/public_export.py` describe a dependency that existed at this
> manifest's freeze SHA. The filtered-export tool has since been retired; the
> existing HybridInference repository follows the
> [direct-publication readiness plan](2026-08-26-direct-publication-readiness.md).

- **Status:** DECLARED. Regenerate only if the freeze is moved.
- **FREEZE_SHA:** `764a6f97477504deb4542e1c28c3128ff9601c94` (`dev`, 2026-08-03)
- **Companion:** [2026-08-03-cloud-agent-repo-split.md](2026-08-03-cloud-agent-repo-split.md)

Task A1 is complete: #1147, #1148, #1167, #1168 and #1170 all merged, which is
what the freeze was waiting for. Those five added 13 files to this manifest —
notably an egress proxy, an MCP proxy, and the remote-runner relay.

Migration tasks may now start. While the freeze holds, agent paths in this
repository change only for Phase C contract work.

Regenerate with:

```bash
git ls-tree -r <FREEZE_SHA> --name-only \
  | grep -Ei '(agent[_-]|/agents?(/|$)|agent_jobs)' \
  | grep -vE '^docs/agents/'
```

Then diff against this file; every new path must be classified before Phase D starts.

**This file says where things are and how they are classified. It does not say
what to do about them.** Every instruction, rationale and design decision lives
in the plan, once. A note here may state what a file *is* and point at the task
that handles it; the moment it starts prescribing behaviour it becomes a second
source that drifts, because the plan gets revised under review and this file
does not. That already happened: an earlier revision of this manifest carried
`requested ∩ registry` for the C5 MCP clamp, which the plan had by then replaced
with *refuse the mint on an unknown name* — and the H4 exception for the MCP
modules existed **only** here, in a note the executor of H4 had no reason to
open. Both are now single-sourced in the plan. Keep it that way: if a row needs
a paragraph, the paragraph belongs in the task.

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
| `apps/backend/serving/agent_jobs/egress_proxy.py` | D2 — generates the runner host's Squid allowlist config |
| `apps/backend/serving/agent_jobs/setup.py` | D3 |
| `apps/backend/serving/agent_jobs/workspace_paths.py` | D3 |
| `apps/backend/serving/agent_jobs/workspace_snapshot.py` | D3 |
| `apps/backend/serving/agent_jobs/patch_gate.py` | D4 — ⚠️ referenced by `ops/release/public_export.py:36`; see the cross-cutting table |
| `apps/backend/serving/agent_jobs/workspace_broker.py` | D5 |
| `apps/backend/serving/agent_jobs/workspace_browser.py` | D5 |
| `apps/backend/serving/agent_jobs/runner.py` | D6 |
| `apps/backend/serving/agent_jobs/workspace_broker_client.py` | D6 |
| `tests/unit/test_agent_sandbox.py` | D2 |
| `tests/unit/test_agent_runtimes.py` | D2 |
| `tests/unit/test_agent_egress.py` | D2 |
| `tests/unit/test_agent_egress_proxy.py` | D2 |
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
| `apps/backend/serving/storage/agent_job_store.py` | E1 | 1,955 lines, **11 tables** (incl. `agent_runner_hosts`, `agent_runner_policy` from #1158) |
| `apps/backend/serving/schemas_agent_jobs.py` | E2 | |
| `apps/backend/serving/agent_jobs/tokens.py` | E3 | |
| `apps/backend/serving/agent_jobs/entitlement.py` | E4 | **repository** allowlist from env config — reads no user row, role or identity claim |
| `apps/backend/serving/agent_jobs/github_app.py` | E5 | |
| `apps/backend/serving/agent_jobs/source_control.py` | E5 | `SourceControlCipher` reads `API_KEY_SECRET` at line 40; re-keyed in E5 |
| `apps/backend/serving/agent_jobs/publisher.py` | E5 | |
| `apps/backend/serving/agent_jobs/publish_worker.py` | E5 | |
| `apps/backend/serving/agent_jobs/terminal_coordination.py` | E6 | Gateway-owned settled-terminal recovery; consumed by the API router and bootstrap wiring, and calls the host through `workspace_broker_client` |
| `apps/backend/serving/servers/routers/agent_jobs.py` | E6 | 2,257 lines. Gateway-side reads to re-point: `mcp_registry` import (line 50), `get_log_store` cost/usage (lines 203, 283–287, 1043) |
| `apps/backend/serving/servers/routers/admin/agent_runner_hosts.py` | E6 | from #1158; retires at F4 |
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

**Rewritten, not moved:** `apps/backend/serving/agent_jobs/visible_models.py` — E4 replaces its gateway-internal catalog read with an HTTP call. See E4 for which endpoint, and why not `GET /v1/models`.

## move:web — frontend (Phase F)

`apps/frontend/src/lib/api/agents.ts` (F2; omit `budget_usd`/`default_budget_usd`) · all 30 files under `apps/frontend/src/components/features/agents/` (F2; remove `budgetUsd` types/mocks/UI, keep actual usage display) · `apps/frontend/src/app/agents/` — `page.tsx`, `layout.tsx`, `layout.test.tsx`, `[jobId]/page.tsx`, `archived/page.tsx`, `connected/page.tsx`, `connected/page.test.tsx`, `integrations/page.tsx` (F3)

## move:deploy — host deployment (D7)

`.github/workflows/agent-job-runner.yml` · `deploy/docker/Dockerfile.agent-runner` · `deploy/docker/Dockerfile.agent-sandbox` · `deploy/docker/docker-compose.agent-runner.yml` · `ops/deploy/agent_runner.sh`

Added by the pre-freeze merges:

| Path | From | Note |
|---|---|---|
| `deploy/docker/Dockerfile.agent-egress-proxy` | #1147 | Squid image for the Trusted tier |
| `deploy/docker/Dockerfile.agent-gateway-tunnel` | #1170 | The relay that lets a runner host with no local gateway reach one |
| `deploy/docker/agent-gateway-tunnel.sh` | #1170 | |
| `deploy/docker/docker-compose.agent-remote-runner.yml` | #1170 | The multi-host compose — phase G builds on this, not on a new transport |
| `ops/deploy/agent_remote_runner.sh` | #1170 | |
| `ops/setup/setup_kata_runtime.sh` | #1168 | Kata provisioning; referenced from the deploy gate |
| `tests/unit/ops/test_agent_runner_preflight.py` | #1168 | |

## move:docs (H5)

`docs/developer/agent-sandbox-operations.md` → new repo `docs/operations.md`

## stay:contract — gateway keeps these (Phase C)

| Path | Why |
|---|---|
| `apps/backend/serving/agent_jobs/model_auth.py` | Grant verification seam (C6 adds the `agr` path; relocated out of the package at H4). |
| `apps/backend/serving/servers/auth.py:354-371` vs `:413-451` | The agent-token branch **returns before the quota gate**. `quota_daily_cost_usd` is on `api_keys` (DDL:238); spend is on `user_daily_cost`. See C6. |
| `apps/backend/serving/storage/log_schema.py:88,143,245` | `api_logs.agent_job_id` column + partial index. Billing attribution — never moves. |
| `apps/backend/serving/storage/postgres_log.py:310,324` | `get_agent_job_cost` / `get_agent_job_usage` — the data source behind C7. |
| `apps/backend/serving/storage/database.py:835,897` | `agent_job_id` in the api_logs INSERT, hand-duplicated with `postgres_log.py` (see the Phase C design note). |
| `apps/backend/serving/servers/routers/completions.py:598-601` | Propagates `agent_job_id` from the capability token into log metadata. |
| `apps/backend/serving/servers/routers/anthropic_messages.py:1075-1076` | Same, Anthropic surface. |
| `apps/backend/serving/servers/routers/embeddings.py:172` | Same, embeddings surface. |
| `apps/backend/serving/servers/routers/agent_mcp.py` | The MCP proxy endpoint (#1148) — gateway capability surface, not control-plane code. Not under `agent_jobs/`, so H4's deletion does not reach it; its imports are re-pointed there. |
| `apps/backend/serving/agent_jobs/mcp_proxy.py` | Its allowlist/filtering logic. **Inside `agent_jobs/` but survives H4** — relocated, not deleted. |
| `apps/backend/serving/agent_jobs/mcp_registry.py` | The deployment's MCP server registry — overlay config shaped like `models.yaml`, holding upstream credentials. Imported by `agent_jobs.py:50` (see C9, E6). **Inside `agent_jobs/` but survives H4.** |
| `apps/backend/serving/config/settings.py:159-161` | Points at that registry file. |
| `apps/backend/serving/servers/routers/models.py:110-115` | `/v1/models` resolves visibility through `optional_verify_api_key`, which does not accept an identity JWT. See C9. |
| `tests/servers/test_agent_mcp_api.py`, `tests/unit/test_agent_mcp.py` | Their tests. |

## stay:edit-at-H4 — wiring stripped after cutover

| Path | What goes |
|---|---|
| `apps/backend/serving/servers/app.py:21,106` | `agent_jobs` router import + `include_router` |
| `apps/backend/serving/servers/bootstrap.py` (~783+) | `AgentJobStore` init, attempt reaper task, publish loop, GitHub/GitLab credential wiring |
| `tests/servers/test_bootstrap.py` | Agent-specific bootstrap cases are removed at H4; E6 ports only the settled-terminal reconciliation case into the new app-shell tests, while unrelated gateway coverage stays |
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

## ⚠️ Cross-cutting facts, and where each is single-sourced

Five properties of this codebase cut across several rows above. Each is written
out **once**, in the task that has to act on it — read it there, not here.

| Fact | Written out in |
|---|---|
| MCP is a second consumer of the capability token: `agent_mcp.py` authenticates tool calls with the same grant and fence as model calls, and the registry's upstream credentials are what keeps it gateway-side | C5 (scope), C6 (both auth functions), C9 (names over HTTP), H4 (the modules survive deletion) |
| The agent branch in `auth.py` returns before the quota gate, so `agr` calls are authenticated and never metered — closing that is work, not an observation | C6 |
| GitLab credentials are Fernet-wrapped with the gateway's `API_KEY_SECRET`, so the ciphertext cannot travel; copying the bytes appears to succeed and fails later as "reconnect your GitLab" | E5 (re-key), H1 (re-wrap) |
| The `api_logs` INSERT is hand-duplicated across `postgres_log.py` and `database.py` | Phase C design note |
| `ops/release/public_export.py:36` hard-references `patch_gate.py`, which this split removes | Coordination rules, item 4 |
