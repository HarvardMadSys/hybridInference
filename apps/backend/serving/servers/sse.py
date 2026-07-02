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
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buffer: str = ""
        self._pending_cr: str = ""

    def feed(self, chunk: bytes) -> list[SSEMessage]:
        """Feed raw bytes and yield complete SSE messages when available."""
        try:
            decoded = self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError:
            return []

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
