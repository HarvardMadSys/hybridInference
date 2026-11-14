# Verification: serving-health progress evidence (PR #1502)

Date: 2026-11-13. Subject: `HarvardMadSys/hybridInference` PR #1502, *fix(serving):
require evidence of model progress before clearing serving health*.

Scope: the PR's own branch and the upstream `dev` it integrates with. No
deployment was contacted; no provider was called. This is a source-level and
local-test verification record, not live deployment acceptance.

## Subject

- Base: `f7142ef3900c3d81dc5492bb0f062d9027fe1e9d` (`dev`, 2026-11-13).
- Candidate: `4d46ba2acb76b4d99d1bfc5a754c98214f95eab2`.
- The candidate is the PR head with upstream `dev` merged in, plus the
  corrections recorded below. It was pushed to the PR's own head branch as a
  fast-forward (`5ab1a4ca..4d46ba2a`); no shared history was rewritten.

The governing invariant is that HTTP 200, transport activity, or receipt of a
stream chunk does not establish successful model inference. A model must show
genuine progress before the gateway clears serving health, closes a circuit
breaker, confirms prefix warming, or records successful latency-learning
evidence.

## Integration baseline

- Upstream `dev` at the time of verification contains #1761, which merged #1501:
  `git merge-base --is-ancestor ee7246b6 upstream/dev` succeeds. The
  `CompletionOutcome` foundation, `routing/completion_outcome.py` and
  `routing/response_text.py` as merged upstream are the ones the candidate
  extends; the candidate adds to them and does not restore the older versions.
- Upstream `dev` at the base also contains #1756
  (`3b83b75cbc663b55309d2183a98e59baef951739`), whose rewrites the candidate
  integrates with: the RouteWise stream loop's per-attempt TTFT
  (`_ATTEMPT_TTFT_MS_KEY`), the `_record_latency_outcome` latency-learning
  gate, and the Anthropic surface's single-parse SSE handling
  (`_apply_sse_payload`, `_is_content_delta`).
- The candidate's diff against upstream `dev` is 30 files: 3026 insertions,
  178 deletions.

## Corrections made in this candidate

| Correction | File | Why |
|---|---|---|
| The hedge stream race keeps its verdict in a bounded running prefix (`_RaceBufferProgress`) instead of re-reading the whole buffer per chunk. | `apps/backend/routing/routewise/hedging.py` | The race re-parsed every buffered chunk on every chunk, so a whitespace-only stream cost seconds of event-loop CPU and grew quadratically. |
| The Anthropic non-streaming path classifies the response before confirming the prefill lease, and uses that one verdict for both the health recording and the confirmation. | `apps/backend/serving/servers/routers/anthropic_messages.py` | The streaming path already withheld the confirmation until the response proved progress; the non-streaming path confirmed first, so a warmup notice wrote the warm-prefix hint. |
| Removed the unused `_Entry.generation` field and `observe(generation=...)` parameter. | `apps/backend/routing/routewise/prefix_cache.py` | Dead code the branch added; nothing read either. |
| Collapsed the identical `if stream:` / `else:` branches. | `tests/unit/routing/test_offload_routing.py` | Test churn flagged in review. |
| Corrected the PR description. | PR #1502 body | It claimed a generation lock, generation reconciliation, `observed_cached_tokens` evidence and concurrency regression tests, none of which exist in the branch, and said `record_liveness` closes HALF_OPEN probes when it only updates the EWMA. |

## Regression evidence

Failing-first: each new test was run against the tree without its fix, or
against upstream `dev`, and observed to fail.

| Regression | Test | Fails without the fix |
|---|---|---|
| Streamed warmup notice closing HALF_OPEN | `tests/servers/test_anthropic_messages_health_recording.py::test_streaming_warmup_delta_does_not_close_circuit` | upstream `dev`: the breaker closes |
| Ambiguous first chunk then genuine output | `tests/unit/routing/test_completion_outcome_production_path.py::test_short_warmup_prefix_is_undecided_until_stream_end`, `tests/unit/routing/test_serving_health_outcome.py::test_fragmented_genuine_stream_closes_breaker` | upstream `dev`: the first chunk decides |
| Thinking-first and tool-call-first progress | `tests/unit/routing/test_serving_health_outcome.py::test_fixed_and_routewise_health_recorders_accept_structured_stream_output`, `tests/servers/test_anthropic_messages_health_recording.py::test_thinking_first_stream_records_success` | upstream `dev`: EMPTY |
| Thousands of whitespace chunks then genuine content | `tests/unit/routing/test_completion_outcome_production_path.py::test_whitespace_stream_prefix_stays_bounded`, `tests/unit/routing/test_routewise_hedging.py::TestStreamRaceCostIsBounded` | mutation: both fail |
| Non-streaming warmup confirming prefix locality | `tests/servers/test_anthropic_messages_health_recording.py::test_non_streaming_warmup_does_not_confirm_the_prefill` | mutation (confirm before classifying): fails |
| Hedge and probe latency trained from transient responses | `tests/unit/routing/test_routewise_router.py::test_probe_warmup_notice_trains_nothing`, `tests/unit/routing/test_routewise_hedging.py::TestWarmupLegsNeverEnterTheLatencyProfile` | upstream `dev`: both fail; mutation: both fail |
| Stream failure and cancellation cleanup | `tests/unit/routing/test_serving_health_outcome.py::test_routewise_cancelled_partial_warmup_prefix_does_not_record_success`, `tests/servers/test_anthropic_messages_health_recording.py::test_streaming_upstream_cancellation_is_not_recorded` | upstream `dev` |
| Current-upstream compatibility and lifecycle | `tests/unit/routing/test_serving_health_outcome.py`, `tests/unit/routing/test_circuit_recovery_alerts.py` (#1712's contract) | upstream `dev` |

## Streaming cost

Measured through the real `HedgedAdapter` race with whitespace-only chunks, on
the candidate and on the same tree with the pre-fix whole-buffer re-read
restored:

| Chunks | Pre-fix | Candidate |
|---|---|---|
| 500 | 804 ms | 22 ms |
| 1000 | 10017 ms | 45 ms |
| 2000 | 10018 ms | 83 ms |
| 4000 | 20090 ms | 163 ms |

The candidate's cost is linear in the chunk count (about 6 us per chunk); the
pre-fix cost was quadratic. The chat path's `StreamProgressTracker` costs
about 6 us per chunk, and a whitespace flood no longer hides the content that
follows it.

## Repository gates

On the candidate (Python 3.13):

- `ruff format --check .` -- clean (1183 files).
- `ruff check --no-fix .` -- clean.
- `pydocstyle --match-dir='^((?!(tests|\.venv|node_modules|apps/frontend|ops|docs)).)*$'` -- clean.
- `tests/unit/routing` + `tests/unit/servers`: 2726 passed, 2 skipped.
- `tests/unit/routing` + `tests/unit/servers` + the Anthropic health
  recording suite: 2760 passed, 2 skipped.
- `tests/servers`: 3169 passed, 1 skipped.
- Full suite (`not external and not dbtest`, `-n 8 --dist loadfile`): 13840
  passed, 5 skipped, 8 failed.

## Failures compared against untouched upstream `dev`

The same full-suite run on untouched upstream `dev` at the same base gives
13756 passed, 5 skipped, 6 failed. The two sets differ as follows.

Failures present on **both** trees:

- `tests/servers/test_batch_download_timeout.py` (both parametrisations).

Failures only on the candidate, each a wall-clock budget assertion under
8-way parallel load:

- `tests/unit/utils/test_tokens_bounded.py::test_the_estimate_is_cheap`
  (`assert 0.183 < 0.1`).
- `tests/unit/utils/test_tokens.py::test_estimate_stays_fast[punctuation-run]`.
- `tests/unit/storage/test_bounded_log.py::test_measuring_a_value_stops_at_the_budget[wide-dict]`.
- `tests/servers/test_routewise_example_runtime.py::test_neutral_quota_example_boots_and_routes`
  (three parametrisations).

Failures only on **dev**, each also a wall-clock budget assertion:

- `tests/unit/ops/test_classify_changes.py` (three tests).
- `tests/unit/servers/test_stream_accumulation_cost.py::test_a_responses_stream_accumulates_in_linear_time`.

None of these files is touched by the candidate, and each assertion is a
timing budget rather than a behavioural one. Run in isolation on the
candidate, `test_tokens_bounded.py` takes 24-29 s, the same as on dev.

### The quota-example flake

`test_neutral_quota_example_boots_and_routes[available]` is load-sensitive: it
gives a RouteWise example 12 seconds to route to a quota-backed provider, and
the router's decision depends on a measured latency profile and on the quota
pool's own request accounting.

Measured over 8 runs of that file alone under `-n 8 --dist loadfile`, on the
same machine:

- candidate: 2 of 8 runs failed.
- untouched upstream `dev`: 2 of 8 runs failed.

The same test also fails on dev in the full-suite runs above. The candidate is
therefore not the cause; the test's own 12-second budget is.

The mechanism, from the captured router state of a failing run: the quota
provider's shadow price rises with its used fraction, and the selection LP
minimises `mean_ttft + 1e-6 * normalized_cost`, so the latency term dominates
the cost term by six orders of magnitude. When the quota provider's measured
mean TTFT is even slightly above the fallback's, the fallback wins. The
candidate's probe traffic consumes the fixture's quota, which raises the used
fraction and therefore the shadow price, and the candidate's latency gate
changes which attempts are timed. The margin between the two providers'
measured means is a few milliseconds, so load decides the outcome.

This is a pre-existing sensitivity in an example fixture, not a defect in the
serving-health change. It is recorded here as a known flake with its measured
rate on both trees, and it is the one item of this verification that a
maintainer may want addressed separately.

## Not covered

- The `dbtest` tier needs a PostgreSQL this environment does not provide; the
  candidate does not change storage, SQL or migrations.
- The Docker and Tutorial E2E CI jobs need a Docker daemon and were not run
  locally.
- The candidate's GitHub Actions run is `action_required`: this repository's
  fork pull requests need a maintainer to approve the workflow before it
  starts. The approval API needs repository admin rights, which this
  environment does not have. The same state is recorded for the PR's previous
  runs.
- Upstream `dev`'s own scheduled CI run at this base fails its `Security Scan`
  job at the `Security audit` step (`pip-audit`). That is a dependency audit
  on the base, unrelated to this change, and it fails on the untouched base
  too.
