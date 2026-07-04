"""Shared ASGI markers for long-lived streaming responses."""

from __future__ import annotations

STREAMING_RESPONSE_MARKER_HEADER = b"x-hybridinference-streaming-response"
STREAMING_RESPONSE_SCOPE_STATE_KEY = "hybridinference_streaming_response"

# TimeoutMiddleware stashes its per-request ``anyio.CancelScope`` here so
# response generators can tell a middleware-timeout cancellation
# (``scope.cancel_called`` is True) from a client disconnect when classifying
# an aborted stream for the api_logs row.
REQUEST_TIMEOUT_SCOPE_STATE_KEY = "hybridinference_request_timeout_scope"
