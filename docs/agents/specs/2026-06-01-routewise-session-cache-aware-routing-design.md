# RouteWise Session-scoped Cache-aware Routing — Design

**Date:** 2026-06-01
**Status:** Draft → needs Juncheng sign-off on the paper-boundary question (see "Relationship to the paper")
**Author:** RouteWise integration discussion (Murphy + agents)
**Supersedes:** the global "prefix cache tree" sketch from the 2026-05-29 discussion. This design is session-scoped, not a global trie.

## Problem

RouteWise currently routes with a cold-cache assumption: the on-demand (S_A)
effective cost uses `estimated_cached_input_tokens = 0` and the cache term was
dropped from `api_request_cost_usd`. For multi-turn / agentic traffic a large
fraction of input tokens are served from the provider's prefix cache, so the
cold cost systematically over-prices a provider that has already warmed this
session's prefix. We want RouteWise to credit that cache benefit so it tends to
keep a session on the provider that warmed it — as a *cost signal*, not a hard
pin — while staying overridable by latency, budget, quota, and health.

## Goals

1. Within the same user / project / session, if provider A successfully served
   the previous turn and this turn shares a long prefix with it, include a
   conservative cache benefit when computing A's candidate cost, so the router
   leans toward routing back to A.
2. Keep everything inside the existing cost–latency LP: cache enters only as a
   reduction of the S_A effective cost, so it competes against latency under the
   `α` budget and can be overridden.
3. Calibrate against ground truth: use normalized provider-returned cached input
   tokens to correct the estimate (closed loop), and measure before activating.

## Non-goals

- No cross-user / cross-project prefix reuse (KV-cache reuse boundary does not
  span tenants; assuming it would invite mis-estimation and privacy cost).
- No global prefix trie in v1 (see "Why no tree in v1").
- No LMCache, no gateway response cache, no semantic cache.
- No change to billing / logging truth: persisted cost continues to come from
  normalized provider usage, not predicted cache savings.
- No separate session-affinity subsystem, and **do not enable the existing
  5-minute hard affinity pin** (it bypasses the cost LP and would override this
  mechanism — see "Affinity comes from the cost formula").
- No assumption that "same provider" guarantees a cache hit — estimate only.

## Relationship to the paper (decision needed from Juncheng)

This mechanism is a **§6 production enhancement, not a rewrite of the paper's
core cost-layer cache term.** The paper / simulator / real-eval use a **uniform
exogenous** cached-token signal applied to all candidates; the cache does not
create provider stickiness there. This design deliberately creates per-provider
stickiness via cache.

The cost math is the same in both: `adjusted = cold − (p_in − p_cache)·expected`
is algebraically the paper's `p_in·(n − cached) + p_cache·cached + p_out·out`
with `cached = expected`. **The only difference is what feeds
`expected_cached_tokens`:** a uniform exogenous signal (paper) vs a
per-`(session, provider)` estimate (this design).

Action: confirm with Juncheng that (a) this is production-only / §6, and (b) the
paper's cache-on evaluation numbers stay on the uniform-signal regime and are not
re-derived from this per-provider mechanism.

## Affinity comes from the cost formula (why there is no affinity subsystem)

Once provider A is warm, its effective cost drops, so the cost layer prefers A.
That *is* emergent session affinity, and it is the principled form because it
remains overridable by latency / budget / health and stays inside the LP.

Two caveats:

- **Strength varies with `α`.** RouteWise minimizes latency subject to a cost
  budget, not cost. The cache discount mainly affects whether A is the cheapest /
  within budget. At low `α` (cost-priority) the pull is strong; at high `α`
  (latency-priority) selection follows the fastest provider and the discount
  barely moves it.
- **The LP can sample a mixture.** Even when A is favored, the LP may output a
  two-provider mix and sample another provider on some turns. That is the LP's
  intended probability semantics, and v1 should not add an extra session-sticky
  override outside the cost formula.

Therefore v1 keeps affinity entirely inside the cost formula. If deterministic
sampling is ever considered as a production stability feature, it should be
specified separately and kept out of the core cache-aware routing algorithm.

## Core abstractions

```text
SessionProviderPrefixMemory
  lookup(scope, current_blocks) -> CacheSignal
  observe(scope, selected_success_blocks, provider_usage) -> None

CacheAwareCostEstimator
  adjust(candidate_cold_cost, CacheSignal, price_delta) -> adjusted_cost
```

`scope` must include at least:

```text
user_hash
project/org_hash
session_hash
provider_id
endpoint_id
model/profile
key_slot_id
cache_affecting_params_hash
```

v1 stores, per `(session, provider)` scope, the last (or last N) successful
request's block sequence — no trie:

```text
last_blocks
prefix_token_sums
last_seen_at
observed hit/miss/unknown counters
last_observed_cached_tokens
```

Nodes store metadata only (hashes + token counts + counters). Never raw prompt,
canonical bytes, token ids, tool-schema text, or credentials. Block hashes use an
HMAC with a per-process secret.

### Why no tree in v1

A trie's value is finding the longest match across *many branching* sequences.
Scoped to one session + one provider, the relevant history is essentially one
append-only sequence (the growing conversation), so matching reduces to comparing
the current request's block list against the last request's — an array walk, not
a tree. The cross-sequence sharing a global tree would exploit is exactly the
cross-user/session reuse we exclude. A per-session trie is a later upgrade only
if intra-session branching (history edits, regen, tool reordering, parallel
sub-threads) proves common.

## Routing flow

```text
1. canonicalize current request into blocks
2. enumerate RouteWise candidates
3. for each candidate: lookup memory[user/session/candidate_scope]   # provider not known in advance
4. matched_prefix_tokens   (deterministic exact block-by-block compare)
5. n_cache = min(matched_prefix_tokens, n_in)   (matched prefix == cached; no hit-probability model)
6. cache_discount = n_cache × (p_in − p_cache) of THAT candidate
7. adjusted_cost = cold_cost − cache_discount
8. router selects with adjusted_cost + existing latency/quota/health
9. on selected success: update that scope's memory with provider usage
```

Cost time does not need to know the winner in advance: each candidate looks up
its own `(session, provider)` memory; the provider that warmed earlier turns has
a populated entry and naturally comes out cheaper.

## Cost formula

A matched prefix is treated as cached — exactly the paper's on-demand effective
cost, with no hit-probability model:

```text
n_cache        = min(matched_prefix_tokens, n_in)        # matched prefix == cached
cache_discount = n_cache × (p_in − p_cache)              # candidate provider's own prices
adjusted_cost  = cold_cost − cache_discount              # floored so cost >= 0
```

This is `c_OD = p_in·(n_in − n_cache) + p_cache·n_cache + p_out·n_out`. There is
deliberately **no `calibrated_hit_rate` or `confidence_discount` multiplier**:
the simulator removed its provider-local hit predictor as too aggressive, and the
paper / Juncheng line is "if classified as a hit, it is a hit — do not model
provider-specific hit probability." Conservatism comes from the scope and the
minimum matched-prefix threshold, not a magic factor. Provider-returned cached
tokens correct the `n_cache` estimate (closed loop); for staged-rollout caution
use a canary fraction, not a per-request factor.

## Guardrails

Enable the cost adjustment only when all hold: same user/session, same
provider/model/key_slot, `matched_prefix_tokens ≥ threshold` (threshold per
provider via allowlist, carrying each provider's minimum cacheable length and
whether it caches at all), `last_seen_at` within TTL, candidate healthy, and the
attempt is a selected non-synthetic success (not a fallback loser / failed
attempt).

Provider usage classification: missing → `unknown`, `== 0` → `confirmed_miss`,
`> 0` → `confirmed_hit`. `unknown` is never treated as a miss.

Key rotation invalidates cache: the key pool rotates keys (~5-min TTL); a
`key_slot_id` change must be treated as cache invalidation, never estimated from
the prior key's history. **Status: `key_slot` is not yet in the scope**.
Therefore guarded cost adjustment must skip endpoints backed by a rotating
multi-key pool until `key_slot` is available in the scope.

## Implementation plan

1. **Prefix memory primitives** — scope, block sequence, longest-prefix compare,
   TTL/LRU cap. Unit tests only; not wired to the router.
2. **Observation hook** — write memory after a selected success (streaming only
   after successful finalize). Failures, cancels, timeouts, and non-selected
   attempts do not write hit/miss.
3. **Route-time cache signal** — behind `prefix_cache_cost_adjustment_enabled`,
   build blocks once, look up eligible on-demand candidates, adjust effective
   cost, and stash the selected candidate's scope for the selected-success
   memory write. There is no separate shadow-only mode.
4. **Metrics / report** — per provider/model/session bucket: `matched`,
   `expected`, `observed_cached_tokens`, hit/miss/unknown, adjustment_applied,
   route_stayed/switched, calibration error.

## Prerequisite gate (pick a target model first)

The cache discount is `expected × (p_in − p_cache)`. `minimax-m2.5` as wired uses
an S_A leg of OpenRouter→DeepInfra with `input_cache_reads: "0"`, so
`(p_in − p_cache) = 0` and the whole mechanism is a no-op; it is also an
aggregator behind which the warm node cannot be pinned. Choose a **direct,
non-aggregator provider with a real cached-input price** as the first target,
otherwise the implementation can ship but observe nothing.

## Acceptance criteria (first version)

Success is not "money saved", and **not "route-stay rate went up"** (that is
circular — applying a stickiness discount raises stay rate by construction).
Measure instead:

```text
conditional on staying: observed_cached_tokens > 0 and close to predicted   # staying actually warms cache
predicted vs observed calibration error, bucketed by provider/scope          # the estimate is honest
unknown rate is controlled
no latency / cost regression; no quota/health/fallback semantics broken
counterfactual benefit validated via the cost-adjustment small-traffic canary
```

## Open decisions

1. **Paper boundary (Juncheng):** confirm this is production-only / §6, and the
   paper's cache-on numbers stay on the uniform-exogenous regime.
2. **Target model:** which direct provider with a real cached price is the first
   target.
