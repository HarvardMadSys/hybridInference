"""Remote media URLs on self-hosted routes are fetched by the gateway, never the engine.

vLLM and SGLang download whatever ``image_url`` / ``video_url`` / ``audio_url``
a request names, from the GPU network -- a server-side request forgery into the
deployment for any caller, and SGLang opens a local file for a "URL" that is a
path ending in an image extension. On a self-hosted route the gateway now
fetches each URL itself, through a client that connects only to public
addresses, and sends it on as a ``data:`` URL; anything it will not or cannot
fetch is the caller's 400. Cloud routes forward their URLs as before.

The fetch tests serve media from a local server, so they let the guard admit
127.0.0.1 -- and only it -- and check everything else the fetch enforces. The
guard's own verdicts are tested on addresses directly.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import errno
import json
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from serving.adapters import remote_media
from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.adapters.remote_media import (
    RemoteMediaRefused,
    inline_remote_media,
    is_public_address,
)

pytestmark = pytest.mark.unit

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# ---------------------------------------------------------------------------
# The address guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.9",
        "192.168.1.1",
        "100.64.0.1",  # shared address space (CGNAT)
        "169.254.169.254",  # cloud metadata
        "100.100.100.200",  # Alibaba Cloud metadata
        "0.0.0.0",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "fd00:ec2::254",
        "ff02::1",
        "::ffff:127.0.0.1",  # IPv4-mapped
        "::ffff:10.0.0.1",
        "2002:7f00:1::1",  # 6to4 of 127.0.0.1
        "2002:a00:1::1",  # 6to4 of 10.0.0.1
        "64:ff9b::a00:1",  # NAT64 of 10.0.0.1
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
        "fe80::1%eth0",
        "not-an-address",
    ],
)
def test_the_guard_refuses_every_address_that_is_not_public(address):
    assert not is_public_address(address)


@pytest.mark.parametrize(
    "address", ["93.184.215.14", "8.8.8.8", "2606:4700:4700::1111", "64:ff9b::808:808"]
)
def test_the_guard_admits_public_addresses(address):
    assert is_public_address(address)


def test_the_guard_refuses_before_a_socket_exists(monkeypatch):
    import socket

    opened = []
    monkeypatch.setattr(socket, "socket", lambda *a, **k: opened.append(1))

    with pytest.raises(OSError) as caught:
        remote_media._public_socket((socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 80)))

    assert caught.value.errno == errno.EACCES
    assert opened == []


# ---------------------------------------------------------------------------
# What is left alone, and what is refused without any fetch
# ---------------------------------------------------------------------------


def _user(*parts: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"role": "user", "content": [{"type": "text", "text": "look"}, *parts]}]


def _image(url: str, **extra: Any) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": url, **extra}}


@pytest.mark.asyncio
async def test_messages_without_remote_media_come_back_untouched():
    raw = base64.b64encode(PNG).decode()
    messages = [
        {"role": "system", "content": "be brief"},
        *_user(_image(f"data:image/png;base64,{raw}"), _image(raw)),
        {"role": "assistant", "content": "ok"},
    ]

    assert await inline_remote_media(messages) is messages


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("part", "field"),
    [
        (_image("file:///etc/passwd"), "image_url"),
        (_image("/home/dev/secret.png"), "image_url"),  # SGLang opens this as a file
        (_image("C:\\secret.png"), "image_url"),
        (_image("gopher://10.0.0.1:70/"), "image_url"),
        (_image("http://[::1"), "image_url"),  # malformed
        ({"image_url": "ftp://internal/x.png"}, "image_url"),  # vLLM's untyped form
        ({"type": "video_url", "video_url": {"url": "/srv/videos/a.mp4"}}, "video_url"),
        # Bare base64 is decoded only for images; for video SGLang takes it as a path.
        ({"type": "audio_url", "audio_url": {"url": "QUJD"}}, "audio_url"),
        # Base64-looking, but ending like a file name SGLang would open.
        (_image("tmp/ab/xpng"), "image_url"),
    ],
)
async def test_other_schemes_are_refused_before_any_fetch(part, field, monkeypatch):
    async def _no_fetch(*_a, **_k):
        raise AssertionError("nothing may be fetched")

    monkeypatch.setattr(remote_media, "_fetch", _no_fetch)

    with pytest.raises(RemoteMediaRefused) as caught:
        await inline_remote_media(_user(part))

    assert caught.value.status == 400
    assert caught.value.message == (
        f"messages[0].content[1].{field} must be an http(s) or data: URL."
    )


@pytest.mark.asyncio
async def test_too_many_distinct_urls_are_refused_before_any_fetch(monkeypatch):
    async def _no_fetch(*_a, **_k):
        raise AssertionError("nothing may be fetched")

    monkeypatch.setattr(remote_media, "_fetch", _no_fetch)
    parts = [
        _image(f"https://img.example/{i}.png") for i in range(remote_media.MAX_REMOTE_MEDIA + 1)
    ]

    with pytest.raises(RemoteMediaRefused, match="remote media URLs"):
        await inline_remote_media(_user(*parts))


# ---------------------------------------------------------------------------
# The fetch, against a local server the guard is told to admit
# ---------------------------------------------------------------------------


class _MediaServer:
    def __init__(self) -> None:
        self.hits: list[str] = []
        self.routes: dict[str, Any] = {}

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.hits.append(request.path)
        route = self.routes.get(request.path)
        if route is None:
            return web.Response(status=404)
        return await route(request)


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


def _serve_bytes(body: bytes, content_type: str, **headers: str):
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(body=body, headers={"Content-Type": content_type, **headers})

    return handler


@pytest.mark.asyncio
async def test_a_remote_image_is_inlined_and_the_request_keeps_its_url(media):
    media.routes["/cat.png"] = _serve_bytes(PNG, "image/png")
    url = f"{media.base}/cat.png"
    messages = [
        *_user(_image(url, detail="high")),
        {"role": "assistant", "content": "a cat"},
        *_user({"image_url": url}),  # the same image again, in vLLM's untyped form
    ]
    original = copy.deepcopy(messages)
    paused = []

    out = await inline_remote_media(messages, before_fetch=lambda: paused.append(1))

    expected = f"data:image/png;base64,{base64.b64encode(PNG).decode()}"
    assert out[0]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": expected, "detail": "high"},
    }
    assert out[2]["content"][1] == {"image_url": expected}
    assert out[1] is messages[1]  # unchanged messages are not copied
    assert messages == original  # the caller's request keeps its URLs
    assert media.hits == ["/cat.png"]  # fetched once
    assert paused == [1]


@pytest.mark.asyncio
async def test_a_redirect_is_followed_and_each_hop_is_guarded(media):
    media.routes["/cat.png"] = _serve_bytes(PNG, "image/png")

    async def to_cat(_request):
        raise web.HTTPFound(f"{media.base}/cat.png")

    async def to_private(_request):
        # 127.0.0.2 is loopback too, but the guard admits only 127.0.0.1.
        raise web.HTTPFound(f"http://127.0.0.2:{media.port}/cat.png")

    media.routes["/moved.png"] = to_cat
    media.routes["/sneaky.png"] = to_private

    out = await inline_remote_media(_user(_image(f"{media.base}/moved.png")))
    assert out[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")

    media.hits.clear()
    with pytest.raises(RemoteMediaRefused, match="not a public address"):
        await inline_remote_media(_user(_image(f"{media.base}/sneaky.png")))
    assert media.hits == ["/sneaky.png"]  # the private hop was never connected


@pytest.mark.asyncio
async def test_a_private_host_is_refused_without_a_connection(media, monkeypatch):
    monkeypatch.setattr(remote_media, "is_public_address", lambda _a: False)
    media.routes["/cat.png"] = _serve_bytes(PNG, "image/png")

    with pytest.raises(RemoteMediaRefused) as caught:
        await inline_remote_media(_user(_image(f"{media.base}/cat.png")))

    assert caught.value.message == (
        "messages[0].content[1].image_url could not be fetched: its host is not a public address."
    )
    assert media.hits == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (
            _serve_bytes(b"<html>login</html>", "text/html"),
            "is not an image (content type text/html)",
        ),
        (_serve_bytes(b"{}", "application/json"), "is not an image"),
        (_serve_bytes(PNG, "video/mp4"), "is not an image (content type video/mp4)"),
        (_serve_bytes(PNG, "image/png", **{"Content-Encoding": "gzip"}), "Content-Encoding gzip"),
    ],
    ids=["html", "json", "wrong-kind", "encoded"],
)
async def test_what_is_not_the_media_it_claims_is_refused(media, handler, message):
    media.routes["/x"] = handler

    with pytest.raises(RemoteMediaRefused, match=message.replace("(", r"\(").replace(")", r"\)")):
        await inline_remote_media(_user(_image(f"{media.base}/x")))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "media_type"),
    [
        (PNG, "image/png"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 32, "image/jpeg"),
        (b"GIF89a" + b"\x00" * 32, "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 32, "image/webp"),
    ],
)
async def test_an_image_under_a_generic_type_is_recognized_by_its_bytes(media, body, media_type):
    """A storage bucket's ``application/octet-stream`` image still works."""
    media.routes["/obj"] = _serve_bytes(body, "application/octet-stream")

    out = await inline_remote_media(_user(_image(f"{media.base}/obj")))

    assert out[0]["content"][1]["image_url"]["url"] == (
        f"data:{media_type};base64,{base64.b64encode(body).decode()}"
    )


@pytest.mark.asyncio
async def test_a_generic_type_that_is_not_an_image_is_refused(media):
    media.routes["/obj"] = _serve_bytes(b"#!/bin/sh\necho hi\n", "binary/octet-stream")

    with pytest.raises(RemoteMediaRefused, match="is not an image"):
        await inline_remote_media(_user(_image(f"{media.base}/obj")))


@pytest.mark.asyncio
async def test_a_failed_fetch_is_refused(media):
    with pytest.raises(RemoteMediaRefused) as caught:
        await inline_remote_media(_user(_image(f"{media.base}/missing.png")))

    assert (
        caught.value.message == "messages[0].content[1].image_url could not be fetched (HTTP 404)."
    )


@pytest.mark.asyncio
async def test_a_video_must_be_a_video(media):
    media.routes["/clip"] = _serve_bytes(b"\x00\x00\x00\x18ftypmp42", "video/mp4")
    part = {"type": "video_url", "video_url": {"url": f"{media.base}/clip"}}

    out = await inline_remote_media(_user(part))

    assert out[0]["content"][1]["video_url"]["url"].startswith("data:video/mp4;base64,")


@pytest.mark.asyncio
@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
async def test_a_file_over_the_limit_is_refused(media, monkeypatch, chunked):
    monkeypatch.setattr(remote_media, "MAX_MEDIA_BYTES", 1024)
    body = PNG + b"\x00" * 4096

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "image/png"})
        if chunked:
            response.enable_chunked_encoding()
        else:
            response.content_length = len(body)
        await response.prepare(request)
        await response.write(body)
        await response.write_eof()
        return response

    media.routes["/big.png"] = handler

    with pytest.raises(RemoteMediaRefused, match="is larger than"):
        await inline_remote_media(_user(_image(f"{media.base}/big.png")))


@pytest.mark.asyncio
async def test_a_requests_media_together_are_bounded(media, monkeypatch):
    monkeypatch.setattr(remote_media, "MAX_TOTAL_MEDIA_BYTES", 1000)
    media.routes["/a.png"] = _serve_bytes(PNG + b"a" * 600, "image/png")
    media.routes["/b.png"] = _serve_bytes(PNG + b"b" * 600, "image/png")

    with pytest.raises(RemoteMediaRefused, match="together"):
        await inline_remote_media(
            _user(_image(f"{media.base}/a.png"), _image(f"{media.base}/b.png"))
        )


@pytest.mark.asyncio
async def test_a_slow_fetch_is_refused_in_time(media, monkeypatch):
    monkeypatch.setattr(remote_media, "FETCH_TIMEOUT_S", 0.3)

    async def stall(_request):
        await asyncio.sleep(1.5)
        return web.Response(body=PNG, content_type="image/png")

    media.routes["/slow.png"] = stall

    with pytest.raises(RemoteMediaRefused, match="within"):
        await asyncio.wait_for(
            inline_remote_media(_user(_image(f"{media.base}/slow.png"))), timeout=3
        )


# ---------------------------------------------------------------------------
# The adapter: self-hosted routes inline, cloud routes forward
# ---------------------------------------------------------------------------


class _RecordingHTTP:
    """The adapter's HTTP client, recording what would have been sent upstream."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def json_post(self, url: str, *, json: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        self.sent.append(json)
        return {
            "choices": [
                {"message": {"role": "assistant", "content": "a cat"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }

    async def json_post_with_retry(self, url: str, *, json: dict[str, Any], **kw: Any):
        return await self.json_post(url, json=json, **kw)

    async def stream_post(self, url: str, *, json: dict[str, Any], **_kw: Any):
        self.sent.append(json)
        chunk = {"choices": [{"index": 0, "delta": {"content": "a cat"}, "finish_reason": "stop"}]}
        yield f"data: {json_mod.dumps(chunk)}"
        yield "data: [DONE]"


json_mod = json


def _adapter(
    base_url: str, deployment_type: str = "auto"
) -> tuple[OpenAICompatAdapter, _RecordingHTTP]:
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id="vision",
            name="Vision",
            provider="vllm",
            base_url=base_url,
            deployment_type=deployment_type,
            provider_model_id="vision",
            input_modalities=["text", "image"],
            supported_params=["max_tokens"],
        )
    )
    http = _RecordingHTTP()
    adapter.http = http
    return adapter, http


async def _send(adapter: OpenAICompatAdapter, messages, stream: bool) -> None:
    if stream:
        async for _ in adapter.stream_chat_completion(messages):
            pass
    else:
        await adapter.chat_completion(messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["chat", "stream"])
@pytest.mark.parametrize(
    ("base_url", "deployment_type"),
    [
        ("http://localhost:8000/v1", "auto"),
        ("http://host.docker.internal:8200/v1", "auto"),
        ("http://10.0.0.5:8000/v1", "self_hosted"),
    ],
    ids=["localhost", "docker-host", "declared"],
)
async def test_a_self_hosted_route_sends_the_image_inline(media, base_url, deployment_type, stream):
    media.routes["/cat.png"] = _serve_bytes(PNG, "image/png")
    adapter, http = _adapter(base_url, deployment_type)
    messages = _user(_image(f"{media.base}/cat.png"))

    await _send(adapter, messages, stream)

    (sent,) = http.sent
    url = sent["messages"][0]["content"][1]["image_url"]["url"]
    assert url == f"data:image/png;base64,{base64.b64encode(PNG).decode()}"
    assert messages[0]["content"][1]["image_url"]["url"] == f"{media.base}/cat.png"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["chat", "stream"])
@pytest.mark.parametrize(
    ("base_url", "deployment_type"),
    [
        ("https://api.provider.example/v1", "auto"),
        ("http://localhost:9000/v1", "provider_api"),  # a loopback proxy to a provider
    ],
    ids=["provider", "declared-provider"],
)
async def test_a_cloud_route_forwards_the_url_as_before(
    base_url, deployment_type, stream, monkeypatch
):
    async def _no_fetch(*_a, **_k):
        raise AssertionError("a cloud route fetches nothing")

    monkeypatch.setattr(remote_media, "_fetch", _no_fetch)
    adapter, http = _adapter(base_url, deployment_type)
    messages = _user(_image("http://10.0.0.1/internal.png"))

    await _send(adapter, messages, stream)

    (sent,) = http.sent
    assert sent["messages"][0]["content"][1]["image_url"]["url"] == "http://10.0.0.1/internal.png"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["chat", "stream"])
async def test_a_refused_url_never_reaches_the_engine(stream):
    adapter, http = _adapter("http://localhost:8000/v1")

    with pytest.raises(RemoteMediaRefused) as caught:
        await _send(adapter, _user(_image("http://169.254.169.254/latest/meta-data/")), stream)

    assert caught.value.status == 400
    assert http.sent == []


def test_a_refusal_reads_everywhere_as_the_callers_400():
    """The breaker exempts it, and both surfaces answer 400 with its message."""
    from routing.endpoint_health import _is_client_error
    from serving.exceptions import scrub_error_for_user
    from serving.servers.routers.anthropic_messages import _map_upstream_status
    from serving.servers.routers.completions_stream import _extract_exception_status_code
    from serving.servers.routers.routing_info import _status_code_from_exception

    refused = RemoteMediaRefused(
        "messages[0].content[1].image_url could not be fetched (HTTP 404)."
    )

    assert _is_client_error(refused)
    assert isinstance(refused, aiohttp.ClientResponseError)
    assert _status_code_from_exception(refused) == 400
    assert _extract_exception_status_code(refused) == 400
    assert _map_upstream_status(refused.status) == (400, "invalid_request_error")
    assert scrub_error_for_user(refused, "rid", 400) == (
        "messages[0].content[1].image_url could not be fetched (HTTP 404). (request_id: rid)"
    )
    assert "image_url" in str(refused)  # str() needs no request to describe it


@pytest.mark.asyncio
async def test_the_fetch_is_gateway_time_not_engine_time(media):
    """A first-token watch pauses for the fetch; the slot that follows restarts it."""
    from serving.utils import context as req_ctx

    events: list[str] = []

    class _Watch:
        def on_queued(self) -> None:
            events.append("queued")

        def on_sent(self) -> None:
            events.append("sent")

        def on_first_token(self) -> None:
            events.append("first_token")

    async def serve(_request: web.Request) -> web.Response:
        events.append("fetched")
        return web.Response(body=PNG, content_type="image/png")

    media.routes["/cat.png"] = serve
    adapter, http = _adapter("http://localhost:8000/v1")

    with req_ctx.push(**{req_ctx.UPSTREAM_DISPATCH_WATCH: _Watch()}):
        async for _ in adapter.stream_chat_completion(_user(_image(f"{media.base}/cat.png"))):
            pass

    assert events[:3] == ["queued", "fetched", "sent"]
    assert len(http.sent) == 1
