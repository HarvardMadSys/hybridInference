# Greedy / knapsack experiments for the Nimbus project

Nimbus is the research project; this directory does not implement its future
algorithm. It contains an isolated serving experiment for reproducible greedy
and knapsack baseline comparisons. The active baseline definitions and source
provenance are in `baselines.py`; the implementation record is
`docs/agents/plans/2026-10-04-greedy-knapsack-baselines.md`.

## Policies and information boundary

- `greedy` ports the existing online prototype's arrival-order, local-first
  admission rule: full-prompt plus fixed-output-cap KV reservations rounded to
  blocks, and a shared-prefill/sequence-slot TTFT prediction with an explicit guard.
- `rolling_knapsack_v1` maximizes estimated API cost avoided within each bounded
  window of already-arrived requests, using exactly the same feasibility checks
  and FIFO execution order. The default window is 12, with an absolute limit 16.
  Subsequent windows carry forward accepted reservations; decisions in earlier
  windows are not reconsidered. This is a bounded baseline, not an offline oracle.
- `all_local`, `all_api` and `concurrency` are controls. All-local bypasses
  feasibility checks but still uses the same physical local execution slots.

Greedy's pure decision function is differentially checked against frozen source
excerpts from the prototype. Its source commit and exact working-copy hashes
are retained because the reference `greedy.py` was untracked. Equivalent policy
outputs require equivalent prompt and runtime-state inputs.

The current live input feature uses the repository's prompt byte estimate,
including tools. It is explicitly approximate, not tokenizer-aligned. Every
request uses the profile's fixed caller-known generation cap for reservations
and cost estimates; recorded future output lengths are never supplied. The
snapshot distinguishes accepted waiting requests, active prefill and decode.
First usable output marks observed prefill completion; without exact cumulative
usage, remaining decode retains the cap minus that one observed output token.
Elapsed time never frees a physical slot. This observation adapter is documented
separately from the reproduced pure feasibility function.

TPOT is measured for every policy. The first reproduced greedy does not have a
TPOT admission gate. Additional feasibility constraints must be introduced as
explicit, matched ablations rather than silently changing one baseline.

## Serving integration

The replay client sends real HTTP requests to an ephemeral loopback listener.
The existing completion handler calls an experiment-local subclass of
`ModelRouterRegistry`, which binds `BaselineRouter` through a private construction
hook. Registry caching and normal request dispatch remain in use, as do
`LeafBackend` and the existing OpenAI-compatible adapter. No experiment strategy
is registered in the production strategy registry and no production dependency
or default is added. The private hook is a harness seam, not a deployment API.

The experiment supplies `AppServices` directly, exposes only completion routes,
and uses a random in-memory bearer token per run. It omits production bootstrap,
Postgres, account billing, quotas, alerts and Admin APIs. Paid calls use the
campaign ledger. This verifies the serving path, not the full production stack.

The metered adapter replaces only the HTTP response-opening seam: one POST per
attempt, no retries, redirects or key-pool rotation. It retains the existing SSE
reader and adapter processing. Authoritative final usage is captured before any
adapter fallback estimates. Missing/intermediate-only usage retains liability.
`cloud.trust_env_proxy: true` enables the server's HTTP(S) proxy environment for
cloud calls; local calls are always direct. Connection establishment is bounded
to 15 seconds. Proxy credentials are never recorded.

## Configuration and one immutable run

The current configuration schema is **2**. Older schema 1 measurements are
reproduced at their recorded source commits; this version refuses to reinterpret
their different feasibility model.

Required fields are `schema_version`, `campaign_id`, `budget_cap_cny`, `policy`,
`local`, `cloud`, `prices`, `slo`, `baseline_profile`, and `replay`.

`baseline_profile` supplies `kv_capacity_tokens`, `max_tokens`, `prefill_tput`,
`tpot_s`, `first_token_overhead_s`, `slo_s`, `ttft_guard_s`, and optional
`kv_block_size` (default 16). Its `slo_s` must equal `slo.ttft_s`; `slo.tpot_s` is a
reporting threshold. Profile `max_tokens` must cover every workload request cap.
Record numerical profile provenance and prediction residuals; these proxies are
not engine performance guarantees or measured KV capacity.

`replay.local_max_inflight` is required and controls actual local slots.
`replay.max_in_flight` separately caps replay-client concurrency: keep it high
and identical across policy comparisons so requests are not hidden from the
router in a client queue. Additional options are `knapsack_window` (default 12),
`dispatch_window_s`, `max_pending`, `cancel_grace_s`, `request_timeout_s`,
`arrival_speedup`, `max_cloud_attempts`, and `prewarm_tokenizer`.

Endpoints provide `base_url`, `model`, `context_length`, `max_output_tokens`, and
optional `provider_profile`, `extra_body`, `chat_path`. The cloud input-token
reservation bound must equal its declared context length. Explicitly record
thinking controls and other endpoint protocol differences. `generation` accepts
`temperature`, `top_p` and `seed`; caps belong to workload rows.

From a clean source commit, with an already-configured provider environment:

```bash
PYTHONPATH=apps/backend:. python -m benchmark.nimbus.runner \
  --config /path/to/config.json --workload /path/to/workload.jsonl \
  --output /path/to/new-run-directory --ledger /path/to/campaign.sqlite \
  --api-key-env NIMBUS_DEEPSEEK_API_KEY
```

Output directories must be new. The CLI does not start, stop or change model
servers. `max_cloud_attempts: 0` prevents any cloud POST, including accidental
ones in a local-only smoke case. Optional tokenizer prewarm requires an already
staged SHA-256-verified `cl100k_base` cache in `TIKTOKEN_CACHE_DIR`; it does not
download assets or provide DeepSeek-exact prompt features.

Each JSONL row contains `id`, `session_id`, `round_index`, `arrival_s`, `messages`,
`max_tokens`, optional `tools`, and `tool_wait_s`. Source traces use frozen
recorded histories; tools are never executed. Later session rounds wait for the
prior generated response and recorded tool delay, then use the recorded prompt.
This is serving replay, not an agent task-success benchmark. Independent
open-loop requests provide policy-independent scheduled arrivals.

## Measurements, spending and evidence

TTFT starts when a request becomes eligible, including client/gateway queue time,
and ends at the first nonempty text, reasoning or function/tool output. Preserve
separate dispatch-to-first and upstream-wire timings. Primary TPOT is
`(response_end - first_output)/(authoritative_completion_tokens - 1)`; preserve
the last-usable-output version too. These are request averages, not per-token ITL.
Missing authoritative counts and one-token responses have undefined TPOT.

Record p50/p90/p95/p99 of TTFT/TPOT/E2E, throughput, local/cloud decisions, output
lengths, errors, truncation, usage coverage and separate/joint SLO outcomes.
Keep errors and unknown coverage in outcome denominators. Report marginal API
cost separately from unmeasured local GPU cost. Do not average run percentiles
or claim stable tails from small cohorts.

All paid attempts share one transactional SQLite ledger and the user's cumulative
**CNY 1000** ceiling. Reserve the context-limit input bound plus the actual hard
output cap before each POST; settle only authoritative final usage. Unknown usage
keeps the full hold. Costs use pinned peak pricing and an explicit conservative
billing-currency conversion; they are estimates, not provider invoices. CSV exports
include unresolved liability and whether cost is complete.

Every run retains source commit, config/workload hashes, raw SSE, request and
attempt events, policy inputs/predictions, budget snapshots and summary. Failures
remain immutable and retries get new IDs. Keep private records in version control
and use consistent SQLite backups, not copies of a live database. Secrets stay
in environment/pipes only. Shared prefix-cache state is uncontrolled unless an
experiment explicitly proves otherwise; a pricing fallback of zero cache tokens
is not evidence of a cache miss.

Begin with execution smoke, then matched development workloads and counterbalanced
repetitions. Complete authenticated cloud/model/usage checks before paid hybrid
runs. Freeze definitions and parameters before held-out evaluation. Preserve
negative results and distinguish local-only execution evidence from completed
local/API policy comparisons.
