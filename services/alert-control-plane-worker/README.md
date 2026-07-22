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

Later phases register the reviewed environment bindings and sink, run a
synthetic Slack lifecycle in staging, and only then enable an authenticated
producer. None of those activation steps are enabled by this package.
