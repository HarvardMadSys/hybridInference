"""RouteWise builds a request's prefix-cache blocks once, cheaply, and the same way.

Prefix-cache costing (``prefix_cache_cost_adjustment_enabled``) cuts the
canonical request into token blocks. That used to happen on every solve -- the
first decision, every re-solve after a failure, every hedge checkpoint -- on the
event loop and under the router's commit lock, each time serializing and
tokenizing the whole request, base64 media included, and then writing every
token out in decimal to hash it.

Now: the blocks are built once per request, in the tokenizer pool, and reused by
every solve; a media payload is represented by its identity rather than its
base64; each block hashes its tokens' bytes; and only the first
``MAX_CANONICAL_CHARS`` characters are tokenized. For a text-only prompt every
match is exactly what it was.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import threading
from typing import Any
from unittest.mock import AsyncMock

import pytest

from routing.routewise import prefix_cache as prefix_cache_module
from routing.routewise.config import RouteWiseConfig
from routing.routewise.prefix_cache import (
    Block,
    PrefixCacheCoordinator,
    SessionProviderPrefixMemory,
    build_blocks,
    longest_common_prefix_tokens,
)
from serving.utils import context as req_ctx
from serving.utils.tokens import tokenize_text
from tests.unit.routing.test_routewise_router import _make_router_with_two_api

SECRET = b"unit-test-secret"


def _reference_build_blocks(
    messages: list[dict[str, Any]],
    *,
    tools: Any = None,
    response_format: Any = None,
    block_size: int,
    tokenize: Any,
) -> tuple[Block, ...]:
    """``build_blocks`` as it was: whole canonical text, ids hashed in decimal."""
    payload = {"messages": list(messages), "tools": tools, "response_format": response_format}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    token_ids = list(tokenize(text))
    blocks = []
    for start in range(0, len(token_ids), block_size):
        chunk = token_ids[start : start + block_size]
        data = b",".join(str(int(tok)).encode("ascii") for tok in chunk)
        blocks.append(
            Block(
                digest=hmac.new(SECRET, data, hashlib.sha256).hexdigest(),
                token_count=len(chunk),
            )
        )
    return tuple(blocks)


def _conversation(rng: random.Random, turns: int) -> list[dict[str, Any]]:
    words = ["alpha", "beta", "gamma", "delta", "def f(x):", "\n", "日本語", "émoji 🙂", '"q"']
    messages: list[dict[str, Any]] = [{"role": "system", "content": "You are a coding agent."}]
    for turn in range(turns):
        text = " ".join(rng.choice(words) for _ in range(rng.randint(5, 400)))
        messages.append({"role": "user" if turn % 2 == 0 else "assistant", "content": text})
    return messages


def _corpus() -> list[tuple[list[dict[str, Any]], Any]]:
    rng = random.Random(1234)
    tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]
    corpus = []
    for _ in range(6):
        base = _conversation(rng, rng.randint(1, 6))
        corpus.append((base, None))
        # An append-only continuation, a fork, and the same prompt with tools.
        corpus.append(([*base, {"role": "user", "content": "next " * rng.randint(1, 300)}], None))
        corpus.append(([*base[:-1], {"role": "user", "content": "forked"}], None))
        corpus.append((base, tools))
    return corpus


def _chars(text: str) -> list[int]:
    return [ord(c) for c in text]


@pytest.mark.unit
@pytest.mark.parametrize("tokenize", [tokenize_text, _chars], ids=["tiktoken", "chars"])
@pytest.mark.parametrize("block_size", [8, 128])
def test_text_only_matches_are_exactly_what_they_were(tokenize, block_size):
    corpus = _corpus()
    new = [
        build_blocks(m, tools=t, block_size=block_size, secret=SECRET, tokenize=tokenize)
        for m, t in corpus
    ]
    old = [
        _reference_build_blocks(m, tools=t, block_size=block_size, tokenize=tokenize)
        for m, t in corpus
    ]

    for new_blocks, old_blocks in zip(new, old, strict=True):
        assert [b.token_count for b in new_blocks] == [b.token_count for b in old_blocks]
    for i in range(len(corpus)):
        for j in range(len(corpus)):
            assert longest_common_prefix_tokens(new[i], new[j]) == longest_common_prefix_tokens(
                old[i], old[j]
            )


@pytest.mark.unit
def test_ids_beyond_64_bits_still_hash_and_match():
    def huge(text: str) -> list[int]:
        return [2**70 + ord(c) for c in text]

    a = build_blocks(
        [{"role": "user", "content": "abc"}], block_size=2, secret=SECRET, tokenize=huge
    )
    b = build_blocks(
        [{"role": "user", "content": "abd"}], block_size=2, secret=SECRET, tokenize=huge
    )
    again = build_blocks(
        [{"role": "user", "content": "abc"}], block_size=2, secret=SECRET, tokenize=huge
    )

    assert a == again
    assert 0 < longest_common_prefix_tokens(a, b) < sum(block.token_count for block in a)


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------


def _image_turn(payload: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is in this picture?"},
            {"type": "image_url", "image_url": {"url": payload, "detail": "high"}},
        ],
    }


_PNG = "data:image/png;base64," + "iVBORw0KGgo" * 100_000
_OTHER_PNG = "data:image/png;base64," + "R0lGODlhAQ" * 110_000


@pytest.mark.unit
def test_a_media_payload_is_never_tokenized():
    seen: list[str] = []

    def spy(text: str) -> list[int]:
        seen.append(text)
        return _chars(text)

    blocks = build_blocks([_image_turn(_PNG)], block_size=8, secret=SECRET, tokenize=spy)

    (text,) = seen
    assert "iVBORw0KGgo" not in text
    assert len(text) < 1_000
    # The rest of the block is still there: the detail level changes the prompt.
    assert '"detail":"high"' in text
    assert blocks == build_blocks([_image_turn(_PNG)], block_size=8, secret=SECRET, tokenize=spy)


@pytest.mark.unit
def test_a_different_attachment_still_breaks_the_match_where_it_sits():
    text_turn = {"role": "user", "content": "describe it " * 50}
    same = build_blocks(
        [text_turn, _image_turn(_PNG)], block_size=8, secret=SECRET, tokenize=_chars
    )
    again = build_blocks(
        [text_turn, _image_turn(_PNG)], block_size=8, secret=SECRET, tokenize=_chars
    )
    other = build_blocks(
        [text_turn, _image_turn(_OTHER_PNG)], block_size=8, secret=SECRET, tokenize=_chars
    )

    full = sum(block.token_count for block in same)
    assert longest_common_prefix_tokens(same, again) == full
    assert 0 < longest_common_prefix_tokens(same, other) < full


@pytest.mark.unit
def test_a_short_media_reference_is_kept_as_it_is():
    url = "https://example.com/cat.png"
    seen: list[str] = []

    def spy(text: str) -> list[int]:
        seen.append(text)
        return _chars(text)

    build_blocks([_image_turn(url)], block_size=8, secret=SECRET, tokenize=spy)

    assert url in seen[0]


@pytest.mark.unit
def test_the_callers_messages_are_not_modified():
    messages = [_image_turn(_PNG)]
    snapshot = json.dumps(messages)

    build_blocks(messages, block_size=8, secret=SECRET, tokenize=_chars)

    assert json.dumps(messages) == snapshot


# ---------------------------------------------------------------------------
# The bound on what is tokenized
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_only_the_bounded_prefix_is_tokenized(monkeypatch):
    monkeypatch.setattr(prefix_cache_module, "MAX_CANONICAL_CHARS", 1_000)
    lengths: list[int] = []

    def spy(text: str) -> list[int]:
        lengths.append(len(text))
        return _chars(text)

    long_prompt = [{"role": "user", "content": "x" * 50_000}]
    short_prompt = [{"role": "user", "content": "x" * 500}]
    capped = build_blocks(long_prompt, block_size=8, secret=SECRET, tokenize=spy)
    exact = build_blocks(short_prompt, block_size=8, secret=SECRET, tokenize=spy)

    assert lengths[0] == 1_000
    assert sum(block.token_count for block in capped) == 1_000
    # Below the bound nothing is cut.
    assert (
        exact == build_blocks(short_prompt, block_size=8, secret=SECRET, tokenize=_chars)
        and lengths[1] < 1_000
    )


# ---------------------------------------------------------------------------
# Once per request, off the event loop
# ---------------------------------------------------------------------------


class _CountingTokenizer:
    def __init__(self) -> None:
        self.calls = 0
        self.threads: list[str] = []

    def __call__(self, text: str) -> list[int]:
        self.calls += 1
        self.threads.append(threading.current_thread().name)
        return _chars(text)


def _cost_router():
    router, api_a, api_b = _make_router_with_two_api(
        RouteWiseConfig(prefix_cache_cost_adjustment_enabled=True, fallback_mode="policy")
    )
    tokenizer = _CountingTokenizer()
    router.prefix_cache = PrefixCacheCoordinator(
        enabled=True,
        memory=SessionProviderPrefixMemory(min_match_tokens=1),
        block_size=8,
        secret=SECRET,
        tokenize=tokenizer,
    )
    return router, api_a, api_b, tokenizer


def _context() -> dict[str, Any]:
    return {
        "request_id": "r-blocks",
        "messages": [{"role": "user", "content": "hello " * 200}],
        "params": {"session_id": "sess-1", "prompt_tokens": 1000},
    }


@pytest.mark.unit
def test_every_solve_reuses_the_requests_blocks():
    router, _a, _b, tokenizer = _cost_router()
    req_ctx.set({"request_id": "r-blocks", "affinity_key": "userA"})
    context = _context()

    first = router._select_decision("test-model", context)
    first_prefix = router._prefix_cache_cost_context("test-model", context)
    first.release()
    second = router._select_decision("test-model", context)
    second.release()
    # A hedge checkpoint prices the candidates the same way.
    router._build_candidates(
        "test-model",
        prompt_tokens=1000,
        predicted_output_tokens=10.0,
        envelope=None,
        now=0.0,
        context=context,
    )

    assert tokenizer.calls == 1
    assert first_prefix is not None
    # The blocks are the ones a fresh build gives.
    assert first_prefix[0] == router.prefix_cache.build_blocks(context["messages"])


@pytest.mark.unit
def test_a_request_with_other_inputs_is_built_again():
    router, _a, _b, tokenizer = _cost_router()
    context = _context()

    router._prefix_cache_cost_context("test-model", context)
    context["messages"] = [{"role": "user", "content": "something else"}]
    router._prefix_cache_cost_context("test-model", context)

    assert tokenizer.calls == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_request_builds_its_blocks_once_in_the_tokenizer_pool_across_a_re_solve():
    router, api_a, api_b, tokenizer = _cost_router()
    calls = {"n": 0}

    async def first_call_fails(messages, **params):
        calls["n"] += 1
        if calls["n"] == 1:
            error = RuntimeError("upstream unavailable")
            error.status_code = 503  # type: ignore[attr-defined]
            raise error
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    api_a.chat_completion = AsyncMock(side_effect=first_call_fails)
    api_b.chat_completion = AsyncMock(side_effect=first_call_fails)
    req_ctx.set({"affinity_key": "userA"})

    response = await router.chat_completion(
        "test-model",
        [{"role": "user", "content": "hello " * 200}],
        session_id="sess-1",
        request_id="r-resolve",
    )

    assert response["choices"][0]["message"]["content"] == "ok"
    assert calls["n"] == 2  # one failure, one re-solve
    assert tokenizer.calls == 1
    assert tokenizer.threads[0].startswith("tokenizer")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_blocks_are_built_without_a_session():
    router, api_a, _b, tokenizer = _cost_router()
    api_a.chat_completion = AsyncMock(
        return_value={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    )
    _b.chat_completion = api_a.chat_completion

    await router.chat_completion("test-model", [{"role": "user", "content": "hi"}])

    assert tokenizer.calls == 0
