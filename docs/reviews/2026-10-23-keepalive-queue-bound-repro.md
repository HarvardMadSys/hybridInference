# Keepalive queue bound: gateway-level slow-client reproduction

Date: 2026-10-23. PR: HarvardMadSys/hybridInference#1560.

## Question

Does the hand-off bound in the forced-streaming chain actually reach the
provider through a slow HTTP client, and what does it cost in memory?

## Method

scripts/repro_gateway_slow_client.py drives the real serving app
(serving.servers.app.create_app, the same factory uvicorn runs) over real
uvicorn HTTP, against a real local SSE upstream (aiohttp) that emits
pre-built ~2 KB chunks as fast as the socket takes them. The client is a raw
socket: it sends the request, reads the response head, then stops reading
while keeping the socket open. That is a stalled reader, not a disconnect.

Two scenarios:

1. force-streaming buffered branch - stream=false plus the
   force_chat_completions_streaming runtime switch, through the documented
   app.dependency_overrides pattern. Chain: provider, adapter, inner queue,
   session generator, outer queue, JSON body, client. This branch returns ONE
   JSON body, so the client sees no bytes until the whole generation is
   buffered; the stall shows up in memory and in teardown shape, not in
   incremental delivery.
2. plain SSE path (stream=true) - only the inner queue exists. The client
   stalls mid-stream and uvicorn write flow control suspends the response.

Instrumentation: process RSS (VmRSS), queue depths, and per-queue put/get
counts, observed through a scoped asyncio.Queue spy that only records.
Traffic is identical for both runs: 6000 chunks x ~2 KB (~12 MB).

## Before - revision 88843553 (previous head of this PR)

    [force-streaming] queue[0] maxsize=256 peak_depth=2 puts=3403 gets=3402
    [force-streaming] queue[1] maxsize=0   peak_depth=0 puts=3404 gets=3404
    [force-streaming] RSS delta=25176KB
    [force-streaming] queue depths after client disconnect: (2, 103)
    [sse-stall]       queue[0] maxsize=0   peak_depth=1 puts=192  gets=191
    [sse-stall]       RSS delta=4628KB
    [sse-stall]       queue depths after client disconnect: (0, 96)

The inner queue had no cap: maxsize=0, unbounded.

## After - this branch

    [force-streaming] queue[0] maxsize=256 peak_depth=2 puts=4207 gets=4206
    [force-streaming] queue[1] maxsize=256 peak_depth=1 puts=4208 gets=4208
    [force-streaming] RSS delta=33396KB
    [force-streaming] queue depths after client disconnect: (2, 209)
    [sse-stall]       queue[0] maxsize=256 peak_depth=256 puts=1578 gets=1322
    [sse-stall]       RSS delta=9640KB
    [sse-stall]       queue depths after client disconnect: (0, 256)

In the SSE scenario the inner queue saturates at exactly the bound (256) and
stays there: 1578 items were put, 1322 delivered, 256 held in the capped
queue, and the reader is parked on put(). The application stops pulling from
the adapter, so the provider observes the stall at the application boundary.

## What the numbers do and do not show

- After the change, the application-side buffering of the chain is capped at
  both hops (256 + 256). The inner queue is pinned at its bound, never above
  it.
- Before the change, the inner queue had no cap at all. On this loopback
  setup the provider socket throttled the chain before the unbounded queue
  visibly accumulated (peak depth 1 in the SSE scenario), so unbounded growth
  here is a latent hazard for fast or remote providers rather than the
  dominant buffer. The guarantee is therefore stated as what it is:
  application-side buffering is capped, and the provider observes the stall
  once those caps fill. The kernel socket buffers and the read buffer of the
  HTTP client remain outside this chain.
- The two runs differ in pulled volume (192 before, 1578 after). That
  difference depends on the mock upstream sharing the event loop with the
  gateway, so it is a property of this harness, not a throughput comparison.
  The depth measurements are the signal.
- RSS is reported, not asserted: the delta is dominated by the by-design
  whole-generation buffering of the force-streaming branch plus interpreter
  and lazy-import noise on the first request. The depth checks are the hard
  assertions in the script.

## Reproducing

    python scripts/repro_gateway_slow_client.py

REPRO_CHUNKS (default 1200), REPRO_UPSTREAM_PORT and REPRO_GATEWAY_PORT
override the traffic size and the ports. The script exits non-zero if a
queue exceeds the bound, if the total buffered items exceed 2 x bound plus
slack, or if a queue still holds more than its bound after the client
disconnects.

To reproduce the "before" numbers, stash the source change (the script only
needs the tests and the script itself):

    git stash push apps/backend/serving/servers/routers/completions_stream.py \
        apps/backend/serving/servers/routers/completions.py
    python scripts/repro_gateway_slow_client.py
    git stash pop
