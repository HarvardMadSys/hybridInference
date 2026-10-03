# Nimbus live gateway experiment, 2026-10-04

The user approved continuing live experimentation after aligning three repository roles:
HybridInference owns the actual online runtime; realtmxi/nimbus supplies prior online
prototype research; glenliu21/nimbus supplies the offline model/solver. User authorization
covers isolated Taiji experiments and a cumulative CNY 1000 DeepSeek API ceiling, not a
production traffic cutover. The experiment runs use a feature worktree from dev.

## Implementation

- A pure admission selector and an opt-in `nimbus` RouterProtocol peer implement
  all-local, all-API, concurrency, arrival-order feasibility greedy, value-density
  selection and bounded knapsack. The first version makes irreversible whole-request
  local/cloud choices and does not migrate running work, hedge or retry.
- One explicitly shared local pool owns reservations. Arrived requests can accumulate
  for a small dispatch window. Cancellation and streaming iterator closure release
  owned capacity deterministically; an explicit cancellation grace is a proxy rather
  than proof the engine has stopped work.
- Experimental composition includes the real completion HTTP handler, model router
  registry, adapter/leaf and streaming session. It uses loopback-only serving and a
  per-run bearer token, omits production account storage/quota/alerts, and replaces the
  HTTP response-opening seam with exactly one POST to allow strict per-attempt metering.
  The original SSE parser and adapter remain in use. This validates the inference path,
  not the complete production deployment or billing/account stack.
- The replay client calls HTTP and collects client-observed timing, authoritative
  upstream usage and routing decision events. It uses public fixed-history agent traces,
  never executes tools, and excludes recorded future output lengths from policy inputs.
- A persistent transactional SQLite ledger reserves a conservative context-limit input
  bound and the hard output cap before every paid POST. Unknown usage retains liability.
  Exported costs are configured-rate estimates, not provider invoices. USD billing and
  the conservative CNY budget conversion are recorded explicitly.

## Validation and evidence

Focused tests cover selection, concurrent pool ownership, stream/cancel cleanup,
registry dispatch, HTTP streaming, missing-usage and rejected-budget paths. Run existing
routing and serving tests, repository formatting/lint and default test suite before PR.
Live validation begins with short gateway/API and TP4 smoke requests and an exact
hardware/process/version snapshot. Defer local runs while another experiment owns or
reconfigures the service; require drained queues before comparison runs.

Each measured run has a unique directory, clean source commit, immutable config and
workload hashes, per-request observations, decisions, budget snapshots and a final
summary. Failed runs remain evidence and retries receive new IDs. Initial comparisons
use counterbalanced run order and separate development/held-out tasks. Closed-loop
agent sessions have policy-dependent arrivals; an independent open-loop workload gives
identical scheduled arrivals. Prefix caches are not reset on a shared service and this
limitation must remain visible.

Report TTFT, request-average TPOT and E2E distributions (p50/p90/p95/p99), errors,
truncation and metric coverage, throughput, marginal API cost and explicit SLO thresholds.
A bounded selector optimum is not an offline workload oracle. The local GPU's sunk cost
is distinct from marginal API charges. Small-sample observations do not establish a
stable p99 or a general algorithmic advantage.
