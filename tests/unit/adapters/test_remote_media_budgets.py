"""What the remote-media fetcher may use of the process, whatever URLs a caller names.

The fetcher resolved host names with aiohttp's ``ThreadedResolver``, on the event
loop's default executor. A name whose DNS never answers holds its thread past
the fetch that asked, so three requests naming black-holed hosts stalled every
other lookup in the process -- provider hosts, database reconnects -- for 20 s.
Nothing bounded how many fetches one caller, or all of them, ran at once, every
attempt at a request fetched its media again, and the base64 encoding ran in
one piece on the event loop.
"""

from __future__ import annotations

import asyncio
import base64
import socket
import threading
from typing import Any

import pytest
from aiohttp import web

from serving.adapters import remote_media
from serving.adapters.remote_media import RemoteMediaRefused, inline_remote_media
from serving.utils import context as req_ctx
from tests.unit.adapters.test_remote_media import (
    PNG,
    _image,
    _MediaServer,
    _serve_bytes,
    _user,
)

pytestmark = pytest.mark.unit


@pytest.fixture
async def media(monkeypatch):
    """A local media server, and a guard that admits 127.0.0.1 and nothing else."""
    real = remote_media.is_public_address
    monkeypatch.setattr(remote_media, "is_public_address", lambda a: a == "127.0.0.1" or real(a))
    server = _MediaServer()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", server.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    server.port = site._server.sockets[0].getsockname()[1]
    server.base = f"http://127.0.0.1:{server.port}"
    try:
        yield server
    finally:
        await runner.cleanup()


_REAL_GETADDRINFO = socket.getaddrinfo


class _FakeDNS:
    """``socket.getaddrinfo``: ``*.blackhole.test`` never answers, ``*.media.test`` is local."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.running: dict[str, int] = {}
        self.peak: dict[str, int] = {}
        self.threads: set[str] = set()

    def __call__(self, host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(host, str) or not host.endswith((".blackhole.test", ".media.test")):
            return _REAL_GETADDRINFO(host, port, *args, **kwargs)
        caller = host.split(".")[-3]  # <n>.<caller>.blackhole.test
        with self.lock:
            self.threads.add(threading.current_thread().name)
            self.running[caller] = self.running.get(caller, 0) + 1
            self.peak[caller] = max(self.peak.get(caller, 0), self.running[caller])
        try:
            if host.endswith(".blackhole.test"):
                self.release.wait(10)
                raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
            return _REAL_GETADDRINFO("127.0.0.1", port, *args, **kwargs)
        finally:
            with self.lock:
                self.running[caller] -= 1


@pytest.fixture
def dns(monkeypatch):
    fake = _FakeDNS()
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    monkeypatch.setattr(remote_media, "_DNS_TIMEOUT_S", 0.3, raising=False)
    monkeypatch.setattr(remote_media, "FETCH_TIMEOUT_S", 2.0)
    try:
        yield fake
    finally:
        fake.release.set()  # let the stuck threads go


async def _fetch_as(caller: str, messages: list[dict[str, Any]], request_id: str | None = None):
    with req_ctx.push(user_id=caller, request_id=request_id or f"req-{caller}"):
        return await inline_remote_media(messages)


@pytest.mark.asyncio
async def test_hosts_that_never_resolve_leave_the_default_executor_free(media, dns):
    loop = asyncio.get_running_loop()
    attack = asyncio.gather(
        *(
            _fetch_as(
                f"c{r}",
                _user(*(_image(f"http://{i}.c{r}.blackhole.test/x.png") for i in range(8))),
            )
            for r in range(3)
        ),
        return_exceptions=True,
    )
    await asyncio.sleep(0.1)

    # Every thread of the default executor is still there for everything else.
    free = await asyncio.wait_for(
        asyncio.gather(*(loop.run_in_executor(None, lambda: "free") for _ in range(16))), 1.0
    )
    results = await attack

    assert free == ["free"] * 16
    assert all(isinstance(r, RemoteMediaRefused) for r in results)
    assert dns.threads and all(name.startswith("media-dns") for name in dns.threads)


@pytest.mark.asyncio
async def test_one_caller_holds_no_more_than_two_resolver_threads(media, dns):
    """Its lookups that never end hold two threads; another caller's still resolve."""
    media.routes["/cat.png"] = _serve_bytes(PNG, "image/png")
    stuck = asyncio.gather(
        _fetch_as("u1", _user(*(_image(f"http://{i}.u1.blackhole.test/x.png") for i in range(4)))),
        return_exceptions=True,
    )
    await asyncio.sleep(0.1)

    out = await _fetch_as("u2", _user(_image(f"http://a.u2.media.test:{media.port}/cat.png")))

    assert out[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    (refused,) = await stuck
    assert isinstance(refused, RemoteMediaRefused)
    assert dns.peak["u1"] == remote_media._DNS_LOOKUPS_PER_CALLER == 2


@pytest.mark.asyncio
async def test_a_request_naming_several_hosts_waits_for_its_lookups_rather_than_failing(media, dns):
    media.routes["/cat.png"] = _serve_bytes(PNG, "image/png")
    parts = [_image(f"http://{i}.u3.media.test:{media.port}/cat.png") for i in range(6)]

    out = await _fetch_as("u3", _user(*parts))

    assert all(
        part["image_url"]["url"].startswith("data:image/png;base64,")
        for part in out[0]["content"][1:]
    )
    assert dns.peak["u3"] <= 2


@pytest.fixture
def slow_media(media):
    """Media that is served only once the test lets it, counting fetches in flight."""
    state = {"running": 0, "peak": 0, "go": asyncio.Event()}

    async def handler(_request):
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        try:
            await state["go"].wait()
            return await _serve_bytes(PNG, "image/png")(_request)
        finally:
            state["running"] -= 1

    for i in range(16):
        media.routes[f"/{i}.png"] = handler
    return media, state


async def _peak_while_held(state: dict[str, Any], limit: int) -> int:
    """The most fetches the media server held at once, read once *limit* are held.

    Waits for *limit* fetches to reach the server, then a while longer for any
    fetch past the limit to show itself. Reading the peak after a fixed sleep
    alone read 0 whenever the event loop stalled (a garbage collection, a busy
    CI runner): the sleep's deadline passed during the stall, before any fetch
    had had a turn to reach the server.
    """
    try:
        async with asyncio.timeout(10):
            while state["running"] < limit:
                await asyncio.sleep(0.01)
    except TimeoutError:
        return state["peak"]  # the limit was never reached; the assertion says how far
    await asyncio.sleep(0.3)
    return state["peak"]


@pytest.mark.asyncio
async def test_a_caller_runs_at_most_four_fetches_across_its_requests(slow_media, monkeypatch):
    media, state = slow_media
    monkeypatch.setattr(remote_media, "_FETCH_SLOTS", remote_media._FetchSlots())

    async def request(n: int):
        parts = [_image(f"{media.base}/{n * 4 + i}.png") for i in range(4)]
        return await _fetch_as("same-user", _user(*parts), request_id=f"r{n}")

    requests = asyncio.gather(*(request(n) for n in range(3)))
    peak_while_held = await _peak_while_held(state, remote_media.MAX_CALLER_FETCHES)
    state["go"].set()
    await requests

    assert peak_while_held == remote_media.MAX_CALLER_FETCHES == 4


@pytest.mark.asyncio
async def test_all_callers_together_run_at_most_the_global_number(slow_media, monkeypatch):
    media, state = slow_media
    monkeypatch.setattr(remote_media, "MAX_GLOBAL_FETCHES", 6)
    monkeypatch.setattr(remote_media, "_FETCH_SLOTS", remote_media._FetchSlots())

    async def request(n: int):
        parts = [_image(f"{media.base}/{n * 4 + i}.png") for i in range(4)]
        return await _fetch_as(f"user-{n}", _user(*parts))

    requests = asyncio.gather(*(request(n) for n in range(3)))
    peak_while_held = await _peak_while_held(state, 6)
    state["go"].set()
    await requests

    assert peak_while_held == 6


@pytest.mark.asyncio
async def test_a_later_attempt_at_the_same_request_fetches_nothing_again(media):
    media.routes["/cat.png"] = _serve_bytes(PNG, "image/png")
    messages = _user(_image(f"{media.base}/cat.png"))
    paused: list[int] = []

    with req_ctx.push(user_id="u", request_id="req-memo-1"):
        first = await inline_remote_media(messages, before_fetch=lambda: paused.append(1))
        again = await inline_remote_media(messages, before_fetch=lambda: paused.append(1))
    with req_ctx.push(user_id="u", request_id="req-memo-2"):
        other = await inline_remote_media(messages)

    assert first == again == other
    assert media.hits == ["/cat.png", "/cat.png"]  # once per request
    assert paused == [1]  # the second attempt waited for no fetch


def test_the_memo_drops_the_oldest_media_past_its_size(monkeypatch):
    monkeypatch.setattr(remote_media, "_MEMO_MAX_CHARS", 10)
    memo = remote_media._MediaMemo()

    memo.put(("r", "u"), "image_url", "a", "x" * 6)
    memo.put(("r", "u"), "image_url", "b", "y" * 6)

    assert memo.get(("r", "u"), "image_url", "a") is None
    assert memo.get(("r", "u"), "image_url", "b") == "y" * 6
    assert memo.get(("other", "u"), "image_url", "b") is None


def test_the_memo_forgets_media_after_its_time(monkeypatch):
    memo = remote_media._MediaMemo()
    now = [1000.0]
    monkeypatch.setattr(remote_media.time, "monotonic", lambda: now[0])

    memo.put(("r", "u"), "image_url", "a", "x")
    now[0] += remote_media._MEMO_TTL_S + 1

    assert memo.get(("r", "u"), "image_url", "a") is None


@pytest.mark.asyncio
async def test_large_media_is_encoded_a_slice_at_a_time():
    body = bytes(range(256)) * (3 * 1024 * 1024 // 256 + 7)  # 3 MiB and a bit
    ticks = 0
    done = False

    async def ticker() -> None:
        nonlocal ticks
        while not done:
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    before = ticks
    url = await remote_media._data_url("image/png", body)
    during = ticks - before
    done = True
    await task

    assert url == f"data:image/png;base64,{base64.b64encode(body).decode()}"
    assert during >= 4  # one turn of the loop per 768 KiB
