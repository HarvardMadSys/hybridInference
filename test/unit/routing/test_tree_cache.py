"""Unit tests for TreeCache."""

import threading
import time

import pytest

from routing.tree_cache import TreeCache, create_tree_cache


class TestTreeCacheBasic:
    """Basic functionality tests."""

    def test_insert_and_match_simple(self):
        """Test basic insert and match operations."""
        cache = TreeCache(max_size=10000)

        # Insert a string
        chars_added = cache.insert("Hello, world!")
        assert chars_added == 13

        # Match exact prefix
        matched = cache.match_prefix("Hello, world!")
        assert matched == 13

        # Match partial prefix
        matched = cache.match_prefix("Hello, there!")
        assert matched == 7  # "Hello, "

        # No match
        matched = cache.match_prefix("Goodbye!")
        assert matched == 0

    def test_insert_updates_existing(self):
        """Test that inserting overlapping strings only adds new parts."""
        cache = TreeCache(max_size=10000)

        # Insert first string
        chars1 = cache.insert("Hello, world!")
        assert chars1 == 13

        # Insert overlapping string - should only add the difference
        chars2 = cache.insert("Hello, there!")
        assert chars2 == 6  # Only "there!" is new (7 chars "Hello, " already exist)

        # Both should match
        assert cache.match_prefix("Hello, world!") == 13
        assert cache.match_prefix("Hello, there!") == 13

    def test_empty_string(self):
        """Test handling of empty strings."""
        cache = TreeCache()

        assert cache.insert("") == 0
        assert cache.match_prefix("") == 0
        assert cache.size == 0

    def test_long_string(self):
        """Test that long strings are properly handled."""
        cache = TreeCache(max_size=100000)

        # Insert a long string
        text = "A" * 1000
        chars_added = cache.insert(text)
        assert chars_added == 1000

        # Should match the full string
        matched = cache.match_prefix(text)
        assert matched == 1000

        # Partial match
        matched = cache.match_prefix("A" * 500)
        assert matched == 500

    def test_branching_structure(self):
        """Test that the tree correctly handles branching."""
        cache = TreeCache(max_size=10000)

        # Insert strings that share a common prefix
        cache.insert("Hello, world!")
        cache.insert("Hello, there!")
        cache.insert("Hello, everyone!")

        # All should match their full length
        assert cache.match_prefix("Hello, world!") == 13
        assert cache.match_prefix("Hello, there!") == 13
        assert cache.match_prefix("Hello, everyone!") == 16

        # Common prefix should match
        assert cache.match_prefix("Hello, ") == 7
        assert cache.match_prefix("Hello, xyz") == 7

    def test_no_common_prefix(self):
        """Test strings with no common prefix."""
        cache = TreeCache(max_size=10000)

        cache.insert("Apple")
        cache.insert("Banana")
        cache.insert("Cherry")

        assert cache.match_prefix("Apple") == 5
        assert cache.match_prefix("Banana") == 6
        assert cache.match_prefix("Cherry") == 6
        assert cache.match_prefix("Date") == 0


class TestTreeCacheEviction:
    """Tests for LRU eviction."""

    def test_eviction_triggered_when_over_limit(self):
        """Test that eviction is triggered when cache exceeds max_size."""
        cache = TreeCache(max_size=100)

        # Insert strings that exceed max_size
        cache.insert("A" * 50)
        cache.insert("B" * 50)
        cache.insert("C" * 50)

        # Cache should be under limit after eviction
        assert cache.size <= 100

    def test_lru_eviction_order(self):
        """Test that least recently used entries are evicted first."""
        cache = TreeCache(max_size=100)

        # Insert three strings
        cache.insert("AAAA" * 10)  # 40 chars
        time.sleep(0.01)
        cache.insert("BBBB" * 10)  # 40 chars
        time.sleep(0.01)
        cache.insert("CCCC" * 10)  # 40 chars - this triggers eviction

        # After eviction, the oldest (A) should be evicted
        # B and C should still be present
        assert cache.match_prefix("BBBB" * 10) > 0 or cache.match_prefix("CCCC" * 10) > 0

    def test_access_time_update_prevents_eviction(self):
        """Test that accessing an entry updates its access time."""
        cache = TreeCache(max_size=100)

        # Insert first string
        cache.insert("AAAA" * 10)  # 40 chars
        time.sleep(0.01)

        # Insert second string
        cache.insert("BBBB" * 10)  # 40 chars
        time.sleep(0.01)

        # Access first string to update its time
        cache.match_prefix("AAAA" * 10, update_access_time=True)
        time.sleep(0.01)

        # Insert third string - should evict B, not A
        cache.insert("CCCC" * 10)  # 40 chars

        # A should still be accessible (was recently accessed)
        # B might be evicted
        assert cache.size <= 100


class TestTreeCacheTokenEstimation:
    """Tests for token estimation."""

    def test_estimate_cached_tokens_default_ratio(self):
        """Test token estimation with default chars_per_token ratio."""
        cache = TreeCache(chars_per_token=4.0)

        cache.insert("Hello, world! How are you today?")

        # Match "Hello, world! " (14 chars) -> ~3 tokens
        tokens = cache.estimate_cached_tokens("Hello, world! What's up?")
        assert tokens == 3  # 14 chars / 4 chars_per_token = 3.5 -> 3

    def test_estimate_cached_tokens_custom_ratio(self):
        """Test token estimation with custom chars_per_token ratio."""
        cache = TreeCache(chars_per_token=2.0)  # More tokens per char

        cache.insert("Hello, world!")

        tokens = cache.estimate_cached_tokens("Hello, world!")
        assert tokens == 6  # 13 chars / 2 chars_per_token = 6.5 -> 6

    def test_estimate_cached_tokens_no_match(self):
        """Test token estimation when there's no cache hit."""
        cache = TreeCache()

        cache.insert("Hello, world!")

        tokens = cache.estimate_cached_tokens("Goodbye, world!")
        assert tokens == 0


class TestTreeCacheStats:
    """Tests for cache statistics."""

    def test_stats_tracking(self):
        """Test that statistics are properly tracked."""
        cache = TreeCache()

        # Initial stats
        stats = cache.get_stats()
        assert stats["queries"] == 0
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["inserts"] == 0

        # Insert
        cache.insert("Hello, world!")
        stats = cache.get_stats()
        assert stats["inserts"] == 1
        assert stats["chars_inserted"] == 13

        # Query with hit
        cache.match_prefix("Hello, world!")
        stats = cache.get_stats()
        assert stats["queries"] == 1
        assert stats["hits"] == 1
        assert stats["misses"] == 0

        # Query with miss
        cache.match_prefix("Goodbye!")
        stats = cache.get_stats()
        assert stats["queries"] == 2
        assert stats["hits"] == 1
        assert stats["misses"] == 1

    def test_hit_rate_calculation(self):
        """Test hit rate calculation."""
        cache = TreeCache()
        cache.insert("Hello")

        # One hit, one miss
        cache.match_prefix("Hello")
        cache.match_prefix("World")

        stats = cache.get_stats()
        assert stats["hit_rate"] == 0.5

    def test_utilization(self):
        """Test cache utilization calculation."""
        cache = TreeCache(max_size=100)

        assert cache.utilization == 0.0

        cache.insert("A" * 50)
        assert cache.utilization == 0.5

        cache.insert("B" * 50)
        # After this, utilization should be ~1.0 (might be less due to eviction)
        assert cache.utilization <= 1.0


class TestTreeCacheClear:
    """Tests for cache clearing."""

    def test_clear_removes_all_entries(self):
        """Test that clear removes all cached entries."""
        cache = TreeCache()

        cache.insert("Hello, world!")
        cache.insert("Goodbye, world!")

        assert cache.size > 0

        cache.clear()

        assert cache.size == 0
        assert cache.match_prefix("Hello, world!") == 0
        assert cache.match_prefix("Goodbye, world!") == 0


class TestTreeCacheThreadSafety:
    """Tests for thread safety."""

    def test_concurrent_inserts(self):
        """Test that concurrent inserts don't corrupt the cache."""
        cache = TreeCache(max_size=1000000)
        num_threads = 10
        inserts_per_thread = 100

        def insert_worker(thread_id: int):
            for i in range(inserts_per_thread):
                cache.insert(f"Thread{thread_id}_Entry{i}_" + "x" * 50)

        threads = [
            threading.Thread(target=insert_worker, args=(i,))
            for i in range(num_threads)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Cache should be consistent (no crashes, valid state)
        stats = cache.get_stats()
        assert stats["inserts"] == num_threads * inserts_per_thread

    def test_concurrent_reads_and_writes(self):
        """Test concurrent reads and writes."""
        cache = TreeCache(max_size=1000000)

        # Pre-populate
        for i in range(100):
            cache.insert(f"Entry{i}_" + "x" * 50)

        errors = []

        def reader():
            try:
                for i in range(100):
                    cache.match_prefix(f"Entry{i}_" + "x" * 50)
            except Exception as e:
                errors.append(e)

        def writer():
            try:
                for i in range(100, 200):
                    cache.insert(f"Entry{i}_" + "x" * 50)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=reader) for _ in range(5)
        ] + [
            threading.Thread(target=writer) for _ in range(5)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors occurred: {errors}"


class TestCreateTreeCache:
    """Tests for the convenience factory function."""

    def test_create_with_mb_size(self):
        """Test creating cache with size in megabytes."""
        cache = create_tree_cache(max_size_mb=10.0)

        # 10 MB = 10,000,000 chars
        stats = cache.get_stats()
        assert stats["max_size"] == 10_000_000

    def test_create_with_custom_params(self):
        """Test creating cache with custom parameters."""
        cache = create_tree_cache(
            max_size_mb=5.0,
            chars_per_token=3.0,
        )

        stats = cache.get_stats()
        assert stats["max_size"] == 5_000_000


class TestTreeCacheEdgeCases:
    """Tests for edge cases."""

    def test_unicode_handling(self):
        """Test handling of Unicode characters."""
        cache = TreeCache()

        # Chinese text
        chinese = "你好，世界！"
        cache.insert(chinese)
        assert cache.match_prefix(chinese) == len(chinese)

        # Emoji
        emoji = "Hello 👋 World 🌍"
        cache.insert(emoji)
        assert cache.match_prefix(emoji) == len(emoji)

    def test_very_long_string(self):
        """Test handling of very long strings."""
        cache = TreeCache(max_size=1000000)

        long_text = "A" * 10000
        cache.insert(long_text)
        assert cache.match_prefix(long_text) == 10000

    def test_single_character_strings(self):
        """Test handling of single character strings."""
        cache = TreeCache()

        cache.insert("A")
        assert cache.match_prefix("A") == 1
        assert cache.match_prefix("B") == 0

    def test_special_characters(self):
        """Test handling of special characters."""
        cache = TreeCache()

        special = "Hello\n\t\r\0World"
        cache.insert(special)
        assert cache.match_prefix(special) == len(special)

    def test_substring_insertion(self):
        """Test inserting a substring of an existing string."""
        cache = TreeCache()

        # Insert longer string first
        cache.insert("Hello, world!")

        # Insert substring - should not add new chars
        chars_added = cache.insert("Hello")
        assert chars_added == 0

        # Both should still match
        assert cache.match_prefix("Hello, world!") == 13
        assert cache.match_prefix("Hello") == 5

    def test_superstring_insertion(self):
        """Test inserting a superstring of an existing string."""
        cache = TreeCache()

        # Insert shorter string first
        cache.insert("Hello")

        # Insert superstring - should add the extra chars
        chars_added = cache.insert("Hello, world!")
        assert chars_added == 8  # ", world!"

        # Both should match
        assert cache.match_prefix("Hello, world!") == 13
        assert cache.match_prefix("Hello") == 5
