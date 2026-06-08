# RouteWise Current Body Router - Design

**Date:** 2026-05-17
**Status:** Draft -> ready for plan
**Author:** RouteWise integration discussion

## Problem

FreeInference needs a production implementation of the current RouteWise paper
and simulator body router. The checked-in `apps/backend/routing/routewise/`
implementation is not the target semantics for this work; treat it as
replaceable scaffolding. The source of truth is the current RouteWise
paper/simulator design:

- Every feasible provider, across API, quota, and concurrency tiers, receives a
  unified effective cost.
- The body router solves a cost-budgeted mean-TTFT LP across all feasible
  providers.
- Quota PD still exists, but only as the quota effective-cost shadow-price
  curve.
- Output length prediction is a lower-level cost-estimation input, not a top
  level routing strategy.
- `L/U` are workload-level API-equivalent request cost percentiles. If a
  RouteWise route has no real API provider, it must use a configured reference
  API price as the price scale.
- Hedging is probability-targeted, multi-checkpoint, and in-flight, but real
  backup dispatch is out of scope for this PR.

This PR builds that body router and wires it to the existing `router: routewise`
strategy name. It deliberately leaves real hedging for a follow-up PR.

## Goals

1. Build the production `routewise` router around current RouteWise semantics:
   unified effective cost and a cost-budgeted mean-TTFT LP across all feasible
   providers.
2. Use the paper/simulator semantics as the sole RouteWise target.
3. Support two price references: a real S_A API provider, or a configured
   reference API price for subscription-only routes.
4. Add a bucket-mean output length predictor for route-time cost estimation.
5. Extend latency profiling so all provider tiers can participate in the LP,
   not only API providers.
6. Preserve existing request execution, fallback, request logging, and adapter
   semantics.
7. Emit enough RouteWise decision metadata to debug and compare against the
   simulator and real-eval implementations.

## Non-goals

- Real backup dispatch, cancellation, or probability-targeted hedging.
- Distributed quota / concurrency state across multiple backend workers. The
  current prod/staging deployment uses a single backend worker, so this is not
  a gate for this PR.
- A cache-locality predictor beyond conservative route-time defaults.
- A full admin UI for RouteWise internals.
- Rewriting `completions.py` or the serving protocol.
- Importing simulator or `experiments/real_evaluation` modules into production
  code.

## Current Integration Points

Relevant existing files:

| File | Current responsibility |
|---|---|
| `config/models.yaml` | Model routes, pricing, provider metadata. |
| `apps/backend/serving/servers/registry.py` | Builds adapters and passes route-level `provider_type` into `ModelConfig`. |
| `apps/backend/routing/model_router_registry.py` | Chooses per-model routers and attaches `FixedRouter`. |
| `apps/backend/routing/routewise/router.py` | Implementation target for the new current RouteWise router. |
| `apps/backend/routing/routewise/lp_solver.py` | Replace with the cost-budgeted mean-TTFT LP. |
| `apps/backend/routing/routewise/predictor.py` | Replace or bypass with bucket-mean output prediction. |
| `apps/backend/routing/routewise/latency.py` | Rolling provider latency profiles and SWRR sampler. |
| `apps/backend/serving/admin/provider_quotas.py` | Existing provider quota dashboard fetchers; RouteWise S_Q should reuse this for provider-side quota truth. |
| `apps/backend/serving/storage/database.py` | `api_logs` schema with model, provider, tokens, cache, TTFT, and cost fields. |
| `apps/backend/serving/storage/utils.py` | Current cache-aware request cost formula. |

## Proposed Architecture

Restructure `apps/backend/routing/routewise/` around the current implementation:

```text
apps/backend/routing/routewise/
  __init__.py
  config.py                 # current RouteWise params and defaults
  candidates.py             # adapter -> ProviderCandidate extraction
  output_predictor.py       # bucket-mean output token predictor
  envelope.py               # L/U bootstrap and online estimator
  effective_cost.py         # API/quota/concurrency effective costs
  lp.py                     # cost-budgeted mean-TTFT LP
  router.py                 # current RouteWiseRouter
```

Keep the strategy name `routewise`:

```yaml
models:
  - id: minimax-m2.5
    router: routewise
    router_params:
      alpha: 0.75
      slo_ms: 3000
      envelope:
        bootstrap_window_hours: 168
        lower_percentile: 10
        upper_percentile: 90
      output_predictor:
        type: bucket_mean
      reference_api_price:
        prompt: "1.2"
        completion: "4.0"
```

If a model needs RouteWise disabled, operators can switch that model to
`router: fixed`. There is no second RouteWise strategy in this design.

## Core Concepts

### Output Length Prediction

Output length prediction estimates the current request's future completion
tokens. It is measured in tokens.

Initial predictor:

```text
key = (model_id, log2_bucket(prompt_tokens))

prediction fallback order:
  1. bucket mean for (model_id, bucket)
  2. model-level mean
  3. global mean
  4. configured cold-start default
```

Route-time prediction is clamped by the request cap when present:

```text
predicted_output_tokens = min(prediction, max_tokens)
```

On completion, update the predictor with actual `completion_tokens`.

### Reference API Price

RouteWise needs an API-equivalent price scale to estimate each request's
opportunity cost:

```text
v_t = prompt_tokens * prompt_price + predicted_output_tokens * completion_price
```

This price scale has two possible sources:

1. If the route has real S_A API providers, use the cheapest API-equivalent
   cost among the current feasible API candidates.
2. If the route is subscription-only, meaning it has only S_Q / S_C providers
   and no real S_A provider, configure a reference API price or explicitly use
   model-level `pricing` as the reference.

In subscription-only mode, the reference API price does not participate in real
routing. It is used only to:

- Compute request value / API-equivalent request cost.
- Update the `L/U` envelope.
- Put S_Q shadow prices on the same USD scale.

If a RouteWise route has no S_A provider and no reference API price, it cannot
enable S_Q economic routing. At most it can degrade into a simple capacity
scheduler, which is not the target of this PR.

Recommended default:

```text
if any S_A provider exists:
  reference = cheapest S_A request cost
else:
  reference = model-level pricing or router_params.reference_api_price
```

### Cost Envelope `L/U`

`L/U` estimate the request-cost scale of the workload. They are measured in
USD, not tokens.

For each historical request `r`, compute the API-equivalent reference cost
under current pricing and the same route-time estimator assumptions:

```text
if S_A candidates exist:
  v(r) = min_j cold_cache_api_cost_j(r)
else:
  v(r) = cold_cache_reference_api_cost(r)
```

Then:

```text
L = P10({v(r)})
U = P90({v(r)})
```

Important details:

- Historical bootstrap uses actual `completion_tokens`, not bucket-mean
  predictions.
- Historical bootstrap should not blindly reuse the actual
  `api_logs.cache_read_tokens` as if every candidate provider would have the
  same cache hit. Cache hits are provider- and session-dependent. Until we have
  a provider-specific route-time cache estimator, use the conservative
  cold-cache assumption for `estimated_cached_input_tokens_j`.
- Do not use `api_logs.cost_usd` directly. It is the cost of the provider that
  actually served the request, not necessarily the cheapest API-equivalent
  opportunity cost.
- Online routing uses the current envelope. The envelope is updated after
  request completion with actual token counts.
- If a pool has too few samples, use configured seeds and keep a
  `sample_count` field in metadata.

Note: `api_logs` is suitable for bootstrapping and updating the `L/U` price
scale, but it should not be the preferred provider quota truth source. S_Q
remaining quota should come from provider-side quota snapshots when available.

### Effective Cost

For a provider candidate `j`:

```text
API:
  c_eff_j = estimated request token cost

Quota:
  c_eff_j = L * (U / L) ** quota_used_fraction

Concurrency:
  c_eff_j = 0 if a slot is available
  infeasible if saturated
```

The quota formula is the retained primal-dual component. PD is no longer a
top-level router decision; it is the quota provider's effective cost.

If there is no API candidate, the LP can still run across S_Q / S_C providers,
but `L/U` must be calibrated by a reference API price. The reference API is not
added to the LP as a candidate.

### S_Q Quota Source

S_Q `used / limit / reset_at` should not live only inside the in-memory
`QuotaManager`. FreeInference already has a provider quota dashboard:

- The Providers tab calls `/admin/provider-quotas`.
- The backend fetches provider-side quota through
  `serving.admin.provider_quotas.gather_all()`.
- The Chutes `Daily requests 0 / 5,000 requests` card is one example of this
  data path.

RouteWise S_Q should reuse this fetcher with:

```text
provider quota snapshot + local optimistic increments
```

Semantics:

1. Refresh provider quota snapshots in the background.
2. Do not call provider quota APIs on the request path.
3. When an S_Q candidate is selected, apply one local optimistic increment so
   repeated requests inside the refresh interval do not overuse quota.
4. After the next successful refresh, reconcile `used/limit/reset_at` from
   provider-side truth and clear matching local increments.
5. If the provider quota snapshot is unavailable, mask the corresponding S_Q
   candidate by default instead of guessing.

The first implementation should only support provider quota signals that map
directly to request quota:

```text
provider = chutes
usage_label = "Daily requests"
unit = requests
```

This supports the narrow initial scope of one model mapped to one Chutes
account/key. ZAI time/token quotas, MiniMax's model/interval-specific labels,
and Featherless `no_quota_api` are out of scope for automatic mapping in the
first version.

`api_logs` can be a fallback or diagnostics source, but it is not the primary
S_Q truth source. After deploy/restart, a new process should recover quota
state from provider quota snapshots instead of starting from `used_today = 0`.

### Prefix Cache Handling

Cache affects RouteWise through API request cost. It does not directly change
the quota shadow-price formula except through the `L/U` envelope calibrated
from estimated API-equivalent request costs.

There are three different cost concepts:

1. Actual billing cost after completion: use actual `cache_read_tokens` and
   `cache_write_tokens` returned by the provider. This remains the source of
   truth for accounting and `api_logs.cost_usd`.
2. Route-time API cost estimate: use estimated cached-token counts because the
   provider has not returned usage yet.
3. RouteWise effective cost: use the route-time API cost estimate for API
   providers, and shadow prices for quota/concurrency providers.

For this PR, route-time cache estimation is conservative for both routing and
`L/U` calibration:

```text
estimated_cached_input_tokens = 0
```

This matches the current RouteWise simulator direction: do not synthesize
provider-local prefix-cache hits without a trustworthy trace or request signal.
It avoids making API routes look artificially cheap before we can prove that a
specific provider will hit cache.

Actual billing and diagnostics remain cache-aware:

```text
actual_cost =
  input_price * (prompt_tokens - actual_cache_read_tokens)
+ cache_read_price * actual_cache_read_tokens
+ output_price * completion_tokens
```

Future work can replace the conservative route-time estimate with a
session-aware cache estimator:

```text
key = (endpoint_id, model_id, session_id or prefix_id)
estimated_cached_input_tokens = recent_cache_read_ratio * prompt_tokens
```

That estimator should be introduced only after the body router is correct,
because over-predicting cache hits can make API providers look too cheap and
distort both LP weights and quota usage.

### LP Body Router

For feasible candidates:

```text
budget = c_min + alpha * (c_max - c_min)

minimize    sum_j pi_j * mean_ttft_j
subject to  sum_j pi_j * c_eff_j <= budget
            sum_j pi_j = 1
            pi_j >= 0
```

Sample the primary provider from the resulting sparse mixture. The LP can be
implemented with the same two-provider support enumeration used by the current
RouteWise simulator, avoiding a PuLP dependency in the production request path.

## Request Flow

At request arrival:

```text
1. Resolve model route entries into ProviderCandidate objects.
2. Estimate prompt_tokens from the request context if not already supplied.
3. Predict output tokens using bucket mean.
4. Read current L/U for the model or routewise pool.
5. Build feasible candidates:
   - API candidates with valid pricing
   - quota candidates with remaining quota
   - concurrency candidates with available slots
6. Compute c_eff for every feasible candidate.
7. Read rolling mean TTFT for every feasible candidate.
8. Solve the RouteWise LP.
9. Sample primary.
10. Commit quota or concurrency only for the sampled primary.
11. Store decision metadata under request_id.
12. Execute through existing BaseRouter flow.
```

At request completion:

```text
1. Update bucket-mean output predictor with actual completion_tokens.
2. Update rolling latency profile with ttft_ms, or a penalized error sample.
3. Update L/U envelope with actual token-count API-equivalent reference cost.
4. Release any committed concurrency slot.
5. Forward RouteWise observation metadata for logs and tests.
```

## Provider Candidate Model

Internal candidate shape:

```python
@dataclass(frozen=True, slots=True)
class ProviderCandidate:
    endpoint_id: str
    model_id: str
    adapter: BaseAdapter
    provider_type: Literal["on_demand", "quota", "concurrency"]
    weight: float
    pricing: Pricing
    routewise_pool: str
    quota_pool: str | None
    concurrency_pool: str | None
```

`endpoint_id` remains the health, latency-profile, and observation key.
`routewise_pool` controls which `L/U` envelope to use. The default is
`model_id`. If a quota subscription is shared across several models, those
models should use the same `routewise_pool`.

## Configuration Additions

Model-level router selection:

```yaml
models:
  - id: minimax-m2.5
    router: routewise
    router_params:
      alpha: 0.75
      slo_ms: 3000
      envelope:
        bootstrap_window_hours: 168
        lower_percentile: 10
        upper_percentile: 90
        min_samples: 100
        seed_l: 0.001
        seed_u: 0.05
      output_predictor:
        cold_start_tokens: 512
        min_bucket_samples: 20
      reference_api_price:
        prompt: "1.2"
        completion: "4.0"
```

Route-level metadata:

```yaml
route:
  - kind: zai
    weight: 1.0
    provider_type: on_demand
    routewise_pool: glm-paid-pool

  - kind: chutes
    weight: 1.0
    provider_type: quota
    routewise_pool: glm-paid-pool
    quota_pool: chutes-glm-daily
    quota_source:
      provider: chutes
      usage_label: "Daily requests"
      unit: requests
    quota:
      limit: 5000
      window: daily
      reset_timezone: UTC

  - kind: featherless
    weight: 1.0
    provider_type: concurrency
    routewise_pool: glm-paid-pool
    concurrency_pool: featherless-glm
    concurrency:
      limit: 4
```

No-on-demand-baseline example:

```yaml
models:
  - id: glm-5-turbo
    router: routewise
    router_params:
      reference_api_price:
        prompt: "1.2"
        completion: "4.0"
    route:
      - kind: chutes
        weight: 1.0
        provider_type: quota
        quota_source:
          provider: chutes
          usage_label: "Daily requests"
          unit: requests
      - kind: featherless
        weight: 1.0
        provider_type: concurrency
        concurrency:
          limit: 4
```

If the route keeps a real P_O baseline, use a standard P_O + S_Q shape:

```yaml
route:
  - kind: zai
    weight: 1.0
    provider_type: on_demand
  - kind: chutes
    weight: 1.0
    provider_type: quota
    quota_source:
      provider: chutes
      usage_label: "Daily requests"
      unit: requests
```

The first implementation may support a narrower subset if current model config
does not yet declare all pool fields. Missing pool IDs default to
`{model_id}:{endpoint_id}` for provider-local state and `model_id` for the
envelope.

## Bootstrap `L/U` From `api_logs`

Add a storage-facing helper for recent successful request samples:

```python
async def fetch_routewise_cost_samples(
    *,
    model_ids: list[str],
    since: datetime,
    limit: int,
) -> list[RouteWiseCostSample]:
    ...
```

Sample fields:

```python
model_id: str
prompt_tokens: int
completion_tokens: int
cache_read_tokens: int
timestamp: datetime
```

The router uses current route pricing or the configured reference API price to
transform those samples into API-equivalent reference costs. This keeps
bootstrap independent of historical provider choices and historical `cost_usd`
values.

Cold start policy:

```text
if sample_count >= min_samples:
  use P10/P90
else:
  use seed_l / seed_u and report envelope_source="seed"
```

## Observability

Attach a `routewise` metadata object to the existing routing metadata:

```json
{
  "version": "current_body",
  "selected_endpoint": "minimax-m2.5:zai",
  "selected_provider_type": "on_demand",
  "alpha": 0.75,
  "budget_usd": 0.0123,
  "envelope": {
    "L": 0.001,
    "U": 0.050,
    "source": "api_logs",
    "sample_count": 4821,
    "pool": "glm-paid-pool"
  },
  "c_eff": {
    "endpoint-a": 0.002,
    "endpoint-b": 0.008
  },
  "lp_weights": {
    "endpoint-a": 0.74,
    "endpoint-b": 0.26
  },
  "lp_status": "optimal",
  "predicted_output_tokens": 640,
  "prompt_bucket": "8192-16383",
  "hedging": {
    "mode": "disabled"
  }
}
```

This PR should not add a new database column. Store this inside the existing
metadata path used by routing observations and request logs.

## Shadow Hedging Placeholder

This PR may compute and log a shadow hedging decision only if it can be done
without changing request execution:

```text
hedging.mode = "shadow" | "disabled"
hedging.would_dispatch = true | false
hedging.backup_endpoint = ...
hedging.checkpoint_ms = ...
```

No backup request is dispatched in this PR.

## Failure Behavior

Fallback order:

1. If the LP fails but feasible candidates exist, choose the lowest mean-TTFT
   candidate within budget.
2. If none are within budget, choose the cheapest feasible effective cost.
3. If no RouteWise candidate is feasible, fall back to the existing
   `BaseRouter`/`FixedRouter` behavior for the model.
4. If envelope bootstrap fails, use seeds and emit `envelope_source="seed"`.
5. If latency profiles are cold, use a configured unprofiled latency penalty
   or existing profile fallback.

Quota and concurrency commit must happen only after the sampled primary is
known. Failed LP candidates must not consume capacity.

## Testing

Unit tests:

- `output_predictor`: bucket selection, fallback order, max-token clamp,
  completion update.
- `envelope`: actual-token bootstrap from samples, P10/P90 calculation,
  seed fallback, invalid sample filtering.
- `effective_cost`: API cache math, quota `exp_lu`, concurrency feasible /
  saturated behavior.
- `lp`: parity with RouteWise simulator examples, sparse support, budget
  boundary cases.
- `router`: provider-category candidate collection, sampling, quota commit, concurrency
  acquire/release, metadata shape.

Integration tests:

- Fake adapters with on-demand + quota + concurrency routes.
- Historical log bootstrap with deterministic `L/U`.
- `router: routewise` uses the current RouteWise body router.
- The existing checked-in RouteWise decision semantics are not preserved as a
  selectable mode.
- Non-streaming and streaming observation paths update predictor and profiles.

Replay validation:

- Build an offline replay harness over recent `api_logs`.
- Compare `routewise` decisions against the RouteWise real-eval
  `BudgetRangePolicy` on the same token/pricing/profile inputs where possible.

## Rollout

1. Land code with `router: routewise` mapped to the current body router.
2. Keep cost adjustment disabled until staging validation passes.
3. Start with a small explicit model set; do not auto-enable every provider.
4. If the route has a real P_O provider, validate with `ZAI on-demand baseline +
   Chutes S_Q` first.
5. If the route has no on-demand baseline, require `reference_api_price` or
   model-level pricing as the reference.
6. For Chutes S_Q, initially map only `Daily requests` and reuse provider quota
   snapshots.
7. Add an S_C provider after slot accounting is verified.
8. Disable RouteWise for problematic models by switching those models to
   `fixed`.

## Open Questions

1. Should `routewise_pool` default to model-level or subscription-level once a
   shared quota pool is configured?
2. Which exact models are in the first RouteWise rollout? Should we start with
   the narrow "one model to one Chutes key" mapping?
3. Should provider quota snapshots refresh every 1 minute, every 5 minutes, or
   adapt to provider reset/TTL metadata?
4. Should online envelope updates use only completed actual-token samples, or
   also tentative route-time predicted samples with later correction?
5. Should cache-aware route-time cost remain conservative at zero cached tokens
   for the first PR, or should session affinity feed a first cache estimator?
6. Should replaced files be deleted outright, or should some pure utilities be
   retained only when they still match current RouteWise semantics?

## Acceptance Criteria

- `router: routewise` selects the current RouteWise body router.
- No second RouteWise mode is exposed.
- Both real P_O/on-demand baseline and no-on-demand reference API price are
  supported as price references.
- `L/U` can be bootstrapped from existing `api_logs` and exposed in decision
  metadata.
- Chutes S_Q uses provider quota snapshots to recover `used/limit/reset_at`, so
  deploy/restart does not reset quota state to zero.
- The router solves a unified provider-category cost-budgeted mean-TTFT LP.
- The router records enough metadata to explain every selected provider.
- Existing request execution, cost logging, fallback, and response wire format
  remain unchanged.
- No real backup hedging dispatch occurs in this PR.
