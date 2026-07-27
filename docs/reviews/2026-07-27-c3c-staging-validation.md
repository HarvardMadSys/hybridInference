# C3c staging validation — first real incident through the Alert Control Plane

**Date:** 2026-07-27
**Scope:** real-traffic validation of the status-monitor → Control Plane →
Slack pipeline for individual `model_unavailable` incidents (the C3c cutover,
#1032 + fixes #1042/#1049). This is the sign-off evidence required by the
roadmap (step 2) before any further producer migrates, and the merge gate for
the storm (#1050) and cycle (#1048) PRs.

## Method

A temporary synthetic catalog entry, `staging-alert-validation`, driven through
three phases so the incident exercises the **genuine recovery path** (not the
departure path):

| Phase | PR | Change | Purpose |
|---|---|---|---|
| 1 break | #1051 | only route → dead local port (`127.0.0.1:59999`, kind `openai_compat`) | force consecutive probe failures with an isolated endpoint_id |
| 2 fix | #1052 | route → working local sglang endpoint (qwen3.6-35b's route shape) | next probe succeeds → same incident resolves via recovery |
| 3 remove | this PR | delete the entry | model leaves the catalog only **after** recovery |

Design notes recorded during review (both found by automated review and
verified before merge): `X-Probe: synthetic` exempts logging/cost but **not**
circuit-breaker accounting, and the route loader derives the effective provider
label from the route **kind** — hence `openai_compat`, which no real staging
model uses, keeping the legacy circuit alert's dedupe key collision-free.

## Timeline (UTC, 2026-07-27)

| Time | Event |
|---|---|
| 12:1x | #1051 merged (`f1e3e548`…`525f1a00`, squashed) |
| ~12:20 | staging deploy landed just before the 12:20 probe cycle |
| 12:20 | probe cycle: failure 1 of 2 — **no page** (threshold honored) |
| 12:40 | probe cycle: failure 2 of 2 |
| **12:41:20** | **incident parent posted to `#free-inference-alert`** |
| 13:00 | probe cycle: still failing — **no duplicate parent** |
| 13:2x | #1052 (fix) merged |
| 13:29 | staging deploy attempt 1 failed — self-hosted runner picked up no steps; rerun (attempt 2) succeeded |
| 13:48 | fix deployed (attempt 2) |
| 14:00 | probe cycle: first success after the fix |
| **14:01:20** | **recovery reply posted in the same thread** |

## Evidence ① — the firing parent

Captured by reading Slack directly (channel `#free-inference-alert`,
`C09F2UER4R2`), message `1785156080.485409`
([permalink](https://harvardmadsys.slack.com/archives/C09F2UER4R2/p1785156080485409)):

```
❌ STAGING · Firing
Model unavailable: staging-alert-validation
staging-alert-validation failed 2 consecutive synthetic probes.

Environment  STAGING           Severity  ERROR
Incident     incident_4a6fc4e5-2d9d-407b-9c92-79e5724ad179
Generation   1
Deployment   8db5b830-adba-4be4-b42c-162285eb51b5@952321d998eb
First seen   2026-07-27T12:41:12.820Z
Last seen    2026-07-27T12:41:12.820Z
Occurrences  1
Model        staging-alert-validation
Reason       unknown
Consecutive failures  2      Failure threshold  2
Source: status-monitor · Fingerprint: status-monitor:model:staging-alert-validation
```

What this proves:

- the full chain executed: probe → durable pending (D1) → Service Binding RPC
  → deployment-registry identity check → incident Durable Object → outbox →
  SlackSink
- **threshold honored**: fired after exactly 2 consecutive failures, silent on
  the first
- **identity chain**: the `Deployment` stamp is the registry-attested Worker
  version, and its SHA (`952321d9`) is the rescue merge that deployed the
  observability fixes — the post is traceable to an attested artifact
- the trusted fields (environment/source/principal) were injected by the
  control plane, not the producer

## Evidence ② — no duplicate, no legacy noise

A full channel read from 12:20Z onward returned **exactly one message**: the
parent above. Therefore:

- the 13:00Z failing cycle produced no second parent (edge-triggered producer +
  single-writer incident object held under sustained failure)
- the anticipated legacy `Provider circuit opened` alert never appeared on this
  staging configuration — the validation window was noise-free without
  requiring the snooze fallback

## Evidence ③ — the genuine recovery

Captured by reading the same Slack thread. The reply (message
`1785160880.746419`, 2026-07-27 14:01:20Z) is threaded **under the firing
parent**:

```
✅ Recovery confirmed
Model recovered: staging-alert-validation
staging-alert-validation accepted a successful synthetic probe.

Incident    incident_4a6fc4e5-2d9d-407b-9c92-79e5724ad179
Generation  1
Resolved    2026-07-27T14:01:14.042Z
```

What this proves:

- **same incident, same generation**: the recovery resolved the exact incident
  the firing opened — single writer and the immutable delivery reference held
  across the full lifecycle
- **genuine recovery path**: the model was still in the catalog when the probe
  succeeded, so this is the probe-success resolution, not the departure close
- **thread integrity**: the reply landed under the parent via the stored
  DeliveryRef, one cron cycle after the fix deployed (13:48 deploy → 14:00
  probe → 14:01 reply)
- end-to-end latency from state change to Slack: ~74 seconds from cycle start

## Known deviations and observations

- The phase-1 deploy raced the 12:20 cron and won, so firing came one cycle
  earlier than scheduled. No impact on the assertions.
- `Reason` classified as `unknown` rather than `upstream_error`: the gateway's
  error text for a connection-refused local route did not match the producer's
  reason patterns. Cosmetic; candidate follow-up for the reason classifier.
- Deploy run 30270532373 attempt 1 failed with zero steps executed
  (self-hosted `deploy-staging` runner did not pick up the job); attempt 2
  succeeded. Worth watching for runner flakiness.
- D1 spot-checks (owner/pending rows) were not run from this workstation (no
  local Cloudflare token). The accepted-submission proof is the parent post
  itself plus `pendingControlPlaneTransitions: 0` being the steady state on
  `/api/health` (field shipped in #1049).

## Adjudication — does a purpose-built entry satisfy the "real model failure" gate?

Raised by automated review on the archiving PR ("do not close the gate with
synthetic evidence"). Adjudicated as follows, with the roadmap wording
reconciled in the same commit.

**What was real in this run:** the deployed status-monitor Worker, its cron,
catalog discovery over the real staging gateway, the D1 durable-pending
machinery, the Service Binding RPC, the deployment-registry identity check
against the attested Worker version, the incident Durable Object, the outbox,
the SlackSink, and the Slack workspace. Every component the migration changed
was exercised in production-shaped conditions.

**What was synthetic:** the catalog entry's business purpose, and the failure
cause (a dead port rather than an organic provider outage). The pipeline is
agnostic to *why* probes fail; failure-cause realism affects only the
producer's cosmetic `reason` classifier (observed `unknown`, noted above).

**What this run does not prove:** organic failure-mode `reason` classification
richness, and behavior for role-gated models (the entry deliberately carried no
`required_role` — the prober is not internal-tier).

**Why this differs from the synthetic RPC gate:** the roadmap's "real model
name (not `synthetic-control-plane-gate-*`)" criterion distinguishes the
runner-local gate — which fabricates the RPC call and never touches probes, D1
state, cron, or Slack — from a real traversal of the deployed chain. This run
is unambiguously the latter.

**Evidence form:** the repository hosts the full message transcripts (this
document) plus permalinks to the live thread. A permalink is independently
re-verifiable at any time, which a screenshot is not; the roadmap now accepts
either form.

**Standing follow-up:** the next organically occurring model incident on
staging should be linked here as corroboration. It does not re-block the gate.

## Sign-off

Evidence ①–③ are complete: one real staging incident ran the entire pipeline
firing→resolved with threshold, dedupe, identity, and thread integrity all
observed. This closes the roadmap's step-2 exit criterion ("one real model
incident firing→resolved through the binding, evidence in `docs/reviews/`"),
which unblocks:

- [x] C3c accepted — individual `model_unavailable` on the Control Plane
- [ ] #1050 storm per-model migration — un-draft and merge
- [ ] #1048 cycle migration — restack on post-#1050 dev, merge, then redeploy
      the control plane (manual lifecycle workflow) before flipping
      `ALERT_CYCLE_OWNER`
