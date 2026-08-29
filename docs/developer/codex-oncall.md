# Codex On-Call

The Codex On-Call pipeline turns structured alerts — from the gateway's own
alert engine, or from any other producer that speaks the event contract below —
into read-only Codex investigations. A small always-on relay receives alerts,
posts the original message to Slack immediately, and hands the analysis to
one of two backends (`CODEX_ONCALL_DISPATCH_BACKEND`):

- **`github`** — a GitHub Actions workflow checks out the current `dev`
  branch, runs `codex exec` against the relay-configured Responses API
  endpoint (the gateway), and replies in the same Slack thread itself.
- **`cloud-agent`** — the relay creates a job on a cloud agent control plane
  (any service that implements the `/v1/agent/service/oncall/*` contract in
  `apps/backend/serving/oncall/agent_backend.py`); that platform's own runner
  host executes Codex in a sandbox with a per-attempt inference grant, and the
  relay polls the job, validates the result, and posts it into the thread
  itself. No GitHub-hosted minutes — an Actions outage cannot take the analysis
  path down with it — no long-lived model key in Actions secrets, spend
  attributed in `api_logs.agent_job_id`, and the checkout pinned to whatever
  commit the `CODEX_ONCALL_AGENT_BASE_REF` ref resolved to at job creation
  (the ref is a branch name — it defaults to `dev` — not a sha).

```text
off-host alert producer ──── restricted HTTPS ─┐
                                               │
Docker Compose network                         ▼
gateway backend ───────────────────────► codex-oncall relay ──► Slack alert
                                               │                    ▲
                     backend = github          │      backend = cloud-agent
                   ┌───────────────────────────┴─────────────────┐  │
                   │ repository_dispatch                         │  │ thread reply
                   ▼                                             ▼  │ (relay posts)
      GitHub Actions: codex-oncall ──► thread reply   cloud agent control plane
        checkout dev → codex exec                        job create + poll
                   │ Responses API                               │ claim + grant
                   ▼                                             ▼
  https://your-gateway.example/v1                      runner host: codex sandbox
                   │                                             │ Responses API
                   ▼                                             ▼
     <model-id> (CODEX_ONCALL_CODEX_MODEL)             gateway /v1/responses
```

Split of responsibilities, on the `github` backend:

- **Relay (Docker, always on):** authenticates producers, posts the original
  alert immediately, deduplicates incidents in SQLite, queues one durable
  hand-off per firing alert, and falls back loudly when GitHub is unreachable.
  It runs no Codex, holds no model credentials, and contains no repository
  snapshot.
- **Workflow (GitHub Actions, per alert):** ephemeral runner checks out the
  live `dev` branch (no image-staleness), installs a pinned Codex CLI, runs
  the read-only investigation, and posts the structured analysis (or its own
  failure notice) into the original Slack thread. The runner VM is destroyed
  after each run.

The analysis is read-only by construction: the workflow job runs with
`permissions: contents: read`, so nothing the analysis itself does can create
issues, branches, pull requests, or merges. The relay's own GitHub token is a
weaker claim than that. It is scoped to sending `repository_dispatch`, but
GitHub grants that through *Contents: read & write* (see Configure GitHub step 3
below), and a token with Contents:write can also push commits. Treat it as a
write credential for this repository and scope the PAT to this repository
alone. The structured result carries
`issue_recommendation` and `draft_pr_recommendation` fields
(`apps/backend/serving/oncall/models.py`) that a human acts on. Giving the
pipeline write actions would mean separate credentials and explicit policy
gates, and is deliberately not part of this feature.

## Runtime boundaries

- Alert delivery never waits for Codex. The relay returns `202` after Slack
  accepts the original alert and the durable SQLite job is queued.
- Producers keep their existing incoming webhook as a fallback. A relay timeout
  or non-2xx response therefore does not drop the page.
- The dispatch payload contains only the sanitized, bounded alert (secrets
  redacted, `slack_text` stripped) plus Slack thread coordinates and the model
  configuration. Payload values are treated as untrusted in the workflow: they
  reach disk through env indirection and are never interpolated into shell
  scripts.
- The workflow job runs with a read-only `GITHUB_TOKEN` (`permissions:
  contents: read`) and step-scoped secrets: the gateway API key is visible
  only to the Codex step, the Slack bot token only to the posting steps.
- `codex exec` runs with user config, rules, hooks, apps, subagents, and web
  search disabled, and a shell environment restricted to
  `PATH/HOME/LANG/LC_ALL`. A sandbox self-check picks `--sandbox read-only`
  when the runner supports it and otherwise falls back — with a visible
  workflow warning — to the ephemeral VM as the isolation boundary.
- One workflow run per incident fingerprint at a time (Actions concurrency
  group); duplicates queue instead of racing.
- The relay retries only the hand-off itself. After a successful dispatch the
  workflow owns the outcome and posts its own failure notice (with the run
  URL) if the analysis fails; if the dispatch itself finally fails, the relay
  posts the failure notice.
- Fingerprints deduplicate transport retries. Recovery events close the active
  incident and reply in its original Slack thread.

## Configure GitHub

1. Ensure `.github/workflows/codex-oncall.yml` exists on the default branch so
   `repository_dispatch` can trigger it.
2. Create two **Actions secrets** in the repository:

   | Secret | Purpose |
   |---|---|
   | `CODEX_ONCALL_MODEL_API_KEY` | HybridInference `hyi-...` key the workflow uses against the gateway's Responses API. It must be able to see `CODEX_ONCALL_CODEX_MODEL`; if the deployment role-gates that model (`apps/backend/serving/config/model_visibility.py`), issue the key with a role that can. Not an upstream provider key — Codex cannot call providers directly (see below). |
   | `CODEX_ONCALL_SLACK_BOT_TOKEN` | Same Slack bot token the relay uses (`chat:write`, invited to the channel). |

3. Create a **fine-grained PAT** for the relay with *Contents: read & write*
   on this repository only — that is the permission `repository_dispatch`
   requires. It goes into `.env.oncall` below, not into Actions secrets.

## Configure the relay

Create the relay-only environment file. Do not put these secrets in the shared
backend `.env`, because the backend container loads that whole file.

```bash
cp .env.oncall.example .env.oncall
chmod 0600 .env.oncall
openssl rand -hex 32
```

Populate `.env.oncall`:

```text
CODEX_ONCALL_RELAY_TOKEN=<random shared bearer token>
CODEX_ONCALL_SLACK_BOT_TOKEN=xoxb-...
CODEX_ONCALL_SLACK_CHANNEL_ID=C0123456789
CODEX_ONCALL_GITHUB_TOKEN=github_pat_...
CODEX_ONCALL_GITHUB_REPOSITORY=<owner>/<repo>
CODEX_ONCALL_CODEX_MODEL=llama-3.3-70b
CODEX_ONCALL_MODEL_BASE_URL=https://your-gateway.example/v1
```

`CODEX_ONCALL_CODEX_MODEL` and `CODEX_ONCALL_MODEL_BASE_URL` are forwarded in
each dispatch payload, so model policy is controlled from one place — changing
the model is an `.env.oncall` edit plus a relay restart, no code or workflow
change (the API key is the one exception: it lives in the
`CODEX_ONCALL_MODEL_API_KEY` Actions secret). The base URL must be reachable
from GitHub-hosted runners — use the public gateway, not a Compose-internal
hostname.

Why the base URL must be the gateway: Codex speaks only the OpenAI Responses
API — chat-wire support was removed upstream
([openai/codex#7782](https://github.com/openai/codex/discussions/7782)) — and
most OpenAI-compatible provider endpoints serve only Chat Completions, not
`/v1/responses`. The gateway's northbound Responses router
(`apps/backend/serving/servers/routers/responses.py`) is what makes those
models reachable for Codex at all; pointing the workflow straight at a provider
fails at config load. To pay for analysis tokens through a specific provider,
wire that provider into the model registry the gateway loads and keep the
workflow on the gateway. That registry is whatever
`MODELS_CONFIG_PATH` names, else the `paths.models` entry of an active
distribution manifest (`DISTRIBUTION_CONFIG_PATH` with
`DISTRIBUTION_CONFIG_MODE=active`), else the shipped reference registry
`config/examples/models.openrouter.yaml`.

Model choice: any model the gateway serves and that survives an agentic loop
will do. Two practical constraints: the model must tolerate multi-turn tool
calling (an analysis run drives `codex exec` through many shell steps), and
each run sends tens of thousands of prompt tokens, so per-token price matters
more here than latency. `.env.oncall.example` names `llama-3.3-70b`, the model
the reference registry registers; a deployment with its own catalog names one
of its own.

The Slack app needs `chat:write` and must be added to the target channel. The
relay uses `chat.postMessage` so the workflow can reply in the original alert
thread.

## Backend: cloud agent

The `cloud-agent` backend replaces the whole GitHub half above — no Actions
secrets, no `repository_dispatch` PAT, no workflow. Instead:

1. **On the gateway** create (or reuse) a service account for on-call
   analyses. Its role decides which models the job's grant may carry, so it
   must be able to see `CODEX_ONCALL_CODEX_MODEL`; its daily quota is what the
   analyses spend.
2. **On the cloud agent control plane**, following that platform's own
   documentation. The relay expects it to accept an on-call dispatch token, to
   attribute jobs to the gateway user id of the service account above, and to
   allow the repository the analysis checks out. The GitHub App the platform
   holds must be installed on that repository — the runner clones with a
   read-only installation token.
3. **In `.env.oncall`**:

   ```text
   CODEX_ONCALL_DISPATCH_BACKEND=cloud-agent
   CODEX_ONCALL_AGENT_BASE_URL=https://your-agent-control-plane.example
   CODEX_ONCALL_AGENT_DISPATCH_TOKEN=<same token as the control plane>
   CODEX_ONCALL_AGENT_BASE_REF=dev
   CODEX_ONCALL_AGENT_CONSOLE_URL=https://your-agent-console.example/agents/jobs/{job_id}
   ```

   `CODEX_ONCALL_CODEX_MODEL` keeps meaning what it meant; the model must
   resolve for the service account. `CODEX_ONCALL_GITHUB_*` and
   `CODEX_ONCALL_MODEL_BASE_URL` are unused on this backend — the platform
   injects its own gateway address into the sandbox.
   `CODEX_ONCALL_AGENT_CONSOLE_URL` is only a link template: the relay
   substitutes `{job_id}` and posts the result into Slack, so the path has to
   be whatever the agent console uses for a job page. Leave it unset and Slack
   carries the bare job id instead.

Runtime behaviour: the relay's worker parks each dispatched analysis in an
`await_result` stage (SQLite, restart-safe), polls the platform every
`CODEX_ONCALL_AGENT_POLL_SECONDS`, and cancels jobs that outlive
`CODEX_ONCALL_AGENT_TIMEOUT_SECONDS`. A result is posted only if the
normalized event log contains at least one successful command execution and
the final message parses as exactly one schema-valid analysis — the same
groundedness rule the workflow's posting step enforces. Only one analysis per
incident fingerprint is in flight at a time; re-fires during it update the
incident but do not enqueue another job.

Rollback is one edit: set `CODEX_ONCALL_DISPATCH_BACKEND=github` (with the
GitHub values still present) and restart the relay. Jobs parked in
`await_result` at that moment fail closed with a notice in their thread — the
GitHub backend cannot poll them.

## Enable the Compose profile

Set the producer values in the shared backend `.env`. The relay token must
match `.env.oncall`.

```text
COMPOSE_PROFILES=oncall
CODEX_ONCALL_RELAY_URL=http://codex-oncall:8091
CODEX_ONCALL_RELAY_TOKEN=<same shared token>
SLACK_ALERTS_WEBHOOK_URL=<existing fallback webhook>
```

Set `ALERTS_ENABLED=true` as well when enabling the gateway's rule-based alert
engine. Other existing gateway alert producers use the relay automatically when
the URL and token are present.

Build and start the existing Compose stack — `make build` is
`docker compose up -d --build`, so it does both:

```bash
make build
curl -fsS http://127.0.0.1:8091/healthz
```

The health response must contain `"ready":true`. The relay image contains only
the FastAPI service — the Codex CLI version is pinned inside
`.github/workflows/codex-oncall.yml` (`CODEX_CLI_VERSION`), and each workflow
run analyzes a fresh checkout of `dev`, so there is no image snapshot to keep
in sync.

## Producers outside the Compose network

The gateway backend reaches the relay over the Compose network, so
`http://codex-oncall:8091` is enough for it. A producer running anywhere else —
an external monitor, a scheduled job on another host — cannot resolve that
name, and the Compose service publishes port `8091` on host loopback only
(`deploy/docker/docker-compose.yml`). Put a restricted TLS route in front of
that loopback port and give the external producer the public URL plus the same
`CODEX_ONCALL_RELAY_TOKEN`; the relay authenticates every `POST /v1/alerts`
with a constant-time comparison against it and answers `401` otherwise.

Keep the producer's existing Slack webhook configured alongside the relay URL.
The relay is a best-effort accelerator: the webhook is what still pages when
the relay is unreachable.

## Smoke test

Send a synthetic event from the Compose host, using the token from
`.env.oncall`:

```bash
curl -i http://127.0.0.1:8091/v1/alerts \
  -H "Authorization: Bearer $CODEX_ONCALL_RELAY_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{
    "version":"1",
    "alert_id":"smoke-1",
    "fingerprint":"smoke:staging:provider",
    "source":"manual-smoke-test",
    "status":"firing",
    "severity":"warn",
    "title":"Synthetic provider warning",
    "environment":"staging",
    "occurred_at":"2026-07-10T00:00:00Z",
    "summary":"Synthetic event; no production impact",
    "context":{"provider":"example"},
    "slack_text":"Synthetic Codex on-call smoke test"
  }'
```

A successful request returns `202` with
`{"accepted": ..., "duplicate": ..., "fingerprint": ..., "slack_thread_ts": ...}`
and posts the synthetic alert to Slack immediately. On the `github` backend it
then starts a `Codex On-Call` run under the repository's Actions tab; on the
`cloud-agent` backend it creates a job on the control plane. Either way the
analysis reply lands in the alert's Slack thread when the run finishes.

Reusing the same fingerprint inside the dedupe window (`dedupe_window_seconds`
in the payload, default `300`) returns `duplicate: true` without another
top-level message. On the first live run of the `github` backend, check the
workflow log for the sandbox self-check result — it says whether Codex got its
own read-only sandbox or fell back to the ephemeral runner VM.

