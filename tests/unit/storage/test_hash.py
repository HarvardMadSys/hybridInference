"""Unit tests for prompt hashing functions."""

import pytest

from serving.storage.database import compute_prompt_hash, compute_prompt_hash_chunked


class TestPromptHashing:
    """Test suite for prompt hash functions."""

    def test_compute_prompt_hash_string(self):
        """Test full prompt hash with string input."""
        prompt = "Hello, world!"
        hash_result = compute_prompt_hash(prompt)

        # Should return a 64-character hex string (SHA256)
        assert len(hash_result) == 64
        assert all(c in "0123456789abcdef" for c in hash_result)

        # Same input should produce same hash
        assert compute_prompt_hash(prompt) == hash_result

    def test_compute_prompt_hash_messages(self):
        """Test full prompt hash with message list input."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"},
        ]
        hash_result = compute_prompt_hash(messages)

        assert len(hash_result) == 64
        assert all(c in "0123456789abcdef" for c in hash_result)

        # Same messages should produce same hash
        assert compute_prompt_hash(messages) == hash_result

    def test_compute_prompt_hash_deterministic(self):
        """Test that hash is deterministic (same input = same output)."""
        messages = [
            {"role": "user", "content": "Test message"},
            {"role": "assistant", "content": "Response"},
        ]

        hash1 = compute_prompt_hash(messages)
        hash2 = compute_prompt_hash(messages)

        assert hash1 == hash2

    def test_compute_prompt_hash_dict_key_order_independence(self):
        """Test that dict key order doesn't affect hash (sort_keys=True)."""
        # Same content, different key order
        messages1 = [{"role": "user", "content": "Hello"}]
        messages2 = [{"content": "Hello", "role": "user"}]

        hash1 = compute_prompt_hash(messages1)
        hash2 = compute_prompt_hash(messages2)

        # Should produce same hash due to sort_keys=True
        assert hash1 == hash2

    def test_compute_prompt_hash_chunked_string(self):
        """Test 4-token chunked hash with string input."""
        prompt = "Hello, world! This is a test prompt."
        hash_result = compute_prompt_hash_chunked(prompt)

        # Should return a 64-character hex string (SHA256)
        assert len(hash_result) == 64
        assert all(c in "0123456789abcdef" for c in hash_result)

        # Same input should produce same hash
        assert compute_prompt_hash_chunked(prompt) == hash_result

    def test_compute_prompt_hash_chunked_messages(self):
        """Test 4-token chunked hash with message list input."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is the capital of France?"},
        ]
        hash_result = compute_prompt_hash_chunked(messages)

        assert len(hash_result) == 64
        assert all(c in "0123456789abcdef" for c in hash_result)

    def test_compute_prompt_hash_chunked_deterministic(self):
        """Test that chunked hash is deterministic."""
        messages = [
            {"role": "user", "content": "Test message for chunked hashing"},
            {"role": "assistant", "content": "This is a response"},
        ]

        hash1 = compute_prompt_hash_chunked(messages)
        hash2 = compute_prompt_hash_chunked(messages)

        assert hash1 == hash2

    def test_compute_prompt_hash_chunked_dict_key_order_independence(self):
        """Test that chunked hash is also independent of dict key order."""
        messages1 = [{"role": "user", "content": "Test"}]
        messages2 = [{"content": "Test", "role": "user"}]

        hash1 = compute_prompt_hash_chunked(messages1)
        hash2 = compute_prompt_hash_chunked(messages2)

        # Should produce same hash due to sort_keys=True
        assert hash1 == hash2

    def test_compute_prompt_hash_chunked_different_inputs(self):
        """Test that different inputs produce different hashes."""
        prompt1 = "This is the first prompt"
        prompt2 = "This is the second prompt"

        hash1 = compute_prompt_hash_chunked(prompt1)
        hash2 = compute_prompt_hash_chunked(prompt2)

        assert hash1 != hash2

    def test_compute_prompt_hash_chunked_empty(self):
        """Test chunked hash with empty input."""
        empty_hash = compute_prompt_hash_chunked("")

        # Should still return a valid hash
        assert len(empty_hash) == 64
        assert all(c in "0123456789abcdef" for c in empty_hash)

    def test_compute_prompt_hash_chunked_custom_chunk_size(self):
        """Test chunked hash with custom chunk size."""
        prompt = "Test prompt for custom chunk size"

        hash_4 = compute_prompt_hash_chunked(prompt, chunk_size=4)
        hash_8 = compute_prompt_hash_chunked(prompt, chunk_size=8)

        # Different chunk sizes should produce different hashes
        assert hash_4 != hash_8

        # Both should be valid hashes
        assert len(hash_4) == 64
        assert len(hash_8) == 64

    def test_compute_prompt_hash_chunked_invalid_chunk_size(self):
        """Test that invalid chunk_size raises ValueError."""
        prompt = "Test prompt"

        # chunk_size = 0 should raise ValueError
        with pytest.raises(ValueError, match="chunk_size must be >= 1"):
            compute_prompt_hash_chunked(prompt, chunk_size=0)

        # Negative chunk_size should also raise ValueError
        with pytest.raises(ValueError, match="chunk_size must be >= 1"):
            compute_prompt_hash_chunked(prompt, chunk_size=-1)

    def test_compute_prompt_hash_vs_chunked(self):
        """Test that full hash and chunked hash produce different results."""
        prompt = "Compare full hash vs chunked hash"

        full_hash = compute_prompt_hash(prompt)
        chunked_hash = compute_prompt_hash_chunked(prompt)

        # They should be different (different algorithms)
        assert full_hash != chunked_hash

        # Both should be valid 64-char hashes
        assert len(full_hash) == 64
        assert len(chunked_hash) == 64

    def test_compute_prompt_hash_chunked_long_prompt(self):
        """Test chunked hash with a long prompt (many chunks)."""
        # Create a long prompt with ~1000 tokens
        long_prompt = " ".join([f"word{i}" for i in range(500)])

        hash_result = compute_prompt_hash_chunked(long_prompt)

        assert len(hash_result) == 64
        assert all(c in "0123456789abcdef" for c in hash_result)

    def test_compute_prompt_hash_chunked_unicode(self):
        """Test chunked hash with Unicode characters."""
        prompt = "你好世界!This is a test with 中文字符 and émojis 🚀"

        hash_result = compute_prompt_hash_chunked(prompt)

        assert len(hash_result) == 64
        assert all(c in "0123456789abcdef" for c in hash_result)

        # Should be deterministic
        assert compute_prompt_hash_chunked(prompt) == hash_result


class TestResponseHashing:
    """Test suite for response hash functions (reuses prompt hash functions)."""

    def test_response_hash_string(self):
        """Test hashing a string response."""
        response = "This is a response from the AI assistant."

        # Response hashing uses the same function as prompt hashing
        hash_full = compute_prompt_hash(response)
        hash_chunked = compute_prompt_hash_chunked(response)

        assert len(hash_full) == 64
        assert len(hash_chunked) == 64
        assert hash_full != hash_chunked

    def test_response_hash_dict(self):
        """Test hashing a dict response (OpenAI format)."""
        response = {
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "The capital of France is Paris.",
                    },
                    "finish_reason": "stop",
                }
            ],
        }

        # Hash the dict (will be JSON serialized)
        hash_result = compute_prompt_hash(response)

        assert len(hash_result) == 64
        assert all(c in "0123456789abcdef" for c in hash_result)

    def test_response_hash_dict_key_order_independence(self):
        """Test that response dict key order doesn't affect hash."""
        # Same response, different key order
        response1 = {
            "id": "123",
            "content": "Hello",
            "role": "assistant",
        }
        response2 = {
            "role": "assistant",
            "content": "Hello",
            "id": "123",
        }

        hash1 = compute_prompt_hash(response1)
        hash2 = compute_prompt_hash(response2)

        # Should produce same hash due to sort_keys=True
        assert hash1 == hash2

    def test_response_hash_deterministic(self):
        """Test that response hashing is deterministic."""
        response = "Deterministic response for testing"

        hash1 = compute_prompt_hash_chunked(response)
        hash2 = compute_prompt_hash_chunked(response)

        assert hash1 == hash2

    def test_response_hash_different_responses(self):
        """Test that different responses produce different hashes."""
        response1 = "First response"
        response2 = "Second response"

        hash1 = compute_prompt_hash_chunked(response1)
        hash2 = compute_prompt_hash_chunked(response2)

        assert hash1 != hash2

    def test_prompt_and_response_hash_independence(self):
        """Test that same text produces same hash whether it's prompt or response."""
        text = "This could be either a prompt or a response"

        # The hash function doesn't care if it's prompt or response
        hash1 = compute_prompt_hash_chunked(text)
        hash2 = compute_prompt_hash_chunked(text)

        assert hash1 == hash2

    def test_response_hash_long_content(self):
        """Test hashing a long response."""
        # Simulate a long AI response
        long_response = " ".join(
            [
                "This is a very long response from the AI assistant.",
                "It contains multiple sentences and paragraphs.",
                "The purpose is to test that hashing works correctly",
                "even with large amounts of text content.",
            ]
            * 50
        )

        hash_result = compute_prompt_hash_chunked(long_response)

        assert len(hash_result) == 64
        assert all(c in "0123456789abcdef" for c in hash_result)

    def test_response_hash_with_special_characters(self):
        """Test response hashing with special characters and formatting."""
        response = """
        Here's a code example:

        ```python
        def hello():
            print("Hello, world!")
        ```

        Special chars: @#$%^&*()_+-=[]{}|;:'",.<>?/~`
        """

        hash_result = compute_prompt_hash_chunked(response)

        assert len(hash_result) == 64
        assert all(c in "0123456789abcdef" for c in hash_result)
