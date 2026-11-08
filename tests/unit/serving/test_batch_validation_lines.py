"""Splitting a file batch's input into lines (``serving.batch.validation.iter_lines``).

The lines must be exactly what the earlier splitter produced -- the ``\\r`` of a
CRLF kept for the parser to strip, a last line with no newline still a line, the
length cap enforced on complete and on unfinished lines -- without copying the
rest of a chunk for every line it holds.
"""

from __future__ import annotations

import random
import time
from typing import Any

from serving.batch.validation import LineTooLong, iter_lines


class _Chunks:
    """A file store whose upload is *chunks*, served ``limit`` at a time."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.reads = 0

    async def read_chunks(
        self, file_id: str, *, after_seq: int = -1, limit: int = 8
    ) -> list[tuple[int, bytes]]:
        self.reads += 1
        start = after_seq + 1
        return list(enumerate(self.chunks))[start : start + limit]


async def _reference(chunks: list[bytes], max_line_bytes: int) -> Any:
    """The splitter as it was before the offset scan, kept to compare against."""
    store = _Chunks(chunks)
    buffer = b""
    line_no = 0
    after = -1
    while True:
        batch = await store.read_chunks("f", after_seq=after, limit=4)
        if not batch:
            break
        for seq, data in batch:
            after = seq
            buffer += data
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    break
                line, buffer = buffer[:newline], buffer[newline + 1 :]
                line_no += 1
                if len(line) > max_line_bytes:
                    raise LineTooLong(
                        "line_too_long",
                        f"Line {line_no} is longer than {max_line_bytes} bytes.",
                        line_no,
                    )
                yield line_no, line
            if len(buffer) > max_line_bytes:
                raise LineTooLong(
                    "line_too_long",
                    f"Line {line_no + 1} is longer than {max_line_bytes} bytes.",
                    line_no + 1,
                )
    if buffer:
        line_no += 1
        yield line_no, buffer


async def _outcome(lines: Any) -> tuple[list[tuple[int, bytes]], dict[str, Any] | None]:
    """Every line produced, and the error that ended the split (None at the end)."""
    seen: list[tuple[int, bytes]] = []
    try:
        async for item in lines:
            seen.append(item)
    except LineTooLong as exc:
        return seen, exc.entry
    return seen, None


async def _split(chunks: list[bytes], max_line_bytes: int = 64) -> Any:
    return await _outcome(iter_lines(_Chunks(chunks), "f", max_line_bytes=max_line_bytes))


def _chunked(data: bytes, rng: random.Random) -> list[bytes]:
    """Cut *data* at random points, empty chunks included."""
    chunks = []
    position = 0
    while position < len(data):
        size = rng.choice((0, 1, 2, 3, 7, 16, 50, 200))
        chunks.append(data[position : position + size])
        position += size
    return chunks


def _file(rng: random.Random) -> bytes:
    """A file of short, empty, CRLF, near-cap and over-cap lines, maybe unterminated."""
    pieces = []
    for _ in range(rng.randint(0, 30)):
        length = rng.choice((0, 1, 5, 20, 63, 64, 65, 120))
        line = bytes(rng.choice(b'ab {}:,"\r') for _ in range(length))
        ending = rng.choice((b"\n", b"\r\n", b"\n\n"))
        pieces.append(line + ending)
    data = b"".join(pieces)
    if rng.random() < 0.5:
        data += bytes(rng.choice(b"xy\r") for _ in range(rng.randint(1, 80)))
    return data


async def test_the_lines_and_errors_are_exactly_the_earlier_splitters():
    rng = random.Random(52)
    for _ in range(2000):
        data = _file(rng)
        chunks = _chunked(data, rng)
        max_line_bytes = rng.choice((1, 20, 64, 1000))
        expected = await _outcome(_reference(chunks, max_line_bytes))
        assert await _split(chunks, max_line_bytes) == expected, (chunks, max_line_bytes)


async def test_crlf_is_kept_for_the_parser_and_a_last_line_needs_no_newline():
    lines, error = await _split([b'{"a": 1}\r\n{"b"', b': 2}\r\n\n{"c": 3}'])

    assert error is None
    assert lines == [(1, b'{"a": 1}\r'), (2, b'{"b": 2}\r'), (3, b""), (4, b'{"c": 3}')]


async def test_a_final_newline_adds_no_line():
    assert await _split([b"one\ntwo\n"]) == ([(1, b"one"), (2, b"two")], None)


async def test_a_line_at_the_cap_passes_and_one_byte_more_fails_with_its_number():
    at_cap = b"x" * 64
    lines, error = await _split([at_cap + b"\n" + at_cap + b"y\nnever\n"])

    assert lines == [(1, at_cap)]
    assert error is not None
    assert (error["code"], error["line"]) == ("line_too_long", 2)


async def test_an_unfinished_line_over_the_cap_fails_before_more_is_read():
    store = _Chunks([b"ok\n" + b"x" * 65] + [b"x"] * 20)

    lines, error = await _outcome(iter_lines(store, "f", max_line_bytes=64))

    assert lines == [(1, b"ok")]
    assert error is not None
    assert (error["code"], error["line"]) == ("line_too_long", 2)
    # One read of four chunks: the first already holds more than a line may.
    assert store.reads == 1


async def test_chunks_of_short_lines_are_split_in_linear_time():
    """Four 1 MiB chunks of 4-byte lines.

    Copying the rest of the chunk at every line took about 20 s for these on
    the machine this was written on; the offset scan takes under half a second.
    """
    line = b"abc\n"
    chunk = line * (1024 * 1024 // len(line))
    store = _Chunks([chunk] * 4)

    started = time.perf_counter()
    count = 0
    async for _ in iter_lines(store, "f", max_line_bytes=1024):
        count += 1
    elapsed = time.perf_counter() - started

    assert count == 4 * len(chunk) // len(line)
    assert elapsed < 4.0, f"split {count} lines in {elapsed:.1f}s"
