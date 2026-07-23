# Alert Control Plane Worker

This package contains the staged implementation of the unified alert control
plane described in
[`docs/agents/specs/2026-07-20-unified-alert-control-plane-target-design.zh.md`](../../docs/agents/specs/2026-07-20-unified-alert-control-plane-target-design.zh.md).

It owns the canonical alert contract, per-incident Durable Object state,
SQLite outbox, alarm scheduling, deterministic rendering, Slack sink, and
principal quota authority. Phase C1 can compose those executors only when
`CONTROL_PLANE_MODE=staging-runtime` and every required binding validates.
The checked-in example has no such mode or credential, so it remains dormant.
Public `/v1/events` still returns `503` in every mode; no producer has been
migrated and the existing alert path is unchanged.

## Current Phase C1 boundaries

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
- Deployment registry ingress and producer authentication remain disabled for
  the identity-wiring phase. C2 must first choose and validate the real
  CI-attestation verifier; an allow-all/fake verifier is not an acceptable
  reason to bind an otherwise unusable registry object in C1.
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

The next phase provisions reviewed staging identity bindings, enables the
authenticated ingress, and runs a synthetic lifecycle. The real producer is
migrated only after that gate; production remains a separate approval.

## Later activation gates

- C2 adds the `DeploymentRegistry` Durable Object only with a reviewed
  GitHub-OIDC or controlled-CI verifier, then exposes authenticated staging
  ingress and runs firing → repeat → resolved → re-fire. The live Slack
  readback gate above remains a separate prerequisite.
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
