# Issue #1337 Deliverable Report

## Root Architectural Interpretation

Issue #1337 asks to "bake human traffic detection into router." Within HybridInference's existing architecture, this is interpreted as:

**A bounded, evidence-based traffic classifier that derives an automation-likelihood signal from request evidence already observed by the gateway, exposed to routing policy via structured context.**

This is NOT:
- A CAPTCHA system
- A browser fingerprinting system  
- An ML model
- A proof of human identity

This IS:
- A conservative signal derived from request cadence, concurrency, shape repetition, session continuity, and the existing client-tool taxonomy
- A narrowly scoped input to routing policy, with a conservative scheduling policy
- Deterministic, side-effect free, and cheap

## Files Changed

| File | Type | Description |
|------|------|-------------|
| `apps/backend/serving/utils/traffic_classifier.py` | New | Core classifier module |
| `apps/backend/serving/utils/traffic_state.py` | New | Bounded per-process observation state |
| `apps/backend/serving/utils/context.py` | Modified | Resets traffic metadata at request boundaries |
| `apps/backend/serving/servers/concurrency.py` | Modified | Exposes the admitted in-flight count as evidence |
| `apps/backend/serving/servers/routers/completions.py` | Modified | Collects evidence and passes typed routing options |
| `apps/backend/serving/servers/routers/anthropic_messages.py` | Modified | Covers the native Messages path |
| `apps/backend/routing/protocols.py` | Modified | Carries typed traffic routing hints |
| `apps/backend/routing/traffic_policy.py` | New | Shared conservative scheduling policy |
| `apps/backend/routing/routers.py` | Modified | Applies the policy in FixedRouter dispatch |
| `apps/backend/routing/routewise/router.py` | Modified | Carries traffic metadata through RouteWise decisions without applying priority |
| `apps/backend/serving/analytics/automation_score.py` | Existing source reused | Shared client-tool taxonomy |
| `tests/unit/utils/test_traffic_classifier.py` | New | 39 test functions, including state-boundary tests |
| `tests/unit/utils/test_traffic_evaluation.py` | New | Runnable synthetic evaluation harness and regression test |
| `tests/unit/utils/test_traffic_context.py` | New | Request-scope reset regression test |
| `tests/unit/routing/test_traffic_routing.py` | New | Typed handoff and scheduling-policy tests |
| `tests/unit/serving/test_traffic_handler_contract.py` | New | Serving-handler ordering and routing-handoff guards |
| `tests/servers/test_traffic_classifier_integration.py` | New | Runtime ASGI handler, priority, rejection, routing, and concurrency regressions |
| `docs/agents/plans/2026-09-12-traffic-classifier.md` | New | Architecture documentation |

## Classification Model

```python
@dataclass(frozen=True, slots=True)
class TrafficClassification:
    automation_score: float      # 0.0 (human-like) to 1.0 (automated)
    confidence: float            # 0.0 (no evidence) to 1.0 (strong evidence)
    class_hint: TrafficClass     # UNKNOWN | LIKELY_HUMAN | LIKELY_AUTOMATED
    reasons: tuple[str, ...]     # contributing signal codes

class TrafficClass(enum.Enum):
    UNKNOWN = "unknown"
    LIKELY_HUMAN = "likely_human"
    LIKELY_AUTOMATED = "likely_automated"
```

## Exact Signals Used

| Signal | Source | Weight | Bounded | Documentation |
|--------|--------|--------|---------|---------------|
| Request cadence | `inter_arrival_ms` | 0.30 | 0.1–0.9 | Short inter-arrivals increase the score |
| Concurrency | `concurrent_requests` | 0.25 | 0.1–0.9 | Parallel in-flight requests increase the score |
| Shape repetition | `shape_repeat_count` | 0.25 | 0.05–0.8 | Repeated structural request fingerprints increase the score |
| Session continuity | `session_continuity` | 0.10 | 0.1–0.3 | Continuity weakly reduces the score |
| Client tool | `user_agent` | 0.10 | 0.1–0.85 | Reuses FI's existing interactive/SDK/script taxonomy |

`is_authenticated` is identity/confidence evidence only. Client-IP provenance is
deliberately outside this PR and is not converted into an automation score.
`request_count` is also not a score signal: it multiplies base confidence by
`min(request_count / 5, 1)` so short histories cannot receive a scheduling hint.

## State/Storage Design

**The classifier is stateless; evidence collection is bounded per-process state.** The scoring function is pure and all evidence is passed in. The optional observation tracker:

- caps identities and shapes;
- evicts stale entries lazily and supports explicit cleanup;
- uses a monotonic clock; and
- hashes identity keys so raw identities are not retained in the tracker.

The tracker is process-local, so it is an observation aid rather than a globally
consistent identity or abuse-control system. Auth-disabled requests are not
tracked at all; the transport peer and the shared `anonymous` sentinel are never
used as behavioral identities. The actual authenticated per-user in-flight count
comes from the existing concurrency admission counter.

## Router Integration

The classifier output can be attached to routing context:

```python
from serving.utils.traffic_classifier import classify_traffic, classification_to_metadata

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

The server passes the same values through `RoutingRequestOptions`. FixedRouter
and the native Messages path consume the typed hint at dispatch time: only a
`likely_human` classification with confidence at least `0.70` receives a
one-point preference within the existing prefill-cost tier. The cost tier is
the hard ordering constraint, so a human-looking elephant remains an elephant.
RouteWise carries the hint in decision metadata but does not fabricate a
priority without its own prefill-cost accounting. It can compose with
prefill-aware RouteWise accounting when #1419 (or equivalent) is available;
that is not current runtime behavior. Unknown, automated, malformed, and
low-confidence hints retain the existing priority. This is a scheduling
decision, not a provider-selection rule or an abuse-control decision.

## Trusted Client Identity

This PR does not own the #1036 client-IP contract. Behavioral state is keyed by
authenticated `user_id` only; auth-disabled requests do not use the shared
`anonymous` sentinel or transport peer as a behavioral identity. The classifier
does not inspect forwarding headers directly. Client provenance is not part of
this evidence type; a future shared resolver contract can be added
independently if a routing policy needs it.

The online client-tool score reuses `serving.analytics.automation_score`'s
existing taxonomy, including its human-like treatment of Claude Code, Cursor,
Codex, and similar interactive coding agents.

## Configuration Added

No new configuration is required.

## Tests Added

### Unit Tests (`test_traffic_classifier.py`)

The 39 test functions cover cold start and missing evidence, score bounds,
single-signal non-domination, contradictory evidence, determinism, metadata and
shape-hash behavior, observation-count confidence, authenticated identity, and the observation tracker's
capacity, TTL, injected-clock, reset, session, shape-LRU, and identity-LRU behavior.

The synthetic evaluation module adds a regression test that executes all seven
scenarios through the same `TrafficEvidence` API used by the router.

### Synthetic Evaluation (7 scenarios in `test_traffic_evaluation.py`)

| Scenario | Class | Score | Confidence |
|----------|-------|-------|------------|
| interactive | likely_human | 0.133 | 0.780 |
| agentic_ide | unknown | 0.335 | 0.800 |
| batch | likely_automated | 0.800 | 0.780 |
| burst | likely_automated | 0.744 | 0.780 |
| shared_network_human | likely_human | 0.133 | 0.780 |
| distributed_automation | unknown | 0.678 | 0.780 |
| cold_start | unknown | 0.000 | 0.000 |

## Hot-Path Performance Implications

- **O(1)** scoring per request — fixed number of signal calculations
- **No I/O** — pure computation
- **No locks** — state is process-local and accessed synchronously
- **Memory** — bounded by configured identity and shape caps
- **Memory estimate** — the prior 2,000-identity/100-shape measurement was
  approximately 39.8 MiB; the current 2,000-identity/32-shape defaults
  extrapolate to roughly 13 MiB, with actual usage depending on Python object
  overhead and shape diversity
- **No database/network round trips**

## Privacy Implications

- No persistent or cross-worker tracker state; the optional tracker is transient, bounded, process-local memory
- No raw prompt inspection
- No browser/device fingerprinting
- No raw prompt or session-content storage
- Classification fields are persisted as request-level metadata in existing API
  logs for calibration/observability; bounded behavioral counters remain
  transient and process-local

## Known False-Positive/Negative Cases

### Expected ambiguity and error cases
- **Agentic/IDE usage**: Human-triggered multi-call behavior (e.g., Claude Code,
  Cursor). FI's shared client-tool taxonomy gives recognized interactive coding
  agents a human-like prior; cadence or concurrency may still leave the result
  UNKNOWN. This signal describes traffic shape, not user identity.
- **Batch jobs**: Legitimate batch processing by human operators
- **High-concurrency legitimate services**: Multiple simultaneous requests from a single service

### False Negatives (automated classified as human)
- **Low-rate automation**: Slow, randomized automation may not accumulate enough evidence
- **Distributed automation**: Requests without an authenticated shared identity
  do not accumulate enough state to be classified from cross-request behavior
- **Jittered timing**: Intentional timing randomization reduces cadence signal

## Adversarial Findings

| Attack | Mitigation |
|--------|------------|
| Jittered request timing | Cadence signal bounded, requires other signals for classification |
| Spoofed IP/forwarding headers | This feature reads no forwarding headers and does not use transport peer as behavioral identity |
| Distributed requests across origins | Without a shared authenticated identity, requests do not merge behavioral state |
| Auth-disabled callers | No shared behavioral bucket; anonymous traffic normally remains UNKNOWN |
| State cardinality exhaustion | Identity and shape caps enforce bounded memory; eviction degrades evidence rather than growing state |
| One user using many API clients | Authenticated activity is intentionally grouped by FI's existing user scope |

## Remaining Blockers or Concerns

1. **Limited routing integration**: The signal affects only a bounded FixedRouter/Messages scheduling preference for high-confidence human-like traffic. It does not choose providers, change RouteWise's cost envelope, or enforce quotas; RouteWise currently carries metadata until prefill-cost accounting is available there.

2. **Signal availability**: Authenticated completions and Messages requests collect cadence, request-shape repetition, session continuity, client-tool, and the admitted in-flight count from the existing concurrency limiter. Auth-disabled traffic deliberately loses cross-request state and per-user concurrency evidence.

3. **No ground truth validation**: Synthetic evaluation only. Real-world accuracy cannot be claimed without production traffic analysis.

4. **Threshold tuning**: The score bands (0.30, 0.70) and the separate 0.70 scheduling-confidence gate are conservative defaults and require production calibration.

## Recommended PR Title

```
feat(routing): add traffic classification signal for routing policy (#1337)
```

## Concise PR Description

```
Adds a bounded, evidence-based traffic classifier that derives an
automation-likelihood signal from request evidence already observed by
the gateway (cadence, concurrency, request-shape repetition, session continuity,
and FI's existing client-tool taxonomy).

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

IMPORTANT SEMANTIC DISTINCTION:
This is traffic-pattern classification, NOT proof that a requester is or
is not human. A coding agent is automated but legitimate. A human can be
abusive. A batch job can be legitimate.

Fixes #1337
```
