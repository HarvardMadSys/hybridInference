"""Benchmark runner for load testing."""

import asyncio
import time
from collections.abc import AsyncGenerator, Callable

from serving.base import LLMRequest

from .metrics import MetricsCollector


class BenchmarkRunner:
    """Runs benchmarks with configurable request patterns."""

    def __init__(self, requests_per_second: float = 1.0):
        """
        Args:
            requests_per_second: Rate of request generation
        """
        self.rps = requests_per_second
        self.request_interval = 1.0 / requests_per_second

    async def run(
        self,
        request_generator: AsyncGenerator[LLMRequest, None],
        request_handler: Callable,
        duration_limit: float | None = None,
        max_requests: int | None = None,
    ) -> MetricsCollector:
        """
        Run the benchmark.

        Args:
            request_generator: Generator that yields LLM requests
            request_handler: Async function to handle each request
            duration_limit: Optional time limit in seconds
            max_requests: Optional limit on number of requests

        Returns:
            MetricsCollector with results
        """
        start_time = time.time()
        request_count = 0
        metrics = MetricsCollector()

        async for request in request_generator:
            # Check limits
            if duration_limit and (time.time() - start_time) > duration_limit:
                print(f"Duration limit reached ({duration_limit}s)")
                break

            if max_requests and request_count >= max_requests:
                print(f"Request limit reached ({max_requests})")
                break

            # Rate limiting
            if request_count > 0:
                await asyncio.sleep(self.request_interval)

            # Send request asynchronously
            request_id = f"req_{request_count}"
            _ = asyncio.create_task(  # noqa: RUF006
                self._handle_request_with_metrics(request, request_id, request_handler, metrics)
            )

            request_count += 1

        # Wait a bit for remaining requests to complete
        await asyncio.sleep(5.0)
        return metrics

    async def _handle_request_with_metrics(
        self,
        request: LLMRequest,
        request_id: str,
        request_handler: Callable,
        metrics: MetricsCollector,
    ):
        """Handle a single request and collect metrics."""
        start_time = time.time()

        # Record request start
        metrics.record_request_start(
            request_id=request_id,
            provider="unknown",  # Will be updated by handler
            model=request.model,
            prompt_tokens=len(request.prompt.split()),  # Rough estimate
        )

        try:
            response = await request_handler(request, request_id)

            # Record success
            latency_ms = (time.time() - start_time) * 1000
            metrics.record_request_end(
                request_id=request_id,
                completion_tokens=len(response.text.split()) if hasattr(response, "text") else 0,
                request_latency_ms=latency_ms,
                success=True,
            )

        except Exception as e:
            # Record failure
            latency_ms = (time.time() - start_time) * 1000
            metrics.record_request_end(
                request_id=request_id,
                completion_tokens=0,
                request_latency_ms=latency_ms,
                success=False,
                error=str(e),
            )
