"""Bounded token estimates: exact for short prompts, sampled and cheap for long ones."""

from __future__ import annotations

import asyncio
import random
import threading
import time
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


def test_the_estimate_is_deterministic_and_cheap() -> None:
    messages = _conversation(random.Random(3), 2_000_000)
    started = time.perf_counter()
    first = estimate_prompt_tokens_bounded(messages)
    elapsed = time.perf_counter() - started
    assert estimate_prompt_tokens_bounded(messages) == first
    # A full encode of these two million characters takes well over 100 ms.
    assert elapsed < 0.1


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
