"""Close accepted HTTP connections that stop moving bytes.

Uvicorn arms its keep-alive timer only while it is waiting for a request, and
cancels that timer on the first byte of the next one. The timer is armed again
only after the response completes. A client that sends part of a request and
then stops — the console proxy does this when a browser goes away before the
upstream request is torn down — otherwise holds the accepted socket until the
process restarts. The request timeout does not cover it either: that middleware
runs only once the headers are complete and the application is called.

Bytes in either direction reset the clock, so a stream that keeps sending is
unaffected. ``HTTP_IDLE_TIMEOUT_SECONDS`` of 0 or less disables it.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import TYPE_CHECKING

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

#: Above the default request timeout (120s) and the inter-chunk stream idle
#: (180s), and above the 300s request timeout production sets, so a slow
#: prefill still gets the gateway's 504 instead of a dropped connection.
#: The sockets this exists for sit idle for hours.
_DEFAULT_IDLE_TIMEOUT_S = 600.0

_PROTOCOLS = (
    ("uvicorn.protocols.http.httptools_impl", "HttpToolsProtocol"),
    ("uvicorn.protocols.http.h11_impl", "H11Protocol"),
)
_INSTALLED = "_idle_connection_timeout_installed"


def idle_timeout_seconds() -> float:
    """Return the accepted-connection idle timeout, or 0 when it is disabled.

    A missing or unreadable setting keeps the default. Zero and negative
    values disable the clock.
    """
    try:
        from serving.config.app_config import config_value

        raw = config_value("HTTP_IDLE_TIMEOUT_SECONDS", str(_DEFAULT_IDLE_TIMEOUT_S))
    except Exception:
        return _DEFAULT_IDLE_TIMEOUT_S
    return parse_idle_timeout_seconds(raw)


def parse_idle_timeout_seconds(raw: object) -> float:
    """Parse a configured idle timeout. Invalid input keeps the default."""
    if raw is None:
        return _DEFAULT_IDLE_TIMEOUT_S
    text = str(raw).strip()
    if text == "":
        return _DEFAULT_IDLE_TIMEOUT_S
    try:
        value = float(text)
    except (TypeError, ValueError):
        return _DEFAULT_IDLE_TIMEOUT_S
    if value <= 0:
        return 0.0
    return value


class IdleWatch:
    """One timer for one accepted connection."""

    def __init__(
        self,
        transport: object,
        timeout_s: Callable[[], float],
        peer: Callable[[], str],
    ) -> None:
        self._transport = transport
        self._timeout_s = timeout_s
        self._peer = peer
        self._loop = asyncio.get_running_loop()
        self._handle: asyncio.TimerHandle | None = None
        self._closed = False
        self._armed_for = 0.0

    def touch(self) -> None:
        """Push the deadline out because a byte just moved."""
        if self._closed:
            return
        self._cancel_handle()
        timeout_s = self._timeout_s()
        if timeout_s <= 0:
            self._armed_for = 0.0
            return
        self._armed_for = timeout_s
        self._handle = self._loop.call_later(timeout_s, self._fire)

    def cancel(self) -> None:
        """Drop the timer. The connection is already going away."""
        self._closed = True
        self._cancel_handle()

    def _cancel_handle(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _fire(self) -> None:
        self._handle = None
        if self._closed:
            return
        is_closing = getattr(self._transport, "is_closing", None)
        if callable(is_closing) and is_closing():
            return
        logger.warning(
            "http_idle_timeout timeout_s=%s peer=%s",
            self._armed_for,
            self._peer(),
        )
        # Mark closed before closing so connection_lost's cancel is a no-op
        # if the transport drops the connection from inside close().
        self._closed = True
        close = getattr(self._transport, "close", None)
        if callable(close):
            close()


def install_idle_connection_timeout() -> None:
    """Arm the idle clock on uvicorn's HTTP protocol classes.

    Safe to call more than once. A protocol implementation that is not
    importable is skipped; httptools is what the image serves with.
    """
    for module_name, class_name in _PROTOCOLS:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        protocol = getattr(module, class_name, None)
        if protocol is not None and not getattr(protocol, _INSTALLED, False):
            _wrap(protocol)
            setattr(protocol, _INSTALLED, True)
        cycle = getattr(module, "RequestResponseCycle", None)
        if cycle is not None and not getattr(cycle, _INSTALLED, False):
            _wrap_cycle(cycle)
            setattr(cycle, _INSTALLED, True)


def _wrap(protocol: type) -> None:
    orig_made = protocol.connection_made
    orig_data = protocol.data_received
    orig_lost = protocol.connection_lost

    def connection_made(self: object, transport: object) -> None:
        orig_made(self, transport)
        _attach(self, transport)

    def data_received(self: object, data: object) -> None:
        watch = getattr(self, "_idle_watch", None)
        if watch is not None:
            watch.touch()
        orig_data(self, data)

    def connection_lost(self: object, exc: object) -> None:
        watch = getattr(self, "_idle_watch", None)
        if watch is not None:
            watch.cancel()
        _forget_transport(getattr(self, "transport", None))
        orig_lost(self, exc)

    protocol.connection_made = connection_made  # type: ignore[method-assign]
    protocol.data_received = data_received  # type: ignore[method-assign]
    protocol.connection_lost = connection_lost  # type: ignore[method-assign]


# uvloop's transport.write is read-only, so outbound bytes are observed from
# the response cycle instead of by replacing the method. Keyed by id because
# a uvloop transport is not weak-referenceable. connection_lost drops the entry.
_BY_TRANSPORT: dict[int, IdleWatch] = {}


def _attach(protocol: object, transport: object) -> None:
    watch = IdleWatch(transport, idle_timeout_seconds, lambda: _peer(protocol))
    protocol._idle_watch = watch  # type: ignore[attr-defined]
    _BY_TRANSPORT[id(transport)] = watch
    watch.touch()


def _touch_transport(transport: object) -> None:
    watch = _BY_TRANSPORT.get(id(transport))
    if watch is not None:
        watch.touch()


def _forget_transport(transport: object | None) -> None:
    if transport is not None:
        _BY_TRANSPORT.pop(id(transport), None)


def _wrap_cycle(cycle: type) -> None:
    orig_send = cycle.send

    async def send(self: object, message: object) -> None:
        _touch_transport(getattr(self, "transport", None))
        await orig_send(self, message)

    cycle.send = send  # type: ignore[method-assign]


def _peer(protocol: object) -> str:
    client = getattr(protocol, "client", None)
    return "" if client is None else str(client)
