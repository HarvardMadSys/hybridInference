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

    # --- cache read: details present but null ---
    #
    # The null is ambiguous on the wire: sglang with --enable-cache-report sends
    # it for a prefix-cache miss, sglang without the flag and vLLM without
    # --enable-prompt-tokens-details send it on every request. Default is
    # "unknown"; only a route that declares it reports may read it as 0.

    def test_null_prompt_tokens_details_stays_unknown_by_default(self):
        """Unlicensed, a null details block must not become a measured 0.

        Reading it as 0 for an endpoint that never reports would replace an
        honest NULL in api_logs with a fabricated miss.
        """
        usage = {"prompt_tokens": 415, "prompt_tokens_details": None}
        assert extract_cache_tokens(usage) == (None, None)

    def test_null_input_tokens_details_stays_unknown_by_default(self):
        assert extract_cache_tokens({"input_tokens_details": None}) == (None, None)

    def test_null_input_token_details_stays_unknown_by_default(self):
        assert extract_cache_tokens({"input_token_details": None}) == (None, None)

    def test_null_prompt_tokens_details_is_a_reported_zero_when_licensed(self):
        """On a route that declares it reports, the null is a measured miss.

        Filing it as None there would make a measured miss indistinguishable
        from a provider that cannot report cache usage at all.
        """
        usage = {"prompt_tokens": 415, "prompt_tokens_details": None}
        assert extract_cache_tokens(usage, null_details_means_miss=True) == (0, None)

    def test_null_input_tokens_details_is_a_reported_zero_when_licensed(self):
        usage = {"input_tokens_details": None}
        assert extract_cache_tokens(usage, null_details_means_miss=True) == (0, None)

    def test_null_input_token_details_is_a_reported_zero_when_licensed(self):
        usage = {"input_token_details": None}
        assert extract_cache_tokens(usage, null_details_means_miss=True) == (0, None)

    def test_null_details_does_not_mask_a_populated_sibling(self):
        """A null field must not short-circuit one that carries a real count."""
        usage = {"prompt_tokens_details": None, "input_tokens_details": {"cached_tokens": 64}}
        assert extract_cache_tokens(usage) == (64, None)
        assert extract_cache_tokens(usage, null_details_means_miss=True) == (64, None)

    def test_direct_field_preferred_over_null_details(self):
        usage = {"cached_tokens": 128, "prompt_tokens_details": None}
        assert extract_cache_tokens(usage) == (128, None)
        assert extract_cache_tokens(usage, null_details_means_miss=True) == (128, None)

    def test_no_cache_keys_at_all_stays_none(self):
        """No details key at all is unknown even on a licensed route."""
        usage = {"prompt_tokens": 415, "completion_tokens": 64, "total_tokens": 479}
        assert extract_cache_tokens(usage) == (None, None)
        assert extract_cache_tokens(usage, null_details_means_miss=True) == (None, None)

    def test_null_details_does_not_affect_cache_write(self):
        usage = {"prompt_tokens_details": None}
        assert extract_cache_tokens(usage)[1] is None
        assert extract_cache_tokens(usage, null_details_means_miss=True)[1] is None

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

    def test_sglang_prefix_cache_hit_flattens_to_count(self):
        """Verbatim sglang usage for a warm prompt (h200a :8003, 2026-08-28)."""
        usage = {
            "prompt_tokens": 4817,
            "total_tokens": 4818,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 4608},
            "reasoning_tokens": 0,
        }
        result = normalize_usage(usage)
        assert result["cache_read_tokens"] == 4608

    def test_null_details_stays_absent_without_route_context(self):
        """normalize_usage has no route context, so a null block stays unknown.

        Verbatim sglang usage for a cold prompt (h200a :8003, 2026-08-28). Its
        miss is recorded as 0 by the adapter, which does hold the route's
        ``null_cache_details_means_miss``; this helper -- used on the gateway's
        own already-normalized chunks and on the storage path -- must not guess.
        """
        usage = {
            "prompt_tokens": 4817,
            "total_tokens": 4818,
            "completion_tokens": 1,
            "prompt_tokens_details": None,
            "reasoning_tokens": 0,
        }
        result = normalize_usage(usage)
        assert "cache_read_tokens" not in result

    def test_adapter_reported_zero_survives_the_storage_path(self):
        """A 0 already resolved by the adapter must stay 0, not fall back to NULL."""
        usage = {
            "prompt_tokens": 4817,
            "total_tokens": 4818,
            "completion_tokens": 1,
            "cache_read_tokens": 0,
            "cached_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 0},
        }
        result = normalize_usage(usage)
        assert result["cache_read_tokens"] == 0

    def test_provider_without_cache_reporting_stays_absent(self):
        """A provider that sends no cache key at all is unknown, not a miss."""
        usage = {"prompt_tokens": 415, "completion_tokens": 64, "total_tokens": 479}
        result = normalize_usage(usage)
        assert "cache_read_tokens" not in result
