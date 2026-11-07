# mypy: disable-error-code=no-any-unimported
"""Lightweight shared async HTTP client for adapters.

Provides a shared aiohttp session with convenience helpers for JSON
requests and streaming responses. Adapters can depend on this instead
of each maintaining their own sessions.
"""

from __future__ import annotations

import asyncio
import codecs
import json as jsonlib
import logging
import re
import sys
from contextlib import aclosing
from typing import TYPE_CHECKING, Any

import aiohttp

# aiohttp's marker for "use the session default timeout". An explicit
# timeout=None is NOT equivalent: aiohttp treats it as "no timeout at all".
from aiohttp.helpers import sentinel as _aiohttp_sentinel

from serving.exceptions import (
    UpstreamModelQueued,
    UpstreamRequestTimedOut,
    is_model_queued_body,
    is_upstream_timeout_body,
)
from serving.servers.sse import MAX_FRAME_CHARS, SSEFrameTooLarge, SSEMessage, SSEParser
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger
from serving.utils.metadata_addresses import refuse_metadata_socket

logger = get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

# SGLang answers a request whose image, audio or video it cannot decode with a
# 500 -- "An exception occurred while loading IMAGE data at index 0: ... cannot
# identify image file" -- although nothing about the server is wrong: the caller
# sent bytes that are not media (seen: a chat platform's JSON error body,
# base64-encoded and labelled image/jpeg). Raised as a 500 it is scored against
# the endpoint, so one client retrying one bad attachment opened the circuit for
# every user of the model; it also rotates keys on a fault no key can fix, and
# reaches a downstream gateway as a server failure. It is the caller's input, so
# it is raised as the 400 it is. Matched on the decode failure's own wording, not
# on "500 from a multimodal model", so a genuine server error still counts.
_UNDECODABLE_MEDIA_RE = re.compile(
    r"exception occurred while loading (?:IMAGE|AUDIO|VIDEO) data|cannot identify image file"
)


def _upstream_status_error(resp: Any, error_body: str) -> aiohttp.ClientResponseError:
    """Build the ``ClientResponseError`` raised for an upstream error response.

    The upstream body rides along as ``error_body`` so the provider's own message
    survives for logging and user-facing display. One construction for both the
    JSON and the streaming path, so the status they raise cannot drift apart.
    """
    status = resp.status
    message = resp.reason or "Unknown error"
    if status == 500 and error_body and _UNDECODABLE_MEDIA_RE.search(error_body):
        status = 400
        message = "Bad Request (upstream answered 500: the request's media could not be decoded)"
        logger.info(
            "upstream_media_rejection",
            extra={"event": "upstream_media_rejection", "upstream_status": resp.status},
        )
    queued = status == 503 and is_model_queued_body(error_body)
    error_type: type[aiohttp.ClientResponseError] = aiohttp.ClientResponseError
    if queued:
        error_type = UpstreamModelQueued
    elif status == 504 and is_upstream_timeout_body(error_body):
        error_type = UpstreamRequestTimedOut
    error = error_type(
        request_info=resp.request_info,
        history=resp.history,
        status=status,
        message=message,
        headers=resp.headers,
    )
    if queued:
        error.retry_after = _retry_after_seconds(resp.headers)  # type: ignore[attr-defined]
    if error_body:
        error.error_body = error_body  # type: ignore[attr-defined]
    return error


def _retry_after_seconds(headers: Any) -> float | None:
    """Return a ``Retry-After`` header given in seconds, ``None`` otherwise."""
    value = headers.get("Retry-After") if headers is not None else None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


# -- What an upstream response may make the gateway hold ----------------------

#: Error responses are read for the provider's message only. This much holds
#: any provider's; the rest of the body is not read.
MAX_ERROR_BODY_BYTES = 64 * 1024

#: The largest non-streaming response body read. Generous on purpose: the
#: biggest legitimate answers -- an embeddings batch of large vectors encoded as
#: JSON floats, a long completion with ``top_logprobs`` -- reach about a hundred
#: MiB. What it bounds is an upstream that never stops sending, which the total
#: timeout alone lets fill memory at line rate for as long as it runs.
MAX_RESPONSE_BODY_BYTES = 256 * 1024 * 1024

#: An NDJSON line may be as long as an SSE frame, for the same reasons.
MAX_LINE_CHARS = MAX_FRAME_CHARS

_READ_CHUNK_BYTES = 64 * 1024

#: Connection bounds for a request with no total timeout -- a stream, which then
#: runs for as long as generation does. ``connect`` covers the whole acquisition
#: of a connection (a wait for the pool, DNS, the handshakes), ``sock_connect``
#: the TCP and TLS handshake with one address. Without them a stream to an
#: unreachable host waited as long as the kernel kept retrying, and one waiting
#: for a pooled connection waited forever, holding its user's and its
#: provider's slots the whole time.
STREAM_CONNECT_TIMEOUT_S = 30.0
STREAM_SOCK_CONNECT_TIMEOUT_S = 10.0


class UpstreamPayloadTooLarge(aiohttp.ClientPayloadError):
    """An upstream response body, or one frame of a stream, passed the size read.

    A ``ClientPayloadError``, the type a truncated body raises, so every caller
    already fails the attempt for it as for any other broken upstream payload:
    the router records it against the endpoint and falls back, and a stream ends
    with the usual upstream error.
    """


def stream_timeout(timeout: aiohttp.ClientTimeout | None = None) -> aiohttp.ClientTimeout:
    """Return the timeout for opening a stream: ``timeout`` with connection bounds.

    A missing ``timeout`` means no total, as a generation runs as long as it
    runs. Whatever the caller set (``total``, ``sock_read``) is kept; only an
    unset ``connect`` / ``sock_connect`` is filled in. Running out of either
    raises ``aiohttp.ConnectionTimeoutError``, a ``ClientError`` and a
    ``TimeoutError``, which callers already treat as a failed attempt that has
    sent nothing: the router falls back from it like any unreachable upstream.
    """
    if timeout is None:
        timeout = aiohttp.ClientTimeout(total=None)
    return aiohttp.ClientTimeout(
        total=timeout.total,
        connect=STREAM_CONNECT_TIMEOUT_S if timeout.connect is None else timeout.connect,
        sock_read=timeout.sock_read,
        sock_connect=(
            STREAM_SOCK_CONNECT_TIMEOUT_S if timeout.sock_connect is None else timeout.sock_connect
        ),
        ceil_threshold=timeout.ceil_threshold,
    )


async def read_body(resp: Any, limit: int | None = None) -> bytes:
    """Read a response body, refusing one longer than ``limit`` bytes.

    ``limit`` defaults to :data:`MAX_RESPONSE_BODY_BYTES`. A ``Content-Length``
    over it is refused before anything is read; otherwise the body is counted as
    it arrives and refused the moment it passes the limit, so no more than
    ``limit`` bytes are ever held.

    Raises:
        UpstreamPayloadTooLarge: The body is longer than ``limit``.
    """
    if limit is None:
        limit = MAX_RESPONSE_BODY_BYTES
    declared = resp.content_length
    if declared is not None and declared > limit:
        raise UpstreamPayloadTooLarge(
            f"upstream response body of {declared} bytes exceeds the {limit}-byte limit"
        )
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.content.iter_chunked(_READ_CHUNK_BYTES):
        size += len(chunk)
        if size > limit:
            raise UpstreamPayloadTooLarge(f"upstream response body exceeds the {limit}-byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def read_error_text(resp: Any, limit: int | None = None) -> str:
    """Return the start of an error response's body as text: at most ``limit`` bytes.

    ``limit`` defaults to :data:`MAX_ERROR_BODY_BYTES`. Only the provider's
    message is wanted from the body, so the rest is left unread (the connection
    is then closed rather than reused). Decoded with the response's charset,
    UTF-8 by default; a malformed byte costs a replacement character, not the
    message.
    """
    if limit is None:
        limit = MAX_ERROR_BODY_BYTES
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.content.iter_chunked(_READ_CHUNK_BYTES):
        piece = chunk[: limit - size]
        chunks.append(piece)
        size += len(piece)
        if size >= limit:
            break
    return b"".join(chunks).decode(_response_charset(resp), errors="replace")


def _response_charset(resp: Any) -> str:
    """The charset ``resp`` declares, when Python knows it; UTF-8 otherwise."""
    charset = getattr(resp, "charset", None)
    if charset:
        try:
            return codecs.lookup(charset).name
        except LookupError:
            pass
    return "utf-8"


# What ``ClientResponse.json`` accepts by default: application/json and any
# ``+json`` type.
_JSON_CONTENT_TYPE_RE = re.compile(r"^application/(?:[\w.+-]+?\+)?json")


def _json_from_body(resp: Any, body: bytes) -> Any:
    """Decode a body read with :func:`read_body` as ``resp.json()`` would have.

    The same content-type check (an ``aiohttp.ContentTypeError`` for anything
    but JSON), ``None`` for an empty body, and the declared charset.
    """
    content_type = str(resp.headers.get("Content-Type", "")).lower()
    if not _JSON_CONTENT_TYPE_RE.match(content_type):
        raise aiohttp.ContentTypeError(
            resp.request_info,
            resp.history,
            status=resp.status,
            message=f"Attempt to decode JSON with unexpected mimetype: {content_type}",
            headers=resp.headers,
        )
    stripped = body.strip()
    if not stripped:
        return None
    return jsonlib.loads(stripped.decode(_response_charset(resp)))


class _LineSplitter:
    """Split decoded text into lines without copying or rescanning what it holds.

    The line still waiting for its newline is kept as the pieces it arrived in,
    and only new text is searched: appending each read to one string and
    searching all of it instead was quadratic in the length of a line.

    Raises ``UpstreamPayloadTooLarge`` once a line passes ``max_chars``.
    """

    def __init__(self, max_chars: int | None = None) -> None:
        self._pending: list[str] = []
        self._pending_chars = 0
        self._max_chars = MAX_LINE_CHARS if max_chars is None else max_chars

    def feed(self, text: str) -> list[str]:
        lines: list[str] = []
        pos = 0
        while (end := text.find("\n", pos)) >= 0:
            lines.append(self.flush() + text[pos:end])
            pos = end + 1
        if pos < len(text):
            rest = text[pos:]
            self._pending.append(rest)
            self._pending_chars += len(rest)
            if self._pending_chars > self._max_chars:
                raise UpstreamPayloadTooLarge(
                    f"upstream NDJSON line exceeded {self._max_chars} characters without ending"
                )
        return lines

    def flush(self) -> str:
        """Return the unfinished line, and start a new one."""
        text = "".join(self._pending)
        self._pending = []
        self._pending_chars = 0
        return text


class AsyncHTTPClient:
    """Shared async HTTP client with a single underlying session."""

    _shared: AsyncHTTPClient | None = None

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    @classmethod
    def shared(cls) -> AsyncHTTPClient:
        """Get or create a shared AsyncHTTPClient instance."""
        if cls._shared is None:
            cls._shared = AsyncHTTPClient()
        return cls._shared

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # Set a conservative default timeout; callers can override per request.
            timeout = aiohttp.ClientTimeout(total=60)
            # Every adapter's upstream request goes through this connector, so
            # it is where a Base URL whose name now resolves to a cloud metadata
            # endpoint is refused -- whatever it resolved to when it was saved.
            #
            # No connection limit, in total or per host. What bounds the
            # gateway's upstream concurrency is its own admission -- each
            # user's concurrency limit, the per-account outbound limiter
            # (``upstream_limiter``), engine holds for local servers -- and a
            # request holds its slots there while it waits here. A pool limit
            # under those ceilings only made the next request queue behind the
            # pool instead: the 51st concurrent stream to one provider host
            # waited, with no timeout at all, for one of fifty streams that
            # each run for minutes. Every connection belongs to a request one of
            # those limits admitted, so the count is theirs to bound.
            connector = aiohttp.TCPConnector(
                limit=0, limit_per_host=0, socket_factory=refuse_metadata_socket
            )
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
            )
        return self._session

    async def json_post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> dict[str, Any]:
        """Send a POST request with JSON payload.

        On a non-2xx response, the upstream body is read and attached to the
        raised ``ClientResponseError`` as ``error_body`` so the actual provider
        error message survives for logging and user-facing display (the URL is
        scrubbed downstream before any user sees it).

        ``timeout=None`` means "use the session default" (60s). It must NOT be
        forwarded verbatim: aiohttp treats an explicit ``None`` as *no timeout
        at all*, which would let a black-holed upstream hold the request (and
        its connection-pool slot) forever.

        The body is read up to :data:`MAX_RESPONSE_BODY_BYTES` (a longer one
        raises ``UpstreamPayloadTooLarge``), an error body up to
        :data:`MAX_ERROR_BODY_BYTES`.
        """
        session = await self._ensure_session()
        async with session.post(
            url,
            json=json,
            headers=headers,
            timeout=timeout if timeout is not None else _aiohttp_sentinel,
        ) as resp:
            if resp.status >= 400:
                from contextlib import suppress

                error_body = ""
                with suppress(Exception):
                    error_body = await read_error_text(resp)
                raise _upstream_status_error(resp, error_body)
            from typing import cast

            return cast("dict[str, Any]", _json_from_body(resp, await read_body(resp)))

    async def json_post_with_retry(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
        retries: int = 3,
        backoff_base: float = 0.5,
        backoff_factor: float = 2.0,
    ) -> dict[str, Any]:
        """POST JSON with simple exponential backoff retries.

        Retries on aiohttp client errors and timeouts. Backoff delays are
        computed as backoff_base * (backoff_factor ** attempt).
        """
        last_err: BaseException | None = None
        for attempt in range(retries):
            try:
                return await self.json_post(url, json=json, headers=headers, timeout=timeout)
            except (TimeoutError, aiohttp.ClientError) as err:
                last_err = err
                if attempt == retries - 1:
                    raise
                delay = backoff_base * (backoff_factor**attempt)
                ctx = req_ctx.get()
                logger.info(
                    "http_retry",
                    extra={
                        "event": "http_retry",
                        "provider": str(ctx.get("provider", "unknown")),
                        "reason": err.__class__.__name__,
                        "attempt": attempt + 1,
                    },
                )
                await asyncio.sleep(delay)
        # Should never reach here, but keep mypy happy.
        assert last_err is not None
        raise last_err

    async def stream_post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
        mode: str = "sse",
    ) -> AsyncIterator[str]:
        """Stream a POST request line-by-line.

        Performs at most one transparent retry when the underlying TCP socket
        was a stale pooled keep-alive connection that the upstream had already
        dropped. A clean half-close surfaces as ``ServerDisconnectedError``,
        but a socket the upstream reset with a TCP RST (or a half-open one we
        write into) instead surfaces as ``ClientOSError`` / ``ConnectionResetError``
        (ECONNRESET) or ``BrokenPipeError`` (EPIPE). All are raised from
        ``session.post(...).__aenter__()`` — i.e. while sending the request and
        reading status/headers, before any response body byte has been read —
        so retrying once on a fresh connection is safe: the upstream has not
        begun responding, so it never sees a duplicate after it started work.
        The retry is scoped to that connect phase only; once ``__aenter__``
        returns, body iteration runs once with no retry.

        A ``ClientConnectorError`` (DNS failure, connection refused, TLS error
        on a *fresh* connection) is a genuine connectivity failure rather than
        a stale pooled socket, so it propagates without a retry.

        Args:
            url: Target URL.
            json: JSON payload.
            headers: Optional headers.
            timeout: Optional timeout override.
            mode: Streaming mode - "sse" for Server-Sent Events, "ndjson" for
                  newline-delimited JSON, or "auto" to detect from Content-Type.

        Yields:
            Lines from the response (SSE format or raw lines).

        Raises:
            aiohttp.ClientResponseError: If the response status is not 2xx.
        """
        session = await self._ensure_session()
        # Streaming responses can run for minutes (LLM generation + queue time).
        # Let the upstream manage its own lifecycle via [DONE] sentinel -- but
        # not the connection: opening one is bounded (see ``stream_timeout``).
        timeout = stream_timeout(timeout)

        # Phase 1: open connection. Retry once on a stale pooled keep-alive
        # socket, whether it surfaces as a clean disconnect or a reset/broken
        # pipe. A fresh-connection failure (ClientConnectorError: DNS, refused,
        # TLS) and anything else (mid-stream payload error) propagates so the
        # router can decide what to do.
        max_attempts = 2
        cm: Any = None
        resp: Any = None
        for attempt in range(max_attempts):
            cm = session.post(url, json=json, headers=headers, timeout=timeout)
            try:
                resp = await cm.__aenter__()
                break
            except (
                aiohttp.ServerDisconnectedError,
                aiohttp.ClientOSError,
                ConnectionResetError,
            ) as err:
                # ClientConnectorError (subclass of ClientOSError) means we
                # never had a connection to go stale — a real connectivity
                # failure. Don't retry it, and give up after the last attempt.
                if isinstance(err, aiohttp.ClientConnectorError) or attempt == max_attempts - 1:
                    raise
                ctx = req_ctx.get()
                logger.info(
                    "http_retry",
                    extra={
                        "event": "http_retry",
                        "provider": str(ctx.get("provider", "unknown")),
                        "reason": type(err).__name__,
                        "attempt": attempt + 1,
                    },
                )
                logger.warning("Stale keep-alive socket on stream_post %s; retrying once", url)

        assert resp is not None and cm is not None

        # Phase 2: stream body. Manual __aexit__ because __aenter__ ran inside
        # the retry loop above. Surface any in-flight exception to __aexit__
        # so aiohttp can abort the connection rather than release it back to
        # the pool.
        exc_type: type[BaseException] | None = None
        exc_val: BaseException | None = None
        exc_tb: Any = None
        try:
            async for chunk in self._iterate_response_body(resp, mode, url):
                yield chunk
        except BaseException:
            exc_type, exc_val, exc_tb = sys.exc_info()
            raise
        finally:
            await cm.__aexit__(exc_type, exc_val, exc_tb)

    async def _iterate_response_body(self, resp: Any, mode: str, url: str) -> AsyncIterator[str]:
        """Yield SSE / NDJSON / raw lines from an already-connected response.

        No retry: any failure here means the upstream has begun responding,
        so resending the request would risk duplicate generation side effects
        and double billing.
        """
        # Check status and read error body if present before raising
        if resp.status >= 400:
            error_body = ""
            from contextlib import suppress

            with suppress(Exception):
                error_body = await read_error_text(resp)

            raise _upstream_status_error(resp, error_body)

        # Detect content type for streaming mode if requested
        content_type = str(resp.headers.get("Content-Type", "")).lower()
        detected_mode = mode
        if mode == "auto":
            if "text/event-stream" in content_type:
                detected_mode = "sse"
            elif (
                "application/x-ndjson" in content_type
                or "ndjson" in content_type
                or "application/json" in content_type
            ):
                # Many upstreams return a single JSON object for stream endpoints.
                # Treat it as NDJSON and flush the tail at end.
                detected_mode = "ndjson"
            else:
                # Default to SSE when unsure
                detected_mode = "sse"

        if detected_mode == "sse":
            logger.debug(
                f"Connected to {url}, status={resp.status}, type={content_type or 'unknown'}"
            )
            logger.debug(f"Response headers: {dict(resp.headers)}")

            parser = SSEParser(max_frame_chars=MAX_FRAME_CHARS)
            chunk_count = 0
            message_count = 0

            async def _frames() -> AsyncIterator[SSEMessage]:
                """Yield parsed frames, including the one the body end held back.

                The flush sits *after* the read loop, so it runs only on a clean
                end of body: an aborted read (ClientPayloadError, a reset) raises
                out of ``iter_chunked`` and never reaches it, which keeps a real
                truncation a truncation rather than capping it with a frame that
                was never fully received.
                """
                nonlocal chunk_count
                async for raw in resp.content.iter_chunked(4096):
                    chunk_count += 1
                    if chunk_count <= 5 or chunk_count % 10 == 0:
                        logger.debug(f"Chunk {chunk_count}: received {len(raw)} bytes")
                        # Show first few bytes to debug encoding issues
                        preview = raw[:200].decode("utf-8", errors="replace")
                        logger.debug(f"Chunk {chunk_count} preview: {preview}")

                    try:
                        messages = parser.feed(raw)
                    except SSEFrameTooLarge as exc:
                        raise UpstreamPayloadTooLarge(str(exc)) from exc
                    if messages and chunk_count <= 5:
                        logger.debug(
                            f"Chunk {chunk_count} parser produced {len(messages)} messages"
                        )

                    for msg in messages:
                        yield msg

                # Not every upstream terminates its last frame with the blank
                # line SSE asks for -- some close the body right after
                # ``data: [DONE]`` or the final ``finish_reason`` chunk. Emitting
                # it here is what keeps that terminal signal: without the flush
                # the frame dies in the parser's buffer, and the adapter reports
                # a complete generation as an incomplete stream (which then
                # counts against the endpoint's availability). The NDJSON branch
                # below has always flushed its tail for the same reason.
                for msg in parser.flush():
                    logger.debug("Emitting SSE frame held back at end of body")
                    yield msg

            # ``aclosing`` so the inner generator is finalized the moment this
            # one is -- on the [DONE] return, and on the client disconnect that
            # closes this generator mid-stream. Without it the read loop would
            # sit unfinalized until the event loop's async-generator hooks got
            # to it, holding the response body open past the point the caller
            # gave up on it.
            async with aclosing(_frames()) as frames:
                async for msg in frames:
                    if not msg.data:
                        logger.debug("Empty message data, skipping")
                        continue

                    message_count += 1
                    if message_count <= 10 or message_count % 10 == 0:
                        logger.debug(f"Message {message_count} SSE data: {msg.data[:200]}")

                    # Preserve legacy adapter expectations (no trailing newlines)
                    if msg.data.strip() == "[DONE]":
                        logger.debug(
                            f"Received [DONE], total chunks: {chunk_count}, total messages: {message_count}"
                        )
                        yield "data: [DONE]"
                        return

                    output = f"data: {msg.data}"
                    if message_count <= 5:
                        logger.debug(f"Yielding message {message_count}: {output[:200]}")
                    yield output

            logger.info(f"Stream complete: chunks={chunk_count}, messages={message_count}")
        elif detected_mode == "ndjson":
            # Incremental UTF-8 decode + line buffering
            # ``errors="replace"`` for the same reason as the SSE parser: the
            # strict decoder raises for a whole 4 KiB read, and the handler that
            # caught it dropped every line in that read. The comment it carried
            # ("wait for next chunk to complete sequence") described something
            # the incremental decoder already does on its own -- ``final=False``
            # buffers a multibyte sequence split across chunks rather than
            # raising -- so the except branch only ever fired on genuinely
            # invalid bytes, and paid for one bad byte with the whole read.
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            lines = _LineSplitter()
            async for raw in resp.content.iter_chunked(4096):
                text = decoder.decode(raw, final=False)
                if text:
                    for line in lines.feed(text):
                        line = line.strip()
                        if line:
                            yield line
            # Flush tail
            tail = lines.flush().strip()
            if tail:
                yield tail
        else:
            # Fallback to raw decoding (legacy behavior)
            async for raw in resp.content:
                yield raw.decode("utf-8").strip()

    async def request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
        max_body_bytes: int | None = None,
    ) -> tuple[int, bytes, str]:
        """Send an arbitrary HTTP request and return (status, body, content_type).

        Intended for proxy-style forwarding where we need the raw response.
        ``timeout=None`` falls back to the session default (see json_post).
        The body is read up to *max_body_bytes*, by default
        :data:`MAX_RESPONSE_BODY_BYTES`; a longer one raises
        :class:`UpstreamPayloadTooLarge`.
        """
        session = await self._ensure_session()
        async with session.request(
            method,
            url,
            data=data,
            headers=headers,
            timeout=timeout if timeout is not None else _aiohttp_sentinel,
        ) as resp:
            body = await read_body(resp, max_body_bytes)
            content_type = resp.headers.get("content-type", "application/json")
            return resp.status, body, content_type

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
