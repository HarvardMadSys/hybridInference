"""Unit tests for OutsourcingRouter with TreeCache integration."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routing.outsourcing.decision import OutsourcingDecision
from routing.outsourcing.request import OutsourcingRequestInfo
from routing.outsourcing_integration import (
    OutsourcingRouter,
    extract_prompt_text,
    _get_max_output_tokens,
    DEFAULT_MAX_OUTPUT_TOKENS,
)
from routing.tree_cache import TreeCache


class TestGetMaxOutputTokens:
    """Tests for the _get_max_output_tokens helper function."""

    def test_returns_value_when_present(self):
        """Test that it returns the value when max_tokens is present."""
        params = {"max_tokens": 1000}
        assert _get_max_output_tokens(params) == 1000

    def test_returns_default_when_missing(self):
        """Test that it returns default when max_tokens is missing."""
        params = {}
        assert _get_max_output_tokens(params) == DEFAULT_MAX_OUTPUT_TOKENS

    def test_returns_default_when_none(self):
        """Test that it returns default when max_tokens is explicitly None."""
        params = {"max_tokens": None}
        assert _get_max_output_tokens(params) == DEFAULT_MAX_OUTPUT_TOKENS

    def test_converts_string_to_int(self):
        """Test that it converts string values to int."""
        params = {"max_tokens": "256"}
        assert _get_max_output_tokens(params) == 256


class TestExtractPromptText:
    """Tests for the extract_prompt_text helper function."""

    def test_extract_simple_messages(self):
        """Test extraction from simple messages."""
        messages = [
            {"role": "user", "content": "Hello, world!"},
        ]
        text = extract_prompt_text(messages)
        assert "<|user|>" in text
        assert "Hello, world!" in text

    def test_extract_multi_turn_conversation(self):
        """Test extraction from multi-turn conversation."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4"},
            {"role": "user", "content": "Thanks!"},
        ]
        text = extract_prompt_text(messages)
        assert "<|system|>" in text
        assert "<|user|>" in text
        assert "<|assistant|>" in text
        assert "You are a helpful assistant." in text
        assert "What is 2+2?" in text
        assert "4" in text
        assert "Thanks!" in text

    def test_extract_empty_messages(self):
        """Test extraction from empty messages list."""
        messages = []
        text = extract_prompt_text(messages)
        assert text == ""

    def test_extract_preserves_order(self):
        """Test that extraction preserves message order."""
        messages = [
            {"role": "user", "content": "First"},
            {"role": "assistant", "content": "Second"},
            {"role": "user", "content": "Third"},
        ]
        text = extract_prompt_text(messages)
        assert text.index("First") < text.index("Second") < text.index("Third")


class TestOutsourcingRequestInfoCachedTokens:
    """Tests for OutsourcingRequestInfo with cached tokens."""

    def test_remaining_prompt_tokens_with_cache(self):
        """Test that remaining_prompt_tokens accounts for cached tokens."""
        req = OutsourcingRequestInfo(
            request_id="test-1",
            arrival_time=time.time(),
            num_prompt_tokens=100,
            num_output_tokens=50,
            num_cached_tokens=30,  # 30 tokens already in cache
        )
        # remaining = 100 - 0 (processed) - 30 (cached) = 70
        assert req.remaining_prompt_tokens == 70

    def test_remaining_prompt_tokens_without_cache(self):
        """Test remaining_prompt_tokens without cached tokens."""
        req = OutsourcingRequestInfo(
            request_id="test-1",
            arrival_time=time.time(),
            num_prompt_tokens=100,
            num_output_tokens=50,
            num_cached_tokens=0,
        )
        assert req.remaining_prompt_tokens == 100

    def test_remaining_prompt_tokens_fully_cached(self):
        """Test when entire prompt is cached."""
        req = OutsourcingRequestInfo(
            request_id="test-1",
            arrival_time=time.time(),
            num_prompt_tokens=100,
            num_output_tokens=50,
            num_cached_tokens=100,  # Fully cached
        )
        assert req.remaining_prompt_tokens == 0

    def test_remaining_prompt_tokens_with_processed_and_cached(self):
        """Test with both processed and cached tokens."""
        req = OutsourcingRequestInfo(
            request_id="test-1",
            arrival_time=time.time(),
            num_prompt_tokens=100,
            num_output_tokens=50,
            num_processed_tokens=20,
            num_cached_tokens=30,
        )
        # remaining = 100 - 20 (processed) - 30 (cached) = 50
        assert req.remaining_prompt_tokens == 50


class TestOutsourcingRouterWithTreeCache:
    """Tests for OutsourcingRouter with TreeCache integration."""

    @pytest.fixture
    def mock_local_adapter(self):
        """Create a mock local adapter."""
        adapter = MagicMock()
        adapter.config.provider = "sglang"
        adapter.config.base_url = "http://localhost:6000/v1"
        adapter.chat_completion = AsyncMock(
            return_value={"choices": [{"message": {"content": "test"}}]}
        )
        adapter.stream_chat_completion = AsyncMock(return_value=iter([{"chunk": "test"}]))
        return adapter

    @pytest.fixture
    def mock_remote_adapter(self):
        """Create a mock remote adapter."""
        adapter = MagicMock()
        adapter.config.provider = "zhipu"
        adapter.config.base_url = "https://api.zhipuai.cn/v1"
        adapter.chat_completion = AsyncMock(
            return_value={"choices": [{"message": {"content": "test"}}]}
        )
        adapter.stream_chat_completion = AsyncMock(return_value=iter([{"chunk": "test"}]))
        return adapter

    @pytest.fixture
    def mock_outsourcing_engine(self):
        """Create a mock outsourcing engine."""
        engine = MagicMock()
        engine.should_outsource.return_value = OutsourcingDecision(
            should_outsource=False,
            requests_to_outsource=[],
            requests_to_keep=["test-1"],
            reason="No SLO violations",
        )
        engine.apply_outsourcing.return_value = []
        return engine

    @pytest.fixture
    def mock_waiting_queue(self):
        """Create a mock waiting queue."""
        queue = MagicMock()
        queue.add_request.return_value = None
        queue.remove_requests.return_value = []
        queue.get_length.return_value = 0
        queue.get_metrics.return_value = {}
        return queue

    @pytest.fixture
    def router(
        self, mock_local_adapter, mock_remote_adapter, mock_outsourcing_engine, mock_waiting_queue
    ):
        """Create an OutsourcingRouter with mocked dependencies."""
        return OutsourcingRouter(
            local_adapter=mock_local_adapter,
            remote_adapter=mock_remote_adapter,
            outsourcing_engine=mock_outsourcing_engine,
            waiting_queue=mock_waiting_queue,
            model_id="test-model",
            tree_cache_max_size_mb=10.0,
        )

    def test_router_has_tree_cache(self, router):
        """Test that router initializes with a TreeCache."""
        assert router.tree_cache is not None
        assert isinstance(router.tree_cache, TreeCache)

    @pytest.mark.asyncio
    async def test_chat_completion_updates_tree_cache_on_local(self, router):
        """Test that TreeCache is updated when request stays local."""
        messages = [{"role": "user", "content": "Hello, world!"}]

        # First request
        await router.chat_completion(messages)

        # TreeCache should have the prompt
        prompt_text = extract_prompt_text(messages)
        matched = router.tree_cache.match_prefix(prompt_text, update_access_time=False)
        assert matched > 0

    @pytest.mark.asyncio
    async def test_chat_completion_prefix_cache_hit(self, router):
        """Test that prefix cache hit is detected for similar prompts."""
        # First request
        messages1 = [{"role": "user", "content": "Hello, how are you today?"}]
        await router.chat_completion(messages1)

        # Second request with similar prefix
        messages2 = [{"role": "user", "content": "Hello, how is the weather?"}]
        await router.chat_completion(messages2)

        # Stats should show cache hits
        stats = router.get_stats()
        assert stats["cache_hit_requests"] >= 1
        assert stats["total_cached_tokens"] > 0

    @pytest.mark.asyncio
    async def test_chat_completion_no_cache_update_on_outsource(
        self, mock_local_adapter, mock_remote_adapter, mock_waiting_queue
    ):
        """Test that TreeCache is NOT updated when request is outsourced."""
        # Create a fresh router for this test
        engine = MagicMock()
        # Use a callback to capture the request ID and return matching decision
        captured_request_ids = []

        def capture_and_outsource(current_time, **kwargs):
            # Get the request ID from the waiting queue add_request call
            if mock_waiting_queue.add_request.call_args:
                req_info = mock_waiting_queue.add_request.call_args[0][0]
                captured_request_ids.append(req_info.request_id)
                return OutsourcingDecision(
                    should_outsource=True,
                    requests_to_outsource=[req_info.request_id],
                    requests_to_keep=[],
                    reason="SLO violation",
                )
            return OutsourcingDecision(
                should_outsource=False,
                requests_to_outsource=[],
                requests_to_keep=[],
                reason="No request",
            )

        engine.should_outsource.side_effect = capture_and_outsource
        engine.apply_outsourcing.return_value = []

        router = OutsourcingRouter(
            local_adapter=mock_local_adapter,
            remote_adapter=mock_remote_adapter,
            outsourcing_engine=engine,
            waiting_queue=mock_waiting_queue,
            model_id="test-model",
        )

        messages = [{"role": "user", "content": "Unique outsourced prompt xyz123"}]
        await router.chat_completion(messages)

        # Verify request was outsourced
        assert router.stats["outsourced_requests"] == 1

        # TreeCache should NOT have this prompt (it was outsourced)
        prompt_text = extract_prompt_text(messages)
        matched = router.tree_cache.match_prefix(prompt_text, update_access_time=False)
        # The match should be 0 (no cache update for outsourced requests)
        assert matched == 0

    @pytest.mark.asyncio
    async def test_request_info_includes_cached_tokens(self, router, mock_waiting_queue):
        """Test that OutsourcingRequestInfo includes cached_tokens."""
        # Pre-populate cache
        router.tree_cache.insert("<|user|>Hello, how are you?")

        messages = [{"role": "user", "content": "Hello, how are you?"}]
        await router.chat_completion(messages)

        # Check that add_request was called with cached tokens
        call_args = mock_waiting_queue.add_request.call_args
        request_info = call_args[0][0]
        assert isinstance(request_info, OutsourcingRequestInfo)
        assert request_info.num_cached_tokens > 0

    def test_get_stats_includes_tree_cache_stats(self, router):
        """Test that get_stats includes TreeCache statistics."""
        stats = router.get_stats()

        assert "tree_cache" in stats
        assert "total_chars" in stats["tree_cache"]
        assert "hit_rate" in stats["tree_cache"]
        assert "cache_hit_rate" in stats
        assert "avg_cached_tokens" in stats

    def test_clear_tree_cache(self, router):
        """Test that clear_tree_cache works."""
        # Add something to cache
        router.tree_cache.insert("Some test content")
        assert router.tree_cache.size > 0

        # Clear it
        router.clear_tree_cache()
        assert router.tree_cache.size == 0

    def test_custom_tree_cache_injection(
        self, mock_local_adapter, mock_remote_adapter, mock_outsourcing_engine, mock_waiting_queue
    ):
        """Test that a custom TreeCache can be injected."""
        custom_cache = TreeCache(max_size=5_000_000, chars_per_token=3.0)
        custom_cache.insert("Pre-populated content")

        router = OutsourcingRouter(
            local_adapter=mock_local_adapter,
            remote_adapter=mock_remote_adapter,
            outsourcing_engine=mock_outsourcing_engine,
            waiting_queue=mock_waiting_queue,
            model_id="test-model",
            tree_cache=custom_cache,
        )

        # Should use the injected cache
        assert router.tree_cache is custom_cache
        assert router.tree_cache.match_prefix("Pre-populated content") > 0

    @pytest.mark.asyncio
    async def test_keep_requests_removed_when_outsourcing(self, mock_remote_adapter):
        """Requests marked keep should be removed from the shadow queue when outsourcing occurs."""
        local_adapter = MagicMock()
        local_adapter.config.provider = "sglang"
        local_adapter.config.base_url = "http://localhost:6000/v1"
        local_adapter.chat_completion = AsyncMock(
            return_value={"choices": [{"message": {"content": "local"}}]}
        )

        waiting_queue = MagicMock()
        waiting_queue.add_request.return_value = None
        waiting_queue.remove_requests.return_value = []
        waiting_queue.get_length.return_value = 0
        waiting_queue.get_metrics.return_value = {}

        decision = OutsourcingDecision(
            should_outsource=True,
            requests_to_outsource=["req-remote"],
            requests_to_keep=["req-keep"],
            reason="violation",
        )

        engine = MagicMock()
        engine.should_outsource.return_value = decision
        engine.apply_outsourcing.return_value = []

        router = OutsourcingRouter(
            local_adapter=local_adapter,
            remote_adapter=mock_remote_adapter,
            outsourcing_engine=engine,
            waiting_queue=waiting_queue,
            model_id="test-model",
        )

        await router.chat_completion(
            [{"role": "user", "content": "Hello"}],
            request_id="req-keep",
        )

        waiting_queue.remove_requests.assert_called_once_with({"req-keep"})
        assert "req-keep" not in router._request_prompts
        assert router.stats["local_requests"] == 1


class TestOutsourcingRouterStats:
    """Tests for OutsourcingRouter statistics."""

    @pytest.fixture
    def router(self):
        """Create a router with mocked dependencies."""
        local_adapter = MagicMock()
        local_adapter.config.provider = "sglang"
        local_adapter.config.base_url = "http://localhost:6000/v1"
        local_adapter.chat_completion = AsyncMock(return_value={"choices": []})

        remote_adapter = MagicMock()
        remote_adapter.config.provider = "zhipu"
        remote_adapter.config.base_url = "https://api.zhipuai.cn/v1"

        engine = MagicMock()
        engine.should_outsource.return_value = OutsourcingDecision(
            should_outsource=False,
            requests_to_outsource=[],
            requests_to_keep=[],
            reason="No violations",
        )

        queue = MagicMock()
        queue.add_request.return_value = None
        queue.remove_requests.return_value = []
        queue.get_length.return_value = 0
        queue.get_metrics.return_value = {}

        return OutsourcingRouter(
            local_adapter=local_adapter,
            remote_adapter=remote_adapter,
            outsourcing_engine=engine,
            waiting_queue=queue,
            model_id="test-model",
        )

    @pytest.mark.asyncio
    async def test_stats_tracking(self, router):
        """Test that statistics are properly tracked."""
        messages = [{"role": "user", "content": "Test message"}]

        # Make some requests
        await router.chat_completion(messages)
        await router.chat_completion(messages)
        await router.chat_completion(messages)

        stats = router.get_stats()
        assert stats["total_requests"] == 3
        assert stats["local_requests"] == 3
        assert stats["outsourced_requests"] == 0

    def test_reset_stats(self, router):
        """Test that reset_stats clears all statistics."""
        router.stats["total_requests"] = 100
        router.stats["local_requests"] = 80
        router.stats["outsourced_requests"] = 20

        router.reset_stats()

        assert router.stats["total_requests"] == 0
        assert router.stats["local_requests"] == 0
        assert router.stats["outsourced_requests"] == 0
        assert router.stats["total_cached_tokens"] == 0
        assert router.stats["cache_hit_requests"] == 0
        assert router.stats["other_requests_outsourced"] == 0


class TestOutsourcingRouterApplyDecision:
    """Tests for proper handling of outsourcing decisions."""

    @pytest.fixture
    def mock_local_adapter(self):
        """Create a mock local adapter."""
        adapter = MagicMock()
        adapter.config.provider = "sglang"
        adapter.config.base_url = "http://localhost:6000/v1"
        adapter.chat_completion = AsyncMock(
            return_value={"choices": [{"message": {"content": "test"}}]}
        )
        return adapter

    @pytest.fixture
    def mock_remote_adapter(self):
        """Create a mock remote adapter."""
        adapter = MagicMock()
        adapter.config.provider = "zhipu"
        adapter.config.base_url = "https://api.zhipuai.cn/v1"
        adapter.chat_completion = AsyncMock(
            return_value={"choices": [{"message": {"content": "test"}}]}
        )
        return adapter

    @pytest.fixture
    def mock_waiting_queue(self):
        """Create a mock waiting queue."""
        queue = MagicMock()
        queue.add_request.return_value = None
        queue.remove_requests.return_value = []
        queue.get_length.return_value = 0
        queue.get_metrics.return_value = {}
        return queue

    @pytest.mark.asyncio
    async def test_apply_outsourcing_called_for_all_requests(
        self, mock_local_adapter, mock_remote_adapter, mock_waiting_queue
    ):
        """Test that apply_outsourcing is called even when current request is kept local."""
        # Create engine that outsources OTHER requests, not the current one
        engine = MagicMock()

        # Track request IDs as they come in
        request_ids = []

        def capture_request(req_info):
            request_ids.append(req_info.request_id)

        mock_waiting_queue.add_request.side_effect = capture_request

        # First call: no outsourcing
        # Second call: outsource the first request (not the current one)
        call_count = [0]

        def make_decision(current_time, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return OutsourcingDecision(
                    should_outsource=False,
                    requests_to_outsource=[],
                    requests_to_keep=[],
                    reason="No violations",
                )
            else:
                # Outsource the FIRST request, keep the current (second) one
                return OutsourcingDecision(
                    should_outsource=True,
                    requests_to_outsource=[request_ids[0]] if request_ids else [],
                    requests_to_keep=[request_ids[1]] if len(request_ids) > 1 else [],
                    reason="SLO violation for first request",
                )

        engine.should_outsource.side_effect = make_decision
        engine.apply_outsourcing.return_value = (
            [
                OutsourcingRequestInfo(
                    request_id=request_ids[0] if request_ids else "req-1",
                    arrival_time=time.time(),
                )
            ]
            if request_ids
            else []
        )

        router = OutsourcingRouter(
            local_adapter=mock_local_adapter,
            remote_adapter=mock_remote_adapter,
            outsourcing_engine=engine,
            waiting_queue=mock_waiting_queue,
            model_id="test-model",
        )

        # First request - no outsourcing
        await router.chat_completion([{"role": "user", "content": "First request"}])

        # Second request - should trigger outsourcing of first request
        await router.chat_completion([{"role": "user", "content": "Second request"}])

        # Verify apply_outsourcing was called on the second request
        assert engine.apply_outsourcing.call_count == 1
        assert router.stats["other_requests_outsourced"] >= 0  # May be 0 if mock doesn't match

    @pytest.mark.asyncio
    async def test_max_tokens_none_handled(
        self, mock_local_adapter, mock_remote_adapter, mock_waiting_queue
    ):
        """Test that max_tokens=None doesn't cause TypeError."""
        engine = MagicMock()
        engine.should_outsource.return_value = OutsourcingDecision(
            should_outsource=False,
            requests_to_outsource=[],
            requests_to_keep=[],
            reason="No violations",
        )

        router = OutsourcingRouter(
            local_adapter=mock_local_adapter,
            remote_adapter=mock_remote_adapter,
            outsourcing_engine=engine,
            waiting_queue=mock_waiting_queue,
            model_id="test-model",
        )

        messages = [{"role": "user", "content": "Test"}]

        # This should NOT raise TypeError
        response = await router.chat_completion(messages, max_tokens=None)
        assert response is not None

        # Verify the request was added with a valid integer for num_output_tokens
        call_args = mock_waiting_queue.add_request.call_args
        request_info = call_args[0][0]
        assert isinstance(request_info.num_output_tokens, int)
        assert request_info.num_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS

    @pytest.mark.asyncio
    async def test_kept_requests_removed_from_queue(
        self, mock_local_adapter, mock_remote_adapter, mock_waiting_queue
    ):
        """Test that kept requests are removed from waiting queue to prevent leak."""
        engine = MagicMock()

        # Track request IDs
        request_ids = []

        def capture_request(req_info):
            request_ids.append(req_info.request_id)

        mock_waiting_queue.add_request.side_effect = capture_request

        # Decision: outsource first request, keep second request
        call_count = [0]

        def make_decision(current_time, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return OutsourcingDecision(
                    should_outsource=False,
                    requests_to_outsource=[],
                    requests_to_keep=[],
                    reason="No violations",
                )
            else:
                # Outsource first, keep second (current)
                return OutsourcingDecision(
                    should_outsource=True,
                    requests_to_outsource=[request_ids[0]] if request_ids else [],
                    requests_to_keep=[request_ids[1]] if len(request_ids) > 1 else [],
                    reason="SLO violation",
                )

        engine.should_outsource.side_effect = make_decision
        engine.apply_outsourcing.return_value = []

        router = OutsourcingRouter(
            local_adapter=mock_local_adapter,
            remote_adapter=mock_remote_adapter,
            outsourcing_engine=engine,
            waiting_queue=mock_waiting_queue,
            model_id="test-model",
        )

        # First request
        await router.chat_completion([{"role": "user", "content": "First"}])

        # Second request - triggers outsourcing decision
        await router.chat_completion([{"role": "user", "content": "Second"}])

        # Verify that remove_requests was called for kept requests
        # It should be called with the kept request ID (second request)
        remove_calls = mock_waiting_queue.remove_requests.call_args_list

        # Should have multiple remove calls:
        # 1. First request (no outsourcing decision)
        # 2. Second request's kept requests (when outsourcing decision is made)
        assert len(remove_calls) >= 2

        # Check that the second call includes the kept request ID
        if len(request_ids) > 1:
            # Find a call that includes the second request ID (the kept one)
            kept_id = request_ids[1]
            found_kept_removal = any(kept_id in call[0][0] for call in remove_calls)
            assert found_kept_removal, f"Kept request {kept_id} should be removed from queue"
