from __future__ import annotations

import codecs
from dataclasses import dataclass
from typing import Generator, List, Optional


@dataclass
class SSEMessage:
    """Represents a single SSE message with optional event and id.

    Attributes:
        data: The payload carried by the SSE message (single frame).
        event: Optional event name if provided by the server.
        id: Optional message/event id if provided by the server.
    """

    data: str
    event: Optional[str] = None
    id: Optional[str] = None


class SSEParser:
    """Incremental SSE parser that handles UTF-8 split boundaries.

    This parser accumulates bytes, decodes incrementally, and yields completed
    SSE frames when a blank line ("\n\n") boundary is observed. Only "data:" is
    required by the adapters; "event:" and "id:" are carried if present.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buffer: str = ""

    def feed(self, chunk: bytes) -> List[SSEMessage]:
        """Feed a raw byte chunk and return any completed SSE messages.

        Args:
            chunk: Next raw bytes from the HTTP stream.

        Returns:
            List[SSEMessage]: Zero or more parsed SSE messages extracted from
            the internal buffer after processing the chunk.
        """
        try:
            # Use incremental decoding to safely handle multi-byte boundaries.
            self._buffer += self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError:
            # Incomplete multibyte sequence; wait for next chunk.
            return []
        messages: List[SSEMessage] = []
        while "\n\n" in self._buffer:
            frame, self._buffer = self._buffer.split("\n\n", 1)
            if not frame.strip():
                continue
            messages.append(self._parse_frame(frame))
        return messages

    @staticmethod
    def _parse_frame(frame: str) -> SSEMessage:
        data_lines: List[str] = []
        event: Optional[str] = None
        msg_id: Optional[str] = None
        for line in frame.splitlines():
            if line.startswith(":"):
                # Comment line; ignore
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif line.startswith("event:"):
                event = line[6:].lstrip()
            elif line.startswith("id:"):
                msg_id = line[3:].lstrip()
        return SSEMessage(data="\n".join(data_lines), event=event, id=msg_id)
