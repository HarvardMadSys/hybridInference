# Alert Control Plane Worker

This package is the dormant Phase 1 implementation of the unified alert
control plane described in
[`docs/agents/specs/2026-07-20-unified-alert-control-plane-target-design.zh.md`](../../docs/agents/specs/2026-07-20-unified-alert-control-plane-target-design.zh.md).

It owns the canonical alert contract, per-incident Durable Object state,
SQLite outbox, alarm scheduling, and deterministic rendering. The Phase B
`SlackSink` implementation is present and unit tested, but it is deliberately
not registered by `src/index.ts`. The runtime still returns `503` from
`/v1/events` and uses `DormantActionExecutor`, so no Slack or other external
action can run after this code is deployed by itself.

## Phase 1 boundaries

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
- A future environment-specific Slack activation needs `SLACK_BOT_TOKEN`,
  `SLACK_CHANNEL_ID`, and a stable `SLACK_SINK_ID`. Do not add their values to
  this repository. The app needs `chat:write` plus the target conversation's
  history scope (`channels:history`, `groups:history`, `im:history`, or
  `mpim:history`) and membership/access to that conversation.
- Before any producer is enabled, the target workspace must prove that
  `conversations.history` and `conversations.replies` return the full message
  metadata (`include_all_metadata=true`) after post/update/reply. Mock tests do
  not satisfy the strict-single-parent exit criterion.
- Deployment registry and principal quota persistence primitives are included,
  but their Durable Object bindings remain disabled until the identity-wiring
  phase.
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

The test visibly leaves one `[LIVE GATE]` synthetic parent in the target
conversation, updates that parent, and adds recovery and analysis replies in
the same thread. It does not delete these audit artifacts.

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
for that token/channel at that time. It does not register the sink in the
Worker, enable `/v1/events`, activate a producer, or replace staging lifecycle
verification.

Later phases register the reviewed environment bindings and sink, run a
synthetic Slack lifecycle in staging, and only then enable an authenticated
producer. None of those activation steps are enabled by this package.
