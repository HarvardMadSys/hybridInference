# Traffic Classification for Issue #1337

## Architectural Interpretation

Issue #1337 asks to "bake human traffic detection into router." Within HybridInference's existing architecture, this is interpreted as:

**A bounded, evidence-based traffic classifier that derives an automation-likelihood signal from request evidence already observed by the gateway, exposed to routing policy via structured context.**

This is NOT:
- A CAPTCHA system
- A browser fingerprinting system
- An ML model
- A proof of human identity

This IS:
- A conservative signal derived from request cadence, concurrency, shape repetition, session continuity, and FI's existing client-tool taxonomy
- A narrowly scoped input to routing policy, with a conservative scheduling policy
- Deterministic, side-effect free, and cheap

## Placement Decision

The classifier lives in `serving/utils/traffic_classifier.py` because:

1. **It operates on request context** — `req_ctx`, auth identity, and the existing concurrency admission count; it deliberately does not own client-IP provenance
2. **It's policy-agnostic** — returns a classification, not a routing decision
3. **It's reusable** — can be consumed by RouteWise, FixedRouter, or future policies
4. **It follows existing patterns** — similar to `request_ip.py` helpers

The router integration point is via `classification_to_metadata()` which produces a dict suitable for `req_ctx.update()` or `RoutingRequestOptions`.

## Classification Model

```python
TrafficClassification(
    automation_score: float,    # 0.0 (human-like) to 1.0 (automated)
    confidence: float,          # 0.0 (no evidence) to 1.0 (strong evidence)
    class_hint: TrafficClass,   # UNKNOWN | LIKELY_HUMAN | LIKELY_AUTOMATED
    reasons: tuple[str, ...],   # contributing signal codes
)
```

### Signals Used

| Signal | Source | Weight | Bounded |
|--------|--------|--------|---------|
| Request cadence | `inter_arrival_ms` | 0.30 | 0.1–0.9 |
| Concurrency | `concurrent_requests` | 0.25 | 0.1–0.9 |
| Shape repetition | `shape_repeat_count` | 0.25 | 0.05–0.8 |
| Session continuity | `session_continuity` | 0.10 | 0.1–0.3 |
| Client tool | `user_agent` | 0.10 | 0.1–0.85 |

`is_authenticated` is identity/confidence evidence, not an automation-score
signal. Client-IP provenance is intentionally outside this PR and is never
bucketed by transport peer. `request_count` multiplies base confidence by
`min(request_count / 5, 1)` so short histories cannot receive a scheduling hint.

### Cold Start Behavior

Insufficient evidence → `UNKNOWN` (not `LIKELY_HUMAN`)

### Thresholds

- `0.00–0.30` → `LIKELY_HUMAN`
- `0.30–0.70` → `UNKNOWN`
- `0.70–1.00` → `LIKELY_AUTOMATED`

## State Design

**The classifier is stateless; evidence collection is bounded per-process state.** The classifier is a pure function and all evidence is passed in. The optional observation tracker:

- caps identities and shapes;
- uses lazy TTL eviction and explicit cleanup;
- uses a monotonic clock; and
- hashes identity keys instead of retaining raw identity values.

The tracker is process-local and is not a globally consistent identity or abuse
control. Auth-disabled requests are not tracked at all; the transport peer and
the shared `anonymous` sentinel are never used as behavioral identities. The
actual authenticated per-user in-flight count comes from the existing
concurrency admission counter.

## Router Integration

The classifier output can be attached to routing context:

```python
from serving.utils.traffic_classifier import (
    TrafficEvidence,
    classify_traffic,
    classification_to_metadata,
)

classification = classify_traffic(TrafficEvidence(
    concurrent_requests=...,
    inter_arrival_ms=...,
    shape_repeat_count=...,
    session_continuity=...,
    is_authenticated=...,
    request_count=...,
))
req_ctx.update(classification_to_metadata(classification))
```

This exposes `traffic_classification`, `traffic_automation_score`, `traffic_confidence`, and `traffic_reasons` to downstream routing policy.

The server carries the same values through typed `RoutingRequestOptions`.
FixedRouter and native Messages dispatch consume the hint at dispatch time:
only a `likely_human` result with confidence at least `0.70` receives a
one-point preference within the existing prefill-cost tier. Elephant ordering
remains dominant. RouteWise carries the hint in decision metadata but does not
fabricate a priority without prefill-cost accounting; it can compose with
#1419 (or equivalent) when that accounting is available.
Unknown, automated, malformed, and low-confidence hints retain the existing
priority. No provider choice, quota, or abuse-control decision is changed.

## Trusted Client Identity

This PR does not own the #1036 client-IP contract. Behavioral state is keyed by
authenticated `user_id` only; auth-disabled requests do not use the shared
`anonymous` sentinel or transport peer as a behavioral identity. The classifier
never inspects forwarding headers directly. Its client-tool prior reuses
`serving.analytics.automation_score`, so Claude Code, Cursor, Codex, and similar
interactive coding agents remain human-like under the shared taxonomy.

## Configuration

No new configuration is required.

## Tests

39 classifier/state unit tests covering:
- Cold start → UNKNOWN
- Interactive traffic not aggressively automated
- High cadence → automated
- High concurrency contribution
- Repetitive workload contribution
- Mixed evidence → uncertainty
- Missing evidence doesn't fail
- Determinism
- Score bounds
- Metadata conversion
- Existing client-tool taxonomy
- Auth-disabled callers do not share observation state
- Bounded-state eviction and TTL behavior

## Synthetic Evaluation

7 scenarios evaluated:
- interactive: `likely_human` (score 0.133)
- agentic_ide: `unknown` (score 0.335)
- batch: `likely_automated` (score 0.800)
- burst: `likely_automated` (score 0.744)
- shared_network_human: `likely_human` (score 0.133)
- distributed_automation: `unknown` (score 0.678)
- cold_start: `unknown` (score 0.000)

## Performance

- **O(1)** scoring per request — fixed number of signal calculations
- **No I/O** — pure computation
- **No locks** — state is process-local and accessed synchronously
- **Memory** — bounded by configured identity and shape caps

## Privacy

- No persistent or cross-worker tracker state; the optional tracker is transient, bounded, process-local memory
- No raw prompt inspection
- No browser/device fingerprinting
- No raw prompt or session-content storage
- Classification fields are persisted as request-level metadata in existing API
  logs for calibration/observability; bounded behavioral counters are retained
  only transiently and process-locally

## Known Limitations

1. **Agentic/IDE traffic** is treated according to FI's existing client-tool taxonomy; recognized interactive coding agents contribute a human-like prior
2. **Auth-disabled traffic** has no per-client behavioral identity and therefore
   normally remains UNKNOWN
3. **Low-rate automation** may not accumulate enough evidence
4. **No ground truth** — synthetic evaluation only

## Adversarial Considerations

- **Jittered timing**: Cadence signal bounded, requires other signals
- **Spoofed headers**: This feature reads no forwarding headers and does not use transport peer as behavioral identity
- **Distributed requests**: Without a shared authenticated identity, requests do
  not merge behavioral state
- **Identity isolation**: Authenticated callers are grouped by existing user
  identity; anonymous callers are never grouped by the shared sentinel or peer
- **State poisoning**: Identity and shape caps bound memory; evictions degrade evidence rather than growing state

## Files Changed

- `apps/backend/serving/utils/traffic_classifier.py` (new)
- `apps/backend/serving/utils/traffic_state.py` (new)
- `apps/backend/serving/servers/routers/completions.py` (modified)
- `apps/backend/serving/servers/routers/anthropic_messages.py` (modified)
- `apps/backend/serving/servers/concurrency.py` (modified)
- `apps/backend/serving/utils/context.py` (modified)
- `apps/backend/routing/protocols.py` (modified)
- `apps/backend/routing/traffic_policy.py` (new)
- `apps/backend/routing/routers.py` (modified)
- `apps/backend/routing/routewise/router.py` (modified)
- `tests/unit/utils/test_traffic_classifier.py` (new)
- `tests/unit/utils/test_traffic_evaluation.py` (new)
- `tests/unit/utils/test_traffic_context.py` (new)
- `tests/unit/routing/test_traffic_routing.py` (new)
- `tests/unit/serving/test_traffic_handler_contract.py` (new)
- `tests/servers/test_traffic_classifier_integration.py` (new)

## Recommended PR Title

```
feat(routing): add traffic classification signal for routing policy (#1337)
```

## PR Description

```
Adds a bounded, evidence-based traffic classifier that derives an
automation-likelihood signal from request evidence already observed by
the gateway (cadence, concurrency, request-shape repetition, session
continuity, and FI's existing client-tool taxonomy).

The classifier is:
- Pure scoring function with a separately bounded, process-local observation tracker
- Deterministic for a given evidence set
- Conservative under uncertainty (UNKNOWN default)
- Exposed to routing policy via typed `RoutingRequestOptions` and request metadata

The signal is carried through typed routing options and consumed conservatively
by FixedRouter and the native Messages dispatch path. Only a high-confidence
human-like result receives a one-point preference within the existing
prefill-cost tier; elephant protection remains dominant. RouteWise carries the
metadata without inventing a priority. Provider selection, quotas, and
abuse controls are unchanged.

Fixes #1337
```
