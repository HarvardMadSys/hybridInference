"""Token estimation utilities used across serving components.

This module centralizes token counting logic so that adapters, streaming
formatters, and rate limiters share the same estimation behavior.
"""

from __future__ import annotations

import asyncio
import bisect
import concurrent.futures
import contextvars
import functools
import itertools
import math
import random
from typing import TYPE_CHECKING, Any, TypeVar

import tiktoken

if TYPE_CHECKING:
    from collections.abc import Callable

T = TypeVar("T")


def _get_tiktoken_encoding():
    """Get tiktoken encoding, with fallback handling.

    Returns:
        tiktoken.Encoding object or None if tiktoken is not available.
    """
    return tiktoken.get_encoding("cl100k_base")


# Passed to every ``Encoding.encode()`` call in this module.
#
# tiktoken defaults to ``disallowed_special="all"``, which raises ``ValueError``
# on any input containing the *literal text* of a special token for the
# encoding (for cl100k_base: ``<|endoftext|>``, ``<|endofprompt|>`` and the
# ``<|fim_*|>`` trio). That text is entirely user-controlled -- pasting a
# tokenizer doc, a prompt-engineering article, or training-data samples is
# enough to trip it.
#
# An empty tuple means "treat special-token text as ordinary text", which is
# the only correct choice here: this module *estimates* token counts for usage
# accounting, cost, rate limits and routing size hints. It never constructs
# model input, so there is nothing for a real special token to control and
# nothing to defend against -- the literal characters are just characters.
#
# Do NOT "harden" this back to the default. Doing so turns a user's paste into
# a gateway-side crash: the streaming usage-fallback path runs *after* the
# whole answer has been forwarded to the client, so the request is delivered
# and then fails -- an error frame with no ``[DONE]``, no usage, no cost
# recorded, plus an undeserved hit to the endpoint's availability EWMA. On the
# pre-dispatch routing path it is a plain 500 with nothing delivered at all.
_DISALLOWED_SPECIAL: tuple[str, ...] = ()


def _count_with_tiktoken(text: str) -> int:
    """Count tokens for a string using tiktoken when available.

    Falls back to a simple character-based heuristic when tiktoken is not
    installed. The heuristic assumes approximately 4 characters per token.

    Args:
      text: Input string to count.

    Returns:
      Estimated token count (>= 1 for non-empty strings).
    """
    encoding = _get_tiktoken_encoding()
    if encoding:
        return len(encoding.encode(text, disallowed_special=_DISALLOWED_SPECIAL))
    else:
        # 4 characters ≈ 1 token (rough heuristic)
        return max(1, len(text) // 4)


def tokenize_text(text: str) -> list[int]:
    """Tokenize text into token IDs using tiktoken.

    Falls back to character-based chunking if tiktoken is not available.

    Args:
        text: Input string to tokenize.

    Returns:
        List of token IDs. If tiktoken is unavailable, returns character codes
        grouped by 4 (simulating tokens).
    """
    if not text:
        return []

    encoding = _get_tiktoken_encoding()
    if encoding:
        return encoding.encode(text, disallowed_special=_DISALLOWED_SPECIAL)
    else:
        # Fallback: use character codes grouped by 4 as pseudo-tokens
        # This ensures consistent behavior even without tiktoken
        chars = text.encode("utf-8")
        tokens = []
        for i in range(0, len(chars), 4):
            # Combine 4 bytes into a single "token" ID
            chunk = chars[i : i + 4]
            token_id = int.from_bytes(chunk, byteorder="big", signed=False)
            tokens.append(token_id)
        return tokens


def estimate_text_tokens(text: str) -> int:
    """Estimate token count for a plain text string.

    Uses tiktoken when available; otherwise a character-based heuristic.

    Args:
      text: Input string to estimate.

    Returns:
      Estimated token count.
    """
    if not text:
        return 0
    return _count_with_tiktoken(text)


# Coarse per-modality fallback estimates for multimodal content blocks.
#
# Real image/audio token costs are provider-specific and arrive in the upstream
# ``usage`` payload; these constants only matter when we have to estimate
# locally (rate-limit pre-checks, RouteWise size hints, and usage fallbacks
# when a provider omits ``usage``). The critical point is to NEVER feed a
# base64 data URL / audio blob through the text tokenizer: a single inline
# image can be hundreds of kilobytes, which would otherwise be counted as tens
# of thousands of phantom text tokens and corrupt quota/cost accounting.
IMAGE_TOKEN_ESTIMATE = 85  # matches OpenAI's low-detail image base cost
AUDIO_TOKEN_ESTIMATE = 200  # coarse placeholder; precise counts come from upstream
VIDEO_TOKEN_ESTIMATE = 1000  # coarse placeholder; real cost scales with frames/fps upstream


def _collect_block(block: Any, texts: list[str]) -> int:
    """Collect one content block's text into *texts*; return its flat token count.

    Handles OpenAI-style structured blocks (``{"type": "text"|"image_url"|
    "input_audio", ...}``) as well as bare strings. Non-text blocks contribute
    a flat per-modality estimate rather than the size of their (often base64)
    payload.
    """
    if isinstance(block, str):
        texts.append(block)
        return 0
    if not isinstance(block, dict):
        return 0

    block_type = block.get("type")
    # Text blocks: {"type": "text", "text": ...}; some clients omit the type.
    if block_type == "text" or (block_type is None and "text" in block):
        text = block.get("text")
        if isinstance(text, str):
            texts.append(text)
        return 0
    if block_type in ("image_url", "image", "input_image"):
        return IMAGE_TOKEN_ESTIMATE
    if block_type in ("input_audio", "audio"):
        return AUDIO_TOKEN_ESTIMATE
    if block_type in ("video_url", "video", "input_video"):
        return VIDEO_TOKEN_ESTIMATE

    # Unknown block: count any embedded text, but never the raw payload/blob.
    text = block.get("text")
    if isinstance(text, str):
        texts.append(text)
    return 0


def _collect_content(content: Any, texts: list[str]) -> int:
    """Collect a message ``content`` field of any shape; return its flat token count.

    ``content`` may be a plain string (OpenAI text style), a list of structured
    blocks (multimodal style), a single block mapping, or ``None``.
    """
    if content is None:
        return 0
    if isinstance(content, str):
        texts.append(content)
        return 0
    if isinstance(content, list):
        return sum(_collect_block(block, texts) for block in content)
    if isinstance(content, dict):
        return _collect_block(content, texts)
    texts.append(str(content))
    return 0


def _prompt_token_parts(messages: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """Split a messages list into its flat token count and the texts to tokenize.

    The one walk behind :func:`estimate_prompt_tokens` and
    :func:`estimate_prompt_tokens_bounded`, so the two can never count a
    prompt differently: each message contributes its role and content texts
    plus a small overhead approximating protocol framing, and media blocks a
    flat per-modality estimate.
    """
    if not messages:
        return 0, []

    overhead_per_message = 4
    overhead_end = 3

    texts: list[str] = []
    fixed = 0
    for m in messages:
        texts.append(str(m.get("role", "")))
        fixed += _collect_content(m.get("content"), texts)
        fixed += overhead_per_message

    fixed += overhead_end
    return fixed, texts


def estimate_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate token count for an OpenAI-style messages list.

    Each message contributes role + content tokens plus a small overhead to
    approximate protocol framing costs. This mirrors common counting approaches.

    Multimodal content (a list of text/image/audio blocks) is handled
    structurally: text blocks are tokenized while image/audio blocks add a flat
    per-modality estimate, so inline base64 payloads never inflate the count.

    Tokenizes the whole prompt, which takes tens of milliseconds for a long one;
    on the event loop prefer :func:`estimate_prompt_tokens_bounded`.

    Args:
      messages: Chat messages with ``role`` and ``content`` fields.

    Returns:
      Estimated prompt token count.
    """
    fixed, texts = _prompt_token_parts(messages)
    return fixed + sum(estimate_text_tokens(text) for text in texts)


#: How much text an estimate made on the event loop tokenizes, at most:
#: about 3 ms of tiktoken. A prompt with less text is counted exactly.
INLINE_ESTIMATE_MAX_CHARS = 32_768

#: Bytes in each window a bounded estimate tokenizes from a longer prompt.
_SAMPLE_WINDOW_BYTES = 512

#: Where a sampled estimate's evenly spaced windows begin, drawn afresh for each
#: estimate (seeded from the OS). At fixed offsets a caller could put
#: whitespace exactly where the windows land and be billed for a sliver of a
#: long prompt cut off mid-stream; at a random phase every byte is as likely to
#: be read as any other, so the sample is right on average whatever the text.
_SAMPLE_PHASE = random.Random()

#: More bytes, other than spaces and newlines, than one token covers in real
#: text. Measured with cl100k_base per 40 KB: at most 3.9 in this repository's
#: English docs, Python and TypeScript (3.2-3.4 on average), at most 3.5 in Go,
#: C, C++, Ruby, Java, C# and PHP; Chinese 2.8, JSON 2.0, base64 1.4. Unusual
#: text runs higher and still stays below: a stream of one-token words 4.35,
#: Markdown thick with rules 4.4, URL lists 4.1. Spaces and newlines are left
#: out because their runs (indentation, padded columns) cost next to nothing:
#: counted in, indented code reaches 6 bytes a token.
_FLOOR_BYTES_PER_TOKEN = 4.5


def _utf8_size(text: str) -> int:
    """The UTF-8 length of *text*, without encoding it when it is ASCII."""
    return len(text) if text.isascii() else len(text.encode("utf-8", "surrogatepass"))


def _dense_floor(texts: list[str], sizes: list[int]) -> int:
    """The fewest tokens the texts a sampled window is cut from can hold.

    Their UTF-8 bytes other than spaces and newlines, over
    :data:`_FLOOR_BYTES_PER_TOKEN`, rounded up: counted over every byte, so no
    arrangement of the text lowers it, and below the exact count of any real
    text. A text no longer than a window is left out, because a window reads it
    whole; it is often a role name such as ``assistant``, one token of nine
    bytes.
    """
    dense = sum(
        size - text.count(" ") - text.count("\n")
        for text, size in zip(texts, sizes, strict=True)
        if size > _SAMPLE_WINDOW_BYTES
    )
    return math.ceil(dense / _FLOOR_BYTES_PER_TOKEN)


def estimate_texts_tokens_bounded(texts: list[str], *, max_chars: int) -> int:
    """Estimate the summed token counts of *texts*, tokenizing about *max_chars* at most.

    Exactly ``sum(estimate_text_tokens(t) for t in texts)`` while the texts hold
    no more than *max_chars* characters. Past that, a sample: windows of
    :data:`_SAMPLE_WINDOW_BYTES` spread evenly over the texts' UTF-8 bytes, from
    a phase drawn at random for each estimate, are tokenized, each weighted by
    the share of the prompt it stands for. Bytes rather than characters, because
    tokens per byte vary far less between scripts than tokens per character (a
    CJK character is about one token, an English one about a quarter). A text no
    longer than a window that a window lands in is tokenized whole and weighted
    by its own size, so a prompt of many short messages is counted without bias;
    a window cut out of a longer text gives up the half token each cut edge
    adds. On real prompts the sample comes within a few percent of the exact
    count, and equals it on average, at a cost set by *max_chars* instead of by
    the prompt.

    The caller writes the text and is billed by this number, so the sample is
    never trusted below :func:`_dense_floor`, which no arrangement of the text
    can lower: a prompt that hid from the windows is still billed a token for
    every :data:`_FLOOR_BYTES_PER_TOKEN` bytes of it that are not spaces or
    newlines.
    """
    texts = [text for text in texts if text]
    if sum(len(text) for text in texts) <= max_chars:
        return sum(_count_with_tiktoken(text) for text in texts)

    sizes = [_utf8_size(text) for text in texts]
    total = sum(sizes)
    width = _SAMPLE_WINDOW_BYTES
    windows = max(1, max_chars // width)
    stride = total / windows
    phase = _SAMPLE_PHASE.random()
    starts = list(itertools.accumulate(sizes, initial=0))
    encoded: dict[int, bytes] = {}
    estimate = 0.0
    for window_index in range(windows):
        offset = min(int((window_index + phase) * stride), total - 1)
        index = bisect.bisect_right(starts, offset) - 1
        text, size = texts[index], sizes[index]
        if size <= width:
            estimate += _count_with_tiktoken(text) * stride / size
            continue
        begin = max(0, min(offset - starts[index] - width // 2, size - width))
        cut_edges = (begin > 0) + (begin + width < size)
        if size == len(text):  # ASCII: a byte offset is a character offset
            window = text[begin : begin + width]
        else:
            data = encoded.get(index)
            if data is None:
                data = encoded[index] = text.encode("utf-8", "surrogatepass")
            # A character split by the window's edges is dropped, not guessed.
            window = data[begin : begin + width].decode("utf-8", "ignore")
        window_size = _utf8_size(window)
        if not window_size:
            continue
        tokens = max(_count_with_tiktoken(window) - cut_edges / 2, 0.0)
        estimate += tokens * stride / window_size
    return max(round(estimate), _dense_floor(texts, sizes))


def estimate_prompt_tokens_bounded(
    messages: list[dict[str, Any]],
    *,
    max_chars: int = INLINE_ESTIMATE_MAX_CHARS,
    extra_texts: tuple[str, ...] = (),
) -> int:
    """:func:`estimate_prompt_tokens`, tokenizing at most about *max_chars* characters.

    Equal to ``estimate_prompt_tokens(messages)`` plus the estimate of each of
    *extra_texts* (serialized tool definitions, say) whenever all of that text
    fits in *max_chars*; a sampled estimate beyond it (see
    :func:`estimate_texts_tokens_bounded`). Use it wherever the prompt is
    caller-sized and the work must stay bounded: on the event loop above all.
    """
    fixed, texts = _prompt_token_parts(messages)
    texts.extend(extra_texts)
    return fixed + estimate_texts_tokens_bounded(texts, max_chars=max_chars)


#: One small pool for tokenizing off the event loop. tiktoken releases the GIL
#: while it encodes, so the loop keeps serving meanwhile; a pool of its own
#: keeps a burst of token counting from queueing ahead of other work handed to
#: the default executor, and bounds how much of it runs at once.
_TOKENIZER_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="tokenizer"
)


async def run_tokenization(func: Callable[..., T], *args: Any) -> T:
    """Run ``func(*args)`` in the tokenizer pool and return its result.

    *args* must not be mutated by anyone else while it runs. A cancelled call
    that has not started is dropped from the pool; one that has started runs to
    completion, which the callers bound by bounding the text.
    """
    loop = asyncio.get_running_loop()
    # The caller's context goes along, as with ``asyncio.to_thread``, so log
    # lines from the worker still carry the request they belong to.
    context = contextvars.copy_context()
    return await loop.run_in_executor(_TOKENIZER_POOL, functools.partial(context.run, func, *args))


__all__ = [
    "INLINE_ESTIMATE_MAX_CHARS",
    "estimate_prompt_tokens",
    "estimate_prompt_tokens_bounded",
    "estimate_text_tokens",
    "estimate_texts_tokens_bounded",
    "run_tokenization",
    "tokenize_text",
]
