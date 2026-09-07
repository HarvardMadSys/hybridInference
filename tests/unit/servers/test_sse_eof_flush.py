"""Regression tests for the frame an upstream leaves unterminated at end of body.

SSE delimits frames with a blank line, so the parser cannot know a frame is
complete until it sees one. Providers that close the body right after their last
event used to lose it, and with it the stream's terminal signal -- which the
OpenAI-compatible adapter then reported as
``_INCOMPLETE_STREAM_ERROR``/``stream_exception`` for a generation that had in
fact finished, dragging the endpoint's EWMA availability under the circuit
breaker's floor.
"""

from __future__ import annotations

import pytest

from serving.servers.sse import SSEMessage, SSEParser


@pytest.mark.unit
def test_flush_emits_final_frame_missing_its_blank_line():
    """A last frame terminated by a single newline survives end of body."""
    parser = SSEParser()

    assert parser.feed(b'data: {"a":1}\n\ndata: [DONE]\n') == [SSEMessage(data='{"a":1}')]
    assert parser.flush() == [SSEMessage(data="[DONE]")]


@pytest.mark.unit
def test_flush_emits_final_frame_with_no_trailing_newline_at_all():
    """Not even a closing newline is required -- EOF ends the frame."""
    parser = SSEParser()

    assert parser.feed(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}') == []
    assert parser.flush() == [SSEMessage(data='{"choices":[{"delta":{},"finish_reason":"stop"}]}')]


@pytest.mark.unit
def test_flush_recovers_frame_held_back_by_a_trailing_cr():
    """A CRLF upstream's final frame is not stranded in ``_pending_cr``.

    ``feed`` holds a trailing CR back so a CRLF delimiter split across two reads
    still rejoins. At EOF that CR is simply a line terminator.
    """
    parser = SSEParser()

    assert parser.feed(b'data: {"a":1}\r\n\r\ndata: [DONE]\r') == [SSEMessage(data='{"a":1}')]
    assert parser.flush() == [SSEMessage(data="[DONE]")]


@pytest.mark.unit
def test_flush_is_empty_after_a_properly_terminated_stream():
    """The common case leaves no residue, so the flush adds no frame."""
    parser = SSEParser()

    assert parser.feed(b'data: {"a":1}\n\ndata: [DONE]\n\n') == [
        SSEMessage(data='{"a":1}'),
        SSEMessage(data="[DONE]"),
    ]
    assert parser.flush() == []


@pytest.mark.unit
def test_flush_ignores_a_metadata_only_residue():
    """A dangling ``id:``/``event:``/comment line is not a frame to emit.

    Emitting it would put a bare ``data: `` on the wire for a consumer to parse.
    """
    for residue in (b"id: 42\n", b"event: ping\n", b": keep-alive\n"):
        parser = SSEParser()
        parser.feed(b'data: {"a":1}\n\n' + residue)
        assert parser.flush() == [], residue


@pytest.mark.unit
def test_flush_is_idempotent():
    """A second call has nothing left to give, so it cannot duplicate a frame."""
    parser = SSEParser()
    parser.feed(b"data: [DONE]\n")

    assert parser.flush() == [SSEMessage(data="[DONE]")]
    assert parser.flush() == []


@pytest.mark.unit
def test_flush_does_not_resurrect_a_partial_json_payload_as_valid():
    """A truncated body still reads as truncated downstream.

    The flush deliberately does not judge payloads -- it emits the residue and
    lets the consumer parse it. A half-received frame therefore still fails
    ``json.loads`` in the adapter, which keeps raising for a real truncation.
    """
    import json

    parser = SSEParser()
    parser.feed(b'data: {"choices":[{"delta":{"content":"hel')

    (message,) = parser.flush()
    with pytest.raises(json.JSONDecodeError):
        json.loads(message.data)


@pytest.mark.unit
def test_one_bad_byte_no_longer_discards_the_whole_read():
    """A malformed byte costs a replacement char, not every frame in the chunk.

    The strict decoder raised for the entire 4 KiB read, and the handler dropped
    all of it -- so damage anywhere in the final read took the terminal frame
    with it.
    """
    parser = SSEParser()

    messages = parser.feed(b'data: {"a":1}\n\ndata: \xffbad\n\ndata: [DONE]\n\n')

    assert [m.data for m in messages] == ['{"a":1}', "�bad", "[DONE]"]


@pytest.mark.unit
def test_multibyte_split_across_reads_is_still_buffered_not_replaced():
    """Replacement must not fire for a sequence merely split across chunks."""
    parser = SSEParser()

    assert parser.feed(b'data: {"text":"') == []
    assert parser.feed(b"\xe4\xb8") == []  # first two bytes of '中'
    messages = parser.feed(b'\xad"}\n\n')

    assert messages == [SSEMessage(data='{"text":"中"}')]
