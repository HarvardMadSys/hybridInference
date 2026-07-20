# Alert Control Plane Worker

This package is the dormant Phase 1 implementation of the unified alert
control plane described in
[`docs/agents/specs/2026-07-20-unified-alert-control-plane-target-design.zh.md`](../../docs/agents/specs/2026-07-20-unified-alert-control-plane-target-design.zh.md).

It owns the canonical alert contract, per-incident Durable Object state,
SQLite outbox, alarm scheduling, and deterministic rendering. Phase 1 does not
configure a producer, Slack, GitHub Actions, or a production route.

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

Later phases add the dormant read-only Codex workflow, staging identity and
deployment attestation, then a single-sink staging migration. They are not
enabled by this package.
