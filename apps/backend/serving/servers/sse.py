"""Incremental Server-Sent Events (SSE) parser for streaming responses."""

from __future__ import annotations

import codecs
from dataclasses import dataclass


@dataclass
class SSEMessage:
    """Represents a single SSE frame."""

    data: str
    event: str | None = None
    id: str | None = None


class SSEParser:
    """Incremental parser for Server-Sent Events streams."""

    def __init__(self) -> None:
        # ``errors="replace"``, not the default strict mode: one malformed byte
        # must not cost the whole read. The strict decoder raises for the entire
        # chunk, and the only thing a caller can do with that is drop all 4 KiB
        # of it -- including, when the damage lands in the final read, the
        # stream's terminal frame, which the consumer then reports as a
        # truncated generation. ``final=False`` still *buffers* a multibyte
        # sequence merely split across chunks (that is not an error), so
        # replacement applies only to genuinely invalid bytes, and costs one
        # U+FFFD inside a field instead of every frame in the chunk.
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer: str = ""
        self._pending_cr: str = ""

    def feed(self, chunk: bytes) -> list[SSEMessage]:
        """Feed raw bytes and yield complete SSE messages when available."""
        decoded = self._decoder.decode(chunk, final=False)

        # Providers use CRLF; normalise to LF so frame boundaries are stable.
        # A CRLF delimiter can straddle two chunks (chunk ends '\r', next starts
        # '\n'); hold back a trailing CR so the split pair rejoins and the frame
        # boundary isn't missed. A lone CR is only ever a line terminator in SSE,
        # so mapping it to LF is spec-safe.
        data = self._pending_cr + decoded
        self._pending_cr = ""
        if data.endswith("\r"):
            self._pending_cr = "\r"
            data = data[:-1]
        self._buffer += data.replace("\r\n", "\n").replace("\r", "\n")

        messages: list[SSEMessage] = []
        while "\n\n" in self._buffer:
            frame, self._buffer = self._buffer.split("\n\n", 1)
            if not frame.strip():
                continue
            messages.append(self._parse_frame(frame))
        return messages

    def flush(self) -> list[SSEMessage]:
        """Return the frame the end of the body left unterminated.

        SSE delimits frames with a blank line, so ``feed`` cannot know a frame is
        complete until it sees one. An upstream that closes the body straight
        after its last frame -- ``data: [DONE]`` with a single trailing newline,
        or a final chunk carrying ``finish_reason`` -- leaves that frame in the
        buffer, and dropping it loses the stream's only terminal signal: the
        consumer then reports a generation that finished as truncated. At end of
        body there is nothing left to wait for, so the residue is a whole frame
        by definition.

        Call this only on a *clean* end of body. On an aborted read the residue
        is a genuinely partial frame, and emitting it would dress a real
        truncation up as a complete answer.

        Bytes still held by the incremental decoder are an incomplete multibyte
        sequence -- a truncated body, nothing to recover -- and are dropped.
        Returns at most one message, and nothing for a residue carrying no
        ``data:`` payload (a trailing ``id:``/``event:``/comment line): that is
        metadata, not a frame a consumer can act on. Idempotent -- a second call
        returns nothing.
        """
        residue = self._buffer
        if self._pending_cr:
            # A held-back trailing CR is itself a line terminator (see feed).
            residue += "\n"
        self._buffer = ""
        self._pending_cr = ""
        if not residue.strip():
            return []
        message = self._parse_frame(residue)
        return [message] if message.data else []

    @staticmethod
    def _parse_frame(frame: str) -> SSEMessage:
        data_lines: list[str] = []
        event: str | None = None
        msg_id: str | None = None

        for raw_line in frame.splitlines():
            line = raw_line.rstrip("\r")
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif line.startswith("event:"):
                event = line[6:].lstrip()
            elif line.startswith("id:"):
                msg_id = line[3:].lstrip()

        return SSEMessage(data="\n".join(data_lines), event=event, id=msg_id)
