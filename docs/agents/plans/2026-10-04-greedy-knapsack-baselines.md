# Greedy and knapsack live baseline experiments

This record supersedes the algorithm framing in the earlier Nimbus live experiment
plan. Nimbus is the research project; its future algorithm is still under design.
The authorized work is a reproducible comparison of greedy and knapsack baselines
using HybridInference's real serving path, a local TP4 service, and a metered API.

## Baseline definitions

The first greedy baseline ports the existing online prototype's arrival-order,
local-first decision: reserve full prompt plus the caller-known output cap, round
that reservation to KV blocks, and admit only when KV and the source shared-prefill
and sequence-slot TTFT predictor fit. It does not use future output lengths, cache
credits, the offline solver's lookahead, or a new work/TPOT admission constraint.
Its equivalence is conditional on equal prompt-token and runtime-state inputs;
the experiment's prompt-size estimator is separately identified in every decision.

The reference is `realtmxi/nimbus`, HEAD
`13694272a41a10c9cf2eb6984a16b10115016adc`, with existing working-copy
`router/greedy.py` SHA-256
`335e5bcd44465eff7a4fb90e19f117626d41bd4828fac98164b9d68684217eb9`.
That file is untracked in the reference checkout, so the commit alone is insufficient
provenance. Exact reference bytes and hashes are retained with private campaign data.
The source checkout must remain untouched.

`rolling_knapsack_v1` is an explicitly defined experimental baseline. It maximizes
estimated API spending avoided within an arrived candidate window, using the same
KV, output-cap, slot and TTFT feasibility rules as greedy. Selected requests execute
in FIFO order. Bounded windows are processed in arrival order; earlier windows are
not reconsidered. This is neither Glen's future Nimbus algorithm nor the legacy
FLOP-based knapsack nor a full-workload offline oracle. Record window membership,
search bounds, decision time and all parameter choices.

All-local, all-API and concurrency controls remain available. TPOT, TTFT and E2E
distributions are outcomes for every baseline; the first reproduced greedy has no
TPOT admission guarantee. A later TPOT-aware ablation must change both compared
policies' shared feasibility model explicitly.

## Minimal integration and records

Keep implementation under `benchmark/nimbus/`, where the directory names the
campaign. Remove the experimental global strategy registration, production
dependency additions and misleading `NimbusRouter` class. Bind a neutrally named
baseline router through an experiment-local `ModelRouterRegistry` subclass, then
use the existing HTTP completion handler, registry cache, LeafBackend and adapter.
The subclass overrides a private construction hook and is a harness integration,
not a production extension contract. Default production strategy registration must
be byte-for-byte unchanged from the branch base.

Use a new configuration schema for the corrected feasibility model. Historical
schema-1 runs remain immutable and reproducible at their original source commits.
They contain local calibration only; none is a greedy/knapsack performance result.
Use the same high replay-client concurrency and offered arrivals across policies;
record the separate local execution-slot limit and accepted FIFO queue state.

Retain the persistent cumulative CNY 1000 ledger, exact attempt metering and
unknown-usage reservations. API authentication currently fails; no new paid call
is allowed until a read-only authenticated check succeeds. Source and experiment
changes stay versioned; private raw records remain in the campaign repository.

## Verification and iteration

1. Differential-check greedy against the exact reference on busy-slot, prefill,
   age, KV-rounding and fixed-cap cases; check bounded knapsack against independent
   small-instance feasible subsets.
2. Exercise the real HTTP path with fake endpoints, including cancellation,
   accepted-but-unsent requests, stop/drain, no hidden retries and budget denial.
   Verify importing or constructing the harness does not register a production
   Nimbus or baseline strategy.
3. Run bounded live local smoke cases for both named policies after checking the
   actual TP4 topology and empty engine queues. Such smoke is execution validation,
   not an algorithmic comparison or evidence of API savings.
4. Once API authentication works, verify one short metered API request, then run
   matched baselines and greedy/knapsack development cohorts with counterbalanced
   order. Report continuous TTFT/TPOT/E2E, usage, costs, errors and truncation.
5. Freeze definitions and calibrated parameters before held-out evaluation.
   Preserve negative results, repeated-prefix/cache limitations and all failed runs.
