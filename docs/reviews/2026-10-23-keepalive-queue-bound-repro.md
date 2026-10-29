# SSE keepalive queue bound: gateway-level slow-client reproduction

Date: 2026-10-23. PR: HarvardMadSys/hybridInference#1560.

## Question

Does the bounded hand-off queue in the SSE chain reach the provider through a
slow HTTP client, and what does it cost in memory? The forced-streaming
branch is measured as a comparison, but still buffers the whole generation
before returning one JSON body by design; it is not the memory fix.

## Method

A local one-off gateway harness drove the real serving app
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

- The behavior change is on the plain SSE path: its inner queue is capped at
  256 and pins there under a slow consumer, after which the application stops
  pulling from the adapter.
- In the forced-streaming branch the outer consumer still accumulates the
  complete generation in ``content_parts`` before emitting one JSON body, by
  design. Its hand-off queues are bounded, but the response body remains
  whole-generation buffering; this PR does not solve that separate issue.
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

The harness was a dated local procedure, deliberately not shipped as a
permanent script. Rebuild it from this section: start the real serving app
(``create_app``) with uvicorn, provide a local aiohttp SSE upstream, drive a
stalled raw-socket client, and instrument scoped ``asyncio.Queue`` instances
for maxsize, peak depth, put/get counts, and post-disconnect depth. Assert
that no queue exceeds the bound, total buffered items remain within two
bounds plus slack, and each queue is at or below its bound after disconnect.
Compare the same workload with the source change stashed.

The exact historical script is retained outside the repository in the
authoring environment for traceability; it is not part of the reviewable
contract.
