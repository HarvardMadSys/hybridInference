# Alert Control Plane Worker

This package contains the staged implementation of the unified alert control
plane described in
[`docs/agents/specs/2026-07-20-unified-alert-control-plane-target-design.zh.md`](../../docs/agents/specs/2026-07-20-unified-alert-control-plane-target-design.zh.md).

It owns the canonical alert contract, per-incident Durable Object state,
SQLite outbox, alarm scheduling, deterministic rendering, Slack sink, principal
quota authority, CI-attested deployment registry, and authenticated staging
ingress. `CONTROL_PLANE_MODE=staging-runtime` composes the C1 executors while
keeping public ingress closed. C2 uses the separate
`CONTROL_PLANE_MODE=staging-ingress` gate and opens `/v1/events` only when every
runtime, identity, and registry binding validates. The checked-in example has
no mode or credential, so it remains dormant. No real producer has been
migrated and the existing alert path is unchanged.

## Current Phase C2 boundaries

- Producers submit one canonical `AlertEvent`; trusted environment, source,
  principal, and deployment fields are injected outside the producer body.
- One SQLite-backed Durable Object serializes each incident fingerprint.
- A generation's parent action stays blocked behind a durable principal-quota
  reservation; resolving queues a fenced release before the next generation
  can reserve capacity.
- Every external side effect is represented by a fenced outbox action before
  any adapter can execute it.
- The checked-in Wrangler file is an example only. It contains no account ID,
  route, credential, Slack token, or GitHub token.
- Active staging composition requires `ROUTE_KEY_V1`, `SLACK_BOT_TOKEN`,
  `SLACK_CHANNEL_ID`, `SLACK_SINK_ID`, `PRINCIPAL_ACTIVE_LIMIT`,
  `QUOTA_PENDING_LEASE_MS`, and the `PRINCIPAL_QUOTAS` Durable Object binding.
  Configuration is fail-closed and health responses expose only stable error
  codes, never binding values. Do not add real values to this repository.
- The Slack app needs `chat:write` plus the target conversation's history scope
  (`channels:history`, `groups:history`, `im:history`, or `mpim:history`) and
  membership/access to that conversation.
- Before any producer is enabled, the target workspace must prove that
  `conversations.history` and `conversations.replies` return the full message
  metadata (`include_all_metadata=true`) after post/update/reply. Mock tests do
  not satisfy the strict-single-parent exit criterion.
- Principal quota is wired in C1. A reservation stays pending on its first
  outbox attempt and is confirmed only during reconciliation, so a failed
  incident-side commit cannot immediately create a permanent quota lease.
- `DeploymentRegistryDurableObject` verifies GitHub Actions OIDC with a pinned
  RS256 issuer/JWKS and exact audience, subject, repository ID, owner ID,
  workflow ref, branch ref, environment, event, and hosted-runner allowlist.
  Unknown signing keys force a JWKS refresh; fetch/rotation failures fail
  closed. The ordinary producer credential cannot write this registry.
- A successful CI activation mints a one-hour HMAC capability bound to one
  exact environment, service, deployment ID, artifact digest, principal,
  source, and registry version. Every `/v1/events` request verifies the
  capability and rechecks that exact registry record is still active before
  injecting trusted metadata. Retirement therefore revokes the capability
  immediately even before its expiration.
- The only checked-in caller is the manual staging lifecycle workflow. It
  activates a synthetic deployment, runs firing → repeat → resolved → re-fire,
  verifies the resulting Slack parent/update/recovery/new-generation effects,
  and retires the deployment in an `always()` cleanup step.
- `dispatch_analysis` terminates with `analysis_not_enabled`; C1 never pretends
  that a Codex/GitHub analysis was dispatched.
- Unknown alert types and unknown context fields are rejected. New producer
  migrations must extend the typed contract deliberately.

## Local verification

```bash
npm ci
npm run typecheck
npm test
```

`npm test` only includes `test/**/*.test.ts`; it never discovers the opt-in
suite under `live/` and therefore never calls Slack.

## Authenticated staging lifecycle

The manual **Alert Control Plane Staging Lifecycle** workflow is the C2 exit
gate. Like the Slack readback gate, GitHub can dispatch it at `ref=dev` only
after the reviewed workflow also exists on the default branch through the
normal `dev` → `main` promotion.

The `staging` GitHub environment must provide:

- variable `ALERT_CONTROL_PLANE_STAGING_URL` containing the exact Worker HTTPS
  origin (the dispatching user cannot override this OIDC destination)
- variable `ALERT_CONTROL_PLANE_SLACK_CHANNEL_ID` containing the exact
  readback-approved staging conversation
- `CLOUDFLARE_API_TOKEN`
- `CODEX_ONCALL_SLACK_BOT_TOKEN`
- `ALERT_CONTROL_PLANE_ROUTE_KEY_V1` (at least 32 random bytes)
- `ALERT_CONTROL_PLANE_PRODUCER_SIGNING_KEY_V1` (a different random value of at
  least 32 bytes)

Dispatch the workflow with the exact confirmation
`run-staging-control-plane-lifecycle`. The job has only `contents: read` and
`id-token: write`, runs in the protected `staging` environment, and requires
`ref=dev`. It first deploys a dormant version to apply the additive
`DeploymentRegistryDurableObject` SQLite migration, provisions Worker secrets,
then deploys the fail-closed `staging-ingress` configuration.

The workflow requests a GitHub OIDC token with audience
`alert-control-plane-deployment-attestation`. The registry accepts only this
repository's exact ID/owner ID and
`.github/workflows/alert-control-plane-staging-lifecycle.yml@refs/heads/dev`
under `environment=staging` and `event_name=workflow_dispatch`. The current
repository uses GitHub's default non-immutable subject
`repo:HarvardMadSys/hybridInference:environment:staging`; changing the
repository OIDC subject mode intentionally makes attestation fail closed until
the reviewed allowlist is updated.

The live test visibly leaves two synthetic parent messages (generation 1 and
the re-fire generation 2) plus the generation-1 recovery reply. It verifies
that repeat updated the original parent to occurrence count 2 and that there is
exactly one parent per generation. Default CI never runs this test. To invoke
the same test against already-provisioned credentials:

```bash
export CONTROL_PLANE_URL=https://example.workers.dev
export CONTROL_PLANE_PRODUCER_TOKEN=...
export CONTROL_PLANE_SYNTHETIC_RUN_ID=manual-1
export CONTROL_PLANE_LIVE_CONFIRM=run-staging-control-plane-lifecycle
export SLACK_BOT_TOKEN=...
export SLACK_CHANNEL_ID=C0123456789
npm run test:control-plane-live
```

## Target-workspace Slack readback gate

Run this gate once for each exact bot installation and target conversation
before enabling a producer. It uses the real `SlackSink` with its production
defaults (30-second visibility grace, bounded reconciliation windows, complete
cursor pagination, and a 10-second request timeout).

The Slack app needs `chat:write` and the history scope matching the target
conversation: `channels:history`, `groups:history`, `im:history`, or
`mpim:history`. The bot must be a member of the conversation. Both
`conversations.history` and `conversations.replies` must return complete
metadata when `include_all_metadata=true`.

The command is deliberately separate from normal tests and requires an exact
confirmation value:

```bash
# Populate SLACK_BOT_TOKEN through the operator's secret manager first.
export SLACK_CHANNEL_ID=C0123456789
export SLACK_SINK_ID=slack-staging
export SLACK_LIVE_GATE_CONFIRM=write-synthetic-messages
npm run test:slack-live
```

After the reviewed workflow is promoted through the normal `dev` → `main`
release path, an operator can instead run **Slack Readback Gate** from GitHub
Actions and select `ref=dev` for the staging test. GitHub only dispatches a
workflow that already exists on the repository's default branch, so merging it
to `dev` alone is not sufficient. Enter the same channel ID, sink ID, and
confirmation value. That manual-only workflow uses the existing
`CODEX_ONCALL_SLACK_BOT_TOKEN` repository secret only in the live-test step; it
never exposes the token or runs on a push or pull request. Use this path only
when that secret is the exact bot installation planned for the target sink.

The test visibly leaves one `[LIVE GATE]` synthetic parent in the target
conversation, updates that parent, and adds recovery and analysis replies in
the same thread. It does not delete these audit artifacts.

The reviewed staging installation passed this gate on 2026-07-23 in channel
`C09F2UER4R2`: parent/update `1784780331.287929`, recovery
`1784780335.579209`, and analysis `1784780337.488039`. Independent readback
found exactly one parent and two replies. This evidence applies only to that
bot installation and channel; token, app-installation, or destination changes
must rerun the gate.

For each of the four writes, the transport first verifies that Slack accepted
the write and then deliberately drops the successful response. The sink must
return `uncertain`; the gate subsequently calls only reconciliation until it
finds the exact four-field metadata. It also proves that reconciling the parent
twice yields the same `DeliveryRef`, that the updated parent metadata is
readable, and that both replies have distinct non-parent effect IDs in the
original thread. Retry timing always honors Slack's `Retry-After` through
`reconcileAtMs`; a response without a retry time uses a one-minute fallback.

On success, stdout contains only sanitized evidence: the run, sink, channel,
parent, update, recovery, and analysis IDs. The token and Slack response bodies
are never printed. A pass proves the strict-single-parent readback assumption
for that token/channel at that time. It does not activate the conditionally
registered sink, enable `/v1/events`, activate a producer, or replace staging
lifecycle verification.

The real producer is migrated only after this C2 gate; production remains a
separate approval.

## Later activation gates

- C2 uses the GitHub-OIDC registry verifier and manual synthetic lifecycle
  described above. The live Slack readback gate remains a separate
  prerequisite and does not substitute for the full lifecycle.
- C3 inventories writers by call path. Today that includes status-monitor's
  relay/webhook fallback and the backend `alert_slack` helper used by endpoint
  health, alert rules, the failed-request alerter, and health routes.
- A migrated producer retries the same canonical `event_id` when the control
  plane is unavailable. It must not fall back through V1 relay or a direct
  Slack webhook, and old/new writers must never shadow by both posting.
- Rollback is owner-aware: new fingerprints can return to legacy, while
  control-plane-owned active fingerprints keep routing to the control plane
  until recovery or audited operator close. Turning the control plane dormant
  while those incidents are active is not a safe rollback.
- A log-only/fake sink is safe only in a fully isolated namespace that will be
  discarded. Fake delivery references must never be written into the namespace
  intended for eventual Slack ownership.
