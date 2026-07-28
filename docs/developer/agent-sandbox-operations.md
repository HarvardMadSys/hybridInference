# Cloud agent sandbox — operations

What is on `dev` today, what is verified, and the exact steps left before a
job can run for real. Design rationale lives in
[issue #1041](https://github.com/HarvardMadSys/hybridInference/issues/1041);
this page is only about running it.

## What a job does

```text
POST /v1/agent/jobs           owner queues work
  → dispatcher claims          internal-role credential, mints two tokens
    → sandbox runs the agent   only a model-scoped token crosses the boundary
      → normalized events      streamed to the owner over SSE
      → model calls            billed through the gateway into api_logs
    → patch artifact           the agent never pushes
  → publisher validates        .github/ block, secret scan, path escape, size
    → agent/<job-id> branch    pinned refspec, never dev/main
      → draft PR               a human reviews before anything merges
```

Two properties are load-bearing and easy to break by accident:

- **No repository credential ever enters the sandbox.** The agent emits a
  patch; the publisher — running in the gateway, outside the sandbox — is the
  only component holding a GitHub token.
- **The sandbox's only credential is model-scoped.** It buys inference against
  the job's capped budget. It cannot write the event log, overwrite the patch,
  or move the job to a terminal state; the runner holds the token that can.

## Verified

Against real components on a developer machine:

| | How |
|---|---|
| Store: fencing, reaper, one-shot publish | dbtests against Postgres |
| Job API, SSE resume, worker endpoints | live gateway |
| Per-job credential: auto-revoke, budget 429 | live gateway |
| Runner + runtime adapter | real Claude Code CLI, end to end |
| Publish: patch → branch → draft PR | real `git push` to a local bare repo |
| Sandbox isolation | real Docker: non-root, `CapEff: 0000000000000000`, cap-drop enforced, `--network none`, `--rm` leaves nothing |

## Not verified

Be precise about these when reporting status:

- **Real models.** Every run so far used a deterministic fake provider. The
  chain is proven; model *behaviour* compatibility is not. Run the layer-2
  matrix (below) to close this.
- **The Actions workflow has never executed on GitHub.** See the blockers.
- **Kata.** Isolation was verified on a shared-kernel container. The Kata path
  differs only by `--runtime`, but that has not been run on a host with Kata
  installed.

## Before a job can run for real

Two things are outside the code and must be done by a human.

### 1. The workflow must be on the default branch

`repository_dispatch` only fires for workflows on the default branch, which is
`main`. `agent-job-runner.yml` is on `dev`. Until `dev` reaches `main` the
workflow cannot be triggered at all — this is a production release decision,
not a configuration step.

Self-hosted runners have no such constraint: they poll `/v1/agent/worker/claim`
and can run as soon as the gateway is deployed.

### 2. Two credentials

Neither exists yet. Both must be created by a person.

| Secret | What it is | Why it cannot be shared with anything else |
|---|---|---|
| `AGENT_DISPATCHER_TOKEN` | A gateway API key whose user has the `internal` role | `/v1/agent/worker/claim` takes the oldest queued job **across all tenants** and returns its repo, prompt, and a working capability token. An ordinary key here is a cross-tenant read. |
| `AGENT_GITHUB_TOKEN` | A GitHub token (App installation token preferred) with `contents:write` + `pull_requests:write` | Held only by the gateway's publisher. Short-lived is strongly preferred: an installation token expires in an hour, so a leak has a bounded life. |

```bash
gh secret set AGENT_DISPATCHER_TOKEN --repo HarvardMadSys/hybridInference
gh secret set AGENT_GITHUB_TOKEN --repo HarvardMadSys/hybridInference
```

For the gateway itself, set `AGENT_GITHUB_TOKEN` in its environment. Without
it the publisher loop idles and logs that it is idle — jobs still run and still
produce patches, they just never become PRs.

## Running self-hosted

```bash
docker build -f deploy/docker/Dockerfile.agent-sandbox \
             -t freeinference/agent-sandbox:latest .

docker compose -f deploy/docker/docker-compose.yml \
               -f deploy/docker/docker-compose.agent-runner.yml \
               up -d --scale agent-runner=4
```

Scaling is only `--scale`: `claim_job` uses `FOR UPDATE SKIP LOCKED`, so
runners share one queue with no leader and no sharding.

| Variable | Default | Notes |
|---|---|---|
| `AGENT_SANDBOX_BACKEND` | `kata` | `process` (no isolation) refuses to start unless `AGENT_SANDBOX_ALLOW_UNISOLATED=1` |
| `AGENT_SANDBOX_IMAGE` | `freeinference/agent-sandbox:latest` | |
| `AGENT_SANDBOX_NETWORK` | `agent-egress` | Declared `internal: true`, so a sandbox reaches the gateway and nothing else |
| `AGENT_WORKDIR_ROOT` | `/var/lib/freeinference/agent-jobs` | **Must be bind-mountable by the container runtime.** Preflight test-mounts it and fails at startup if not — otherwise every job dies at spawn with an opaque exit 125 |
| `AGENT_GITHUB_TOKEN` | — | Gateway-side; unset means the publisher idles |
| `AGENT_PUBLISH_BASE_BRANCH` | `dev` | What draft PRs target |

## Layer-2 model matrix

Closes the "real models" gap. Costs tokens.

```bash
cd services/freeinference-harness
FREEINFERENCE_API_KEY=hyi-... python -m freeinference_harness run \
  --targets configs/targets/freeinference.yaml \
  --scenarios configs/scenarios/agent-loop-runtime.yaml
```

The deterministic layer-1 suite needs no key and no tokens, and is the one to
run after bumping an agent CLI — a CLI that changes its event format breaks the
normalized mapping, and this catches it before a user's job does:

```bash
python -m freeinference_harness fake-provider --port 8351 &
python -m freeinference_harness run \
  --targets configs/targets/agent-loop-local.yaml \
  --scenarios configs/scenarios/agent-loop-core.yaml \
  --target fake-direct
```

## Known gateway findings

Surfaced by the conformance suite, tracked separately from this feature:

- An upstream mid-stream disconnect is masked with a synthesized
  `finish_reason: stop` + `[DONE]`, so a client cannot detect truncation. For
  agent workloads that is a poisoning vector: truncated tool-call arguments
  look complete.
- An in-stream 429 error frame is typed `server_error`, so a client branching
  on `type` to decide whether to back off misclassifies a rate limit.
