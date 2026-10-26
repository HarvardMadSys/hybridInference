"""A streaming ``multipart/form-data`` reader for batch file uploads.

The Files upload is the only multipart endpoint the gateway serves, and its
file part can be tens of MiB: it must reach the database chunk by chunk, never
whole in memory and never spooled to local disk (another process serves the
download). So the body is read as it arrives and each part is handed on as it
is found: small fields whole (:data:`MAX_FIELD_BYTES`), the file part as a
sequence of byte runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

MAX_FIELD_BYTES = 4096
MAX_HEADER_BYTES = 16 * 1024
#: Parts one body may hold (one file plus a few fields).
MAX_PARTS = 8


class MultipartError(ValueError):
    """The body is not well-formed ``multipart/form-data``."""


def boundary_of(content_type: str | None) -> bytes:
    """The boundary of a ``multipart/form-data`` content type.

    Raises:
        MultipartError: Not multipart/form-data, or no boundary.
    """
    if not content_type:
        raise MultipartError("Content-Type must be multipart/form-data")
    kind, _, params = content_type.partition(";")
    if kind.strip().lower() != "multipart/form-data":
        raise MultipartError("Content-Type must be multipart/form-data")
    for param in params.split(";"):
        name, _, value = param.strip().partition("=")
        if name.lower() == "boundary" and value:
            value = value.strip().strip('"')
            if 0 < len(value) <= 200:
                return value.encode("latin-1")
    raise MultipartError("multipart/form-data needs a boundary")


def _disposition(headers: dict[str, str]) -> tuple[str, str | None]:
    raw = headers.get("content-disposition", "")
    kind, _, rest = raw.partition(";")
    if kind.strip().lower() != "form-data":
        raise MultipartError("each part needs Content-Disposition: form-data")
    name: str | None = None
    filename: str | None = None
    for param in _split_params(rest):
        key, _, value = param.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"')
        if key == "name":
            name = value
        elif key == "filename":
            filename = value
    if not name:
        raise MultipartError("a part has no name")
    return name, filename


def _split_params(text: str) -> list[str]:
    out, current, quoted = [], [], False
    for char in text:
        if char == '"':
            quoted = not quoted
        if char == ";" and not quoted:
            out.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        out.append("".join(current))
    return [part for part in (p.strip() for p in out) if part]


async def read_multipart(
    stream: AsyncIterator[bytes],
    boundary: bytes,
    *,
    on_field: Callable[[str, str], Awaitable[None]],
    on_file_start: Callable[[str, str], Awaitable[None]],
    on_file_data: Callable[[bytes], Awaitable[None]],
    on_file_end: Callable[[], Awaitable[None]],
) -> None:
    """Read a multipart body from *stream*, calling back for each part.

    Raises:
        MultipartError: The body is malformed, a field is too large, or it ends
            before its closing boundary.
    """
    delimiter = b"--" + boundary
    separator = b"\r\n" + delimiter
    buffer = b""
    state = "preamble"
    field_name: str | None = None
    field_value = bytearray()
    in_file = False
    finished = False
    parts = 0

    async def emit(data: bytes) -> None:
        nonlocal field_value
        if not data:
            return
        if in_file:
            await on_file_data(data)
        else:
            field_value += data
            if len(field_value) > MAX_FIELD_BYTES:
                raise MultipartError("a form field is too large")

    async for chunk in stream:
        if finished:
            # The epilogue after the closing boundary is ignored, but still
            # counted by the caller's byte limit.
            continue
        buffer += chunk
        while True:
            if state == "preamble":
                index = buffer.find(delimiter)
                if index < 0:
                    buffer = buffer[-len(delimiter) :]
                    break
                buffer = buffer[index + len(delimiter) :]
                state = "after_delimiter"
            if state == "after_delimiter":
                if len(buffer) < 2:
                    break
                if buffer[:2] == b"--":
                    finished = True
                    break
                if buffer[:2] != b"\r\n":
                    raise MultipartError("malformed boundary line")
                buffer = buffer[2:]
                state = "headers"
            if state == "headers":
                index = buffer.find(b"\r\n\r\n")
                if index < 0:
                    if len(buffer) > MAX_HEADER_BYTES:
                        raise MultipartError("part headers are too large")
                    break
                headers: dict[str, str] = {}
                for line in buffer[:index].split(b"\r\n"):
                    name, _, value = line.decode("latin-1").partition(":")
                    headers[name.strip().lower()] = value.strip()
                buffer = buffer[index + 4 :]
                parts += 1
                if parts > MAX_PARTS:
                    raise MultipartError("too many parts")
                name, filename = _disposition(headers)
                if filename is not None:
                    in_file = True
                    await on_file_start(name, filename)
                else:
                    in_file = False
                    field_name = name
                    field_value = bytearray()
                state = "body"
            if state == "body":
                index = buffer.find(separator)
                if index < 0:
                    keep = len(separator)
                    if len(buffer) > keep:
                        await emit(buffer[:-keep])
                        buffer = buffer[-keep:]
                    break
                await emit(buffer[:index])
                buffer = buffer[index + len(separator) :]
                if in_file:
                    await on_file_end()
                    in_file = False
                elif field_name is not None:
                    try:
                        await on_field(field_name, field_value.decode("utf-8"))
                    except UnicodeDecodeError:
                        raise MultipartError("a form field is not UTF-8") from None
                    field_name = None
                state = "after_delimiter"
    if not finished:
        raise MultipartError("the multipart body ended before its closing boundary")


async def limited(
    stream: AsyncIterator[bytes], limit: int, on_exceeded: Callable[[], Exception]
) -> AsyncIterator[bytes]:
    """Pass *stream* through, raising ``on_exceeded()`` once it passes *limit* bytes.

    Bounds the whole body -- preamble, every part and the epilogue -- however it
    is sent, ``Content-Length`` or chunked.
    """
    seen = 0
    async for chunk in stream:
        seen += len(chunk)
        if seen > limit:
            raise on_exceeded()
        yield chunk
