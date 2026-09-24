"""Tests for serving.utils.tokens — prompt token estimation.

Focus on multimodal content handling: image/audio blocks must contribute a
small flat estimate rather than having their (often large, base64) payloads run
through the text tokenizer. Also pins encoding cost to linear time on inputs
the pre-tokenizer cannot split.
"""

from __future__ import annotations

import time

import pytest
import tiktoken

from serving.utils import tokens as tokens_mod
from serving.utils.tokens import (
    AUDIO_TOKEN_ESTIMATE,
    IMAGE_TOKEN_ESTIMATE,
    VIDEO_TOKEN_ESTIMATE,
    estimate_prompt_tokens,
    estimate_text_tokens,
    tokenize_text,
)

# Every special token of the encoding this module uses (cl100k_base).
CL100K_SPECIAL_TOKENS = sorted(tiktoken.get_encoding("cl100k_base").special_tokens_set)


class TestEstimatePromptTokensText:
    """Plain-text behavior is unchanged by multimodal support."""

    def test_empty_messages(self):
        assert estimate_prompt_tokens([]) == 0

    def test_simple_string_content(self):
        messages = [{"role": "user", "content": "Hello world"}]
        # role + content + per-message overhead (4) + end overhead (3)
        expected = estimate_text_tokens("user") + estimate_text_tokens("Hello world") + 4 + 3
        assert estimate_prompt_tokens(messages) == expected

    def test_none_content(self):
        messages = [{"role": "assistant", "content": None}]
        expected = estimate_text_tokens("assistant") + 4 + 3
        assert estimate_prompt_tokens(messages) == expected


class TestEstimatePromptTokensMultimodal:
    """List/structured content is counted structurally, not stringified."""

    def test_text_blocks_counted(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Hello"},
                    {"type": "text", "text": "world"},
                ],
            }
        ]
        expected = (
            estimate_text_tokens("user")
            + estimate_text_tokens("Hello")
            + estimate_text_tokens("world")
            + 4
            + 3
        )
        assert estimate_prompt_tokens(messages) == expected

    def test_image_block_uses_flat_estimate(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is this?"},
                    {"type": "image_url", "image_url": {"url": "https://x/img.png"}},
                ],
            }
        ]
        expected = (
            estimate_text_tokens("user")
            + estimate_text_tokens("What is this?")
            + IMAGE_TOKEN_ESTIMATE
            + 4
            + 3
        )
        assert estimate_prompt_tokens(messages) == expected

    def test_base64_image_blob_not_tokenized_as_text(self):
        """A large inline data URL must not inflate the token count."""
        huge_data_url = "data:image/png;base64," + ("A" * 200_000)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "image_url": {"url": huge_data_url}},
                ],
            }
        ]
        result = estimate_prompt_tokens(messages)
        # The 200k-char blob would be ~50k tokens if stringified. It must not be.
        assert result < 1000

    def test_audio_block_uses_flat_estimate(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "transcribe"},
                    {
                        "type": "input_audio",
                        "input_audio": {"data": "B" * 100_000, "format": "wav"},
                    },
                ],
            }
        ]
        expected = (
            estimate_text_tokens("user")
            + estimate_text_tokens("transcribe")
            + AUDIO_TOKEN_ESTIMATE
            + 4
            + 3
        )
        assert estimate_prompt_tokens(messages) == expected

    def test_video_block_uses_flat_estimate(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {
                        "type": "video_url",
                        "video_url": {"url": "data:video/mp4;base64," + ("C" * 300_000)},
                    },
                ],
            }
        ]
        expected = (
            estimate_text_tokens("user")
            + estimate_text_tokens("describe")
            + VIDEO_TOKEN_ESTIMATE
            + 4
            + 3
        )
        # A large inline video blob must contribute the flat estimate, not its size.
        assert estimate_prompt_tokens(messages) == expected

    def test_unknown_block_counts_embedded_text_only(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "weird", "text": "kept", "blob": "X" * 50_000},
                ],
            }
        ]
        expected = estimate_text_tokens("user") + estimate_text_tokens("kept") + 4 + 3
        assert estimate_prompt_tokens(messages) == expected


class TestSpecialTokenTextIsOrdinaryText:
    """Literal special-token text must be counted, never raised on.

    tiktoken's ``encode()`` defaults to ``disallowed_special="all"``, which
    raises ``ValueError`` for input containing e.g. ``<|endoftext|>``. The text
    is user-controlled (pasting a tokenizer doc is enough) and this module only
    estimates counts for accounting/routing, so it must encode such text as the
    ordinary characters it is.
    """

    def test_estimate_text_tokens_with_endoftext(self):
        assert estimate_text_tokens("hello <|endoftext|> world") > 0

    def test_tokenize_text_with_endoftext(self):
        token_ids = tokenize_text("hello <|endoftext|> world")
        assert token_ids
        assert all(isinstance(t, int) for t in token_ids)

    @pytest.mark.parametrize("special", CL100K_SPECIAL_TOKENS)
    def test_every_special_token_of_the_encoding(self, special):
        """Not just ``<|endoftext|>`` -- the whole special set for cl100k_base."""
        text = f"prefix {special} suffix"
        assert estimate_text_tokens(text) > 0
        assert tokenize_text(text)

    def test_special_token_text_is_not_encoded_as_a_special_id(self):
        """It must tokenize as plain characters, not collapse to one special id."""
        token_ids = tokenize_text("<|endoftext|>")
        assert len(token_ids) > 1

    def test_special_token_in_message_content(self):
        messages = [{"role": "user", "content": "explain <|endoftext|> please"}]
        assert estimate_prompt_tokens(messages) > 0

    def test_special_token_in_multimodal_text_block(self):
        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "what is <|fim_prefix|>?"}],
            }
        ]
        assert estimate_prompt_tokens(messages) > 0


class TestOrdinaryTextUnchanged:
    """Exact counts, pinned, so the special-token fix is provably non-regressive."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Hello world", 2),
            ("user", 1),
            ("The quick brown fox jumps over the lazy dog.", 10),
            ("def f(x):\n    return x + 1\n", 11),
        ],
    )
    def test_counts_match_pinned_values(self, text, expected):
        assert estimate_text_tokens(text) == expected
        assert len(tokenize_text(text)) == expected

    def test_empty_string(self):
        assert estimate_text_tokens("") == 0
        assert tokenize_text("") == []


class TestNoTiktokenFallback:
    """The heuristic branch still works when no encoding is available."""

    @pytest.fixture
    def no_encoding(self, monkeypatch):
        monkeypatch.setattr(tokens_mod, "_get_tiktoken_encoding", lambda: None)

    def test_estimate_text_tokens_uses_char_heuristic(self, no_encoding):
        # 4 characters ~= 1 token, with a floor of 1 for non-empty input.
        assert estimate_text_tokens("a" * 40) == 10
        assert estimate_text_tokens("ab") == 1
        assert estimate_text_tokens("") == 0

    def test_tokenize_text_uses_byte_chunks(self, no_encoding):
        token_ids = tokenize_text("abcdefgh")
        assert token_ids == [
            int.from_bytes(b"abcd", byteorder="big"),
            int.from_bytes(b"efgh", byteorder="big"),
        ]
        assert tokenize_text("") == []

    def test_special_token_text_is_fine_in_the_fallback_too(self, no_encoding):
        assert estimate_text_tokens("<|endoftext|>") > 0
        assert tokenize_text("<|endoftext|>")


class TestUnsplittableRunsEncodeInLinearTime:
    """Runs the pre-tokenizer cannot split must not cost quadratic time.

    One character repeated, or letters/CJK with no space or punctuation, form a
    single pre-token, and tiktoken before 0.13 merged such a piece in quadratic
    time: 64K "。" took ~7 s and 256K ~2 min. Estimation runs synchronously on
    request paths, several of them on the event loop, so one such input froze
    every other request and stream. 0.13 fixed the merge, and these keep the
    lock on a version that has the fix.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "。" * 128_000,
            "a" * 128_000,
            " " * 128_000,
            "".join(chr(0x4E00 + (i * 7919) % 20_000) for i in range(128_000)),
        ],
        ids=["punctuation-run", "letter-run", "whitespace-run", "cjk-without-punctuation"],
    )
    def test_estimate_stays_fast(self, text):
        start = time.perf_counter()
        assert estimate_text_tokens(text) > 0
        assert tokenize_text(text)
        elapsed = time.perf_counter() - start
        # Tens of ms on tiktoken 0.13+; 7-60 s on 0.12. The bound leaves slow CI room.
        assert elapsed < 2.0, f"encoding took {elapsed:.1f}s; is tiktoken older than 0.13?"
