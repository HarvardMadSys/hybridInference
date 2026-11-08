"""The response processors read model output in time linear in its length.

They used regular expressions with a lazy ``.*?`` between an opening and a
closing tag, which scan the rest of the text from every opening tag that never
closes: a non-streaming reply of 136 KiB of unclosed ``<think>`` took about
15 s of event loop, and the GLM argument pattern was cubic (13 KB, 3.5 s). The
GLM and Qwen Coder processors also appended each streamed piece of a tool call
to one string, copying all of it per piece.

The scans that replace them must find exactly what the regexes found, so the
first tests compare the two on many generated texts. The time bounds after them
are generous -- the scans need milliseconds -- and the old code overshoots each
of them several times over.
"""

from __future__ import annotations

import random
import re
import time

import pytest

from serving.adapters import processors
from serving.adapters.processors import (
    GLMProcessor,
    QwenCoderProcessor,
    ReasoningExtractProcessor,
    ThinkBlockProcessor,
)

pytestmark = pytest.mark.unit

BOUND_S = 1.5

# The expressions the processors used, as the reference for what they match.
THINK_RE = re.compile(r"<think>[\s\S]*?</think>")
TOOL_CALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)
FUNCTION_RE = re.compile(r"<function=([^>]+)>(.*?)(?:</function>|$)", re.DOTALL)
PARAMETER_RE = re.compile(
    r"<parameter=([^>]+)>(.*?)(?:</parameter>|(?=<parameter=)|(?=</function>)|$)",
    re.DOTALL,
)
GLM_ARGS_RE = re.compile(r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>", re.DOTALL)

# Tags, pieces of tags and the characters the patterns treat specially.
FRAGMENTS = [
    *("<think>", "</think>", "<thi", "</th", "<mm:think>", "</mm:think>", "<mm:"),
    *("<tool_call>", "</tool_call>", "<tool_", "<function=", "<function=f>", "</function>"),
    *("<parameter=", "<parameter=p>", "</parameter>"),
    *("<arg_key>", "</arg_key>", "<arg_value>", "</arg_value>"),
    *("<", ">", "</", "=", "a", "b", " ", "\n", "\t", "\u00a0", "[1]", '{"x":1}'),
]


def _texts(seed: int, count: int = 3000) -> list[str]:
    rng = random.Random(seed)
    return ["".join(rng.choice(FRAGMENTS) for _ in range(rng.randint(0, 40))) for _ in range(count)]


def _lift_with_regex(text: str, names: tuple[str, ...]) -> tuple[str, list[str]]:
    pattern = re.compile(r"<(" + "|".join(map(re.escape, names)) + r")>([\s\S]*?)</\1>")
    insides: list[str] = []

    def grab(match: re.Match[str]) -> str:
        insides.append(match.group(2))
        return ""

    return pattern.sub(grab, text), insides


def test_the_scans_match_what_the_regexes_matched():
    for text in _texts(seed=7):
        assert processors._strip_closed_blocks(text, "<think>", "</think>") == THINK_RE.sub(
            "", text
        )
        assert processors._glm_arg_pairs(text) == GLM_ARGS_RE.findall(text)
        assert processors._qwen_tool_blocks(text) == TOOL_CALL_RE.findall(text)
        match = FUNCTION_RE.search(text)
        assert processors._qwen_function(text) == (match.groups() if match else None)
        assert processors._qwen_parameters(text) == [
            m.groups() for m in PARAMETER_RE.finditer(text)
        ]
        for names in (("mm:think",), ("think", "mm:think"), ("mm:think", "think")):
            assert processors._lift_closed_blocks(text, names) == _lift_with_regex(text, names)


@pytest.mark.parametrize(
    "text",
    [
        "a<think>x</think>b<think>y</think>c",  # repeated
        "<think><think>x</think>y</think>",  # nested: the first close ends the block
        "keep<think>unclosed",  # unclosed: kept as it is
        "<think>x</think><think>",  # closed, then unclosed
    ],
)
def test_think_blocks_strip_as_before(text):
    response = {"choices": [{"message": {"content": text}}]}

    ThinkBlockProcessor().process_response(response)

    assert response["choices"][0]["message"]["content"] == THINK_RE.sub("", text).strip()


def test_a_value_ending_in_newlines_ends_where_the_regex_ended_it():
    """``$`` also matches before a final newline, so an unclosed call loses one.

    The function's body and then its last value each stop before the text's
    final newline, and the value sheds one more of its own: ``v`` here, where a
    scan to the very end would have kept ``v\\n\\n``.
    """
    text = "<tool_call>\n<function=f>\n<parameter=a>\nv\n\n\n"
    response = {"choices": [{"message": {"content": text}}]}

    QwenCoderProcessor().process_response(response)

    (call,) = response["choices"][0]["message"]["tool_calls"]
    assert call["function"] == {"name": "f", "arguments": '{"a": "v"}'}


def _elapsed(fn) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


@pytest.mark.parametrize(
    ("processor", "content"),
    [
        # 136 KiB: about 15 s with the regex.
        (ThinkBlockProcessor, "<think>real reasoning</think>Answer: " + "<think>" * 20_000),
        # 214 KiB and 195 KiB: 18-20 s and 16 s.
        (QwenCoderProcessor, "<tool_call>" * 20_000),
        (QwenCoderProcessor, "x<tool_call>" + "<function=" * 20_000),
        (QwenCoderProcessor, "<tool_call><function=f>" + "<parameter=" * 20_000),
        (ReasoningExtractProcessor, "<mm:think>" * 20_000),
    ],
)
def test_a_reply_of_unclosed_tags_is_read_in_linear_time(processor, content):
    response = {"choices": [{"message": {"content": content}}]}

    elapsed = _elapsed(lambda: processor().process_response(response))

    assert elapsed < BOUND_S, f"{len(content)} characters took {elapsed:.2f}s"


def test_glm_arguments_whose_values_never_close_are_read_in_linear_time():
    """The argument regex was cubic: 400 such pairs (13 KB) took 3.5 s."""
    processor = GLMProcessor()
    processor.process_stream_chunk(
        {"model": "m", "choices": [{"delta": {"content": "<tool_call>run\n"}}]}
    )
    processor.process_stream_chunk(
        {
            "model": "m",
            "choices": [{"delta": {"content": "<arg_key>a</arg_key><arg_value>b" * 20_000}}],
        }
    )

    elapsed = _elapsed(processor.flush)

    assert elapsed < BOUND_S, f"20,000 unclosed argument values took {elapsed:.2f}s"


@pytest.mark.parametrize("processor", [GLMProcessor, QwenCoderProcessor])
def test_a_streamed_tool_call_is_held_in_linear_time(processor):
    """781 KiB in 4-character pieces: about 8 s when each piece was appended to a string."""
    proc = processor()
    head, tail = "<tool_call>write\n<arg_key>content</arg_key><arg_value>", "</arg_value>"
    if processor is QwenCoderProcessor:
        head, tail = "<tool_call>\n<function=write>\n<parameter=content>\n", "</parameter>"
    proc.process_stream_chunk({"model": "m", "choices": [{"delta": {"content": head}}]})
    piece = {"model": "m", "choices": [{"delta": {"content": "abcd"}, "finish_reason": None}]}

    def stream():
        for _ in range(200_000):
            proc.process_stream_chunk(piece)

    elapsed = _elapsed(stream)

    assert elapsed < 2.5, f"800 KB of tool call took {elapsed:.2f}s"
    proc.process_stream_chunk({"model": "m", "choices": [{"delta": {"content": tail}}]})
    (final,) = proc.flush()
    (call,) = final["choices"][0]["delta"]["tool_calls"]
    assert len(call["function"]["arguments"]) > 800_000


@pytest.mark.parametrize("processor", [ThinkBlockProcessor, ReasoningExtractProcessor])
def test_one_large_chunk_of_many_blocks_is_read_in_linear_time(processor):
    """The stream loops moved a cursor on, instead of re-slicing the rest per tag."""
    proc = processor()
    tag = "think" if processor is ThinkBlockProcessor else "mm:think"
    content = f"<{tag}></{tag}>" * 100_000
    chunk = {"model": "m", "choices": [{"delta": {"content": content + "answer"}}]}

    emitted: list[dict] = []

    elapsed = _elapsed(lambda: emitted.extend(proc.process_stream_chunk(chunk)))

    assert elapsed < BOUND_S, f"one chunk of 100,000 blocks took {elapsed:.2f}s"
    assert _contents(emitted) == ["answer"]


@pytest.mark.parametrize("processor", [GLMProcessor, QwenCoderProcessor])
def test_a_tool_call_past_the_cap_is_sent_on_as_content(monkeypatch, processor):
    """No real tool call runs this long; the text is not held to the end of the stream."""
    monkeypatch.setattr(processors, "MAX_TOOL_CALL_CHARS", 100)
    proc = processor()

    def send(text: str) -> list[dict]:
        return proc.process_stream_chunk({"model": "m", "choices": [{"delta": {"content": text}}]})

    # Qwen Coder strips the text before the call; GLM keeps it as it is.
    before = "Hi" if processor is QwenCoderProcessor else "Hi "
    assert _contents(send("Hi <tool_call>" + "x" * 50)) == [before]
    assert send("y" * 30) == []
    overflowed = send("z" * 40)
    assert _contents(overflowed) == ["<tool_call>" + "x" * 50 + "y" * 30 + "z" * 40]
    # Text again, as it arrives.
    assert _contents(send("after")) == ["after"]
    assert proc.flush() == []


def _contents(chunks: list[dict]) -> list[str]:
    return [c["choices"][0]["delta"]["content"] for c in chunks]
