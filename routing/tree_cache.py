"""Tree Cache for Hybrid Inference Routing.

This module implements a prefix tree (trie) cache that approximates the RadixCache
behavior of SGLang. It is used to estimate KV cache hits for outsourcing decisions.

Key Features:
- Character-level prefix matching: Find the longest cached prefix for any prompt
- LRU eviction: Automatically evict least recently used entries when cache is full
- Thread-safe: All operations are protected by a reentrant lock
- Token estimation: Convert character-based matches to approximate token counts

Design:
    This implementation uses a compressed trie (radix tree) where each node stores
    a string segment. Unlike a simple trie where each node stores one character,
    this allows for more memory-efficient storage of common prefixes.

    The key insight is that we need CHARACTER-LEVEL prefix matching, not just
    segment-level matching. When matching "Hello, there!" against a cached
    "Hello, world!", we should match "Hello, " (7 chars), not 0.

Usage in Nimbus:
    Each model's OutsourcingRouter maintains its own TreeCache instance.
    When a request arrives:
    1. Query match_prefix() to estimate cached tokens
    2. Use cached token count to adjust remaining_prompt_tokens
    3. After routing decision, call insert() if request stays local

Example:
    >>> cache = TreeCache(max_size=10_000_000)
    >>> cache.insert("Hello, how are you today?")
    >>> matched_chars = cache.match_prefix("Hello, how is the weather?")
    >>> print(matched_chars)  # 11 (matches "Hello, how ")
    >>> estimated_tokens = cache.estimate_cached_tokens("Hello, how is the weather?")
"""

from __future__ import annotations

import heapq
import threading
import time
from dataclasses import dataclass, field

# Default configuration (can be overridden in constructor)
DEFAULT_MAX_CACHE_SIZE = 100_000_000  # ~100MB of text
DEFAULT_CHARS_PER_TOKEN = 4.0  # Average chars per token (English approximation)


@dataclass
class TreeNode:
    """A node in the radix tree.

    Each node stores a text segment (edge label) and maintains:
    - Children: mapping from first character to child nodes
    - Parent: reference for tree traversal during eviction
    - Last access time: for LRU eviction policy

    In a radix tree, the edge from parent to child is labeled with a string.
    We store this string in the child node as 'text'.

    Attributes:
        text: The text segment (edge label) stored in this node
        children: Dictionary mapping first char of child's text to child TreeNode
        last_access_time: Unix timestamp of last access (for LRU)
        parent: Reference to parent node (None for root)
        node_id: Unique identifier for heap ordering
    """

    text: str
    children: dict[str, TreeNode] = field(default_factory=dict)
    last_access_time: float = field(default_factory=time.time)
    parent: TreeNode | None = None
    node_id: int = 0  # For stable heap ordering


class TreeCache:
    """Thread-safe radix tree cache for KV cache hit estimation.

    This cache approximates the RadixCache behavior of inference engines like SGLang.
    It stores prompt prefixes and allows efficient lookup of the longest matching
    prefix for incoming requests.

    The cache uses LRU (Least Recently Used) eviction when the total cached text
    exceeds max_size. Eviction removes leaf nodes first, working up the tree.

    Thread Safety:
        All public methods are thread-safe and can be called concurrently.

    Attributes:
        max_size: Maximum total characters to cache
        chars_per_token: Conversion factor for token estimation
    """

    def __init__(
        self,
        max_size: int = DEFAULT_MAX_CACHE_SIZE,
        chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
    ):
        """Initialize the tree cache.

        Args:
            max_size: Maximum total characters to store in the cache.
                When exceeded, LRU eviction is triggered.
            chars_per_token: Average characters per token for estimation.
                Default 4.0 is reasonable for English text.
        """
        self._max_size = max_size
        self._chars_per_token = chars_per_token

        # Initialize root node (empty text, no parent)
        self._root = TreeNode(text="", node_id=0)
        self._node_counter = 1  # For unique node IDs

        # Cache statistics
        self._total_chars = 0  # Total characters stored (excluding root)
        self._node_count = 1  # Total nodes (including root)

        # Statistics for monitoring
        self._stats = CacheStats()

        # Thread safety
        self._lock = threading.RLock()

    # =========================================================================
    # Public API
    # =========================================================================

    def insert(self, text: str) -> int:
        """Insert a text string into the cache.

        The text is inserted into the radix tree. If a prefix already exists,
        only the new suffix is added. Access times are updated for all
        traversed nodes.

        Args:
            text: The text to insert (typically a prompt)

        Returns:
            Number of new characters added to the cache

        Example:
            >>> cache = TreeCache()
            >>> cache.insert("Hello world")
            11
            >>> cache.insert("Hello there")  # "Hello " already cached
            5
        """
        if not text:
            return 0

        with self._lock:
            chars_added = self._insert_internal(text)
            self._stats.inserts += 1
            self._stats.chars_inserted += chars_added

            # Trigger eviction if needed
            if self._total_chars > self._max_size:
                self._evict_until_under_limit()

            return chars_added

    def match_prefix(self, text: str, update_access_time: bool = True) -> int:
        """Find the longest matching prefix in the cache.

        Traverses the tree to find how many leading characters of the input
        text are already cached. This performs CHARACTER-LEVEL matching,
        not just node-level matching.

        Args:
            text: The text to match against the cache
            update_access_time: If True, update access times for matched nodes
                (affects LRU eviction). Set False for read-only queries.

        Returns:
            Number of characters matched (0 if no match)

        Example:
            >>> cache = TreeCache()
            >>> cache.insert("Hello world, how are you?")
            >>> cache.match_prefix("Hello world, what's up?")
            13  # Matches "Hello world, "
        """
        if not text:
            return 0

        with self._lock:
            matched = self._match_prefix_internal(text, update_access_time)
            self._stats.queries += 1
            if matched > 0:
                self._stats.hits += 1
                self._stats.chars_matched += matched
            else:
                self._stats.misses += 1
            return matched

    def estimate_cached_tokens(self, text: str, update_access_time: bool = True) -> int:
        """Estimate the number of tokens that would hit the cache.

        This is a convenience method that converts character matches to
        approximate token counts using the chars_per_token ratio.

        Args:
            text: The text to check for cache hits
            update_access_time: If True, update access times for matched nodes

        Returns:
            Estimated number of cached tokens (integer)

        Note:
            This is an approximation. For accurate token counts, use a
            proper tokenizer. The default ratio (4 chars/token) is reasonable
            for English but may be less accurate for other languages.
        """
        matched_chars = self.match_prefix(text, update_access_time)
        return int(matched_chars / self._chars_per_token)

    def clear(self) -> None:
        """Clear all cached entries.

        Resets the cache to its initial empty state.
        """
        with self._lock:
            old_node_count = self._node_count
            self._root = TreeNode(text="", node_id=0)
            self._node_counter = 1
            self._total_chars = 0
            self._node_count = 1
            self._stats.evictions += old_node_count - 1

    def get_stats(self) -> dict:
        """Get cache statistics for monitoring.

        Returns:
            Dictionary containing cache metrics.
        """
        with self._lock:
            queries = self._stats.queries
            hits = self._stats.hits
            return {
                "total_chars": self._total_chars,
                "node_count": self._node_count,
                "max_size": self._max_size,
                "utilization": self._total_chars / self._max_size if self._max_size > 0 else 0.0,
                "queries": queries,
                "hits": hits,
                "misses": self._stats.misses,
                "hit_rate": hits / queries if queries > 0 else 0.0,
                "inserts": self._stats.inserts,
                "chars_inserted": self._stats.chars_inserted,
                "chars_matched": self._stats.chars_matched,
                "evictions": self._stats.evictions,
            }

    @property
    def size(self) -> int:
        """Current number of characters in the cache."""
        with self._lock:
            return self._total_chars

    @property
    def utilization(self) -> float:
        """Cache utilization as a fraction (0.0 to 1.0)."""
        with self._lock:
            return self._total_chars / self._max_size if self._max_size > 0 else 0.0

    # =========================================================================
    # Internal Methods - Radix Tree Operations
    # =========================================================================

    def _insert_internal(self, text: str) -> int:
        """Internal insert implementation using radix tree logic.

        This implements a compressed trie (radix tree) insertion.
        """
        if not text:
            return 0

        time_now = time.time()
        chars_added = 0
        current_node = self._root
        current_node.last_access_time = time_now
        pos = 0  # Position in text being inserted

        while pos < len(text):
            # Find a child that shares a prefix with remaining text
            remaining = text[pos:]
            first_char = remaining[0]

            if first_char not in current_node.children:
                # No matching child - create new leaf node with remaining text
                new_node = self._create_node(remaining, current_node, time_now)
                current_node.children[first_char] = new_node
                chars_added += len(remaining)
                break

            child = current_node.children[first_char]
            child.last_access_time = time_now

            # Find common prefix length between child.text and remaining text
            common_len = self._common_prefix_length(child.text, remaining)

            if common_len == len(child.text):
                # Child's text is fully matched - continue down the tree
                pos += common_len
                current_node = child
            elif common_len == len(remaining):
                # Remaining text is fully matched but child has more
                # Need to split: insert new internal node
                # Before: parent -> child("abcdef")
                # After:  parent -> new_internal("abc") -> child("def")
                #                                       -> (end of inserted text)
                self._split_node(child, common_len, time_now)
                chars_added += 0  # No new chars, just restructured
                break
            else:
                # Partial match - need to split and add new branch
                # Before: parent -> child("abcdef")
                # Insert: "abcxyz"
                # After:  parent -> new_internal("abc") -> child("def")
                #                                       -> new_leaf("xyz")
                new_internal = self._split_node(child, common_len, time_now)
                new_suffix = remaining[common_len:]
                new_leaf = self._create_node(new_suffix, new_internal, time_now)
                new_internal.children[new_suffix[0]] = new_leaf
                chars_added += len(new_suffix)
                break

        return chars_added

    def _match_prefix_internal(self, text: str, update_access_time: bool) -> int:
        """Internal prefix match implementation.

        Returns the number of characters matched.
        """
        if not text:
            return 0

        time_now = time.time() if update_access_time else None
        current_node = self._root
        if update_access_time:
            current_node.last_access_time = time_now

        matched = 0
        pos = 0

        while pos < len(text):
            remaining = text[pos:]
            first_char = remaining[0]

            if first_char not in current_node.children:
                # No matching child
                break

            child = current_node.children[first_char]

            # Find common prefix length
            common_len = self._common_prefix_length(child.text, remaining)

            if common_len == 0:
                # No match (shouldn't happen if first_char matched)
                break

            # Update access time if requested
            if update_access_time:
                child.last_access_time = time_now

            matched += common_len
            pos += common_len

            if common_len < len(child.text):
                # Partial match within this node - stop here
                break

            # Full match of child's text - continue to next level
            current_node = child

        return matched

    def _create_node(self, text: str, parent: TreeNode, time_now: float) -> TreeNode:
        """Create a new tree node."""
        node = TreeNode(
            text=text,
            last_access_time=time_now,
            parent=parent,
            node_id=self._node_counter,
        )
        self._node_counter += 1
        self._node_count += 1
        self._total_chars += len(text)
        return node

    def _split_node(self, node: TreeNode, split_pos: int, time_now: float) -> TreeNode:
        """Split a node at the given position.

        Before: parent -> node("abcdef")
        After:  parent -> new_internal("abc") -> node("def")

        Returns the new internal node.
        """
        if split_pos <= 0 or split_pos >= len(node.text):
            raise ValueError(
                f"Invalid split position {split_pos} for text of length {len(node.text)}"
            )

        parent = node.parent
        prefix = node.text[:split_pos]
        suffix = node.text[split_pos:]

        # Create new internal node with the prefix
        new_internal = TreeNode(
            text=prefix,
            last_access_time=time_now,
            parent=parent,
            node_id=self._node_counter,
        )
        self._node_counter += 1
        self._node_count += 1
        # Note: total_chars doesn't change because prefix + suffix = original

        # Update parent's children
        if parent is not None:
            first_char = prefix[0] if prefix else node.text[0]
            parent.children[first_char] = new_internal

        # Update original node
        node.text = suffix
        node.parent = new_internal
        new_internal.children[suffix[0]] = node

        # Adjust total_chars: we added prefix chars (new_internal) but the
        # original node now has fewer chars
        # Net change: +len(prefix) for new_internal, node changed from old_text_len to len(suffix)
        # But prefix + suffix = old_text_len, so net change = +len(prefix) - (old_text_len - len(suffix))
        # = +len(prefix) - len(prefix) = 0
        # Actually we need to add the new internal node's chars
        self._total_chars += len(prefix)
        # And the original node now has suffix instead of full text
        # But we already counted the full text, so we need to not double count
        # Actually let's recalculate: original had old_text_len chars counted
        # Now: new_internal has prefix chars, node has suffix chars
        # prefix + suffix = old_text_len, so total is same
        # But we did self._total_chars += len(prefix) above, which is wrong
        # We should not add anything because the total chars is preserved
        self._total_chars -= len(prefix)  # Undo the addition above

        return new_internal

    def _common_prefix_length(self, s1: str, s2: str) -> int:
        """Find the length of common prefix between two strings."""
        min_len = min(len(s1), len(s2))
        for i in range(min_len):
            if s1[i] != s2[i]:
                return i
        return min_len

    # =========================================================================
    # Eviction
    # =========================================================================

    def _evict_until_under_limit(self) -> None:
        """Evict LRU leaf nodes until cache is under max_size."""
        leaves = self._collect_leaves()

        if not leaves:
            return

        # Build a min-heap based on (access_time, node_id, node)
        heap: list[tuple[float, int, TreeNode]] = [
            (node.last_access_time, node.node_id, node) for node in leaves
        ]
        heapq.heapify(heap)

        evicted_count = 0

        while self._total_chars > self._max_size and heap:
            _, _, lru_node = heapq.heappop(heap)

            # Skip if node was already removed or is no longer a leaf
            if lru_node.parent is None and lru_node is not self._root:
                continue
            if lru_node.children:
                continue

            parent = lru_node.parent
            if parent is None:
                # Don't delete root
                continue

            # Delete the leaf node
            self._delete_leaf_node(lru_node)
            evicted_count += 1

            # If parent became a leaf (and is not root), add it to heap
            if parent is not self._root and not parent.children:
                heapq.heappush(heap, (parent.last_access_time, parent.node_id, parent))

        self._stats.evictions += evicted_count

    def _delete_leaf_node(self, node: TreeNode) -> None:
        """Delete a leaf node from the tree."""
        if node.children:
            raise ValueError("Cannot delete a non-leaf node")

        parent = node.parent
        if parent is not None:
            # Find and remove from parent's children
            first_char = node.text[0] if node.text else ""
            if first_char in parent.children and parent.children[first_char] is node:
                del parent.children[first_char]

        self._total_chars -= len(node.text)
        self._node_count -= 1

    def _collect_leaves(self) -> list[TreeNode]:
        """Collect all leaf nodes (excluding root)."""
        leaves = []
        stack = [self._root]

        while stack:
            node = stack.pop()
            if not node.children and node is not self._root:
                leaves.append(node)
            else:
                stack.extend(node.children.values())

        return leaves


@dataclass
class CacheStats:
    """Statistics for cache monitoring."""

    queries: int = 0
    hits: int = 0
    misses: int = 0
    inserts: int = 0
    chars_inserted: int = 0
    chars_matched: int = 0
    evictions: int = 0


# =============================================================================
# Convenience Functions
# =============================================================================


def create_tree_cache(
    max_size_mb: float = 100.0,
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
) -> TreeCache:
    """Create a TreeCache with size specified in megabytes.

    Args:
        max_size_mb: Maximum cache size in megabytes
        chars_per_token: Average characters per token

    Returns:
        Configured TreeCache instance
    """
    max_size_chars = int(max_size_mb * 1_000_000)
    return TreeCache(
        max_size=max_size_chars,
        chars_per_token=chars_per_token,
    )
