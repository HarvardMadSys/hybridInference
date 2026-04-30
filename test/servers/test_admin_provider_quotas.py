"""Tests for the admin provider-quotas module."""

from __future__ import annotations

from serving.admin.provider_quotas import _mask_key


class TestMaskKey:
    def test_normal_length_key_shows_prefix_and_suffix(self):
        # >= 16 chars: first 8 + "..." + last 4
        assert _mask_key("cpk_ab123456cccccccxyz9") == "cpk_ab12...xyz9"

    def test_exactly_16_char_key_uses_full_form(self):
        assert _mask_key("0123456789abcdef") == "01234567...cdef"

    def test_15_char_key_uses_placeholder(self):
        assert _mask_key("0123456789abcde") == "***configured***"

    def test_short_key_returns_placeholder(self):
        assert _mask_key("short") == "***configured***"

    def test_long_cookie_string_gets_masked(self):
        cookie = "session=abc123def456ghi789jkl012mno345"
        result = _mask_key(cookie)
        assert result.startswith("session=")
        assert "..." in result
        assert len(result) == 8 + 3 + 4
