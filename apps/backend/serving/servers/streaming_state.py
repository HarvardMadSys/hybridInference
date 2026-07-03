"""Shared ASGI markers for long-lived streaming responses."""

from __future__ import annotations

STREAMING_RESPONSE_MARKER_HEADER = b"x-hybridinference-streaming-response"
STREAMING_RESPONSE_SCOPE_STATE_KEY = "hybridinference_streaming_response"
