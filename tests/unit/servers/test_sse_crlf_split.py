"""Regression tests for CRLF frame delimiters split across read chunks."""

from __future__ import annotations

from apps.backend.serving.servers.sse import SSEMessage, SSEParser


def test_crlf_delimiter_split_across_chunks() -> None:
    """A CRLF frame boundary straddling two chunks must not merge frames."""
    parser = SSEParser()

    # First chunk ends mid-delimiter ('...\r\n\r'); no complete frame yet.
    assert parser.feed(b'data: {"a":1}\r\n\r') == []

    # Second chunk supplies the missing '\n' plus a second full frame.
    messages = parser.feed(b'\ndata: {"b":2}\r\n\r\n')

    assert messages == [
        SSEMessage(data='{"a":1}'),
        SSEMessage(data='{"b":2}'),
    ]


def test_whole_crlf_frame_single_chunk() -> None:
    """Happy path: a complete CRLF-delimited frame in one chunk parses cleanly."""
    parser = SSEParser()

    messages = parser.feed(b'data: {"a":1}\r\n\r\n')

    assert messages == [SSEMessage(data='{"a":1}')]
