# Nimbus experiment campaign

This is an isolated experiment instance of HybridInference. The replay client sends
real HTTP requests to an ephemeral loopback listener using the existing completion
handler, request schema and streaming serializer. `ModelRouterRegistry` selects the
opt-in `router: nimbus` implementation, which calls the existing `LeafBackend` and
OpenAI-compatible adapter. Policies live in `apps/backend/routing/nimbus_policy.py`.

The experimental composition supplies `AppServices` directly and exposes only the
normal completion routes. It replaces account authentication with a random,
in-memory per-run bearer token and skips production bootstrap, Postgres, account
billing, alert configuration and unrelated HTTP endpoints. Paid requests instead
use the persistent campaign ledger. Production Fixed and RouteWise routing are
not selected or modified for this experiment.

The metered adapter replaces only the HTTP response-opening seam: exactly one
POST, no connection retry, no redirects, and no key-pool rotation. It reuses the
repository's SSE reader and OpenAI adapter processing. Raw upstream final usage
is captured before the adapter can substitute estimated usage. Missing or
intermediate-only usage retains the reservation, including on a successful stream.
`cloud.trust_env_proxy: true` explicitly enables the server's HTTP(S) proxy
environment for cloud calls; local inference always connects directly. Both paths
bound connection establishment to 15 seconds while preserving configured total
and stream read timeouts. No proxy URL or credential is written to artifacts.

## Run one immutable cohort

From a clean source commit, with the provider key already in the named environment:

```bash
PYTHONPATH=apps/backend:. python -m benchmark.nimbus.runner \
  --config /path/to/config.json --workload /path/to/workload.jsonl \
  --output /path/to/new-run-directory --ledger /path/to/campaign.sqlite \
  --api-key-env NIMBUS_DEEPSEEK_API_KEY
```

The output directory must not exist. The CLI never writes the credential and does
not start, stop or change model servers. `replay.max_cloud_attempts: 0` prevents any
cloud POST, including an accidental one in an all-local run. The CNY cap is shared
across all runs through the same ledger, not reset per process.

Configuration version 1 requires `campaign_id`, `budget_cap_cny` (decimal string),
`policy`, `local`, `cloud`, `prices`, `slo`, `profile`, and `replay`. Endpoints supply
`base_url`, `model`, `context_length`, `max_output_tokens`, and optional
`provider_profile`, `extra_body`, `chat_path`. The cloud
`input_tokens_upper_bound` must equal its declared context limit. Prices use the
budget module's exact decimal strings and provenance. `slo` supplies `ttft_s` and
`tpot_s`; `profile` contains the explicit `LocalProfile` deployment parameters.
`replay` accepts `arrival_speedup`, `dispatch_window_s`, `request_timeout_s`,
`max_in_flight`, `max_cloud_attempts`, `max_pending`, `estimated_output_tokens`,
and `cancel_grace_s`. `generation` accepts `temperature`, `top_p`, and `seed`.
The router uses the repository's prompt estimator; no trace output label is read.
`replay.prewarm_tokenizer: true` requires a pre-staged, SHA-256-verified
`cl100k_base` cache and warms the gateway's fallback tokenizer before measurement
or any provider request. It never downloads a missing asset. Set
`TIKTOKEN_CACHE_DIR` to the staged cache directory; a missing or mismatched asset
leaves a failed run manifest and sends no requests.

Each JSONL row has `id`, `session_id`, `round_index`, `arrival_s`, `messages`,
`max_tokens`, optional `tools`, and `tool_wait_s`. The wait belongs to the current
round and applies after it completes, before its successor becomes eligible.
The same output cap is sent to both endpoints. Configure thinking controls
explicitly per endpoint in `extra_body` and retain those protocol differences.

The first campaign compares one Taiji TP4 DeepSeek V4.1 Flash instance with the
official `deepseek-flash` API. The 2026-09-27 Nimbus meeting motivates reporting
TTFT and per-request mean TPOT distributions, not only a thresholded SLO outcome.

## Required experiment records

Every measured run must have a unique ID, a clean source commit, an immutable copy
of its configuration and workload, SHA-256 digests, deployment and package
versions, a start/end timestamp, raw per-request results, a budget ledger export,
and a result summary. Failed and interrupted runs are retained. Never overwrite a
run to retry it. A rerun gets a new ID and a `supersedes` explanation in the campaign
journal. Credentials are environment-only and must not enter records.

Keep code in a feature branch. Keep the campaign directory in its own private Git
repository, and mirror it from Taiji to the workstation after each run. Raw logs
remain available alongside summaries; artifact manifests include sizes and hashes.
Do not put a mutable SQLite database in Git while a process is using it: export
the ledger and take a consistent SQLite backup instead.

## Measurements

- TTFT begins when a request is eligible, including gateway/runner waiting, and
  ends at the first nonempty text, reasoning, or tool-call output. Role-only SSE
  frames are not output. Also retain dispatch-to-first-output time separately.
- The campaign's primary TPOT convention is `(response_end - first_output) /
  (completion_tokens - 1)`, matching the preceding Taiji benchmark. Preserve the
  last usable output time and the output-span version as a secondary measurement.
  These are stream-observed request averages, not token-by-token ITL. Unknown
  authoritative usage and responses with at most one output token have undefined
  TPOT and must have an explicit coverage count.
- Report p50/p90/p95/p99 and raw distributions of TTFT, TPOT and end-to-end latency;
  costs, completed/error/truncated counts, session completion, achieved throughput,
  and offered load. Keep errors in request denominators. Preserve the output
  length distribution, since backend differences can otherwise confound latency.
- Report TTFT violations, TPOT violations and their joint outcome under explicit
  thresholds. A thresholded outcome complements the continuous measurements.
- Compare API cost vs TTFT and API cost vs TPOT at matched offered load. For a
  fixed TP4 configuration, report marginal API cost separately from optional local
  hardware/energy cost. Do not label all-local total cost as zero.

## Replay limitations

The initial public agent traces have frozen recorded histories. A later round
waits for the preceding generated response and the recorded tool delay, but its
prompt still uses recorded history. Tools in traces are never executed. This is
serving replay, not an agent task-success benchmark. Both endpoints get the same
messages, tools and output cap; output lengths are observed, not forced with
server-only `ignore_eos` flags.

Only arrived requests can enter a policy's candidate set. Online policies receive
estimated output lengths, never the trace's actual future output length. Capacity
and TTFT/TPOT predictions are explicitly recorded proxies until calibrated on the
target deployment. They are not real-engine latency guarantees.

## Spending

The user authorized a cumulative **CNY 1000** of DeepSeek API experiment spending.
Every paid wire attempt must reserve a conservative upper bound in one persistent
SQLite ledger before dispatch. All runs share that ledger. Known authoritative
usage settles the reservation; missing usage, uncertain cancellation and crashes
retain it. No transport retry is allowed without its own reservation. The runner
must fail closed when its cap cannot cover another attempt.

Pin provider pricing and model mapping in the campaign metadata. Use peak prices
for the conservative budget and retain the applicable peak/off-peak schedule for
cost reporting. If the account bills in USD, record an explicit conservative CNY
conversion and retain both currencies rather than relabeling dollars as yuan.

## Iteration order

1. Verify the transport, counting and ledger using fake servers and bounded live
   smoke requests. Record all deviations and failures.
2. Calibrate one TP4 deployment after other measurements have drained.
3. Run all-local, all-API and concurrency baseline cases on a frozen development
   workload; use identical payloads and reported output caps.
4. Compare FIFO feasibility greedy, value-density greedy and rolling knapsack.
5. Retest promising settings on held-out traces and multiple repetitions, with
   balanced run ordering. Preserve negative results and configuration history.

One first-pass sample is a smoke/baseline observation, not evidence of a stable
algorithmic improvement. Full TraceLab oracle comparisons require a calibrated
model and an explicitly matched router action space.
