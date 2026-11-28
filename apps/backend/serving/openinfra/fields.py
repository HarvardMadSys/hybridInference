"""The metric names the ingest body, the tables and the documents share.

Each tuple is in column order. The tables were created with these names by
revision ``0025_openinfra``, which keeps its own copy, as history must.
"""

from __future__ import annotations

#: Host metrics: ``node.cpu`` in a snapshot, the live node row, each sample.
CPU_FIELDS: tuple[str, ...] = (
    "cpu_util_pct",
    "load1",
    "load5",
    "load15",
    "mem_used_mb",
    "mem_total_mb",
    "mem_available_mb",
    "swap_used_mb",
    "procs_running",
    "procs_total",
    "cpu_count",
)

#: Numeric GPU fields of a snapshot and of the live GPU row.
GPU_NUMBERS: tuple[str, ...] = (
    "memory_total_mb",
    "memory_used_mb",
    "utilization_pct",
    "memory_util_pct",
    "temperature_c",
    "power_w",
    "power_limit_w",
    "fan_speed_pct",
    "clocks_sm_mhz",
    "clocks_mem_mhz",
    "ecc_errors",
    "tensor_active",
    "dram_active",
    "pcie_tx_bps",
    "pcie_rx_bps",
    "nvlink_tx_bps",
    "nvlink_rx_bps",
)

#: What each GPU sample keeps.
GPU_SAMPLE_FIELDS: tuple[str, ...] = (
    "utilization_pct",
    "memory_used_mb",
    "memory_total_mb",
    "temperature_c",
    "power_w",
    "memory_util_pct",
    "clocks_sm_mhz",
    "tensor_active",
    "dram_active",
    "pcie_tx_bps",
    "pcie_rx_bps",
    "nvlink_tx_bps",
    "nvlink_rx_bps",
)

#: What a GPU rollup keeps: memory as a percentage, then the sample's values.
GPU_ROLLUP_FIELDS: tuple[str, ...] = (
    "utilization_pct",
    "memory_pct",
    "memory_util_pct",
    "temperature_c",
    "power_w",
    "clocks_sm_mhz",
    "tensor_active",
    "dram_active",
    "pcie_tx_bps",
    "pcie_rx_bps",
    "nvlink_tx_bps",
    "nvlink_rx_bps",
)

#: Engine metrics: ``sglang[].metrics`` in a snapshot, the live engine row, each sample.
ENGINE_METRICS: tuple[str, ...] = (
    "num_running_reqs",
    "num_queue_reqs",
    "num_used_tokens",
    "token_usage",
    "gen_throughput",
    "cache_hit_rate",
    "prompt_tokens_total",
    "generation_tokens_total",
    "ttft_avg_s",
    "queue_avg_s",
    "prefill_avg_s",
    "e2e_latency_avg_s",
    "tpot_avg_s",
    "sm_util_pct",
    "max_total_num_tokens",
    "num_active_tokens",
    "num_waiting_uncached_tokens",
    "prefill_tps",
    "spec_accept_length",
    "sglang_util_pct",
    "max_running_requests",
    "weight_gb",
    "kv_cache_gb",
    "spec_accept_rate",
    "req_per_min",
    "cached_prefill_tps",
    "chunked_prefill_avg_s",
    "hicache_host_used",
    "hicache_host_total",
    "device_cached_tps",
    "host_cached_tps",
    "loadback_tps",
    "evict_tps",
    "backup_tps",
    "prompt_tokens_avg",
    "uncached_prompt_tokens_avg",
    "generation_tokens_avg",
    "http_error_per_min",
    "retracts",
)

#: The history document's name for each engine metric it plots, in its order.
#: Rollups store the values under these names.
ENGINE_HISTORY: tuple[tuple[str, str], ...] = (
    ("running", "num_running_reqs"),
    ("queued", "num_queue_reqs"),
    ("gen_throughput", "gen_throughput"),
    ("token_usage", "token_usage"),
    ("cache_hit_rate", "cache_hit_rate"),
    ("sm_util_pct", "sm_util_pct"),
    ("used_tokens", "num_used_tokens"),
    ("max_tokens", "max_total_num_tokens"),
    ("active_tokens", "num_active_tokens"),
    ("waiting_tokens", "num_waiting_uncached_tokens"),
    ("prefill_tps", "prefill_tps"),
    ("sglang_util_pct", "sglang_util_pct"),
    ("spec_accept_length", "spec_accept_length"),
    ("spec_accept_rate", "spec_accept_rate"),
    ("ttft_avg_s", "ttft_avg_s"),
    ("queue_avg_s", "queue_avg_s"),
    ("prefill_avg_s", "prefill_avg_s"),
    ("tpot_avg_s", "tpot_avg_s"),
    ("e2e_latency_avg_s", "e2e_latency_avg_s"),
    ("req_per_min", "req_per_min"),
    ("cached_prefill_tps", "cached_prefill_tps"),
    ("chunked_prefill_avg_s", "chunked_prefill_avg_s"),
    ("hicache_host_used", "hicache_host_used"),
    ("hicache_host_total", "hicache_host_total"),
    ("device_cached_tps", "device_cached_tps"),
    ("host_cached_tps", "host_cached_tps"),
    ("loadback_tps", "loadback_tps"),
    ("evict_tps", "evict_tps"),
    ("backup_tps", "backup_tps"),
    ("prompt_tokens_avg", "prompt_tokens_avg"),
    ("uncached_prompt_tokens_avg", "uncached_prompt_tokens_avg"),
    ("generation_tokens_avg", "generation_tokens_avg"),
    ("http_error_per_min", "http_error_per_min"),
    ("retracts", "retracts"),
)

#: The rate sums an engine rollup adds to its values, for the served totals.
ENGINE_SUMS: tuple[str, ...] = ("reqs_sum", "cached_tps_sum", "prefill_tps_sum", "decode_tps_sum")

#: What a CPU rollup and the CPU history keep.
CPU_HISTORY: tuple[str, ...] = (
    "cpu_util_pct",
    "load1",
    "load5",
    "mem_used_mb",
    "mem_total_mb",
    "mem_available_mb",
)

#: Fields of a request event, after the engine id and time.
REQUEST_FIELDS: tuple[str, ...] = (
    "model",
    "input_len",
    "output_len",
    "cache_len",
    "uncached_len",
    "ttft_s",
    "queue_s",
    "prefill_s",
    "tpot_s",
    "e2e_s",
    "decode_tps",
    "spec_accept_len",
    "spec_accept_rate",
    "batch_size",
)

#: Request-event fields that are token counts, stored as integers.
REQUEST_COUNTS: frozenset[str] = frozenset(
    {"input_len", "output_len", "cache_len", "uncached_len", "batch_size"}
)
