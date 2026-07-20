# alert-relay-worker

Cloudflare Worker implementation of the opt-in Unified Alert Control Plane V2.
It is the only V2 Slack writer and uses the Slack bot API for the incident
parent, parent updates, recovery replies, Codex analysis replies, and explicit
workflow-failure replies.

Nothing in this directory is deployed or provisioned by this PR.
`wrangler.toml.example` contains placeholders only. V1 remains the default until
an operator explicitly provisions resources and gives a producer both
`ALERT_RELAY_V2_URL` and its environment-specific `ALERT_RELAY_V2_TOKEN`.

## Runtime flow

1. An authenticated producer posts an `AlertEvent` version `2` to `/v2/alerts`.
2. The producer bearer token maps to `staging`, `production`, or `local`.
   Producer-provided environment and Slack text are rejected.
3. D1 enforces alert-ID idempotency and one active incident per
   environment/fingerprint.
4. The first firing creates one Slack parent and one D1 job. Repeated firing
   updates the parent count/last-seen through `chat.update`; it never replies.
5. A Queue consumer resolves a trusted analysis ref and dispatches
   `codex-oncall-v2.yml` with only `job_id`.
6. The workflow fetches the sanitized job, checks out the relay-authenticated
   ref, runs Codex read-only, and callbacks the relay.
7. Resolution posts recovery in the existing thread and updates the parent.
   A later firing creates a new incident ID and parent.

The workflow token can access only `GET /v2/jobs/{job_id}` and
`POST /v2/jobs/{job_id}/complete`. Job fetches never include Slack credentials
or channel/thread coordinates.

## Local checks

```bash
npm install
npm test
npm run typecheck
```

For local Worker execution, copy the template to an ignored `wrangler.toml`,
replace only local resource values, apply the migration locally, and run:

```bash
npm run migrate:local
npm run dev
```

## Provisioning runbook (not performed by this PR)

Provision only after rollout approval:

```bash
cd services/alert-relay-worker
npx wrangler d1 create freeinference-alert-relay
npx wrangler queues create freeinference-alert-jobs
cp wrangler.toml.example wrangler.toml
```

Copy the returned D1 ID into the untracked `wrangler.toml`. Keep the Queue name
consistent with both producer and consumer bindings, then apply the schema:

```bash
npx wrangler d1 migrations apply freeinference-alert-relay --remote
```

Set secrets with Wrangler; never place their values in TOML:

```bash
npx wrangler secret put SLACK_BOT_TOKEN
npx wrangler secret put SLACK_CHANNEL_ID
npx wrangler secret put ALERT_RELAY_V2_STAGING_TOKEN
npx wrangler secret put ALERT_RELAY_V2_PRODUCTION_TOKEN
npx wrangler secret put ALERT_RELAY_V2_LOCAL_TOKEN
npx wrangler secret put ALERT_RELAY_V2_WORKFLOW_TOKEN
npx wrangler secret put GITHUB_TOKEN
```

Use distinct, random producer tokens. Reusing a token for two environments is
rejected because the environment would be ambiguous. The GitHub token should be
fine-grained to this repository with Contents read and Actions write only.

Configure `CODEX_MODEL`, `MODEL_BASE_URL`, `GITHUB_REPOSITORY`,
and `GITHUB_WORKFLOW_FILE` in the copied TOML. `MODEL_BASE_URL` and
`GITHUB_API_BASE_URL` must be credential-free HTTPS URLs. Configure these
GitHub Actions secrets separately:

- `ALERT_RELAY_V2_URL`
- `ALERT_RELAY_V2_WORKFLOW_TOKEN`
- `CODEX_ONCALL_MODEL_API_KEY`

Deploying the Worker and setting producer variables are separate, explicit
operations. Roll out one environment token at a time and verify one complete
firing/repeat/resolved lifecycle before expanding.

## Trusted ref policy

Staging uses the producer's full deployment SHA only after GitHub proves it is
an ancestor of `dev`; otherwise it analyzes `dev`. Production applies the same
rule against `main`. Local always analyzes `dev`. The chosen `analysis_ref` is
stored in D1 before dispatch. The parent shows `branch@pending` before this
validation, `branch@<validated SHA>` after success, or
`branch@unavailable (analysis ref: branch)` after fallback.

The workflow definition itself is dispatched from `dev` for staging/local and
from `main` for production. The only workflow input remains `job_id`.

GitHub registers `workflow_dispatch` only when the workflow file exists on the
default branch. Before enabling staging, land
`.github/workflows/codex-oncall-v2.yml` on `main` in a separate dormant,
workflow-only bootstrap PR. That bootstrap must not provision the Worker or
enable a producer. Staging dispatches with `ref=dev`, so it still executes and
analyzes the approved `dev` revision.

## Failure behavior

Queue dispatch is retried. On its final failure the relay persists the failed
job and posts an unavailable reply in the incident thread. Workflow callbacks
with `status: failure` require an HTTPS run URL, which the relay includes in an
explicit thread reply. Completion is idempotent.

Queue delivery has exactly three total attempts: the initial delivery and the
two retries configured by `max_retries = 2` in `wrangler.toml.example`.

The Worker does not create issues or PRs, merge code, change operations, or
write repository files. Slack bot display name and avatar come exclusively
from the bot profile.
