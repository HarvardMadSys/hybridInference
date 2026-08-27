# Codex On-Call

The Codex On-Call pipeline turns structured gateway and status-monitor alerts
into read-only Codex investigations. A small always-on relay receives alerts,
posts the original message to Slack immediately, and hands the analysis to
one of two backends (`CODEX_ONCALL_DISPATCH_BACKEND`):

- **`github`** — a GitHub Actions workflow checks out the current `dev`
  branch, runs `codex exec` against the relay-configured Responses API
  endpoint (the gateway), and replies in the same Slack thread itself.
- **`cloud-agent`** — the relay creates a job on the FreeInference cloud
  agent control plane; the platform's own runner host executes Codex in a
  sandbox with a per-attempt inference grant, and the relay polls the job,
  validates the result, and posts it into the thread itself. No GitHub-hosted
  minutes (the 2026-08-05 Actions billing outage took the analysis path down
  with it), no long-lived model key in Actions secrets, spend attributed in
  `api_logs.agent_job_id`, and the checkout pinned to the commit `dev` named
  at creation.

```text
Cloudflare status-monitor ── restricted HTTPS ─┐
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
       https://freeinference.org/v1                    runner host: codex sandbox
                   │                                             │ Responses API
                   ▼                                             ▼
        glm-5.2 (CODEX_ONCALL_CODEX_MODEL)              gateway /v1/responses
```

Split of responsibilities:

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

This first phase does not hold GitHub write credentials beyond
`repository_dispatch`, and cannot create issues, branches, pull requests, or
merges. The structured result only recommends whether an issue or draft PR
would be appropriate for human follow-up.

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
   | `CODEX_ONCALL_MODEL_API_KEY` | HybridInference `hyi-...` key the workflow uses against the gateway's Responses API. Must see the configured model (`glm-5.2` and `deepseek-v4-flash` are internal-only, so an `internal`/`admin` service key). Not an upstream provider key — Codex cannot call providers directly (see below). |
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
CODEX_ONCALL_GITHUB_REPOSITORY=HarvardMadSys/hybridInference
CODEX_ONCALL_CODEX_MODEL=glm-5.2
CODEX_ONCALL_MODEL_BASE_URL=https://freeinference.org/v1
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
provider chat endpoints (DeepSeek, ZAI, Tencent Token Plan, …) do not serve
`/v1/responses`. The gateway's northbound Responses translator is what makes
those models reachable for Codex at all; pointing the workflow straight at a
provider fails at config load. To pay for analysis tokens through a specific
provider, wire that provider into the gateway's `config/models.yaml` routes
instead and keep the workflow on the gateway.

Model choice: `glm-5.2` is the launch default because it is verified working
end-to-end through the Responses API today. The intended steady-state model is
`deepseek-v4-flash` (local H200 sglang route with the official DeepSeek API as
fallback, roughly one-third the official per-token price of `deepseek-v4-pro` —
each analysis run sends tens of thousands of prompt tokens through an agentic
loop). It is blocked on the H200 V4 parser fix (PR #939): until that
deployment is restarted and verified, the model returns empty `content` and
unparsed tool calls, which breaks the agentic loop. Flip the env var once
verified.

The Slack app needs `chat:write` and must be added to the target channel. The
relay uses `chat.postMessage` so the workflow can reply in the original alert
thread.

## Backend: cloud agent

The `cloud-agent` backend replaces the whole GitHub half above — no Actions
secrets, no `repository_dispatch` PAT, no workflow. Instead:

1. **On the gateway** create (or reuse) a service account for on-call
   analyses. Its role decides which models the job's grant may carry —
   `glm-5.2` and `deepseek-v4-flash` are internal-only, so role `internal` or
   `admin` — and its daily quota is what the analyses spend.
2. **On the cloud agent control plane** (see `ENVIRONMENT.md` in
   [freeinference-cloud-agent](https://github.com/HarvardMadSys/freeinference-cloud-agent)):

   ```text
   AGENT_ONCALL_DISPATCH_TOKEN=<random 32 bytes, shared with the relay>
   AGENT_ONCALL_USER_ID=<the service account's user id>
   AGENT_REPO_ALLOWLIST=<must cover the repo below, e.g. HarvardMadSys/*>
   ```

   The GitHub App the platform holds must be installed on the target
   repository — the runner clones with a read-only installation token.
3. **In `.env.oncall`**:

   ```text
   CODEX_ONCALL_DISPATCH_BACKEND=cloud-agent
   CODEX_ONCALL_AGENT_BASE_URL=<control plane origin>
   CODEX_ONCALL_AGENT_DISPATCH_TOKEN=<same token as the control plane>
   CODEX_ONCALL_AGENT_BASE_REF=dev
   CODEX_ONCALL_AGENT_CONSOLE_URL=https://freeinference.org/agents/{job_id}
   ```

   `CODEX_ONCALL_CODEX_MODEL` keeps meaning what it meant; the model must
   resolve for the service account. `CODEX_ONCALL_GITHUB_*` and
   `CODEX_ONCALL_MODEL_BASE_URL` are unused on this backend — the platform
   injects its own gateway address into the sandbox.

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

Build and start the existing Compose stack:

```bash
make build
curl -fsS http://127.0.0.1:8091/healthz
```

The health response must contain `"ready":true`. The relay image contains only
the FastAPI service — the Codex CLI version is pinned inside
`.github/workflows/codex-oncall.yml` (`CODEX_CLI_VERSION`), and each workflow
run analyzes a fresh checkout of `dev`, so there is no image snapshot to keep
in sync.

## Configure the status monitor

The Cloudflare Worker cannot reach the internal Compose hostname. The relay
publishes port `8091` only on host loopback; expose it through a restricted TLS
reverse-proxy route or Cloudflare Tunnel. The status-monitor Worker is owned by
the external [freeInference repository](https://github.com/HarvardMadSys/freeInference);
set its secrets from that repository's checkout:

```bash
cd /path/to/freeInference/services/status-monitor-worker
npx wrangler secret put CODEX_ONCALL_RELAY_URL
npx wrangler secret put CODEX_ONCALL_RELAY_TOKEN
npx wrangler secret put SLACK_WEBHOOK_URL
```

Use the restricted HTTPS URL for `CODEX_ONCALL_RELAY_URL` and retain
`SLACK_WEBHOOK_URL` during rollout. The webhook is used only when the relay
cannot confirm delivery.

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

A successful request returns `202`, posts the synthetic alert immediately, and
starts a `Codex On-Call` run under the repository's Actions tab; the
analysis reply lands in the alert's Slack thread when the run finishes.
Reusing the same fingerprint inside the dedupe window returns
`duplicate: true` without another top-level message. On the first live run,
check the workflow log for the sandbox self-check result.

## Rollout

1. Configure the GitHub secrets, enable the `oncall` profile on staging, and
   configure only one producer.
2. Confirm raw-alert latency, analysis usefulness, workflow duration, and
   false conclusions for at least one week.
3. Enable the production gateway producer while retaining webhook fallback.
4. Add status-monitor delivery after the restricted public TLS route is ready.
5. After the H200 V4 parser fix (PR #939) is deployed (sglang container
   restarted) and verified — non-empty `content`, populated `tool_calls`, and
   `/v1/responses` returning message items — set
   `CODEX_ONCALL_CODEX_MODEL=deepseek-v4-flash` and re-run the smoke test.
6. Consider GitHub Issue and Draft PR actions in a separate change with
   separate credentials, explicit policy gates, and branch protection.
   Automatic merge remains out of scope.
