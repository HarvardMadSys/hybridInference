"""Tests for serving.utils.token_utils — cache token extraction."""

from serving.utils.token_utils import extract_cache_tokens, normalize_usage

# ---------------------------------------------------------------------------
# extract_cache_tokens
# ---------------------------------------------------------------------------


class TestExtractCacheTokens:
    """Tests for extract_cache_tokens()."""

    # --- guard clauses ---

    def test_none_input(self):
        assert extract_cache_tokens(None) == (None, None)

    def test_empty_dict(self):
        assert extract_cache_tokens({}) == (None, None)

    def test_non_dict_input(self):
        assert extract_cache_tokens("not a dict") == (None, None)  # type: ignore[arg-type]

    # --- cache read: direct fields ---

    def test_anthropic_cache_read(self):
        usage = {"cache_read_input_tokens": 50}
        assert extract_cache_tokens(usage) == (50, None)

    def test_direct_cache_read(self):
        usage = {"cache_read_tokens": 30}
        assert extract_cache_tokens(usage) == (30, None)

    def test_deepseek_cache_hit(self):
        usage = {"prompt_cache_hit_tokens": 100}
        assert extract_cache_tokens(usage) == (100, None)

    def test_sglang_cached_tokens(self):
        """SGLang returns cached_tokens as a direct field."""
        usage = {"cached_tokens": 913}
        assert extract_cache_tokens(usage) == (913, None)

    def test_zero_cache_read_is_recorded(self):
        """An explicit 0 means 'supported but no hit' — should not be None."""
        usage = {"cache_read_input_tokens": 0}
        assert extract_cache_tokens(usage) == (0, None)

    def test_anthropic_field_takes_priority(self):
        """When multiple read fields exist, first match wins."""
        usage = {"cache_read_input_tokens": 10, "cache_read_tokens": 20}
        assert extract_cache_tokens(usage) == (10, None)

    def test_sglang_field_priority_over_openai_nested(self):
        """SGLang cached_tokens should be found before nested prompt_tokens_details."""
        usage = {"cached_tokens": 913, "prompt_tokens_details": {"cached_tokens": 100}}
        assert extract_cache_tokens(usage) == (913, None)

    # --- cache read: nested (OpenAI / Azure) ---

    def test_openai_nested_cached_tokens(self):
        usage = {"prompt_tokens_details": {"cached_tokens": 80}}
        assert extract_cache_tokens(usage) == (80, None)

    def test_minimax_input_tokens_details_cached_tokens(self):
        usage = {"input_tokens_details": {"cached_tokens": 80}}
        assert extract_cache_tokens(usage) == (80, None)

    def test_minimax_input_token_details_cached_tokens(self):
        usage = {"input_token_details": {"cached_tokens": 80}}
        assert extract_cache_tokens(usage) == (80, None)

    def test_minimax_input_tokens_details_cache_read_tokens(self):
        usage = {"input_tokens_details": {"cache_read_tokens": 80}}
        assert extract_cache_tokens(usage) == (80, None)

    def test_minimax_input_tokens_details_cache_hit_tokens(self):
        usage = {"input_tokens_details": {"cache_hit_tokens": 80}}
        assert extract_cache_tokens(usage) == (80, None)

    def test_nested_cached_tokens_preferred_over_cache_read_tokens(self):
        usage = {
            "input_tokens_details": {
                "cached_tokens": 80,
                "cache_read_tokens": 60,
                "cache_hit_tokens": 40,
            }
        }
        assert extract_cache_tokens(usage) == (80, None)

    def test_nested_zero_cached_tokens(self):
        usage = {"prompt_tokens_details": {"cached_tokens": 0}}
        assert extract_cache_tokens(usage) == (0, None)

    def test_direct_field_preferred_over_nested(self):
        """Direct field should be found first, nested should not override."""
        usage = {
            "cache_read_input_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 80},
        }
        assert extract_cache_tokens(usage) == (50, None)

    def test_nested_fallback_when_no_direct(self):
        usage = {"prompt_tokens_details": {"cached_tokens": 42}}
        assert extract_cache_tokens(usage) == (42, None)

    def test_nested_non_dict_details_ignored(self):
        usage = {"prompt_tokens_details": "not a dict"}
        assert extract_cache_tokens(usage) == (None, None)

    # --- cache read: details present but null (a reported miss) ---

    def test_null_prompt_tokens_details_is_a_reported_zero(self):
        """sglang answers a prefix-cache miss with a null details block.

        The key is there, so the provider *did* report -- filing it as None
        would make a measured miss indistinguishable from a provider that
        cannot report cache usage at all.
        """
        usage = {"prompt_tokens": 415, "prompt_tokens_details": None}
        assert extract_cache_tokens(usage) == (0, None)

    def test_null_input_tokens_details_is_a_reported_zero(self):
        usage = {"input_tokens_details": None}
        assert extract_cache_tokens(usage) == (0, None)

    def test_null_input_token_details_is_a_reported_zero(self):
        usage = {"input_token_details": None}
        assert extract_cache_tokens(usage) == (0, None)

    def test_null_details_does_not_mask_a_populated_sibling(self):
        """A null field must not short-circuit one that carries a real count."""
        usage = {"prompt_tokens_details": None, "input_tokens_details": {"cached_tokens": 64}}
        assert extract_cache_tokens(usage) == (64, None)

    def test_direct_field_preferred_over_null_details(self):
        usage = {"cached_tokens": 128, "prompt_tokens_details": None}
        assert extract_cache_tokens(usage) == (128, None)

    def test_no_cache_keys_at_all_stays_none(self):
        """A provider with no cache reporting must stay None, not become 0."""
        usage = {"prompt_tokens": 415, "completion_tokens": 64, "total_tokens": 479}
        assert extract_cache_tokens(usage) == (None, None)

    def test_null_details_does_not_affect_cache_write(self):
        usage = {"prompt_tokens_details": None}
        assert extract_cache_tokens(usage)[1] is None

    # --- cache write ---

    def test_anthropic_cache_write(self):
        usage = {"cache_creation_input_tokens": 25}
        assert extract_cache_tokens(usage) == (None, 25)

    def test_direct_cache_write(self):
        usage = {"cache_write_tokens": 15}
        assert extract_cache_tokens(usage) == (None, 15)

    def test_zero_cache_write_is_recorded(self):
        usage = {"cache_write_tokens": 0}
        assert extract_cache_tokens(usage) == (None, 0)

    # --- both read and write ---

    def test_both_read_and_write(self):
        usage = {"cache_read_input_tokens": 50, "cache_creation_input_tokens": 25}
        assert extract_cache_tokens(usage) == (50, 25)

    # --- invalid values ---

    def test_non_numeric_value_ignored(self):
        usage = {"cache_read_input_tokens": "not_a_number"}
        assert extract_cache_tokens(usage) == (None, None)

    def test_negative_value_ignored(self):
        usage = {"cache_read_input_tokens": -5}
        assert extract_cache_tokens(usage) == (None, None)


# ---------------------------------------------------------------------------
# normalize_usage — cache token integration
# ---------------------------------------------------------------------------


class TestNormalizeUsageCacheTokens:
    """Tests that normalize_usage() flattens cache tokens to top level."""

    def test_none_returns_none(self):
        assert normalize_usage(None) is None

    def test_empty_dict_returns_none(self):
        assert normalize_usage({}) is None

    def test_anthropic_format(self):
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 10,
        }
        result = normalize_usage(usage)
        assert result["cache_read_tokens"] == 30
        assert result["cache_write_tokens"] == 10

    def test_openai_nested_format(self):
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 80},
        }
        result = normalize_usage(usage)
        assert result["cache_read_tokens"] == 80

    def test_deepseek_format(self):
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_cache_hit_tokens": 60,
        }
        result = normalize_usage(usage)
        assert result["cache_read_tokens"] == 60

    def test_sglang_format(self):
        """SGLang returns cached_tokens as a direct field."""
        usage = {
            "prompt_tokens": 914,
            "completion_tokens": 1,
            "cached_tokens": 913,
        }
        result = normalize_usage(usage)
        assert result["cache_read_tokens"] == 913

    def test_no_cache_fields_leaves_them_absent(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        result = normalize_usage(usage)
        assert "cache_read_tokens" not in result
        assert "cache_write_tokens" not in result

    def test_does_not_modify_original(self):
        usage = {"prompt_tokens": 100, "cache_read_input_tokens": 30}
        normalize_usage(usage)
        assert "cache_read_tokens" not in usage  # original unchanged

    def test_reasoning_and_cache_both_extracted(self):
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "reasoning_tokens": 20,
            "cache_read_input_tokens": 30,
        }
        result = normalize_usage(usage)
        assert result["reasoning_tokens"] == 20
        assert result["cache_read_tokens"] == 30

    def test_sglang_prefix_cache_miss_flattens_to_zero(self):
        """Verbatim sglang usage for a cold prompt (h200a, 2026-08-28).

        A miss arrives as a null details block, not `{"cached_tokens": 0}`.
        It must reach api_logs as 0 -- NULL there means "provider does not
        report cache usage", which is not what happened.
        """
        usage = {
            "prompt_tokens": 37,
            "total_tokens": 38,
            "completion_tokens": 1,
            "prompt_tokens_details": None,
            "reasoning_tokens": 0,
        }
        result = normalize_usage(usage)
        assert result["cache_read_tokens"] == 0

    def test_sglang_prefix_cache_hit_flattens_to_count(self):
        """Same request warm: the paired hit the miss above has to be told from."""
        usage = {
            "prompt_tokens": 1430,
            "total_tokens": 1431,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 1280},
            "reasoning_tokens": 0,
        }
        result = normalize_usage(usage)
        assert result["cache_read_tokens"] == 1280

    def test_provider_without_cache_reporting_stays_absent(self):
        """vLLM/diffusiongemma send no cache key at all -- still not a miss."""
        usage = {"prompt_tokens": 415, "completion_tokens": 64, "total_tokens": 479}
        result = normalize_usage(usage)
        assert "cache_read_tokens" not in result
