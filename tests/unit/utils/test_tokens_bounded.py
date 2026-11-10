"""Bounded token estimates: exact for short prompts, sampled and cheap for long ones."""

from __future__ import annotations

import argparse
import asyncio
import collections
import inspect
import json
import random
import re
import statistics
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from serving.utils import tokens
from serving.utils.tokens import (
    INLINE_ESTIMATE_MAX_CHARS,
    estimate_prompt_tokens,
    estimate_prompt_tokens_bounded,
    estimate_text_tokens,
    run_tokenization,
)


@pytest.fixture(autouse=True)
def _repeatable_phases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Draw each estimate's sample phase from a fixed seed, so a test repeats."""
    monkeypatch.setattr(tokens, "_SAMPLE_PHASE", random.Random(20261107))


_WORDS = [
    "def",
    "return",
    "import",
    "class",
    "self",
    "value",
    "config",
    "request",
    "response",
    "the",
    "of",
    "and",
    "to",
    "in",
    "is",
    "for",
    "with",
    "that",
    "this",
    "await",
    "async",
    "yield",
    "lambda",
    "None",
    "True",
    "False",
    "print",
    "data",
]


def _prose(rng: random.Random, chars: int) -> str:
    words: list[str] = []
    size = 0
    while size < chars:
        word = rng.choice(_WORDS)
        words.append(word)
        size += len(word) + 1
    return " ".join(words)


def _conversation(rng: random.Random, chars: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": _prose(rng, 20_000)}]
    size = 20_000
    turn = 0
    while size < chars:
        text = _prose(rng, rng.randint(200, 15_000))
        if turn % 3 == 0:
            messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
        elif turn % 3 == 1:
            messages.append({"role": "assistant", "content": text})
        else:
            # CJK tool output, which tokenizes far denser per character.
            cjk = "".join(
                rng.choice("的一是不了人我在有他这为之大来以个中上们") for _ in range(2000)
            )
            messages.append({"role": "tool", "content": text + cjk})
        size += len(text)
        turn += 1
    return messages


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "user", "content": "Hello there"}],
        [
            {"role": "system", "content": "Be brief."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this image?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + "A" * 10},
                    },
                ],
            },
            {"role": "assistant", "content": None},
            {"role": "tool", "content": {"type": "text", "text": "result"}},
        ],
    ],
    ids=["empty", "one", "multimodal"],
)
def test_a_prompt_within_the_budget_is_counted_exactly(messages: list[dict[str, Any]]) -> None:
    assert estimate_prompt_tokens_bounded(messages) == estimate_prompt_tokens(messages)


def test_extra_texts_count_like_separate_estimates() -> None:
    messages = [{"role": "user", "content": "Use the tool."}]
    tools_json = '[{"name": "Read", "description": "Read a file from disk."}]'
    assert estimate_prompt_tokens_bounded(messages, extra_texts=(tools_json,)) == (
        estimate_prompt_tokens(messages) + estimate_text_tokens(tools_json)
    )


@pytest.mark.parametrize("seed", range(4))
def test_a_long_prompt_is_estimated_closely_from_a_bounded_sample(monkeypatch, seed: int) -> None:
    messages = _conversation(random.Random(seed), 600_000)
    exact = estimate_prompt_tokens(messages)

    tokenized: list[int] = []
    count = tokens._count_with_tiktoken

    def spy(text: str) -> int:
        tokenized.append(len(text))
        return count(text)

    monkeypatch.setattr(tokens, "_count_with_tiktoken", spy)
    bounded = estimate_prompt_tokens_bounded(messages)

    assert abs(bounded - exact) <= 0.05 * exact
    # The whole prompt is ~600k characters; the estimate read about the budget.
    assert sum(tokenized) <= INLINE_ESTIMATE_MAX_CHARS + 2 * 512


def test_many_short_messages_are_estimated_without_bias() -> None:
    rng = random.Random(7)
    replies = ["ok", "yes", "go on", "thanks!", "continue please", "no", "sure thing"]
    messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": rng.choice(replies)}
        for i in range(20_000)
    ]
    exact = estimate_prompt_tokens(messages)
    assert abs(estimate_prompt_tokens_bounded(messages) - exact) <= 0.03 * exact


def test_the_estimate_is_cheap() -> None:
    messages = _conversation(random.Random(3), 2_000_000)
    started = time.perf_counter()
    estimate_prompt_tokens_bounded(messages)
    elapsed = time.perf_counter() - started
    # A full encode of these two million characters takes well over 100 ms.
    assert elapsed < 0.1


def test_the_sample_is_right_on_average() -> None:
    messages = _conversation(random.Random(5), 600_000)
    exact = estimate_prompt_tokens(messages)
    estimates = [estimate_prompt_tokens_bounded(messages) for _ in range(40)]
    assert abs(statistics.mean(estimates) - exact) <= 0.015 * exact


def test_each_estimate_reads_windows_of_its_own(monkeypatch) -> None:
    messages = _conversation(random.Random(6), 300_000)
    windows: list[list[str]] = []
    count = tokens._count_with_tiktoken

    def spy(text: str) -> int:
        windows[-1].append(text)
        return count(text)

    monkeypatch.setattr(tokens, "_count_with_tiktoken", spy)
    for _ in range(2):
        windows.append([])
        estimate_prompt_tokens_bounded(messages)
    assert windows[0] != windows[1]


def _hidden_from_fixed_windows(payload: str, size: int) -> list[dict[str, Any]]:
    """*size* bytes of the ASCII *payload*, with spaces where fixed windows read.

    The windows used to be centred at fixed offsets, the middle of each
    stride, so a caller who put spaces there had the sample read little else
    and billed a long prompt as a few thousand tokens.
    """
    width = tokens._SAMPLE_WINDOW_BYTES
    windows = INLINE_ESTIMATE_MAX_CHARS // width
    stride = size / windows
    # The sample's offsets run over the role text, then the content.
    cursor = len("user")
    used = 0
    pieces: list[str] = []
    for index in range(windows):
        centre = int((index + 0.5) * stride)
        dense = centre - width - cursor
        pieces.append(payload[used : used + dense])
        used += dense
        pieces.append(" " * (2 * width))
        cursor = centre + width
    pieces.append(payload[used : used + size - cursor])
    content = "".join(pieces)
    assert len(content) == size - len("user")
    return [{"role": "user", "content": content}]


def _numbers(rng: random.Random, chars: int) -> str:
    """Dense text: a JSON list of floats, about two bytes a token."""
    return json.dumps([round(rng.random(), 6) for _ in range(chars // 9)])


def test_a_prompt_hidden_from_fixed_windows_is_billed_in_full_on_average() -> None:
    messages = _hidden_from_fixed_windows(_numbers(random.Random(8), 450_000), 400_000)
    exact = estimate_prompt_tokens(messages)
    estimates = [estimate_prompt_tokens_bounded(messages) for _ in range(32)]
    assert statistics.mean(estimates) >= 0.95 * exact


def test_a_sample_that_reads_only_whitespace_is_raised_to_the_dense_floor(monkeypatch) -> None:
    english = _english()
    messages = _hidden_from_fixed_windows(english * (450_000 // len(english) + 1), 400_000)
    exact = estimate_prompt_tokens(messages)
    # Every window lands in the spaces: the phase the windows used to have.
    monkeypatch.setattr(tokens, "_SAMPLE_PHASE", SimpleNamespace(random=lambda: 0.5))

    estimate = estimate_prompt_tokens_bounded(messages)

    content = messages[0]["content"]
    dense = len(content) - content.count(" ") - content.count("\n")
    assert estimate >= dense / tokens._FLOOR_BYTES_PER_TOKEN
    # Most of the prompt is still billed, where the sample alone read next to
    # nothing of it.
    assert estimate >= 0.75 * exact
    monkeypatch.setattr(tokens, "_dense_floor", lambda texts, sizes: 0)
    assert estimate_prompt_tokens_bounded(messages) < 0.05 * exact


def _python_source() -> str:
    return inspect.getsource(argparse)


def _english() -> str:
    docs = [inspect.getdoc(value) for value in vars(collections).values()]
    docs += [inspect.getdoc(value) for value in vars(argparse).values()]
    return "\n\n".join(doc for doc in docs if doc)


def _cjk(rng: random.Random) -> str:
    sentences = []
    for _ in range(4000):
        words = "".join(
            rng.choice("的一是不了人我在有他这为之大来以个中上们说时年") for _ in range(18)
        )
        # Full-width full stop, comma, question and exclamation marks.
        sentences.append(words + rng.choice("\u3002\uff0c\uff1f\uff01"))
    return "".join(sentences)


def _padded_table(rng: random.Random) -> str:
    names = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "theta", "kappa"]
    rows = (" | ".join(f"{rng.choice(names):<18}" for _ in range(5)) for _ in range(4000))
    return "\n".join(f"| {row} |" for row in rows)


_HONEST_TEXTS = {
    "prose of code words": lambda: _prose(random.Random(10), 200_000),
    "english docstrings": _english,
    "python source": _python_source,
    "python indented by 8": lambda: re.sub(
        r"^( +)", lambda m: m.group(1) * 2, _python_source(), flags=re.M
    ),
    "python indented by tabs": lambda: re.sub(
        r"^((?:    )+)", lambda m: "\t" * (len(m.group(1)) // 4), _python_source(), flags=re.M
    ),
    "chinese": lambda: _cjk(random.Random(11)),
    "padded table": lambda: _padded_table(random.Random(12)),
    "indented json": lambda: json.dumps(
        {"items": [{"id": i, "tags": ["a", "b"], "meta": {"ok": True}} for i in range(3000)]},
        indent=4,
    ),
}


@pytest.mark.parametrize("kind", list(_HONEST_TEXTS))
def test_the_floor_never_raises_an_honest_estimate(kind: str) -> None:
    text = _HONEST_TEXTS[kind]()
    while len(text) < 4 * INLINE_ESTIMATE_MAX_CHARS:
        text += "\n" + text
    exact = estimate_text_tokens(text)
    # Below the exact count, so an honest prompt is billed by its sample alone.
    assert tokens._dense_floor([text], [tokens._utf8_size(text)]) < exact
    messages = [{"role": "user", "content": text}]
    assert abs(estimate_prompt_tokens_bounded(messages) - estimate_prompt_tokens(messages)) <= (
        0.05 * exact
    )


def test_texts_a_window_reads_whole_are_left_out_of_the_floor() -> None:
    # "assistant" is one token of nine bytes: floored, it would count as two.
    texts = ["assistant", "ok", "user", "go on"] * 10_000
    assert tokens._dense_floor(texts, [tokens._utf8_size(text) for text in texts]) == 0
    long_text = "x" * (tokens._SAMPLE_WINDOW_BYTES + 1)
    assert tokens._dense_floor([*texts, long_text], [*map(tokens._utf8_size, texts), 513]) > 0


@pytest.mark.asyncio
async def test_tokenization_runs_off_the_event_loop() -> None:
    loop_thread = threading.get_ident()
    seen: list[int] = []
    text = "word " * 400_000

    def count(value: str) -> int:
        seen.append(threading.get_ident())
        return estimate_text_tokens(value)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.001)
            ticks += 1

    ticking = asyncio.create_task(ticker())
    try:
        result = await run_tokenization(count, text)
    finally:
        ticking.cancel()
    assert result == estimate_text_tokens(text)
    assert seen and seen[0] != loop_thread
    # The loop kept running while the text was encoded.
    assert ticks > 0
