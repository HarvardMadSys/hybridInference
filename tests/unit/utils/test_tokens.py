"""Tests for serving.utils.tokens — prompt token estimation.

Focus on multimodal content handling: image/audio blocks must contribute a
small flat estimate rather than having their (often large, base64) payloads run
through the text tokenizer.
"""

from __future__ import annotations

from serving.utils.tokens import (
    AUDIO_TOKEN_ESTIMATE,
    IMAGE_TOKEN_ESTIMATE,
    estimate_prompt_tokens,
    estimate_text_tokens,
)


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
