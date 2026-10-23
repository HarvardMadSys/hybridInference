# Staging cache-locality and routing diagnosis — 2026-10-22

Record of a measurement-driven investigation into whether normal FreeInference
staging routing reliably reuses warmed prefixes for `glm-5.3-flash`, and where
reuse actually stops. It complements
[`2026-10-22-staging-cache-diagnosis.md`](2026-10-22-staging-cache-diagnosis.md),
which proved the telemetry-side defect (a missing usage-request capability) from
client-side evidence. This record is about **physics**: whether the KV/prefix
cache is reused at all, on which endpoint, and why not.

**Current status (bottom line).** Two gateway-side mechanisms are identified,
corrected in local commits on `bamn/fix/route-include-usage`, and covered by
regression tests: (1) a fixed 0.8 s first-token deadline that cancelled
legitimate cold prefills and re-sent them cold to the offload route, and (2)
selection with no locality term, so a conversation's warm prefix was ignored by
the weighted draw. The short-prefix boundary that motivated the brief is **not**
staging-specific: it is the engine's, and production shows identical values (see
"The reuse boundary, measured, on both deployments"). Nothing here has been
deployed, so no improvement in staging behaviour is claimed; the controlled
local comparison that *was* run is in "Controlled local experiment (run)", and
the staging comparison to run after deployment is specified in "Controlled
staging comparison (prepared, not run)".

## Authorized observability used

No admin session was available. Admin HTTP routes are unreachable through the
local bridge (`/staging/admin/*` 404s) and no `ADMIN_TOKEN` exists in the
environment; `/admin/*` requires a JWT or `ADMIN_TOKEN`
(`servers/deps.py::verify_admin_access`). Three authorized paths were used
instead:

1. **Local FI gateway request log** (`FI-gateway-release/fi-gateway-log.jsonl`)
   — per-request `request_id`, `physical_attempt_count`, `cached_input_tokens`,
   `first_event_latency_ms`, `duration_ms`. Its `served_provider` /
   `served_endpoint_id` are **product-level only** (`freeinference`, `staging`),
   never a worker, so it cannot identify an upstream endpoint.
2. **Staging inference API** through the local bridge
   (`http://127.0.0.1:18769/staging/v1`), with the staging provider key.
3. **`X-Route-Pin`**, which the gateway honours only for admin accounts
   (`servers/routers/completions.py:772`). The staging key resolves to an admin
   user, so pins are honoured — this is the authorized endpoint-pinned path, and
   the pre-flight `400 Pinned provider ... not found` response makes the header a
   provider-name oracle.

A second, unpinned fingerprint is available for free: the two staging endpoints
tokenize the same text differently, so `usage.prompt_tokens` identifies the
endpoint that served a request.

Measurement scripts are scratch (`.tmp-probe/`, not committed).

## Measurements

### Staging `glm-5.3-flash` exposes two *pinnable* endpoints; the route has three candidates

Pin-oracle results (`X-Route-Pin` value → accepted/rejected):

| Pin | Result |
|---|---|
| `ollama` | accepted |
| `vllm` | accepted |
| `glm-5.3-flash:ollama-api` | accepted |
| `zai`, `zai-api`, `zai_pro`, `fp_ollama`, `fp_zai`, `staging`, `freeinference`, `local`, `sglang`, … (≈40 more) | `Pinned provider ... not found` |

The pin namespace is therefore `vllm` and `ollama`. `fp_ollama` — the name in the
investigation brief — is **not** in this route's pin namespace; `ollama` is. The
alternation it describes reproduces exactly.

The admin routing panel (read later, see "The routing policy, read from the
console") shows **three** candidates: `zai`, `vllm` and `ollama`. So the
two-endpoint view above is a *pin-namespace* artifact, not the route's full
membership: a candidate the pin oracle does not expose can still take ordinary
selection. That distinction matters for attributing the ~50 % split in F5 and is
the reason the weight values remain the deciding missing input.

### The two endpoints are distinguishable, and behave differently

Identical-prefix repeats, non-streaming, pinned:

| Endpoint | `prompt_tokens` on the same text | cache fields | cached | TTFT | reply |
|---|---|---|---|---|---|
| `vllm` | 2595 / 2659 / 8829 | present (`cached_tokens`, `cache_read_tokens`, `prompt_tokens_details`) | 0 or 4608 (see below) | 420–1300 ms | `ok` |
| `ollama` | 2536 / 2656 / 8815 | **absent entirely** | n/a | 870–2000 ms | empty |

`ollama` is the shape-B endpoint from the earlier diagnosis: a different
tokenizer (≈60 fewer tokens), ~2× slower, no usage/cache telemetry at all, and
an empty completion.

### Prefix reuse on `vllm` is size-gated; on production it is not

Pinned to `vllm`, second request of an identical pair:

| prefix tokens | cached (2nd request) | prompt − cached |
|---|---|---|
| 2595 | 0 | 2595 |
| 2659 | 0 | 2659 |
| 4514 | 0 | 4514 |
| 6274 | 2304 | 3970 |
| 8819 | 4608 | 4211 |
| 13382 | 9216 | 4166 |

`cached ≈ max(0, prompt − ~4100)`: the endpoint reuses tokens **beyond a fixed
~4.1k head** and nothing at all below ~4.2k tokens. This is stable across runs,
fresh salts and repeated prefixes, so it is not eviction: the first request of a
pair is cold, every following one is warm, and the boundary is the same.

Production, same model, same client, same 2595-token prefix, **unpinned**, as
first measured:

| request | cached | TTFT |
|---|---|---|
| 0 | 0 | 2209 ms |
| 1 | 2560 | 5236 ms |
| 2–5 | 2560 | 1500–2626 ms |

> **Superseded — see "The reuse boundary, measured, on both deployments".**
> Re-running this against the live deployments does not reproduce it:
> production at 1 819 and 2 809 tokens now reports `cached = 0`, and at 9 119 and
> 18 319 it reports exactly the same values as staging (4 608 and 13 824) with
> the same un-reused tail. Note also that this "hit" cost 5 236 ms against a
> 2 209 ms cold request, so it may have been a different route instance or a
> different engine state rather than a deployment-wide property. The correct
> conclusion is the one in the later section: the boundary is the engine's and
> is shared, not a staging defect.

The same bytes under the same gateway code do not reuse on staging while
production reused them — that reading is what the correction above withdraws.
The gateway is not rewriting the prompt: `prompt_tokens` is stable on both.

### Normal (unpinned) staging routing only sometimes reaches the warm endpoint

Identical prefix, unpinned, one client key:

| prefix | requests | reached `vllm` (telemetry) | reached `ollama` (no telemetry) | reuse |
|---|---|---|---|---|
| 2659 tok | 12 | 11 | 1 | 0 / 11 on `vllm` (ineligible size) |
| 8815 tok | 8 | 4 | 4 | 4 / 4 on `vllm` at `cached=4608` |
| 8815 tok | 16 | 7 | 9 | 7 / 7 on `vllm` at `cached=4608` |

Two facts:

- For an **eligible** prefix, every request that reaches `vllm` reuses it
  (11 of 11 across the two runs). Reuse is deterministic once the request lands
  on the cache-capable endpoint.
- But only **44–50 %** of eligible requests reached it. The other half went to
  `ollama`, which cannot reuse anything and is ~2× slower.

The endpoint mix depends on prompt size: `ollama` served 1/12 (8 %) of 2.7k
prefixes but 9/16 (56 %) of 8.8k prefixes. Selection is therefore influenced by
the request itself, not a fixed weight.

The *sequence* of choices (lowercase `o` = `ollama`, `v` = `vllm` miss,
`V` = `vllm` hit) shows runs, not independent draws:

| prefix | sequence |
|---|---|
| 2.7k, unpinned | `vvovvvvvvvvv` |
| 8.8k, unpinned | `ooVVVVooVoooooVV` |
| 8.8k, pinned to `vllm` | `VVVVVVVVVVVV` |

When the eligible run leaves `vllm` it stays away for up to five consecutive
requests and then returns. That is the shape of a pin being lost and re-drawn —
and, on re-draw, the cache-blind endpoint winning and holding the caller for its
TTL — rather than a per-request coin flip. Each excursion costs a full cold
prefill on a prefix that was already warm in the sibling endpoint.

### Streaming behaves the same, and `ollama`'s telemetry gap is real

- `vllm`, streaming with `stream_options.include_usage` → a usage chunk is
  delivered (`cached=4608`). The staging route accepts the flag.
- `ollama`, streaming with the same flag → **no usage chunk at all**, and no
  cache fields. This is the missing-telemetry case the capability fix governs:
  an endpoint that does not accept `stream_options` silently drops it, and
  downstream cannot then tell "no cache" from "no telemetry".
- One streaming `vllm` request had TTFT 30.3 s and 49.5 s total with `cached=0`,
  while a cold prefill of the same size measured 1.2–1.9 s elsewhere in the same
  session. That gap is **queueing/backpressure**, not prefill: the slow
  responses are not all cold prefills.

## Findings

- **F1 — The reported alternation is real and structural.** `glm-5.3-flash` on
  staging is a three-candidate route, read directly from
  `/dashboard/admin/routing` (see "The routing policy, read from the console"):
  ordinary candidates `zai` (`https://api.z.ai/api/coding/paas/v4/`) and `vllm`
  (`http://host.docker.internal:8004/v1`), plus `ollama` (`https://ollama.com/v1`)
  designated as the model's **offload route**. The brief's `fp_ollama` maps to the
  `ollama` endpoint, which is not reachable by that literal pin name.
- **F2 — Endpoint behaviour differs by an order of magnitude in usefulness.**
  `ollama`: different tokenizer, no usage/cache telemetry, ~2× slower, empty
  completion. `vllm`: cache-aware, fast, correct content.
- **F3 — The short-prefix reuse boundary is the engine's, and production has it
  too.** A pinned `vllm` request for a 2.6k-token identical prompt reports
  `cached=0`; so does the *same* request against production. At 18k both
  deployments report exactly `cached=13824` with the same ~4.5k un-reused tail,
  so the earlier "production reuses 2560/2595, therefore staging is broken"
  reading does not reproduce and must have come from a different route instance.
  See "The reuse boundary, measured, on both deployments" for the proof set.
  What *is* deployment-specific is contention and offload, not this boundary.
- **F4 — For eligible prefixes, reuse is deterministic once the request lands
  on `vllm`** (11/11). The residual loss is selection, not the cache.
- **F5 — Normal staging routing reuses eligible warmed prefixes only ~44–50 %
  of the time.** Two effects are now separable and were previously conflated:
  the **offload route** (F8) removes requests from the cache-capable endpoint by
  wait, and the residual split among the ordinary candidates is a selection
  effect. The offload share is prompt-size dependent (8 % at 2.7k tokens, 56 % at
  8.8k), which is exactly what a first-token/queue deadline predicts: a longer
  prompt takes longer to produce a first token, so it crosses the threshold more
  often. Whether the residual ordinary split is the configured weights or
  affinity failing to hold could not be settled — the console renders the weight
  values as number inputs whose values are not exposed to a read-only DOM
  snapshot, and all three rows read "Config default". Any claim that it is "not a
  fixed weight" is therefore unproven; it is left open rather than restated.
- **F6 — The slow responses are heterogeneous.** They include genuine cold
  prefills (first request of a pair), queueing (30 s TTFT at an otherwise 1–2 s
  prefill size), endpoint differences (`ollama` ~2× `vllm`), and — for the
  `ollama` endpoint only — plain missing telemetry. These are distinguishable by
  `cached` + TTFT + the `prompt_tokens` fingerprint.
- **F7 — The gateway already contains the locality machinery.** `FixedRouter`
  keeps a per-`(affinity_key, model_id)` endpoint pin
  (`routers.py::_Affinity`, default on via `ROUTING_AFFINITY_ENABLED`, 300 s
  sliding TTL) and discounts warm continuations in prefill accounting
  (`PrefillLoadTracker.uncached_estimate`). The pin is honoured unless the
  endpoint is absent from `allowed` (health/circuit filter, or an offload-route
  reduction), or its foreign in-flight backlog exceeds
  `ROUTING_PREFILL_AFFINITY_CEILING` (150k tokens, `should_keep_affinity`), in
  which case it is re-drawn and re-pinned. A pin is also dropped on failure
  (`_drop_affinity`). Production's behaviour on the same day (5/5 requests
  reported reuse under automatic routing) is what this machinery looks like when
  it holds — though note the superseded reading above, and that reuse there was
  measured with a prompt size the engine does happen to retain.

## What is external, and what is missing

- **External, precisely identified:** the staging vLLM engine retains only the
  prefix older than its final prefill chunk, so a prompt shorter than that chunk
  reports no reuse and a longer one reuses `prompt − tail` (tail measured at
  2 375–4 511 tokens). Production reports identical values at every tested size,
  so this is the engine's behaviour rather than one deployment's defect — see
  "The reuse boundary, measured, on both deployments". No gateway code path can
  make a worker reuse a prefix it declines to retain; pinning the request to that
  worker is the strongest locality guarantee the gateway can give.
- **`ollama` (the offload route)** reuses nothing and reports no cache telemetry.
  That is one of the reasons it is held back from ordinary traffic and used only
  as a fallback.
- **Read from the console, no longer missing:** the model's route table and
  offload policy (see "The routing policy, read from the console"). Still not
  captured: the **numeric weight values** for the three candidates. The panel
  renders them as number inputs; a read-only DOM snapshot exposes neither `value`
  nor `title` for them, and no screenshot is serialized by the bridge. Everything
  else that decides this behaviour — the offload route's identity, its trigger
  and its parameters, and each candidate's status — was read directly.

## Consequence for the objective

"Normal staging routing reliably reuses eligible warmed prefixes" is **not**
satisfied on the evidence: the measured eligible-prefix reuse rate under
automatic routing is 44–50 %. The shortfall has two components, and the first
draft of this record split them wrongly — the measurement that follows corrects
it:

1. **Engine eligibility (not deployment-specific, not fixable here).** The
   staging engine does not retain the final prefill chunk of a prompt, so a
   prompt shorter than that chunk (~2.4–4.5k tokens, capacity-dependent) reports
   `cached = 0`. Production reports the *same* values at every tested size, so
   this is not "staging is broken and production is not": it is a property of the
   engine on both. The 44–50 % figure is therefore not an eligibility problem
   that a different endpoint would solve — above the boundary the reuse is real
   (TTFT halves) and what varies is *which* endpoint receives the request.
2. **Endpoint selection (gateway-side, dominant).** Half of identical-prefix
   requests were steered to an endpoint that had never seen the conversation even
   though the gateway held evidence that another one had. This split in two and
   both halves are now corrected: the **offload** half (the deadline was
   cancelling cold prefills that would have completed — see "Correction
   implemented"), and the **ordinary-candidate** half (`PrefillLoadTracker` knew
   the per-endpoint un-cached estimate but used it only to discount load and gate
   elephants, while `select_index` broke ties by backlog — see "Selection now
   accounts for locality").

What remains open is *attribution*, not mechanism: how much of the residual the
configured weights explain needs the numeric weights, and a first turn has no
prefix anywhere, so ordinary weighted selection on it is correct as it stands.
That is a tuning question, and this record proposes no weight change.

## Reproduce

The commands below are the **historical local procedures** that produced the
measurements in this record. They ran from this branch's own scratch area
(`.tmp-probe/*.py.scratch`, renamed so the lint gate does not format them) and
that scratch is deliberately **not part of this PR** — a checkout of this
branch does not contain them, and the deployment overlays they pointed at are
not published in this repository. They are kept here as the exact record of
what was run, not as executable instructions for reviewers.

```bash
# staging, automatic routing, 2.7k-token identical prefix
python3 .tmp-probe/probe.py.scratch --mode chat --count 12 --salt U1
# staging, pinned to the cache-capable endpoint
python3 .tmp-probe/probe.py.scratch --mode chat --count 12 --pin vllm --salt U1
# staging, eligible size (compare cached on vllm-pinned vs the tail after ~4.1k)
python3 .tmp-probe/probe.py.scratch --mode chat --count 4 --pin vllm --prefix-repeats 200 --salt B2
# production reference (unpinned)
python3 .tmp-probe/probe.py.scratch --provider production --mode chat --count 6
# the pin oracle
python3 .tmp-probe/enumerate_pins.py.scratch glm-5.3-flash staging
```

Independently executable verification on a checkout of this branch:

- **Unit-level evidence (no deployment needed):**
  `uv run pytest -q tests/unit/routing tests/unit/serving -p no:cacheprovider`
  exercises every contract this record motivated — the strict
  `uncached_estimate` evidence semantics, the locality gates, the
  prefill-aware engine wait, and the offload accounting — against fakes.
- **Deployment-level reproduction (needs an authorized host and its overlay
  credentials):** any OpenAI-compatible client suffices. Send
  `count` consecutive turns of one conversation whose prompt is a fixed
  preamble plus a per-turn delta, with no pin, and compare
  `usage.prompt_tokens_details.cached_tokens` between the first turn and
  turn N+1, then repeat with `route_id` pinned to each candidate. The
  expected shape is exactly what the Measurements section shows: cached on
  the warm endpoint after the engine's size boundary, a full charge
  elsewhere. `/metrics` `gpu_prefix_cache_hit_rate` on the vLLM engine
  corroborates that the reuse is physical.


## Recommendation

1. Capture the three numeric route weights (and `ROUTING_AFFINITY_ENABLED`) for
   staging `glm-5.3-flash`, plus `api_logs` for the probe requests. That is the
   remaining input for the *ordinary-candidate* half of F5; the offload half is
   no longer open (see "Correction implemented").
2. Collect the engine's prefix-cache facts (`/metrics`
   `gpu_prefix_cache_hit_rate`/`queries`/`hits`, vLLM version and the
   `--block-size` / `--max-num-batched-tokens` / `--num-gpu-blocks-override`
   flags) next to production's. The "~4.1k no-reuse head" is **not** a
   staging-only provider defect — production reports identical values — so what
   remains is to decide whether the last-chunk-not-retained behaviour is a
   known vLLM limit worth upgrading or tuning for, not to raise a staging bug.
3. Do **not** reweight `zai` on the strength of this record. A first turn of a
   conversation has no prefix on any endpoint, so ordinary weighted selection is
   correct for it, and a remote provider's weight may encode cost the local
   endpoint does not have. The locality change is what makes *continuations*
   reach the endpoint holding their prefix, and it explicitly declines to
   overturn a strong weight preference
   (`LOCALITY_MIN_WEIGHT_SHARE`). If the staging comparison still shows
   identical-prefix turns splitting after this lands, the weights are the place
   to look — with the numbers, not before.

## The routing policy, read from the console

Read-only, from `/dashboard/admin/routing` with `glm-5.3-flash` selected. API-key
cells are redacted here; the page shows only "Configured default `<x>` key" text
in them.

| Candidate | `on_demand` | Target | Weight control | Status |
|---|---|---|---|---|
| `zai` | yes | `https://api.z.ai/api/coding/paas/v4/` | `YAML · Effective` + Save + **Reset** | Config default |
| `vllm` | yes | `http://host.docker.internal:8004/v1` | `YAML · Effective` + Save | Config default |
| `ollama` | yes | **`https://ollama.com/v1`** | `YAML · Effective` + Save; *"Offload route: gets no ordinary traffic"* | **Offload** |

The `ollama` row's `Status` carries, verbatim:

> title: "Takes requests that waited **0.8s** for a slot or a first token, and
> serves as the **last resort**"

Three consequences:

- The `ollama` candidate is **`ollama.com`, a remote cloud service**, not a local
  server. That explains its tokenizer offset, its absent usage/cache telemetry,
  its ~2× latency and its occasional empty content (F2).
- It is the model's **offload route**, so it receives *no ordinary traffic*: the
  `ollama` observations under automatic routing were **offload**, not a weighted
  draw. The 8 % → 56 % prompt-size dependence is the deadline's signature, not a
  weight's.
- `zai` is the only row offering `Reset`, i.e. the only candidate with a runtime
  weight override beyond the YAML default. The numeric weights are still not
  captured (see above); no console control was operated.

## The offload lifecycle, traced in source

`routing.offload` arms **three** distinct triggers, all with the same
`wait_seconds`:

1. **Gateway queue wait.** `routers.FixedRouter._queue_deadline` returns
   `time.monotonic() + policy.wait_seconds`, pushed as
   `req_ctx.UPSTREAM_QUEUE_DEADLINE` around an attempt. It is enforced in
   `upstream_limiter._queue_wait` (a remote provider's slot queue) and
   `_hold_wait` (a local engine's `EngineHold`). Expiry raises
   `UpstreamQueueWaitExpired` → `OFFLOAD_QUEUE_WAIT`. The request was never sent.
2. **Engine first-token wait.** `FirstTokenWatch` (`routing.engine_wait`),
   enforced by `asyncio.timeout` inside `FixedRouter._await_first_token`. Expiry
   raises `EngineWaitExpired` → `OFFLOAD_ENGINE_WAIT`.
3. **Last resort.** `FallbackOrder` hands the offload route over when nothing
   else is admissible, or after everything else has failed.

Precise answers to the questions the objective asks:

- **When the timer begins.** The *queue* clock is absolute, armed when the router
  prepares the attempt — before dispatch, and one value is reused for that
  request's later attempts. The *engine* clock is different: `FirstTokenWatch.arm`
  starts it, but `on_sent` (`dispatch_watch.report_sent`, called by
  `upstream_limiter.acquire` when the request takes a slot or is sent to a local
  engine) *restarts* it, so the engine wait begins when the request leaves the
  gateway for the engine.
- **What pauses it.** `on_queued` (`dispatch_watch.report_queued`) pauses the
  engine clock while the request waits for an outbound slot or inside a local
  `EngineHold`. Gateway queue time therefore never counts against the engine —
  proven by `test_time_in_the_gateway_queue_does_not_count_against_the_engine`.
- **What the first token is.** The first frame the adapter reads from the
  upstream that carries generated output — text, reasoning, or any part of a tool
  call — reported *before* a stream processor can hold it back
  (`dispatch_watch.report_first_token`). Output an adapter buffers whole (a MiniMax
  `think` block, a tool call's JSON) therefore does *not* count against the
  deadline (`test_output_a_processor_holds_back_is_not_an_engine_wait`).
- **Does the gateway cancel an attempt that was about to finish prefill? Yes.**
  There is no prefill-progress signal at all: the only evidence of progress is
  the first token, and it arrives *after* prefill. On expiry
  `_await_first_token` unwinds the attempt's stream, which closes the connection
  and aborts the request inside the engine. The prefill already performed is
  discarded, and the prompt is prefilled **from scratch, cold, on the offload
  route** — duplicated work and a guaranteed cache miss on a service that shares
  no KV with the engine that was answering. Closing the stream is what aborts the
  request at the engine (`_Engine.cancelled` in
  `tests/unit/routing/test_engine_wait_routing.py`).
- **The engine is not blamed.** `EngineWaitExpired` is exempt from the circuit
  breaker (observed in a test run as `engine_wait_skip_breaker`, and asserted in
  `test_a_stream_with_no_first_token_is_cancelled_and_offloaded`). The policy's
  intent is right; only its trigger is blind.

## Correction implemented — a prefill-aware, bounded engine wait

The defect is that one constant bounds a quantity — prefill + time-to-first-token
— whose expected value grows with the prompt's *un-cached* length, so the
deadline fires hardest exactly when the engine is doing legitimate work. The
correction keeps the policy's shape and bounds and makes the wait a function of
the work it bounds:

- `routing/offload.py`: `OffloadPolicy.engine_wait_seconds(uncached_tokens)` =
  `min(wait_seconds * (1 + uncached / OFFLOAD_PREFILL_CREDIT_TOKENS),
  OFFLOAD_MAX_WAIT_SECONDS)`, never below `wait_seconds`. `OFFLOAD_PREFILL_CREDIT_TOKENS
  = 4096` and `OFFLOAD_MAX_WAIT_SECONDS = 8.0`.
- `routing/routers.py`: `_watch_for` builds the watch from
  `engine_wait_seconds`; both the primary and the fallback stream get their
  budget from `_engine_wait_tokens`, which requires the conversation
  fingerprint (see below).

**Offset, not proportion.** The first revision scaled the base
(`max(base, base * uncached / credit)`), which gives a prompt of exactly one
credit — 4.1k tokens — the base and nothing more. Replaying that rule against
the measured staging prefills shows the defect is in the *tail*, not the mean:
twelve cold 2.6k-token requests on the pinned engine finished in 0.49–0.97 s
(mean 0.63 s) under the 0.80 s floor, so two of twelve successful prefills would
have been cancelled and re-sent cold elsewhere; at 9.1k the 1.78 s budget sat ten
milliseconds *under* a measured 1.79 s. Offsetting gives the base as a fixed
allowance — queueing, scheduling, the first decode step — and buys another base
wait of prefill per credit.

Replay of both rules over the 27 successfully completed pinned prefills measured
on staging (TTFT, i.e. prefill plus first token, as the observable proxy; the
same samples the calibration below is drawn from):

| un-cached prefill | measured TTFT | old budget | new budget | old would cancel | new would cancel |
|---|---|---|---|---|---|
| 1 819 | 0.46–0.56 s | 0.80 s | 1.16 s | 0/2 | 0/2 |
| 2 659 ×12 | 0.49–0.97 s | 0.80 s | 1.32 s | **2/12** | 0/12 |
| 4 519 | 0.86 s | 0.88 s | 1.68 s | 0/1 | 0/1 |
| 4 321 (8.9k prompt, 4.6k cached) ×7 | 0.67–0.77 s | 0.84 s | 1.64 s | 0/7 | 0/7 |
| 9 119 ×2 | 1.52–1.79 s | 1.78 s | 2.58 s | **1/2** | 0/2 |
| 18 319 ×2 | 1.52–1.79 s | 3.58 s | 4.38 s | 0/2 | 0/2 |

Three of twenty-seven measured successful prefills would have been cancelled by
the old rule; none by the new one. This is a replay of a decision rule over
measured observations, not a deployment measurement — it says the rule was
mis-sized against the engine that is actually running, and nothing more.

Headroom is deliberately not uniform. The new budget is above every measured
sample, but the tightest case is the one where the deadline binds on the tail:
1.32 s against 0.97 s at 2.6k (1.36×), versus 1.44× at 9.1k and 2.45× at 18.3k.
A fit through these points is not a straight line — the 9.1k and 18.3k runs both
land at ~1.5–1.8 s, which looks like a scheduling quantum rather than
token-proportional prefill — so the constant encodes a conservative bound, not a
measured rate. The ceiling is about the extrapolated cold prefill of a
100k-token prompt and well under the contended remote TTFT already observed on
the offload route (30–49 s at the tail), so waiting out the engine stays the
cheaper choice even when the ceiling binds.

**Evidence scope.** The budget is sized on an *un-cached* estimate, so it
inherited the same weakness the locality term had: `affinity_key` is the API-key
hash and the hint table keeps one prefix per (key, endpoint), so a sibling
conversation's prefix read as warm and produced a *shorter* wait — the same
cancellation, reached through the evidence rather than the deadline.
`_engine_wait_tokens` now requires the conversation fingerprint and charges a
request it cannot identify in full; over-waiting costs nothing while the engine
answers, and the ceiling bounds it when the engine does not.

Unchanged, deliberately: the gateway queue-wait trigger and its absolute
deadline (still `wait_seconds`, not the extended budget — verified by a test
that holds the outbound slot and shows a 100k-token prompt leaving the queue at
0.05 s while its engine budget would be ~1.27 s); `engine_queue_limit` and
`EngineHold`; the last-resort path; the `FallbackOrder` retry of a route cut
short; circuit-breaker and cancellation semantics; `max_input_tokens`. Nothing
here disabled offloading or pinned an endpoint permanently.

Regression coverage:

- `tests/unit/routing/test_offload_routing.py` — the value: the offset form
  against the measured prefill times, the ceiling, monotonicity, and that a
  configured wait above the ceiling is never shortened.
- `tests/unit/routing/test_engine_wait_routing.py` —
  `test_a_cold_prefill_longer_than_the_base_wait_is_not_cancelled_mid_prefill`
  (served locally, `cancelled == 0`), `test_a_short_prompt_still_offloads_at_the_base_wait`
  (the case the feature exists for), `test_a_fallback_stream_is_watched_under_its_own_budget`
  (the second call site), `test_a_warm_continuation_keeps_the_base_wait`,
  `test_a_stale_hint_does_not_shorten_the_wait` (`forget_endpoint` after a
  restart restores the full budget), `test_an_unrelated_conversation_does_not_shorten_the_wait`,
  `test_a_large_prompt_is_bounded_by_the_wait_ceiling`, and
  `test_the_adaptive_wait_does_not_extend_the_queue_wait`.

Mutation checks: a flat base fails the cold-prefill tests and the captured log
shows `engine_wait_skip_breaker ... within 0.1s` followed by
`route_offload reason="engine_wait"` (the staging symptom); loose evidence in
`_engine_wait_tokens` fails `test_an_unrelated_conversation_does_not_shorten_the_wait`.

Status: local commit only. Nothing was pushed, deployed, or configured on
staging, and no live routing policy was changed.

## Selection now accounts for locality

The requirement is that route selection account for expected un-cached prefill
cost, actual endpoint capacity and cache locality, and that it distinguish a
healthy cache-warm endpoint from a short recoverable queue, an overloaded one,
and one with no known prefix. Two of those three inputs already existed and were
not used for selection:

- `PrefillLoadTracker.uncached_estimate(endpoint, tokens, affinity_key)` — how
  much of *this caller's* prompt that endpoint must still prefill. Used to
  discount backlog and to gate elephants; never to choose.
- `self._backlog[endpoint]` — the live, capacity-relevant load. Used only as the
  power-of-two tie-break, and only above `INTERVENE_TOKENS`.

So the draw could give a conversation with a resident prefix to a cold replica
while the tracker knew the warm one would cost thousands fewer tokens. That is
the Fixed-versus-RouteWise asymmetry: `routing.routewise.prefix_cache` tracks
reuse evidence per endpoint, `FixedRouter` had no locality term.

`PrefillLoadTracker.select_index` ends with `_prefer_cached`, under two
independent gates.

**Evidence.** A locality verdict is not a load estimate: it moves the request
*and* records affinity for the next turn, so a wrong one persists instead of
self-correcting. It therefore runs on the strict estimate — conversation
fingerprint plus the prompt anchor from `messages` — and is skipped entirely
when the caller cannot supply a fingerprint. This is what stops a conversation
from inheriting a sibling's prefix on a shared API key. The load and elephant
numbers keep the loose caller-scoped discount, unchanged.

**Authority.** Weights encode provider cost and operator preference, not only a
load split, so locality refines a choice between comparable candidates and does
not overturn a strong one. Two thresholds say what "comparable" and "worth it"
mean:

- `LOCALITY_MIN_SAVING_TOKENS` (4096) — the move must avoid at least this much
  prefill; below it the difference is jitter or a partly-shared prefix.
- `LOCALITY_MIN_WEIGHT_SHARE` (0.5) — the warm candidate must carry at least
  half the weight of the candidate the draw chose. A 60/40 pair may follow the
  prefix in either direction; 90/10 stays on the 90. A zero-weight candidate
  never wins.

It also never resurrects a candidate dropped by `avoid` or by the elephant
saturation filter, and never moves to one whose foreign backlog is past
`AFFINITY_BACKLOG_CEILING` — the same judgement `should_keep_affinity` already
makes for a sticky pin, so a *short recoverable queue* is preferred but an
*overloaded* endpoint is not.

The mapping to the four states the requirement names: a warm endpoint with a
short queue wins on the un-cached saving; the same endpoint with a foreign
backlog past the ceiling loses to capacity; an overloaded or unavailable endpoint
is already excluded by health/elephant filters; an endpoint with no known prefix
has no discount, so it competes only on weights and backlog as before. A first
turn has no prefix anywhere, so it is unaffected by all of this — which is why
this change proposes no reweighting of `zai`.

Not a hard pin and not a cache-hit maximizer:
`ROUTING_PREFILL_AWARE_ENABLED=0` still disables the whole prefill-aware path.

Regression coverage — tracker (`tests/unit/routing/test_prefill_load.py`): a
material saving redirecting an equal-weight draw; a 90/10 pair staying on the
90; a 40/60 pair following the prefix; an unrelated conversation on the same key
getting no locality; no fingerprint keeping the draw; a fork rejected by the
anchor while its continuation is accepted; an immaterial saving; a backlogged
warm endpoint; `avoid`; no caller identity. Router
(`tests/unit/routing/test_session_affinity.py`): the same three cases through
`_select_adapter`, plus two end-to-end `chat_completion` cases — one warm
conversation routed to its prefix, one sibling conversation not — so a call site
that forgets to pass the fingerprint fails.

Mutation checks: removing the fingerprint gate, removing the weight guard,
sizing the wait on loose evidence, and dropping the call-site argument each fail
a test; the end-to-end cases are the ones that catch the last of those.

**The second entry point.** The opt-in hybrid composition draws globally in
`FixedPolicy.select_backend` and then forces the target it names, so that draw
also needed the conversation: `select_adapter()`/`_select_adapter()` carry the
fingerprint and `select_backend` computes it from the same inputs, or the one
remaining cache-blind selection would be the first one in that path. Covered by
`test_policy_carries_the_conversation_into_locality_selection`.

Status: local commit only, as above.

## The reuse boundary, measured, on both deployments

Measured directly, after the console read, with the probe in `.tmp-probe/`
(pinned `X-Route-Pin: vllm` on staging; production unpinned). Each row is the
*second* of two byte-identical requests, so the engine has had a chance to
populate its cache. `SALT` differs per experiment, so no row inherits another's
prefix.

### The law

| prompt | staging cached | staging tail | production cached | production tail |
|---|---|---|---|---|
| 1819 | 0 | 1819 | 0 | 1819 |
| 4519 | 0 | 4519 | 0 | 4519 |
| 9119 | 4608 | 4511 | 4608 | 4511 |
| 18319 | 13824 | 4495 | 13824 | 4495 |

Production is *numerically identical* to staging at every size. Controls:

| request | cached | tail |
|---|---|---|
| production, 2809 tokens, `max_tokens=8` | 0 | 2809 |
| staging, 2809 tokens, `max_tokens=8` | 0 | 2809 |
| staging, 2809 tokens, `max_tokens=512` | 0 | 2809 |
| staging, 4679 tokens, `max_tokens=1` | 2304 | 2375 |
| staging, 9369 tokens, `max_tokens=1` | 6912 | 2457 |

So `cached = prompt − tail`, where the tail is **not** fixed: it ranges from
about 2.4k to 4.5k with what the engine scheduled, and a prompt that fits inside
one such chunk reports `cached = 0` in full. `max_tokens` is irrelevant (8 vs
512 vs 1 changes nothing), so it is not a generation-side reservation.

### Reuse is physically real — this is prefix matching, not a counter

| probe | cached | TTFT first → second |
|---|---|---|
| staging, 9369 tokens | 6912 | 1517 ms → 720 ms (**2.11×**) |
| staging, 18319 tokens | 13824 | 1519 ms → 787 ms (**1.93×**) |
| staging, 18019 tokens, **tail changed** | 13824 | unchanged from identical |
| staging, 18020 tokens, **head changed** | **0** | full recompute |

A one-character change at the *head* destroys reuse; the same change at the
*tail* does not. That is the signature of a head-anchored block-hash prefix
cache: the matched part is a genuine prefix of the prompt, and what is never
retained is its final chunk.

### What this means

- There is **no staging-specific short-prefix defect**. The "~4.1k threshold"
  in the original brief is this engine-side cache-insertion boundary, and
  production exhibits it identically. Any claim that production caches a 2.6k
  prompt and staging does not is not reproducible against the current
  deployments.
- The boundary is **engine- and capacity-dependent, not a fixed number**: the
  same 9k prompt reported `4608` in one run and `0` in another, which is what a
  shared, capacity-limited block pool looks like from outside. That makes it a
  scheduling/occupancy property, not a configured threshold.
- No gateway change can make a 2.6k prompt reuse on this engine. What the
  gateway *can* control is already addressed above: send the request to the
  endpoint that holds the prefix (locality selection) and stop cancelling the
  prefill before it finishes (the bounded engine wait). Both were implemented.
- For the engine side, the exact non-secret telemetry to collect next to
  production's is: the vLLM version, `--enable-prefix-caching`, `--block-size`,
  `--max-num-batched-tokens`, `--num-gpu-blocks-override` and the prefix-cache
  counters on `/metrics` (`gpu_prefix_cache_hit_rate`, `gpu_prefix_cache_queries`,
  `gpu_prefix_cache_hits`). Those distinguish "one chunk is never inserted into
  the cache" from "the block pool is too small to retain it", which is the only
  remaining question and is not answerable from the gateway.

Rejected as fixes: disabling offload, pinning every request to `vllm`, or
declaring the boundary a configuration finding. The first two trade capacity
protection and fallback for a hit rate; the third reports a measurement as a
repair.

## Controlled local experiment (run)

Before the staging comparison, the two policies were compared under control
here: the real `FixedRouter`, `PrefillLoadTracker` and `OffloadPolicy` driven
against simulated engines — a cache-capable local engine calibrated to the
measured 9.1k cold tail (0.30 s + 164 µs/token = 1.79 s, the sample the old
budget cancels) on one GPU slot, and a remote route at the measured 1.8 s that
never caches. The harness lives in `.tmp-probe/experiment.py.scratch`
(read-only scratch, not committed). This isolates the *policy*; it says nothing
about staging's actual engine.

| workload | old (proportional) | new (offset) |
|---|---|---|
| idle: one cold 9.1k prefill | offloaded 1/1, TTFT 3 605 ms | served locally, TTFT 1 805 ms |
| warm: seed 9.1k, then 3 continuations | offloaded **4/4**, all cold | offloaded 0/4; seed 1 803 ms, continuations ~21 ms |
| moderate: 4 concurrent cold 9.1k, one GPU slot | offloaded 4/4 | offloaded 3/4 (head of line survives) |
| overload: 8 concurrent cold 16k | offloaded 7/8, p50 4 930 ms | offloaded 7/8, p50 5 744 ms |

Two readings, one on each side of the ledger:

- **The warm row is the staging complaint reproduced.** The old rule cancels the
  seed 10 ms before it finishes, so the prefix never forms; every subsequent
  identical request is cold again, and all of them — not just the first — pay
  the full remote latency. A deadline that mis-sizes one prefill doesn't cost
  one request; it costs the whole conversation's cache.
- **The overload row is the honest cost.** Under a deep engine queue both rules
  shed the same 7 of 8, but the longer budget delays each departure by ~0.8 s.
  Offloading load faster is something the old rule did *better*. Whether
  staging's engine ever queues deep enough for this to matter is precisely what
  the staging comparison's bounded-overload phase measures.

Selection-layer probes from the same harness, for the locality half: 16
concurrent `select_adapter` calls for distinct conversations with no affinity
split 6/2 across equal-weight replicas (the cache-blind draw), while a burst
under one shared API key converges on the replica that served its first request
— the pre-existing per-(key, model) affinity pin, which precedes locality and
dominates same-key traffic in one process. Sixteen concurrent 11k prefills
collapse onto one replica outright through the elephant gate. So in-process,
the locality term is a backup for when the pin is dropped (backlog ceiling,
which locality deliberately respects too), and the burst split the objective
opens with is either that drop or more than one gateway instance — which is a
staging observation to take, not a local one.

## Open-PR landscape and cross-PR audit (2026-10-23)

`upstream/dev` moved `4b80416f` -> `9b48afd9` mid-review: #1627 (DeepSeek-profile
OpenAI-spelled cache counts, `adapters/profiles.py`) and #1628 (Next.js). Neither
touches this branch's files. New in the open set: #1632 (static provider-key
lookup on route edit), extending the #1631/#1630 route-editing family. All the
PRs this work depends on or overlaps are MERGEABLE; #1609 and #1616 carry
CHANGES_REQUESTED and #1462/#1502 still hold their known open threads
(fallback-lease; duplicate-classification) — none of which this branch touches,
and none of which it should resolve. The full 18-commit stack was cherry-picked
onto `9b48afd9` in a throwaway worktree as a merge rehearsal: zero conflicts,
and routing + servers + adapters (including #1627's new profile tests) pass
together — 2 872 passed, 2 skipped.

Capability loader against the runtime admin paths (#1631/#1630), audited in
source at the base the branch carries:

- **YAML routes** re-derive `include_usage_in_stream` from models.yaml on every
  reload, kind defaults included, so the explicit-False-must-not-flip-to-default
  contract holds across restarts and restores.
- **Runtime-created routes** copy the template route's config
  (`_config_to_dict` walks every `ModelConfig` field, the flag among them), so
  the new route carries the template's value — including one the template only
  had as a *kind default*, now silently pinned as if declared.
- **The edit path can repoint a route at a different kind**
  (`UpdateProviderRouteRequest.upstream_provider` -> `_make_adapter(target.kind,
  cfg)`). Because the copied key is present, the new kind's default never
  applies: a route moved from vLLM (assumed True) onto a kind that rejects the
  unknown body field keeps sending `stream_options.include_usage` there. That
  is a narrow, admin-invoked drift, and the right home for fixing it is the
  capability schema #1630 is actively reshaping (`RuntimeModelCapabilities`
  persists exactly four fields today) — recorded here for those reviewers, not
  patched across their open PRs.
- **Restore** rebuilds from the stored row, which persists only the four
  declared capabilities; the usage flag falls back to template copy or kind
  default. Same owner.

Hot-path composition with #1462's transaction boundary and #1461's priority
propagation, measured (3 ordinary candidates):

| evidence | cost | where it runs |
|---|---|---|
| `conversation_fingerprint` | <= 4 messages x 2 048 chars, microseconds | before `routing_transaction()`, outside the lock |
| `prompt_anchor` (full prefix) | 0.8 ms at 9k tok; ~3 ms at 500k tok | before the transaction, outside the lock |
| anchor re-verification per warm candidate (in `select_index`) | 0.27 ms at 45k; ~3 ms at 500k tok | inside the transaction, per hinted candidate |
| provider I/O in either | none | — |

No provider I/O enters the transaction, and the fingerprint/anchor hashing the
router itself performs sits outside it. What remains inside is the anchor
re-verification `uncached_estimate` does for a candidate whose hint fingerprint
matches — one incremental blake2b pass over the request prefix, scaling with the
remembered turn, worst case a few ms for a 500k-token conversation with two warm
candidates. Hoisting it out would mean reading the hint table outside the
transaction and a stale-hint race argued against #1462's reservation semantics —
that boundary is #1462's to own while its fallback-lease thread is open, so the
measurement is recorded for those reviewers rather than restructured here.

## Controlled staging comparison (prepared, not run)

Nothing above is a deployed measurement. The two corrections are local commits
on `bamn/fix/route-include-usage`; staging still runs the code these findings
describe. This is the comparison to run once that branch is deployed, written so
the *result* is falsifiable rather than a report of effort.

### What must be true before it can run

1. **Deployment authorization** for the branch on staging. Every number below is
   baseline-versus-patched on the same host, so it cannot be produced from here.
2. **The three route weights** for staging `glm-5.3-flash` (`zai`, `vllm`,
   `ollama`) as stored configuration. The console exposes the endpoint list but
   not the numeric weights, and the 90/10 case above is the contract that needs
   them: if `zai` is weighted far above `vllm`, locality will correctly decline
   to move traffic, and the measured effect will be smaller than expected for a
   reason the experiment must be able to name.
3. **Engine telemetry** from the staging vLLM: version, `--enable-prefix-caching`,
   `--block-size`, `--max-num-batched-tokens`, `--num-gpu-blocks-override`, and
   `/metrics` `gpu_prefix_cache_hit_rate` / `_queries` / `_hits`. These separate
   an engine-side retention limit from a capacity limit, and they are the only
   inputs the gateway cannot infer.

None of the three changes the code under test; they change what the result can
be attributed to.

### Workloads

Three phases, each with the same prompt corpus so the cells are comparable. The
corpus is a fixed preamble of a known size (2.6k, 9k, 18k) plus a per-turn delta,
sent as consecutive turns of one conversation on one API key, which is the shape
that has a warm prefix.

| phase | concurrency | purpose |
|---|---|---|
| idle | 1 | the cleanest locality signal: does turn N+1 reach the endpoint that holds turn N? |
| moderate | 4 conversations | contention for endpoints, not for the engine: does the locality term still win, and does the backlog ceiling engage? |
| bounded | 4 conversations, each with an in-flight uncached prefill of ~16k | overload: does offload still fire, and are the cancellations the ones the policy wants? |

Each cell runs 16 requests per conversation so the per-cell local-share figure
has the same denominator as the 4/8 and 7/16 measurements in the brief.

### Metrics, per phase

| metric | source | baseline / pass condition |
|---|---|---|
| local share of identical-prefix turns | `_routing.endpoint_id` per response | baseline measured again on the same day; patched must not be lower |
| `cached_tokens` per turn (engine) | `usage.prompt_tokens_details` | unchanged or higher; the engine boundary is fixed, so this mostly proves the request reached the right engine |
| offload rate and cause | `_routing.offload` (`queue_wait` / `engine_wait` / `last_resort`) | `engine_wait` must fall; `queue_wait` must not rise |
| local cancellations before first token | `EngineWaitExpired` count in `api_logs` / `failed_attempts` | must fall under moderate load and not rise under overload |
| TTFT p50 / p95 / p99 | `api_logs` first-token latency | patched p95 must not regress; the expectation is a fall on warm turns |
| end-to-end latency p50 / p95 / p99 | `api_logs` duration | must not regress |
| throughput and queue wait | request rate at fixed concurrency; `UpstreamQueueWaitExpired` | throughput must not fall |
| remote inference cost | `api_logs.cost_usd` summed per phase | must not rise |
| correctness / fallback | response content equality against the baseline run; `_routing.fallback` | identical content; fallback count must not rise |

The pass condition is deliberately asymmetric: *cache reuse and `engine_wait`
offloads improve, and nothing user-visible regresses.* A hit-rate gain bought
with latency, fallback or cost is the failure mode the objective forbids.

### Reproduce

The probe used for the measurements above lived in this branch's scratch area
(`.tmp-probe/probe.py.scratch`, read-only, **not committed** — a checkout of
this PR does not contain it). The commands that follow are the historical
local procedure, kept for the record rather than as reviewer-runnable steps;
the independently executable path is the unit suite under
[Reproduce](#reproduce) above, and the deployment-level recipe there applies
per cell once staging is authorized.

```bash
# one cell: 16 identical-prefix turns on one conversation, pinned to nothing
python3 .tmp-probe/probe.py.scratch --count 16 --salt CELL1 --size 18319
```

Baseline and patched runs must use different `--salt` values for the *corpus*
and the same sizes, so neither inherits the other's warm blocks. Report each
phase as a table row, not as a single aggregate: the interesting result is
usually the tail.

### What is already measured, and what is not

Measured from here, against the live deployments, with no code change deployed:
the reuse law and its engine-side boundary (both deployments), the engine wait's
calibration inputs (staging cold-prefill times), and the route composition (read
from the console). Not measured, and not claimed: any improvement in staging
reuse, offload rate, cancellation count, TTFT or cost. Those require the
deployment in step 1.

## Full-suite qualification (kept record)

A full-suite run initially showed 18 failures in
`tests/unit/serving/test_http*` and was first mis-attributed to pre-existing
pollution. A pristine `upstream/dev` checkout (`4b80416f`) runs the identical
suite clean, which pinned the polluter to this branch's own
`tests/unit/servers/test_registry_include_usage.py`: it patched `stream_post`
on the process-wide `AsyncHTTPClient.shared()` singleton instead of shadowing
the adapter's instance attribute. The fix lands with the usage-capability PR.
With it, the full `tests/unit tests/api` suite is **6 857 passed, 12 skipped,
0 failed**. Three `tests/unit/ops/test_classify_changes` failures appear only
on the qualifying machine: its global git `core.hooksPath` identity guard
breaks those tests' own commit subprocesses; dev shows the same three under no
override, which identifies them as environmental.

### Rollout and rollback

Merge order A → B → C. A first: it carries the test-pollution fix that keeps
the full suite green for everything after it, and its behavior change
(`stream_options.include_usage` on by default for the usage-capable kinds) is
exactly what B's `cached_tokens` verification wants to see. B and C are mutually
independent — either order merges clean; B before C only because C carries the
shared record that references B's merged locality contract. No PR touches
storage, migrations, or required configuration, so a rollback is a revert, not
a migration.

Per-PR rollback, independent of the others:

| PR | revert | behavior restored |
|---|---|---|
| A usage capability | revert the PR (or set `include_usage_in_stream: false` on affected routes in models.yaml, while the PR is deployed — the explicit per-route escape hatch is the PR's own addition and does not survive its revert) | restores dev's original kind defaults: `deepseek`, `zai`, `kimi`, `minimax`, `sglang` and `vllm` resume sending `stream_options.include_usage` unconditionally, and the ability to override them explicitly (including `false`) is removed; kinds outside the set (e.g. `ollama`) behave exactly as before the PR |
| B locality selection | revert the PR | weighted draw + load only; affinity hints stop influencing selection |
| C offload deadlines | revert the PR | fixed 0.8 s engine deadline; pre-cancellation offload behavior |

Rollback safety notes: no candidate adds an operator-visible config surface
that must be removed by hand; no candidate changes a persisted schema; the
runtime-route inheritance gap documented for #1630/#1631 (#1630-32 territory)
is unaffected by rollback order. During the comparison window, stage A and B
together before C only if the engine-wait comparison needs both — the metrics
table above attributes each effect to its own PR's mechanism, so staggered
deploys are also valid: B's effect reads in `endpoint_id` distribution, C's in
`engine_wait` offload counts, A's in `cached_tokens` presence.
