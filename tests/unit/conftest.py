"""Unit-test level bootstrap to stub optional dependencies at import time.

Some packages (e.g., serving.__init__ importing serving.config) depend on
third-party modules optional in CI. We stub them here to avoid import-time
failures while focusing on pure-unit tests that don't need their behavior.
"""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

# Stub python-dotenv only when the real package is unavailable.
if "dotenv" not in sys.modules:  # pragma: no cover - import-time shim
    try:
        sys.modules["dotenv"] = importlib.import_module("dotenv")
    except ImportError:
        sys.modules["dotenv"] = SimpleNamespace(
            load_dotenv=lambda *a, **k: None,
            dotenv_values=lambda *a, **k: {},
        )

# Stub aiohttp if missing to satisfy imports in serving.http
if "aiohttp" not in sys.modules:  # pragma: no cover - import-time shim

    class _DummySession:  # minimal placeholder
        def __init__(self, *a, **k):
            self.closed = False

        async def close(self):
            self.closed = True

        # Methods used by tests are patched, so we keep placeholders only.

    class _ClientError(Exception):
        """Stub for aiohttp.ClientError (root of aiohttp client errors)."""

    class _ClientResponseError(_ClientError):
        """Stub for aiohttp.ClientResponseError."""

        def __init__(self, request_info=None, history=(), status=0, message="", headers=None):
            self.request_info = request_info
            self.history = history
            self.status = status
            self.message = message
            self.headers = headers
            super().__init__(message)

    class _ServerDisconnectedError(_ClientError):
        """Stub for aiohttp.ServerDisconnectedError.

        Mirrors the real hierarchy: ServerDisconnectedError is a ClientError
        subclass in aiohttp, so ``except aiohttp.ClientError`` paths catch it.
        """

    class _ClientOSError(_ClientError, OSError):
        """Stub for aiohttp.ClientOSError (socket errors: ECONNRESET, EPIPE).

        Mirrors the real hierarchy (ClientOSError subclasses both ClientError
        and OSError) so ``except aiohttp.ClientOSError`` / ``OSError`` paths
        catch it.
        """

    class _ClientConnectorError(_ClientOSError):
        """Stub for aiohttp.ClientConnectorError (fresh-connection failure).

        Subclasses ClientOSError, matching aiohttp, so the stream_post retry
        guard can distinguish a genuine connect failure from a stale socket.
        """

    sys.modules["aiohttp"] = SimpleNamespace(
        ClientError=_ClientError,
        ClientResponseError=_ClientResponseError,
        ServerDisconnectedError=_ServerDisconnectedError,
        ClientOSError=_ClientOSError,
        ClientConnectorError=_ClientConnectorError,
        ClientTimeout=lambda total=None: None,
        ClientSession=_DummySession,
        TCPConnector=lambda **k: None,
    )
