"""Adapter implementations for specific serving engines.

This module contains the SGLang adapter used by the outsourcing engine. In
SGLang, accessing internal scheduler structures for metrics is discouraged.
Instead, this adapter supports reading operational metrics from SGLang's
Prometheus endpoint (e.g. http://localhost:30000/metrics) for observability
while optionally using a provided scheduler object for queue manipulation in
deployments where that is possible.
"""

import re
import time
from typing import Any

import requests

from routing.outsourcing.queue import WaitingQueueInterface
from routing.outsourcing.request import OutsourcingRequestInfo, RequestStatus


class SGLangWaitingQueueAdapter(WaitingQueueInterface):
    """Adapter for SGLang integration.

    Two complementary capabilities are provided:
    1) Queue access/manipulation (when a scheduler object is available).
    2) Metrics collection via SGLang's Prometheus endpoint (no scheduler needed).

    Example usage with metrics-only (recommended for production observability):
        queue_adapter = SGLangWaitingQueueAdapter(metrics_url="http://localhost:30000/metrics")

    Example usage with a scheduler object (for environments where it's allowed):
        from sglang import Scheduler
        scheduler = Scheduler(...)
        queue_adapter = SGLangWaitingQueueAdapter(scheduler=scheduler)
    """

    def __init__(
        self,
        metrics_url: str | None = None,
        http_timeout_s: float = 1.0,
    ):
        """Initialize the adapter.

        Args:
            scheduler: Optional SGLang scheduler instance with waiting queue access.
                If provided, queue operations (get_all_waiting/remove) will use it.
            metrics_url: Optional Prometheus metrics URL (defaults to
                "http://localhost:30000/metrics" if not provided).
            http_timeout_s: Timeout for HTTP requests to the metrics endpoint.
        """
        self.metrics_url = metrics_url or "http://localhost:30000/metrics"
        self.http_timeout_s = http_timeout_s
        
    def get_all_waiting(self) -> list[OutsourcingRequestInfo]:
        """Get snapshot of all waiting requests in queue order (FCFS).
        
        Returns:
            List of OutsourcingRequestInfo for all waiting requests
        """
        if self.scheduler is None:
            # Without a scheduler reference, we cannot enumerate individual
            # requests. Use metrics for aggregate visibility instead via
            # `get_metrics()`. Returning an empty list signals no actionable
            # per-request operations can be performed from here.
            return []

        # Access SGLang's waiting queue. The actual attribute name may vary.
        # Common patterns: waiting_queue, waiting_reqs, pending_requests
        waiting_reqs = getattr(self.scheduler, "waiting_queue", [])
        
        if not waiting_reqs:
            # Try alternative attribute names
            waiting_reqs = getattr(self.scheduler, "waiting_reqs", [])
        
        if not waiting_reqs:
            waiting_reqs = getattr(self.scheduler, "pending_requests", [])
        
        result = []
        current_time = time.time()
        
        for req in waiting_reqs:
            try:
                # Extract request information from SGLang request object
                request_id = getattr(req, "request_id", getattr(req, "rid", str(id(req))))
                
                # Timing information
                arrival_time = getattr(
                    req, "created_time", getattr(req, "arrival_time", current_time)
                )
                queue_time = current_time - arrival_time
                
                # Token information
                # SGLang typically stores tokens or token IDs
                prompt_tokens = getattr(req, "prompt_tokens", getattr(req, "input_ids", []))
                num_prompt_tokens = len(prompt_tokens) if hasattr(prompt_tokens, '__len__') else 0
                
                # Output tokens - check sampling params
                sampling_params = getattr(req, "sampling_params", None)
                if sampling_params:
                    num_output_tokens = getattr(
                        sampling_params, 
                        "max_new_tokens",
                        getattr(sampling_params, "max_tokens", 512),
                    )
                else:
                    num_output_tokens = 512  # Default fallback
                
                # Processed tokens
                num_processed_tokens = getattr(req, "num_processed_tokens", 0)
                
                # SLO information (if available in metadata)
                metadata = getattr(req, "metadata", {}) or {}
                prefill_slo = metadata.get("prefill_slo_seconds")
                total_slo = metadata.get("total_slo_seconds")
                
                # Status and prefill completion
                is_prefill_complete = getattr(req, "is_prefill_complete", False)
                
                # Pricing (can be customized per request via metadata)
                input_price = metadata.get("input_price_per_token", 1.25 / 1_000_000)
                output_price = metadata.get("output_price_per_token", 10.0 / 1_000_000)
                
                result.append(
                    OutsourcingRequestInfo(
                        request_id=request_id,
                        arrival_time=arrival_time,
                        queue_time=queue_time,
                        num_prompt_tokens=num_prompt_tokens,
                        num_output_tokens=num_output_tokens,
                        num_processed_tokens=num_processed_tokens,
                        prefill_slo_seconds=prefill_slo,
                        total_slo_seconds=total_slo,
                        status=RequestStatus.WAITING,
                        is_prefill_complete=is_prefill_complete,
                        input_price_per_token=input_price,
                        output_price_per_token=output_price,
                        metadata=metadata,
                    )
                )
            except Exception as e:
                # Log error but continue processing other requests
                print(
                    f"Warning: Failed to convert request {getattr(req, 'request_id', 'unknown')}: {e}"
                )
                continue
        
        return result
    
    def remove_requests(self, request_ids: set[str]) -> list[OutsourcingRequestInfo]:
        """Remove specified requests from the waiting queue.
        
        Args:
            request_ids: Set of request IDs to remove
            
        Returns:
            List of removed OutsourcingRequestInfo objects
        """
        if self.scheduler is None:
            # Without a scheduler we can't remove specific requests; the serving
            # process must handle cancellation/outsourcing via its own API.
            print("Warning: remove_requests is not supported without a scheduler reference")
            return []

        # Get current waiting requests before removal
        all_waiting = self.get_all_waiting()
        to_remove = [req for req in all_waiting if req.request_id in request_ids]
        
        # Access SGLang's waiting queue
        waiting_attr_name = None
        for attr in ["waiting_queue", "waiting_reqs", "pending_requests"]:
            if hasattr(self.scheduler, attr):
                waiting_attr_name = attr
                break
        
        if waiting_attr_name is None:
            print("Warning: Could not find waiting queue attribute in scheduler")
            return []
        
        # Filter out the requests to remove
        current_queue = getattr(self.scheduler, waiting_attr_name)
        
        # Create a set for faster lookup
        ids_to_remove = set(request_ids)
        
        # Filter the queue
        filtered_queue = []
        for req in current_queue:
            req_id = getattr(req, "request_id", getattr(req, "rid", str(id(req))))
            if req_id not in ids_to_remove:
                filtered_queue.append(req)
        
        # Update the scheduler's queue
        setattr(self.scheduler, waiting_attr_name, filtered_queue)
        
        return to_remove
    
    def get_length(self) -> int:
        """Current number of waiting requests.
        
        Returns:
            Number of requests in the waiting queue
        """
        # Prefer scheduler queue length if available
        if self.scheduler is not None:
            for attr in ["waiting_queue", "waiting_reqs", "pending_requests"]:
                queue = getattr(self.scheduler, attr, None)
                if queue is not None:
                    return len(queue)

        # Fall back to metrics endpoint if configured
        metrics = self.get_metrics(safe=True)
        if metrics and isinstance(metrics.get("request_queue"), (int, float)):
            try:
                return int(metrics["request_queue"])
            except Exception:
                pass
        return 0
    
    def peek(self) -> OutsourcingRequestInfo | None:
        """Look at the head of the queue without removing.
        
        Returns:
            OutsourcingRequestInfo for the first request, or None if empty
        """
        waiting = self.get_all_waiting()
        return waiting[0] if waiting else None

    # --------------------
    # Metrics integration
    # --------------------
    def get_metrics(self, safe: bool = False) -> dict:
        """Fetch key performance metrics from SGLang's Prometheus endpoint.

        Metrics include (when available):
          - throughput_tps: tokens per second
          - ttft_seconds: average time-to-first-token
          - inter_token_latency_seconds: average inter-token latency
          - hpu_memory_utilization: ratio (0-1) or percent (0-100)
          - request_queue: number of pending requests

        Args:
            safe: When True, suppress exceptions and return an empty dict on error.

        Returns:
            A dictionary of parsed metrics.
        """
        try:
            text = self._fetch_metrics_text()
            return self._parse_prometheus_metrics(text)
        except Exception as exc:  # noqa: BLE001 - we want to be resilient here
            if not safe:
                raise
            print(f"Warning: failed to fetch/parse SGLang metrics: {exc}")
            return {}

    def _fetch_metrics_text(self) -> str:
        resp = requests.get(self.metrics_url, timeout=self.http_timeout_s)
        resp.raise_for_status()
        return resp.text

    def _parse_prometheus_metrics(self, text: str) -> dict:
        # Simple Prometheus text parser sufficient for scalar metrics and
        # histogram/counter _sum/_count pairs.
        lines = [ln.strip() for ln in text.splitlines() if ln and not ln.startswith("#")]

        # Collect raw samples by metric name
        samples: dict[str, list[tuple[float, dict[str, str]]]] = {}
        metric_re = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(\{(?P<labels>[^}]*)\})?\s+(?P<value>[-+]?[0-9]*\.?[0-9]+([eE][-+]?[0-9]+)?)")

        def parse_labels(lbl: str) -> dict[str, str]:
            res: dict[str, str] = {}
            if not lbl:
                return res
            for part in lbl.split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    res[k.strip()] = v.strip().strip('"')
            return res

        for ln in lines:
            m = metric_re.match(ln)
            if not m:
                continue
            name = m.group("name")
            labels = parse_labels(m.group("labels") or "")
            try:
                value = float(m.group("value"))
            except ValueError:
                continue
            samples.setdefault(name, []).append((value, labels))

        out: dict[str, float] = {}

        # Helper to compute average from _sum/_count
        def avg_from_sum_count(prefix: str) -> float | None:
            s_list = samples.get(f"{prefix}_sum")
            c_list = samples.get(f"{prefix}_count")
            if not s_list or not c_list:
                return None
            s_val = sum(v for v, _ in s_list)
            c_val = sum(v for v, _ in c_list)
            if c_val > 0:
                return s_val / c_val
            return None

        # Throughput (tokens/sec)
        # Prefer explicit tokens_per_second gauges if present
        for key in [
            "tokens_per_second",
            "throughput_tokens_per_second",
            "sglang_tokens_per_second",
        ]:
            if key in samples:
                out["throughput_tps"] = sum(v for v, _ in samples[key])
                break

        # TTFT average (seconds)
        for prefix in [
            "time_to_first_token_seconds",
            "ttft_seconds",
            "sglang_ttft_seconds",
        ]:
            val = avg_from_sum_count(prefix)
            if val is not None:
                out["ttft_seconds"] = val
                break

        # Inter-token latency average (seconds)
        for prefix in [
            "inter_token_latency_seconds",
            "token_latency_seconds",
            "sglang_inter_token_latency_seconds",
        ]:
            val = avg_from_sum_count(prefix)
            if val is not None:
                out["inter_token_latency_seconds"] = val
                break

        # HPU memory utilization
        # Either a direct utilization metric, or ratio of used/total bytes
        for key in [
            "hpu_memory_utilization",
            "sglang_hpu_memory_utilization",
        ]:
            if key in samples:
                out["hpu_memory_utilization"] = sum(v for v, _ in samples[key])
                break

        if "hpu_memory_utilization" not in out:
            used = 0.0
            total = 0.0
            for key in ["hpu_memory_used_bytes", "sglang_hpu_memory_used_bytes"]:
                if key in samples:
                    used = sum(v for v, _ in samples[key])
                    break
            for key in ["hpu_memory_total_bytes", "sglang_hpu_memory_total_bytes"]:
                if key in samples:
                    total = sum(v for v, _ in samples[key])
                    break
            if total > 0:
                out["hpu_memory_utilization"] = used / total

        # Request queue length / pending requests
        for key in [
            "pending_requests",
            "request_queue_length",
            "waiting_requests",
            "sglang_pending_requests",
        ]:
            if key in samples:
                out["request_queue"] = sum(v for v, _ in samples[key])
                break

        return out
