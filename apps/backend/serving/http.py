# mypy: disable-error-code=no-any-unimported
"""Lightweight shared async HTTP client for adapters.

Provides a shared aiohttp session with convenience helpers for JSON
requests and streaming responses. Adapters can depend on this instead
of each maintaining their own sessions.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING, Any

import aiohttp

from serving.servers.sse import SSEParser
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


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
            connector = aiohttp.TCPConnector(limit=200, limit_per_host=50)
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
        """
        session = await self._ensure_session()
        async with session.post(url, json=json, headers=headers, timeout=timeout) as resp:
            if resp.status >= 400:
                from contextlib import suppress

                error_body = ""
                with suppress(Exception):
                    error_body = await resp.text()
                error = aiohttp.ClientResponseError(
                    request_info=resp.request_info,
                    history=resp.history,
                    status=resp.status,
                    message=resp.reason or "Unknown error",
                    headers=resp.headers,
                )
                if error_body:
                    error.error_body = error_body  # type: ignore[attr-defined]
                raise error
            from typing import cast

            return cast("dict[str, Any]", await resp.json())

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
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
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

    async def json_get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> dict[str, Any]:
        """Send a GET request and return JSON response."""
        session = await self._ensure_session()
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            resp.raise_for_status()
            from typing import cast

            return cast("dict[str, Any]", await resp.json())

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
        # Let the upstream manage its own lifecycle via [DONE] sentinel.
        if timeout is None:
            timeout = aiohttp.ClientTimeout(total=None)

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
                error_body = await resp.text()

            # Create a more informative error
            error = aiohttp.ClientResponseError(
                request_info=resp.request_info,
                history=resp.history,
                status=resp.status,
                message=resp.reason or "Unknown error",
                headers=resp.headers,
            )
            # Attach error body for logging
            if error_body:
                error.error_body = error_body  # type: ignore[attr-defined]
            raise error

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

            parser = SSEParser()
            chunk_count = 0
            message_count = 0
            async for raw in resp.content.iter_chunked(4096):
                chunk_count += 1
                if chunk_count <= 5 or chunk_count % 10 == 0:
                    logger.debug(f"Chunk {chunk_count}: received {len(raw)} bytes")
                    # Show first few bytes to debug encoding issues
                    preview = raw[:200].decode("utf-8", errors="replace")
                    logger.debug(f"Chunk {chunk_count} preview: {preview}")

                messages = list(parser.feed(raw))
                if messages and chunk_count <= 5:
                    logger.debug(f"Chunk {chunk_count} parser produced {len(messages)} messages")

                for msg in messages:
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
            import codecs

            decoder = codecs.getincrementaldecoder("utf-8")()
            buffer = ""
            async for raw in resp.content.iter_chunked(4096):
                try:
                    text = decoder.decode(raw, final=False)
                except UnicodeDecodeError:
                    # Wait for next chunk to complete sequence
                    text = ""
                if text:
                    buffer += text
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if line:
                            yield line
            # Flush tail
            tail = buffer.strip()
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
    ) -> tuple[int, bytes, str]:
        """Send an arbitrary HTTP request and return (status, body, content_type).

        Intended for proxy-style forwarding where we need the raw response.
        """
        session = await self._ensure_session()
        async with session.request(
            method, url, data=data, headers=headers, timeout=timeout
        ) as resp:
            body = await resp.read()
            content_type = resp.headers.get("content-type", "application/json")
            return resp.status, body, content_type

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
