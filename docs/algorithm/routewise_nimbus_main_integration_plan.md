# RouteWise + Nimbus Integration Plan (v5.2)

## 1. Executive Summary

This document defines the integration strategy for adding RouteWise (cost-aware
multi-provider routing) into the existing serving infrastructure that already
contains Nimbus (SLO-aware local/remote outsourcing).

Architecture principle: **shared execution core, separate routing policies**.
`BaseRouter` provides the unified execution facade (fallback, circuit breaker,
streaming lifecycle, metrics). Each routing policy (`FixedRouter`, `NimbusRouter`,
`RouteWiseRouter`) is a peer subclass that only owns selection logic. They are
mutually exclusive at the per-model level: a given model is managed by exactly
one router at any time.

Key decisions already made:

- **Main remains the protected production branch.**
- **Dev / integration branches** absorb heavy merge and conflict resolution work.
- **RouteWise is implemented incrementally**: Stage 1 (S_Q + S_A), then
  latency-aware provider selection (LP mix + hedging), then Stage 2
  (S_Q + S_C + S_A).
- **Simulation and production code are intentionally independent**, validated
  by replay tests with layered strictness.

Expected outcome:

- A single production codebase promotable to `main` via small reviewed PRs.
- Per-model routing strategy selection via `models.yaml`.
- Independent failure domains: Nimbus bugs do not block RouteWise progress.


## 2. Branch Reality (Validated)

Branches involved:

- `origin/main`: protected production branch.
- `origin/dev`: integration baseline containing Nimbus integration work.
- `origin/murphy/dev/req_queue`: original Nimbus router + outsourcing stack.
- `murphy/dev/simulator`: experiment framework, paper, slides, algorithm docs.
- `origin/murphy/dev/routewise-online`: current RouteWise integration branch.

Execution status:

| Step | Description | Status |
|------|-------------|--------|
| A | Merge `main` into `dev` | Completed |
| B | Merge Nimbus (`req_queue`) into `dev` | Completed |
| C | Create `routewise-online` from `dev`, merge `simulator` | Completed |
| D | RouteWise implementation on `routewise-online` | In Progress (PR-1 through PR-4.1 done) |
| E | Promote stable increments to `main` | Planned |

Promotion policy for Step E:

1. `routewise-online` -> `dev` via small validated PRs.
2. `dev` -> `main` only for reviewed, low-blast-radius slices.
3. Feature flags and rollback-by-config for production safety.


## 3. Design Decision: Shared Execution Core

### 3.1 What we keep

Reuse Nimbus architecture as the shared execution core:

- `BaseRouter`: execution, fallback, circuit breaker, stream hooks, metrics.
- `FixedRouter`: baseline weighted routing.
- `NimbusRouter`: SLO-aware outsourcing (local GPU vs remote API).

RouteWise is added as a peer router:

- `RouteWiseRouter` (new): cost-aware multi-provider routing.

```text
BaseRouter (shared execution core)
+-- FixedRouter      (weighted random)
+-- NimbusRouter     (SLO-aware outsourcing)
+-- RouteWiseRouter  (cost-aware multi-provider)
```

### 3.2 What we avoid

- Do not introduce an extra facade that duplicates `BaseRouter`.
- Do not rewrite Nimbus router internals.
- Do not attempt to "unify" NimbusRouter and RouteWiseRouter into a single
  class. They solve fundamentally different problems:
  - **Nimbus**: "Should this request stay on our GPU or go to a remote API?"
    (latency-driven, binary local/remote)
  - **RouteWise**: "Across multiple remote providers with different pricing
    models, which one minimizes total cost?" (cost-driven, multi-choice)

### 3.3 Runtime dispatch: ModelRouterRegistry

Per-model routing strategy requires a thin dispatch layer that maps each
incoming `model_id` to the correct router instance. This is **not** a new
facade that duplicates `BaseRouter`; it is a lookup table populated at init
time.

```text
Request(model_id, messages, params)
  |
  v
ModelRouterRegistry.get_router(model_id)
  |-- model_id="llama-3.3-70b" -> RouteWiseRouter instance
  |-- model_id="qwen3-coder-30b" -> NimbusRouter instance
  |-- model_id="glm-5" -> FixedRouter instance (global default)
  |
  v
router.chat_completion(model_id, messages, params)
  |
  v
BaseRouter execution path (fallback, circuit breaker, metrics)
```

**Initialization** (`bootstrap.py`):

1. Parse `models.yaml`. For each model, read its `routing_strategy` field
   (or fall back to the global default from `settings.py`).
2. Group models by strategy. Create one router instance per strategy that
   has at least one model assigned:
   - `FixedRouter`: always created (serves as default and execution core).
   - `NimbusRouter`: created if any model specifies `nimbus`.
   - `RouteWiseRouter`: created if any model specifies `routewise`.
3. Register routes on the appropriate router. Each model's adapters are
   registered only on its assigned router.
4. Populate `ModelRouterRegistry` with `{model_id: router}` mapping.

**Request path** (`completions.py`):

1. Extract `model_id` from request.
2. `router = registry.get_router(model_id)` (O(1) dict lookup).
3. Call `router.chat_completion(...)` or `router.stream_chat_completion(...)`.
4. After response, call `router.record_observation(obs)`.

**`AppServices`** (`deps.py`):

The `AppServices` container replaces the current `router` + `nimbus_router`
pair with a single `registry: ModelRouterRegistry` field. The completions
endpoint accesses the registry via dependency injection.

```python
@dataclass
class ModelRouterRegistry:
    _routers: dict[str, BaseRouter]     # model_id -> router
    _default: BaseRouter                # FixedRouter (global fallback)

    def get_router(self, model_id: str) -> BaseRouter:
        return self._routers.get(model_id, self._default)

    def record_observation(self, model_id: str, obs: RoutingObservation) -> None:
        self.get_router(model_id).record_observation(obs)
```

This is the **minimum viable dispatch layer**. It adds no execution logic,
no fallback logic, and no streaming logic. All of that remains in `BaseRouter`.

### 3.4 Future: layered composition

A model that has both local GPUs and multiple remote subscription providers
would ideally use Nimbus for local/remote triage and RouteWise for remote
provider selection. This layered composition is deferred to a future phase.
The current design supports mutual exclusion only (one router per model).


## 4. Phase 0 Frozen Decisions

These semantics are locked before any code is written. They were derived from
multi-round design review covering algorithm correctness, systems engineering,
and paper alignment.

### 4.1 Strategy granularity: per-model, mutually exclusive

| Decision | Conclusion |
|----------|------------|
| Granularity | Per-model, defined in `models.yaml` |
| Overlap | Mutually exclusive: one router per model |
| Layered composition (Nimbus + RouteWise) | Future work |

### 4.2 S_C slot lifecycle

Concurrency slots for remote subscription providers (`S_C`) track the full
HTTP request lifetime, not just prefill.  The K=0 binary gate model means
no queue, no eviction, no value-density tracking -- just admit or reject.

Acquire happens in `_select_adapter()` (selection-time commit), **before**
the HTTP request is dispatched to the upstream provider.  Release happens in
`_execute_adapter()` / `_execute_stream_adapter()` finally blocks.

| Event | Action |
|-------|--------|
| Selection commit (`try_acquire()` in `_select_adapter`) | Acquire slot |
| Stream completes normally | Release slot in `_execute_*_adapter` `finally` |
| Provider error (after dispatch) | Release slot in `_execute_*_adapter` `finally` |
| Client disconnect / cancel (after dispatch) | Release slot in `_execute_*_adapter` `finally` |
| Pre-execution failure (local exception before upstream I/O) | Slot was already acquired; released in `finally` |
| S_C not selected (full, disabled, or gain < 0) | No slot acquired |
| Fallthrough path (no S_A available, resources exhausted) | No slot acquired; returns None |

**Note on pre-dispatch failures**: Because `try_acquire()` runs at selection
time, a slot is held even if the subsequent execution fails before an HTTP
request reaches the upstream (e.g., local validation error, synchronous
exception in adapter setup).  The slot is still
released promptly in the finally block, so the window of unnecessary occupancy
is bounded by the local error-handling latency (sub-millisecond).  This is a
deliberate simplification: splitting acquire into "reserve at selection / confirm
at dispatch" would add complexity with negligible benefit for K=0 where slot
duration is dominated by upstream HTTP round-trip time.

The fallthrough path at the end of `_select_adapter()` is restricted to S_A
adapters only -- it never returns an S_C adapter without a prior
`try_acquire()`, preventing spurious `release()` in the finally block.

This differs from Nimbus shadow queue semantics (first-token release) because
remote `S_C` providers count live HTTP connections, not prefill load.

### 4.3 Quota-on-failure semantics

Quota for `S_Q` providers is consumed at **selection time** in
`_select_adapter()`, before the HTTP request is dispatched upstream.  This
is the same selection-time commit model as S_C slot acquisition (Section 4.2).

Once `consume()` is called, the quota slot is gone regardless of what happens
downstream.  There are no refunds.  This is intentional: the PD threshold
already accounts for the risk of wasted quota via the exponential shadow price.

| Scenario | Quota consumed? | Refund? |
|----------|:-:|:-:|
| S_Q selected, provider succeeds | Yes | N/A |
| S_Q selected, provider returns 5xx, fallback to S_A | Yes | No |
| S_Q selected, local exception before upstream I/O | Yes | No |
| S_Q selected, request cancelled before upstream I/O | Yes | No |
| S_Q not selected (v_t < theta_Q or quota exhausted) | No | N/A |

**Why selection-time commit?** Splitting into "reserve at selection / confirm
at dispatch" would require threading quota-reservation state through the
BaseRouter execution path and handling reservation expiry on timeout.  The
simpler model (consume immediately, no refund) matches the online knapsack
formulation in the paper where each decision is irrevocable.

### 4.4 Cost accounting: three-layer separation

Cost metrics are split into three orthogonal layers to align with the paper's
formulation (subscription fee as sunk cost, objective = minimize API spend).

| Layer | Metric name | Description |
|-------|-------------|-------------|
| Cash cost | `api_cash_cost_usd` | Actual API spend (paper objective function) |
| Resource accounting | `quota_burned`, `sc_slots_used` | Constraint tracking |
| Opportunity loss | `quota_wasted` | Quota burned on failed requests (for diagnostics) |

### 4.5 Predictor update rules

The output-length predictor learns from final successful outcomes only.

| Condition | Update predictor? |
|-----------|:-:|
| Primary succeeds with reliable usage | Yes |
| Primary fails, fallback succeeds with reliable usage | Yes (use fallback's usage) |
| All branches fail, no reliable usage | No |
| Streaming interrupted, partial usage unreliable | No |

The predictor learns "how many tokens does this type of request produce",
regardless of which provider ultimately served it.

### 4.6 S_A value baseline

When multiple API providers exist for the same model with different prices,
`v_t` (the value of using a subscription) is computed relative to the
**cheapest available API provider**:

```
v_t = min_{j in S_A} (price_in_j * n_in + price_out_j * predicted_n_out)
```

This prevents overvaluing subscriptions against expensive API providers
and keeps the competitive ratio analysis valid.

**No-S_A edge case**: If a model has no S_A adapter, `v_t` is `inf`.  The
gain computation still works (gain_C = inf when slots exist), so S_C and S_Q
route correctly when resources are available.  When all subscription resources
are exhausted, the router returns `None` -- it does **not** silently fall
through to an S_C or S_Q adapter that would bypass `try_acquire()` /
`consume()` accounting.  Construction-time validation emits a warning when
a model lacks an S_A baseline.

### 4.7 Hedging execution model

Smart Hedging is implemented via a `HedgedAdapter` (composite adapter pattern),
not via BaseRouter fallback or router-level HTTP calls.

| Aspect | Decision |
|--------|----------|
| Selection vs execution | `_select_adapter()` decides hedge plan; execution is the adapter's job |
| Implementation | `HedgedAdapter` wraps primary + backup adapters behind standard adapter interface |
| Algorithm | SMART_ECONOMIC cost-benefit model (see below) |
| Tiebreaker | Primary wins on tie (cost-optimal by LP solver) |
| Fallback interaction | Hedge and fallback remain separate: hedge is proactive, fallback is failure-recovery |
| Metrics | `HedgedAdapter` reports per-provider metrics via shared `ProviderEventSink` (see below) |

**Hedging algorithm: SMART_ECONOMIC**

The hedge trigger uses a cost-benefit economic model with a single tunable
parameter (`cost_ratio = C_b / V`).  Reference implementation:
`experiment/strategies/smart_hedging.py::smart_hedge_economic()`.

Decision rule -- hedge at elapsed time `t` if and only if:

```
P_viol(t) * F_b(remaining) > C_b / V
```

Where:
- `P_viol(t) = S_p(L) / S_p(t)`: conditional probability that the primary
  violates the SLO, given it has survived to time `t`.
- `F_b(remaining)`: CDF of the backup provider at `remaining = L - t - delta`,
  i.e., probability that the backup finishes within the remaining SLO budget.
- `C_b`: cost of the backup request (from pricing data).
- `V`: penalty for an SLO violation (tunable business parameter).
- `C_b / V` = `cost_ratio`: single parameter controlling hedge aggressiveness.

Key properties:
- **Single parameter** replaces the two-parameter heuristic (`theta` +
  `min_backup_cdf`) from earlier iterations.
- **Economically interpretable**: left side = probability that hedging prevents
  a violation; right side = cost-benefit threshold.
- **Backup-adaptive**: cheap backup (low `C_b`) naturally hedges more
  aggressively; expensive backup hedges conservatively.
- **Parallel execution model**: correctly models the parallel hedge execution
  (primary continues running while backup starts), unlike the serial-assumption
  residual formula.

**Provider health contract**: `HedgedAdapter` must not maintain a shadow set
of health/circuit-breaker metrics. It must report individual provider
success/failure/TTFT events to the **same** accounting system that
`BaseRouter` uses for single-adapter execution. Otherwise, when the primary
fails and the backup wins, BaseRouter only sees "logical adapter succeeded"
and the primary's failure signal is lost from provider health tracking.

Implementation: define a `ProviderEventSink` interface (or equivalent internal
callback) that `HedgedAdapter` calls for each real provider outcome:

```python
class ProviderEventSink(Protocol):
    def on_provider_success(self, provider_id: str, ttft_ms: float) -> None: ...
    def on_provider_failure(self, provider_id: str, error: Exception) -> None: ...
```

`BaseRouter` implements this interface (it already has `_on_success` /
`_on_failure` methods). `HedgedAdapter` receives a reference to the sink
at construction time and calls it for both primary and backup outcomes.
This is frozen before Phase 2.5b implementation begins.

**Cost accounting for hedged requests**:
- **Real cost**: both primary and backup are billed when hedge triggers.
  `total_cost = c_primary + c_backup`.
- **Cancel-aware cost** (estimated): assumes the losing request is cancelled
  and only `c_winner` is billed.  This is a counterfactual estimate, not
  actual billing.  Must be clearly labeled as "estimated" in metrics/logs.

### 4.8 Replay test scope

Replay test strictness varies by phase:

| Phase | Replay assertion |
|-------|-----------------|
| Stage 1 (S_Q + S_A) | Exact decision equivalence under deterministic replay |
| Stage 2 (S_Q + S_C + S_A) | Equivalence under deterministic harness + invariants on accounting and cost totals |
| Latency routing | Statistical equivalence of provider mix distributions |


## 5. Runtime Configuration Model

### 5.1 Per-model routing strategy

The routing strategy is defined per model in `models.yaml`, not as a global
setting. A global default in `settings.py` provides the fallback for models
that do not specify a strategy.

`settings.py` (global default only, no per-model lists):

```python
class RoutingStrategy(Enum):
    FIXED = "fixed"
    NIMBUS = "nimbus"
    ROUTEWISE = "routewise"

class Settings(BaseSettings):
    routing_strategy: str = "fixed"  # global default for models without override

    # NOTE: nimbus_enabled_models and routewise_enabled_models are REMOVED.
    # Per-model strategy is now defined exclusively in models.yaml.
    # The old nimbus_enabled_models in settings.py is retained only for
    # backward compatibility during migration and will be removed after
    # all models are migrated to models.yaml routing_strategy field.
```

`settings.py` owns only the global default. Per-model routing strategy is
defined exclusively in `models.yaml`. This eliminates dual-source drift
between settings and model config.

`models.yaml` (per-model override):

```yaml
models:
  # Remote-only model: use RouteWise for cost optimization
  - id: llama-3.3-70b-instruct
    routing_strategy: routewise      # <-- per-model override
    route:
      - kind: chutes
        weight: 1.0
        base_url: ${CHUTES_BASE_URL}
        api_key: ${CHUTES_API_KEY}
        provider_model_id: "meta-llama/Llama-3.3-70B-Instruct"
        subscription_type: quota     # S_Q
        pricing:
          prompt: "0"
          completion: "0"
      - kind: featherless
        weight: 1.0
        base_url: ${FEATHERLESS_BASE_URL}
        api_key: ${FEATHERLESS_API_KEY}
        provider_model_id: "meta-llama/Llama-3.3-70B-Instruct"
        subscription_type: concurrency  # S_C
        pricing:
          prompt: "0"
          completion: "0"
      - kind: openai_compat
        weight: 1.0
        base_url: https://api.together.xyz/v1
        api_key: ${TOGETHER_API_KEY}
        provider_model_id: "meta-llama/Llama-3.3-70B-Instruct"
        # subscription_type: api (default when omitted)
        pricing:
          prompt: "0.59"
          completion: "0.79"

  # Hybrid local+remote model: use Nimbus for SLO-aware outsourcing
  - id: qwen3-coder-30b
    routing_strategy: nimbus         # <-- per-model override
    route:
      - kind: sglang
        weight: 1.0
        base_url: "http://localhost:8003"
        provider_model_id: "Qwen3-Coder-30B-A3B-Instruct"
        pricing:
          prompt: "0"
          completion: "0"
      - kind: chutes
        weight: 1.0
        base_url: ${CHUTES_BASE_URL}
        api_key: ${CHUTES_API_KEY}
        provider_model_id: "Qwen/Qwen3-Coder-30B-A3B-Instruct"
        pricing:
          prompt: "0"
          completion: "0"

  # Simple single-provider model: use default (fixed)
  - id: glm-5
    # routing_strategy: omitted, inherits global default ("fixed")
    route:
      - kind: zhipu
        weight: 1.0
        base_url: https://api.z.ai/api/coding/paas/v4/
        api_key: ${ZAI_API_KEY}
        provider_model_id: "glm-5"
```

### 5.2 RouteWise policy sub-configuration

RouteWise-specific parameters are defined in a dedicated config file
(`config/routewise.yaml`) or as environment variables, separate from
`models.yaml`:

```yaml
routewise:
  decision_rule: lapd             # pd | lapd
  predictor: ema                  # ema | histogram
  risk_quantile: 0.10             # quantile for LCB in lapd mode

  quota:
    daily_quota: 5000             # S_Q: requests per day
    monthly_fee: 20.0             # for amortized cost reporting only
    reset_timezone: UTC           # daily quota reset boundary

  concurrency:
    enabled: false                # Stage 2 flag; false for Stage 1
    limit: 8                      # S_C: max concurrent requests
    monthly_fee: 25.0

  shadow_price:
    L_seed: 0.001                 # initial lower bound (USD)
    U_seed: 0.500                 # initial upper bound (USD)
    adaptive: true                # update L/U from sliding window
    window_hours: 24              # sliding window for L/U adaptation
    min_ratio: 10                 # floor for U/L to prevent degenerate thresholds

  latency:
    slo_sec: 3.0                  # SLO deadline for LP tail constraint
    target_cdf: 0.99              # target CDF for LP mixing
    hedge_mode: shadow            # "shadow" | "economic" | "disabled"
    hedge_cost_ratio: 0.1         # C_b/V for SMART_ECONOMIC trigger
```

Separation of concerns:

- `models.yaml` owns: provider endpoints, model support, `subscription_type` tag, per-route pricing.
- `routewise.yaml` owns: policy parameters (decision rule, predictor, shadow price config, quota/concurrency limits).

### 5.3 Subscription type mapping

Each route entry in `models.yaml` gains an optional `subscription_type` field:

| Value | Meaning | Paper notation |
|-------|---------|:-:|
| `quota` | Daily-quota subscription | S_Q |
| `concurrency` | Concurrency-limited subscription | S_C |
| `api` (default) | Pay-per-token API | S_A |

At init time, `RouteWiseRouter` reads the `subscription_type` tag from each
route entry. Routes without the tag default to `api`.


## 6. RouteWise Decision Flow

### 6.1 Two-layer architecture

RouteWise decisions are split into two layers, both internal to
`RouteWiseRouter._select_adapter()`:

```text
Layer 1 (Cost): Which provider TYPE minimizes cost?
  S_C (if slots available; gain_C = v_t)
  S_Q (if value > shadow price and quota remaining; gain_Q = v_t - theta_Q)
  S_A (baseline tier, if configured; gain_A = 0)

Layer 2 (Latency): If S_A, which specific API provider?
  LP-based provider mixing (minimize cost subject to SLO constraint)
  Smart Hedging via HedgedAdapter (Phase 2.5 only)
```

Note: S_A is the expected baseline for value estimation.  Models without
an S_A adapter are supported (see Section 4.6) but return `None` when
subscription resources are exhausted.

Layer 1 is the core cost optimization (Phases 2-3).
Layer 2 is the latency-aware provider selection (Phase 2.5).

### 6.2 Layer 1: Cost routing decision flow

Priority cascade: **S_C > S_Q > S_A**.

Since `theta_Q > 0` always (`L_seed > 0`), `gain_C = v_t > v_t - theta_Q = gain_Q`
whenever both S_C and S_Q are available.  S_C always wins over S_Q.

```text
Request(model_id, messages, params) arrives
|
|-- 1. Predict output length
|     pred = predictor.predict(model_id, input_tokens)
|     -> QuantilePrediction(q10, q50, q90)
|
|-- 2. Estimate API value (cost saved by using a subscription)
|     baseline_cost = min(S_A provider prices) * (n_in + predicted_n_out)
|     if decision_rule == pd:   v_t = baseline_cost using pred.q50
|     if decision_rule == lapd: v_t = baseline_cost using pred.q10 (LCB)
|
|-- 3. Compute gains (K=0 binary gate for S_C)
|     gain_C = v_t        if S_C adapters exist AND slots available, else -inf
|     gain_Q = v_t - theta_Q  if S_Q adapters exist AND v_t >= theta_Q
|                               AND quota remaining > 0, else -inf
|     gain_A = 0          (baseline)
|
|-- 4. best = max(gain_C, gain_Q, gain_A)
|
|-- 5a. If S_C: try_acquire() -- if race lost, fall through to S_Q/S_A
|-- 5b. If S_Q: consume() -- selection-commit one quota slot
|-- 5c. If S_A and multiple providers: invoke Layer 2
|-- 5d. If S_A and single provider: return that adapter
|-- 5e. If no eligible S_A adapter: return None
```

**Important invariant**: The last-resort fallthrough path at the end of
`_select_adapter()` only returns S_A adapters.  It never returns S_C or S_Q,
because doing so would bypass `try_acquire()` / `consume()` and break resource
accounting.  When all resources are exhausted and no S_A exists, `None` is
returned.

### 6.3 Layer 2: Latency-aware provider selection (Phase 2.5)

When Layer 1 selects `S_A` and multiple API providers are available:

```text
|-- LP solver: minimize expected cost subject to P(latency <= SLO) >= 1 - alpha
|     -> provider mixing weights pi_j (at most 2 nonzero, by LP sparsity)
|
|-- SWRR sampler: select primary provider from mixing weights
|
|-- Hedging decision (if enabled, SMART_ECONOMIC):
|     Compute hedge threshold h* via cost-benefit grid search:
|       h* = min{t : P_viol(t) * F_b(L-t-delta) > cost_ratio}
|     If h* < inf: return HedgedAdapter(primary, backup, h*)
|     Else: return primary adapter (hedge not cost-justified)
```

`HedgedAdapter` implements the standard adapter interface. BaseRouter sees it
as a single adapter. Internally it manages:

- Immediate dispatch of primary request
- Timer at h* seconds; if primary has not produced first content token, dispatch backup
- Race resolution: first adapter to produce non-empty content token wins
- Tiebreaker: primary wins (cost-optimal by LP solver)
- Loser cancellation and cleanup

### 6.4 Key differences from simulation code

The algorithm logic (threshold function, shadow price, value estimation) is
identical to `experiment/strategies/online/primal_dual.py`. Production
differences:

| Aspect | Simulation (`experiment/`) | Production (`routing/routewise/`) |
|--------|---------------------------|-----------------------------------|
| Time model | Simulated day counter | Wall-clock with timezone-aware daily reset |
| Batch vs per-request | Full trace in a loop | One request at a time, async |
| Output tokens | Known after step | Predicted; revealed after response |
| Concurrency (S_C) | Simulated finish_time | Real in-flight tracking via async lifecycle |
| Thread safety | Single-threaded | Must be async-safe (asyncio locks) |
| Value baseline | Single API price | min() over available S_A providers |

The two codebases are **intentionally independent**. Simulation code stays in
`experiment/` for offline evaluation. Production code in `routing/routewise/`
is purpose-built for real-time serving.


## 7. Observation and Feedback Interface

### 7.1 RoutingObservation (V1: minimal 6 fields)

After a request completes, the endpoint constructs a `RoutingObservation` and
passes it to the active router via `router.record_observation()`. This is a
router-agnostic handoff: the endpoint does not know which router policy is
active.

```python
@dataclass
class RoutingObservation:
    selected_provider: str           # provider chosen by _select_adapter()
    selected_provider_type: str      # "quota" | "concurrency" | "api"
    final_provider: str              # same if no fallback; different if fallback used
    fallback_used: bool
    quota_committed: bool            # True if S_Q quota was consumed at selection time
    usage: Usage | None              # None if unreliable or missing
    api_cash_cost_usd: float         # actual API spend for this request
```

`quota_committed` is required in V1 (not deferred) because the selection-commit
semantics defined in Section 4.3 consume quota at selection time (irrevocably).
This field lets downstream observers distinguish "S_Q selected and quota burned"
from "S_Q was a candidate but not selected".

Each router consumes the observation as it sees fit:

- **RouteWiseRouter**: updates predictor (if usage is reliable), updates quota
  ledger (if `quota_committed`), updates latency profiles.
- **NimbusRouter**: updates outsourcing metrics.
- **FixedRouter**: no-op (or basic health tracking).

### 7.2 Future extensions (not in V1)

Fields to add in Stage 2 and Phase 2.5:

- `sc_slot_duration_sec: float | None`
- `hedge_triggered: bool`
- `hedge_winner: str | None`  (primary / backup)
- `hedge_cost_real_usd: float | None`  (c_primary + c_backup when hedged)
- `hedge_cost_cancel_aware_usd: float | None`  (estimated, c_winner only)
- `error_class: str | None`
- `usage_is_estimated: bool`
- `latency_ttft_ms: float | None`
- `latency_e2e_ms: float | None`


## 8. Implementation Phases

### Phase 0: Interface freeze and ADR (0.5 day)

Deliverables:

- Frozen decisions table (Section 4) accepted.
- Per-model routing strategy config schema finalized.
- `RoutingObservation` interface agreed.
- RouteWise config schema (`routewise.yaml`) agreed.

Exit criteria:

- This document (V5) approved as the authoritative ADR.

### Phase 1: Nimbus integration hardening on dev (Completed)

### Phase 1.5: Research asset sync into routewise-online (Completed)

### Phase 2: RouteWise Stage 1 -- S_Q + S_A (4-6 days)

Deliverables:

- `RouteWiseRouter` subclass of `BaseRouter` with PD and LA-PD decision rules
  (single PR, switchable via config).
- Predictor module (EMA first, optional histogram).
- Quota state management (wall-clock reset, selection-commit semantics).
- `RoutingObservation` interface and `record_observation()` on `BaseRouter`.
- Per-model `routing_strategy` parsing in `registry.py` and `bootstrap.py`.
- Replay tests: deterministic decision trace matching simulation output.

Exit criteria:

- Exact decision equivalence with simulation on deterministic replay.
- Stable API behavior under load and provider failures.
- Quota accounting correct across day boundaries and failure/fallback paths.

### Phase 2.5: Latency-aware provider selection for S_A (3-5 days)

Deliverables:

- `ProviderProfile`: mixed-window (short 15min + long 3hr) latency estimation.
- LP solver: minimize cost subject to SLO CDF constraint.
- `SWRRSampler`: smooth weighted round-robin for deterministic provider mixing.
- Shadow hedging mode: compute hedge thresholds, log decisions, but do not
  dispatch backup requests. Collect data for Phase 2.5b.

Exit criteria:

- LP mixing decisions demonstrably improve cost-latency Pareto frontier
  compared to single-provider baseline.
- Shadow hedge logs show correct threshold computation.

### Phase 2.5b: Real hedging (2-3 days, after shadow data validates)

Deliverables:

- `HedgedAdapter` composite adapter with SMART_ECONOMIC trigger:
  `P_viol(t) * F_b(remaining) > cost_ratio`.
- `cost_ratio` config parameter (default from simulation ablation sweep).
- Streaming race resolution (first non-empty content token wins, primary tiebreaker).
- Loser cancellation.
- Hedge-specific metrics (hedge_rate, backup_win_rate, cost_overhead).
- Dual cost reporting: real cost (both billed) and cancel-aware cost (estimated).

Exit criteria:

- Correct behavior under: primary fast, backup fast, primary timeout,
  client disconnect, provider error.
- Hedge rate and cost overhead match shadow-mode predictions.
- `cost_ratio` monotonically controls hedge rate (higher = fewer hedges).

### Phase 3: RouteWise Stage 2 -- S_Q + S_C + S_A (5-8 days) **(Completed)**

Deliverables:

- `ConcurrencyManager` with K=0 binary gate (simplest correct model; upgrade
  path to K>0 CAPQ is clean since K=0 is the degenerate case).
- Three-way gain comparison: `gain_C = v_t` vs `gain_Q = v_t - theta_Q` vs
  `gain_A = 0`.  Priority cascade: S_C > S_Q > S_A.
- S_C slot lifecycle: acquire via `try_acquire()` at selection time, release
  in `_execute_adapter` / `_execute_stream_adapter` finally block (covering
  stream-end, error, and client disconnect).
- Fallthrough safety: last-resort path restricted to S_A adapters only.
  Construction-time warning when a model has no S_A baseline.
- Thread-safe `ConcurrencyManager`: all reads and writes of `_active`
  protected by `threading.Lock`.

Exit criteria:

- No slot leaks under streaming, cancellation, and concurrent request scenarios.
- Accounting invariants hold: sum(committed) <= daily_quota per window,
  active_slots <= concurrency_limit at all times.
- Replay harness validates cost totals within tolerance vs simulation.
- No-S_A edge cases return None (not an unaccounted subscription adapter).

Configuration constraint:

- Every RouteWise model **should** have at least one S_A baseline adapter.
  Without S_A, `v_t` is `inf` and routing works while resources last, but
  returns `None` when exhausted.  This is currently a warning, not an error,
  to support subscription-only provider configurations.

### Phase 4: Rollout and guardrails (2-3 days + canary window)

Deliverables:

- Model-scoped enablement via `models.yaml` `routing_strategy` field.
- Canary rollout plan: start with one low-traffic model on RouteWise.
- Rollback runbook: change `routing_strategy` in config, restart.
- A/B evaluation: run different models under different strategies, compare
  cost and latency metrics.

Exit criteria:

- Rollback by config change only (no code deploy required).
- Canary success criteria defined (cost savings %, SLO violation rate).


## 9. Critical Engineering Notes

### 9.1 Predictor feedback mechanism

Do not rely on a router-level `_on_completion` hook for predictor updates.

**Problem**: Streaming providers often return incomplete or missing `usage`
data. Long responses (most valuable for prediction) are most likely to timeout
or drop the final usage chunk.

**Solution**: The `completions` endpoint already normalizes usage across all
adapters. After normalization, it calls `router.record_observation(obs)` with
a `RoutingObservation`. The router internally routes the observation to its
predictor if `obs.usage` is present and reliable. No event bus or callback
registration needed.

### 9.2 Source-of-truth separation

| Owner | Owns what |
|-------|-----------|
| `models.yaml` | Provider endpoints, model capabilities, `subscription_type` tag, per-route pricing |
| `routewise.yaml` | Policy parameters: decision rule, predictor, shadow price config, quota/concurrency limits |
| `settings.py` | Global default routing strategy, Nimbus-specific settings |

### 9.3 Blast radius isolation

- RouteWise does not depend on Nimbus outsourcing internals.
- Nimbus-only failures do not affect fixed/routewise paths.
- Each router class is instantiated independently in `bootstrap.py`.
- Per-model strategy means a RouteWise bug only affects models assigned to it.


## 10. PR Slicing

1. **PR-1**: Per-model routing strategy + `ModelRouterRegistry`. **(Completed)**
   - `routing_strategy` field in `models.yaml` route parsing (`registry.py`).
   - `RoutingStrategy` enum updated in `settings.py` (remove `nimbus_enabled_models` /
     `routewise_enabled_models`; keep only global default).
   - `ModelRouterRegistry` dispatch layer (`routing/registry.py` or `routing/dispatch.py`).
   - `bootstrap.py` creates router instances per strategy, populates registry.
   - `completions.py` uses `registry.get_router(model_id)` instead of a single router.
   - `RoutingObservation` dataclass (with `quota_committed`) and `record_observation()`
     on `BaseRouter`.

2. **PR-2**: `RouteWiseRouter` scaffold. **(Completed: `821ec7f`)**
   - Subclass of `BaseRouter` with `_select_adapter()` stub.
   - Subscription type parsing from route config.
   - `routewise.yaml` schema and loader.

3. **PR-3**: RouteWise Stage 1 (PD + LA-PD). **(Completed: `08c25f0`)**
   - Quota shadow pricing, value estimation, decision logic.
   - EMA predictor with `record_observation` integration.
   - Replay test harness.

4. **PR-4**: Latency-aware provider selection (Phase 2.5). **(Completed: `061624a`)**
   - `ProviderProfile`, LP solver, `SWRRSampler`.
   - Shadow hedging mode (p50 comparison placeholder).

5. **PR-4.1**: Observation plumbing for Layer 2. **(Completed: `515280b`)**
   - `endpoint_id` propagation through `req_ctx` and exception `_routing`.
   - `_resolve_endpoint_id()` with explicit priority chain.
   - Failure observation emission on all error/cancel paths.
   - Outer `try/except BaseException` pattern in `BaseRouter.chat_completion()`
     and `stream_chat_completion()` for universal `_routing` attachment.

6. **PR-5**: `HedgedAdapter` and real hedging (Phase 2.5b). **(Completed: `e0279bc`)**
   - SMART_ECONOMIC trigger: `P_viol(t) * F_b(remaining) > cost_ratio`.
   - `cost_ratio` config parameter in `routewise.yaml`
     (`latency.hedge_cost_ratio`).
   - `HedgedAdapter` composite adapter with streaming race resolution.
   - `ProviderEventSink` for shared health accounting.
   - Loser cancellation.
   - Replace shadow hedge placeholder (p50 midpoint) with SMART_ECONOMIC
     `find_optimal_hedge_time_economic()` grid search.
   - Dual cost metrics: `hedge_cost_real_usd`, `hedge_cost_cancel_aware_usd`.

7. **PR-6**: RouteWise Stage 2 concurrency support. **(Completed)**
   - `ConcurrencyManager` with K=0 binary gate (no queue, no eviction).
   - Three-way gain comparison: S_C > S_Q > S_A in `_select_adapter()`.
   - Slot lifecycle: `try_acquire()` in `_select_adapter()`, `release()` in
     `_execute_adapter` / `_execute_stream_adapter` finally blocks.
   - Fallthrough safety: last-resort path restricted to S_A only; S_C/S_Q
     never returned without proper accounting.
   - Construction-time validation: warns when a model has no S_A baseline.
   - All `ConcurrencyManager` reads of `_active` are lock-protected.
   - Stage 2 replay tests: accounting invariant, cost tolerance,
     non-saturated decision equivalence.

8. **PR-7**: Observability, canary controls, rollout docs.

9. **PR-8**: Promotion PR from integration branch to `main`.

PD and LA-PD share 90%+ code (LA-PD is PD with a conservative quantile).
They belong in one PR, switchable by config.

Each PR must include:

- Test updates.
- Clear rollback mechanism.
- Explicit non-goals.


## 11. Risks and Mitigations

| # | Risk | Mitigation |
|---|------|------------|
| 1 | Large integration branch drifts from main | Frequent small promotion PRs; periodic main sync |
| 2 | Simulation-to-production time/accounting mismatch | Stage 1 first; replay harness before Stage 2 |
| 3 | Streaming lifecycle causes stale S_C state | Explicit tests for cancel/error/no-usage; acquire/release in finally |
| 4 | Config complexity leads to operator mistakes | Per-model strategy is opt-in; global default covers untagged models |
| 5 | Timeline optimism | Ship by milestone gates, not calendar |
| 6 | HedgedAdapter race conditions | Phase 2.5 shadow mode first; SMART_ECONOMIC validated in simulation ablation before production |
| 7 | Quota accounting drift under failures | Selection-commit semantics; no refunds; explicit wasted-quota metric |
| 8 | L/U bounds degenerate over time | Adaptive sliding window with min_ratio floor |


## 12. Timeline (Realistic Range)

Assuming one primary owner plus code review support:

| Phase | Work | Estimate |
|:-----:|------|:--------:|
| 0 | ADR + interface freeze | 0.5 day |
| 1 | Nimbus integration hardening | Completed |
| 1.5 | Research asset sync | Completed |
| 2 | RouteWise Stage 1 (S_Q + S_A) | 4-6 days |
| 2.5 | Latency-aware provider selection (LP mix + shadow hedge) | 3-5 days |
| 2.5b | Real hedging (HedgedAdapter) | 2-3 days |
| 3 | RouteWise Stage 2 (S_Q + S_C + S_A) | 5-8 days |
| 4 | Rollout hardening + canary | 2-3 days + observation |
| **Total remaining** | | **~3 to 4 weeks** |


## 13. Open Questions (Resolve in Phase 0)

1. **Provider accounts**: Are Chutes (S_Q) and Featherless (S_C) accounts set
   up for the prototype, or do we start with simulated subscription constraints?

2. **Quota tracking**: Does Chutes expose a "remaining quota" API, or do we
   track quota purely client-side (as in the simulation)?

3. **Initial L/U bounds**: Bootstrap from config seed (`L_seed`, `U_seed`),
   then adapt via sliding window. Confirm seed values from simulation results.

4. **Timezone for daily reset**: Default to UTC. Confirm this matches Chutes'
   billing cycle boundary.


## 14. Final Recommendation

1. **Main remains protected production branch.**
2. **BaseRouter is the shared execution core**, not a "unified router".
3. **`ModelRouterRegistry`** provides per-model dispatch (thin lookup, no execution logic).
4. **Per-model routing strategy** in `models.yaml` replaces global-only selection.
   `settings.py` provides only the global default; per-model lists are removed.
5. **RouteWise is two layers internally**: cost routing (Layer 1) and latency-aware
   provider selection (Layer 2), implemented in separate phases.
6. **S_C slots are full-lifetime** (selection-commit acquire via `try_acquire()`,
   release in `_execute_*_adapter` finally), unlike Nimbus shadow queue
   (first-token release).
7. **Quota consumed at selection time** (`consume()` in `_select_adapter`).
   No refunds on failure.
8. **Cost, resource, and opportunity metrics are tracked separately.**
9. **Smart Hedging via HedgedAdapter** using SMART_ECONOMIC cost-benefit model
   (`P_viol * F_b > C_b/V`), with `ProviderEventSink` for shared health/circuit-breaker
   accounting.
10. **Simulation and production code are intentionally independent**, validated by
    replay tests with phase-appropriate strictness.
