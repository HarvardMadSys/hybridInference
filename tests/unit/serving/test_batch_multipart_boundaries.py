"""Multipart upload limits and boundary matching must not depend on chunk cuts."""

from unittest.mock import AsyncMock

import pytest

from serving.batch.multipart import MAX_HEADER_BYTES, MultipartError, read_multipart


async def _read(body, chunk_size):
    async def stream():
        for start in range(0, len(body), chunk_size):
            yield body[start : start + chunk_size]

    data = bytearray()

    async def collect(chunk):
        data.extend(chunk)

    await read_multipart(
        stream(),
        b"batch-test",
        on_field=AsyncMock(),
        on_file_start=AsyncMock(),
        on_file_data=collect,
        on_file_end=AsyncMock(),
    )
    return bytes(data)


@pytest.mark.parametrize("chunk_size", [1, 7, 31, 1024])
async def test_boundary_prefix_inside_file_is_preserved(chunk_size):
    content = b"first\r\n--batch-test-is-file-content\r\nlast\n"
    body = (
        b'--batch-test\r\nContent-Disposition: form-data; name="file"; filename="input.jsonl"'
        b"\r\n\r\n" + content + b"\r\n--batch-test--\r\n"
    )
    assert await _read(body, chunk_size) == content


@pytest.mark.parametrize("chunk_size", [1, 65536])
async def test_header_size_limit_applies_with_or_without_terminator_in_same_chunk(chunk_size):
    body = (
        b'--batch-test\r\nContent-Disposition: form-data; name="file"; filename="input.jsonl"\r\n'
        b"X-Padding: " + b"x" * MAX_HEADER_BYTES + b"\r\n\r\n{}\n\r\n--batch-test--\r\n"
    )
    with pytest.raises(MultipartError, match="headers are too large"):
        await _read(body, chunk_size)


@pytest.mark.parametrize("chunk_size", [1, 7, 65536])
async def test_headers_exactly_at_limit_accept_a_split_terminator(chunk_size):
    headers = b'Content-Disposition: form-data; name="file"; filename="input.jsonl"\r\nX-Padding: '
    headers += b"x" * (MAX_HEADER_BYTES - len(headers))
    body = b"--batch-test\r\n" + headers + b"\r\n\r\n{}\n\r\n--batch-test--\r\n"
    assert await _read(body, chunk_size) == b"{}\n"
