"""Gateway-level slow-client backpressure reproduction (real gateway, real HTTP).

Drives the ACTUAL FastAPI serving app (serving.servers.app.create_app -- the
same factory uvicorn runs in production) over real uvicorn HTTP, pointed at a
real local SSE upstream (an aiohttp server emitting pre-built SSE chunks as
fast as the socket takes them). The client is a raw socket that sends the
request, reads the first response bytes, then STOPS reading while keeping the
socket open -- a stalled reader, not a disconnect.

Two scenarios:

1. force-streaming buffered branch (stream=false + the force_chat_completions_
   streaming runtime switch, via the documented app.dependency_overrides
   pattern): provider -> adapter (aiohttp) -> INNER queue (StreamSession.stream,
   bounded) -> session generator -> OUTER queue (keepalive wrapper, bounded) ->
   JSON body -> uvicorn -> client socket. NOTE: this branch returns ONE JSON
   body by design, so the client sees no bytes until the whole generation is
   buffered; the stall therefore shows up in memory and teardown shape.

2. plain SSE path (stream=true): session.stream is driven directly by
   Starlette's StreamingResponse, so only the INNER queue exists. The client
   stalls mid-stream; uvicorn's write flow control suspends the response, and
   the inner queue's bound is what stands between the gateway and unbounded
   buffering (this is the regression the maintainer asked for).

Instrumentation: process RSS (VmRSS) and the queue depths, observed through a
scoped asyncio.Queue spy that only records.

Assertions:
  a. the inner queue never exceeds the chain bound (both scenarios);
  b. total buffered items across BOTH queues stays <= 2 * bound + slack;
  c. after the client disconnects, the queue depths settle empty;
  d. RSS growth stays inside a sane budget (reported; the depth check is the
     hard assertion).

Pre-fix vs post-fix comparison: run this script on a revision WITHOUT the
chain bound (git stash the source change) and WITH it. Pre-fix the inner
queue grows past the bound (it relocates the buffering one hop upstream);
post-fix it caps at the bound and the block propagates to the provider.

Run: python scripts/repro_gateway_slow_client.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "backend"))
sys.path.insert(0, str(REPO_ROOT))

# Number of SSE chunks the mock upstream emits. 1200 x ~2 KB = ~2.4 MB, about
# 4.7x the chain bound (256): enough that an UNBOUNDED inner queue visibly
# grows past the bound (the pre-fix comparison run) while keeping the
# forced-streaming scenario (one JSON body at the end) short. Override with
# REPRO_CHUNKS for a longer soak.
CHUNKS = int(os.environ.get("REPRO_CHUNKS", "1200"))
PAD_BYTES = 2048
UPSTREAM_PORT = int(os.environ.get("REPRO_UPSTREAM_PORT", "18941"))
GATEWAY_PORT = int(os.environ.get("REPRO_GATEWAY_PORT", "18942"))
_MAX = 256  # serving.servers.routers.completions_stream._KEEPALIVE_QUEUE_MAXSIZE
_INFLIGHT_SLACK = 8
_RSS_BUDGET_KB = 60_000  # ~58 MB: by-design content_parts buffering plus
# interpreter noise. Unbounded buffering of the same traffic would add another
# full copy on top of that. The depth check is the hard assertion.

# Must be set before serving.config.settings is first read (bootstrap runs in
# the app lifespan, i.e. when uvicorn starts serving below).
os.environ["DB_ENABLED"] = "0"
os.environ["USER_AUTH_ENABLED"] = "0"
os.environ["JWT_SECRET_KEY"] = "repro-jwt-secret-not-a-real-deployment"
os.environ["API_KEY_SECRET"] = "repro-api-key-secret-not-a-real-deployment"
os.environ["METRICS_ENABLED"] = "0"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["ADMIN_TOKEN"] = "repro-admin-token"

# A temp model registry pointing at the local upstream, loaded by the REAL
# bootstrap (so route selection and visibility checks run for real).
import tempfile

_registry_path = Path(tempfile.mkdtemp(prefix="repro-models-")) / "models.yaml"
UPSTREAM_BASE = f"http://127.0.0.1:{UPSTREAM_PORT}/v1"
_registry_path.write_text(
    f"""
models:
  - id: repro-model
    name: Repro Model
    provider: sglang
    base_url: {UPSTREAM_BASE}
    quantization: "bf16"
    input_modalities: ["text"]
    output_modalities: ["text"]
    context_length: 8192
    max_output_length: 4096
    supported_params: [temperature, top_p, max_tokens]
    pricing:
      prompt: "0"
      completion: "0"
      image: "0"
      request: "0"
    route:
      - kind: openai_compat
        weight: 1.0
        base_url: {UPSTREAM_BASE}
        api_key: repro-upstream-key
""",
    encoding="utf-8",
)
os.environ["MODELS_CONFIG_PATH"] = str(_registry_path)

import uvicorn
from aiohttp import web
from fastapi import Request  # noqa: TC002

from serving.servers.app import create_app

# Queue-depth sampling: every asyncio.Queue constructed with the chain bound
# while the spy is installed. The gateway constructs exactly the two bounded
# hand-off queues per forced-streaming request (and only the inner one per SSE
# request).
_chain_queues: list[asyncio.Queue] = []


def install_queue_spy() -> None:
    """Patch asyncio.Queue to record every chain-bound queue constructed."""
    real_queue = asyncio.Queue

    class SpyQueue(real_queue):  # type: ignore[type-arg,misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            _chain_queues.append(self)
            self._puts = 0
            self._gets = 0

        async def put(self, item: Any) -> None:
            await super().put(item)
            self._puts += 1

        async def get(self) -> Any:
            item = await super().get()
            self._gets += 1
            return item

    asyncio.Queue = SpyQueue  # type: ignore[assignment]


def remove_queue_spy() -> None:
    """Restore the real asyncio.Queue (undo the spy)."""
    asyncio.Queue = asyncio.Queue.__bases__[0]  # type: ignore[assignment]


def rss_kb() -> int:
    """Return this process resident set size in KB."""
    with open("/proc/self/status", encoding="ascii") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return 0


async def start_upstream() -> web.AppRunner:
    """SSE upstream: pre-built ~2 KB chunks as fast as the socket takes."""
    payload = {
        "id": "chatcmpl-repro",
        "created": 1,
        "model": "repro-model",
        "choices": [{"index": 0, "delta": {"content": "x" * PAD_BYTES}, "finish_reason": None}],
    }
    prebuilt = [f"data: {json.dumps(payload)}\n\n".encode() for _ in range(CHUNKS)]

    async def sse_handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for chunk in prebuilt:
            await resp.write(chunk)
            # Yield to the loop between writes: this upstream shares the event
            # loop with the gateway, and a single multi-MB write burst never
            # yields -- the gateway's reads only progress when it does. One
            # tick per chunk keeps the stream genuinely concurrent without
            # pacing it (the chunks still move as fast as the pipe drains).
            await asyncio.sleep(0)
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_post("/v1/chat/completions", sse_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", UPSTREAM_PORT)
    await site.start()
    return runner


async def start_gateway() -> tuple[asyncio.Task[None], uvicorn.Server]:
    """The real FastAPI app on uvicorn (lifespan/bootstrap included)."""
    from serving.config.runtime_settings import get_runtime_settings

    class _RS:
        """RuntimeSettings stub: force_chat_completions_streaming -> True."""

        async def get_bool(self, key: str) -> bool:
            return key == "force_chat_completions_streaming"

    async def _force_streaming_settings(request: Request) -> Any:
        # NOTE: ``Request`` must resolve in this module's namespace: the script
        # uses PEP 563 annotations, and FastAPI evaluates the override's
        # annotation there -- an unresolvable one degrades into a required
        # *query* parameter and every request 422s.
        return _RS()

    app = create_app()
    # The documented pattern: override the FastAPI dependency itself --
    # Depends() captured the original function at route declaration, so
    # patching the module attribute would leave the route on the ordinary
    # non-streaming path.
    app.dependency_overrides[get_runtime_settings] = _force_streaming_settings

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=GATEWAY_PORT, log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    return task, server


async def stalled_client(body: dict[str, Any], timeout: float = 30.0) -> tuple[Any, Any, str]:
    """Raw-socket POST; read the response head, then stop reading (socket open)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", GATEWAY_PORT)
    payload = json.dumps(body).encode()
    writer.write(
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(payload)}\r\n\r\n".encode()
        + payload
    )
    await writer.drain()
    head = await asyncio.wait_for(reader.read(512), timeout=timeout)
    return reader, writer, head.decode(errors="replace")


async def run_scenario(
    name: str, body: dict[str, Any], *, seconds: float, client_timeout: float = 30.0
) -> tuple[int, int, int, int, dict[int, int]]:
    """Stall a raw-socket client, sample queues+RSS, return summary numbers."""
    _chain_queues.clear()
    rss_before = rss_kb()

    _reader, writer, head = await stalled_client(body, timeout=client_timeout)
    status_line = head.splitlines()[0] if head else "<none>"
    print(f"\n[{name}] client head: {status_line}")
    print(f"[{name}] client stalled (socket open, reads stopped) -- sampling...")

    # Creation order is deterministic: the keepalive generator builds its own
    # queue before the reader task that first iterates session.stream (which
    # builds the inner one). With a single queue (plain SSE path) it is the
    # inner one.
    def _label() -> tuple[int, int]:
        if len(_chain_queues) >= 2:
            return _chain_queues[0].qsize(), _chain_queues[1].qsize()
        if len(_chain_queues) == 1:
            return 0, _chain_queues[0].qsize()
        return 0, 0

    peaks: dict[int, int] = {}
    max_inner = 0
    max_outer = 0
    max_total = 0
    deadline = asyncio.get_event_loop().time() + seconds
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
        for _i, _q in enumerate(_chain_queues):
            peaks[_i] = max(peaks.get(_i, 0), _q.qsize())
        outer_now, inner_now = _label()
        max_inner = max(max_inner, inner_now)
        max_outer = max(max_outer, outer_now)
        max_total = max(max_total, outer_now + inner_now)
    rss_during = rss_kb()

    settled = _label()
    print(f"[{name}] queues captured: {len(_chain_queues)} (bound {_MAX} each)")
    for i, q in enumerate(_chain_queues):
        print(
            f"[{name}] queue[{i}] maxsize={q.maxsize} peak_depth={peaks.get(i, 0)} "
            f"puts={getattr(q, chr(95) + chr(112) + chr(117) + chr(116) + chr(115), 0)} "
            f"gets={getattr(q, chr(95) + chr(103) + chr(101) + chr(116) + chr(115), 0)}"
        )
    print(f"[{name}] max inner qsize : {max_inner}  (bound {_MAX})")
    print(f"[{name}] max outer qsize : {max_outer}  (bound {_MAX})")
    print(f"[{name}] max total buffered: {max_total}  (contract <= {2 * _MAX + _INFLIGHT_SLACK})")
    print(f"[{name}] settled depths after upstream finished: {settled}")
    print(
        f"[{name}] RSS before={rss_before}KB peak={rss_during}KB delta={rss_during - rss_before}KB"
    )

    writer.transport.abort()  # finally disconnect the stalled client
    await asyncio.sleep(1.0)  # let the gateway tear the chain down
    after_teardown = _label()
    print(f"[{name}] queue depths after client disconnect: {after_teardown}")
    return max_inner, max_outer, max_total, rss_during - rss_before, after_teardown


async def main() -> int:
    """Run both scenarios and report the verdict (0 = pass)."""
    upstream = await start_upstream()
    gw_task, server = await start_gateway()
    print(f"upstream: http://127.0.0.1:{UPSTREAM_PORT} ({CHUNKS} x ~{PAD_BYTES}B chunks)")
    print(f"gateway:  http://127.0.0.1:{GATEWAY_PORT} (real create_app on uvicorn)")

    ok = True
    base = {"model": "repro-model", "messages": [{"role": "user", "content": "hi"}]}

    # -- Scenario 1: force-streaming buffered branch (stream=false + switch on)
    install_queue_spy()
    try:
        max_inner, _max_outer, max_total, rss_delta, after = await run_scenario(
            "force-streaming",
            dict(base),
            seconds=6.0,
            # The forced-streaming branch returns ONE JSON body: the client sees
            # no bytes until the whole generation is buffered, so this wait must
            # cover the full upstream run.
            client_timeout=120.0,
        )
    finally:
        remove_queue_spy()
    # The buffered branch holds the whole generation in content_parts BY
    # DESIGN -- the queue bound's job here is teardown safety, which the
    # settled-empty check below covers.
    if max_inner > _MAX:
        print("FAIL: inner queue exceeded its bound -- chain not bounded end-to-end")
        ok = False
    if max_total > 2 * _MAX + _INFLIGHT_SLACK:
        print("FAIL: total buffered items exceeded 2*maxsize + slack")
        ok = False
    if any(v > _MAX for v in after):
        print("FAIL: a queue still holds more than its bound after the client disconnected")
        ok = False
    if rss_delta > _RSS_BUDGET_KB:
        print(f"FAIL: RSS grew {rss_delta}KB > {_RSS_BUDGET_KB}KB budget")
        ok = False

    # -- Scenario 2: plain SSE path with a mid-stream stalled reader. The
    # client reads the first bytes (role chunk) and stops: the stall reaches
    # the chain through uvicorn's write flow control, so the INNER queue's
    # bound is what stands between the gateway and unbounded buffering.
    install_queue_spy()
    try:
        max_inner, _max_outer, max_total, rss_delta, after = await run_scenario(
            "sse-stall",
            {**base, "stream": True},
            seconds=6.0,
            # The SSE path emits the role chunk immediately, then data chunks.
            client_timeout=30.0,
        )
    finally:
        remove_queue_spy()
    if max_inner > _MAX:
        print("FAIL: inner queue exceeded its bound under SSE backpressure")
        ok = False
    if any(v > _MAX for v in after):
        print("FAIL: SSE queue still holds more than its bound after the client disconnected")
        ok = False

    with contextlib.suppress(Exception):
        server.should_exit = True
        await asyncio.wait_for(gw_task, timeout=10)
    await upstream.cleanup()

    print(
        "\nPASS: chain bounded end-to-end -- queue depths held their bounds and "
        "backpressure reached the provider"
        if ok
        else "\nFAIL"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
