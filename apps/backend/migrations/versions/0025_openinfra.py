"""OpenInfra: the GPU fleet and inference-engine metrics behind the public ``/openinfra`` page.

A reporter on each GPU host posts a snapshot every few seconds to
``POST /internal/openinfra/ingest`` (``serving.openinfra``). Three kinds of
table hold what it sends:

- live rows, one per host, GPU and engine, overwritten by every snapshot:
  ``openinfra_nodes``, ``openinfra_gpus`` (id ``<node id>:<index>``) and
  ``openinfra_engines`` (an SGLang or vLLM server, or one model of it);
- samples, at most one per host, GPU and engine in each 6-second bucket
  (``bucket`` is ``ts / 6000``, and the first sample of a bucket is kept):
  ``openinfra_cpu_samples``, ``openinfra_gpu_samples`` and
  ``openinfra_engine_samples``, plus ``openinfra_request_events``, the shape
  and latency of individual requests, without prompts, completions or
  addresses;
- rollups, per 20-minute and per hourly bucket (``grain`` in milliseconds):
  the bucket's last sample and the sums the served totals integrate,
  ``openinfra_cpu_rollups``, ``openinfra_gpu_rollups`` and
  ``openinfra_engine_rollups``, whose ``first_ts`` and ``last_ts`` bound the
  span its samples cover. The gateway's scheduler keeps them current.

Every time is milliseconds since the Unix epoch, as reporters and the page use
them. Metrics are double precision whatever their unit, so a reporter that
sends a fraction where a count was expected is stored, not refused. Nothing is
deleted.

Expand only: new tables that earlier code never reads.

Revision ID: 0025_openinfra
Revises: 0024_removed_provider_env_keys
Create Date: 2026-11-27
"""

from __future__ import annotations

from alembic import op

revision = "0025_openinfra"
down_revision = "0024_removed_provider_env_keys"
branch_labels = None
depends_on = None
compatibility = "expand"


#: Host metrics, on the live row and on each sample.
_CPU = (
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

#: Engine metrics, on the live row and on each sample.
_ENGINE = (
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

#: GPU metrics on each sample.
_GPU_SAMPLE = (
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

#: What a GPU rollup keeps: its bucket's last sample, memory as a percentage.
_GPU_ROLLUP = (
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

#: What an engine rollup keeps: its bucket's last sample under the names the
#: history document uses, and the four rate sums the served totals integrate.
_ENGINE_ROLLUP = (
    "running",
    "queued",
    "gen_throughput",
    "token_usage",
    "cache_hit_rate",
    "sm_util_pct",
    "used_tokens",
    "max_tokens",
    "active_tokens",
    "waiting_tokens",
    "prefill_tps",
    "sglang_util_pct",
    "spec_accept_length",
    "spec_accept_rate",
    "ttft_avg_s",
    "queue_avg_s",
    "prefill_avg_s",
    "tpot_avg_s",
    "e2e_latency_avg_s",
    "req_per_min",
    "cached_prefill_tps",
    "reqs_sum",
    "cached_tps_sum",
    "prefill_tps_sum",
    "decode_tps_sum",
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

#: The host metrics a CPU rollup keeps.
_CPU_ROLLUP = ("cpu_util_pct", "load1", "load5", "mem_used_mb", "mem_total_mb", "mem_available_mb")


def _metrics(names: tuple[str, ...]) -> str:
    return ", ".join(f"{name} DOUBLE PRECISION" for name in names)


#: The revision's statements, in order; each can run again (``IF NOT EXISTS``).
STATEMENTS: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS openinfra_nodes ("
    "id TEXT PRIMARY KEY, "
    "hostname TEXT NOT NULL, "
    "region TEXT, "
    "cluster TEXT, "
    "status TEXT NOT NULL DEFAULT 'unknown', "
    "last_seen_at BIGINT NOT NULL, "
    "created_at BIGINT NOT NULL, "
    "metadata JSONB, "
    f"{_metrics(_CPU)}"
    ")",
    "CREATE TABLE IF NOT EXISTS openinfra_gpus ("
    "id TEXT PRIMARY KEY, "
    "node_id TEXT NOT NULL, "
    "gpu_index INTEGER NOT NULL, "
    "uuid TEXT, "
    "name TEXT, "
    "driver TEXT, "
    "memory_total_mb DOUBLE PRECISION, "
    "utilization_pct DOUBLE PRECISION, "
    "memory_util_pct DOUBLE PRECISION, "
    "memory_used_mb DOUBLE PRECISION, "
    "temperature_c DOUBLE PRECISION, "
    "power_w DOUBLE PRECISION, "
    "power_limit_w DOUBLE PRECISION, "
    "fan_speed_pct DOUBLE PRECISION, "
    "clocks_sm_mhz DOUBLE PRECISION, "
    "clocks_mem_mhz DOUBLE PRECISION, "
    "ecc_errors DOUBLE PRECISION, "
    "pstate TEXT, "
    "processes JSONB, "
    "status TEXT NOT NULL DEFAULT 'unknown', "
    "last_seen_at BIGINT NOT NULL, "
    "tensor_active DOUBLE PRECISION, "
    "dram_active DOUBLE PRECISION, "
    "pcie_tx_bps DOUBLE PRECISION, "
    "pcie_rx_bps DOUBLE PRECISION, "
    "nvlink_tx_bps DOUBLE PRECISION, "
    "nvlink_rx_bps DOUBLE PRECISION"
    ")",
    "CREATE TABLE IF NOT EXISTS openinfra_engines ("
    "id TEXT PRIMARY KEY, "
    "node_id TEXT, "
    "name TEXT, "
    "model_name TEXT, "
    "endpoint TEXT, "
    "engine TEXT, "
    "status TEXT NOT NULL DEFAULT 'unknown', "
    "gpu_indexes TEXT, "
    f"{_metrics(_ENGINE)}, "
    "last_seen_at BIGINT NOT NULL, "
    "created_at BIGINT NOT NULL"
    ")",
    "CREATE TABLE IF NOT EXISTS openinfra_cpu_samples ("
    "node_id TEXT NOT NULL, "
    "bucket BIGINT NOT NULL, "
    "ts BIGINT NOT NULL, "
    f"{_metrics(_CPU)}, "
    "PRIMARY KEY (node_id, bucket)"
    ")",
    "CREATE TABLE IF NOT EXISTS openinfra_gpu_samples ("
    "gpu_id TEXT NOT NULL, "
    "bucket BIGINT NOT NULL, "
    "ts BIGINT NOT NULL, "
    f"{_metrics(_GPU_SAMPLE)}, "
    "PRIMARY KEY (gpu_id, bucket)"
    ")",
    "CREATE TABLE IF NOT EXISTS openinfra_engine_samples ("
    "engine_id TEXT NOT NULL, "
    "bucket BIGINT NOT NULL, "
    "ts BIGINT NOT NULL, "
    f"{_metrics(_ENGINE)}, "
    "PRIMARY KEY (engine_id, bucket)"
    ")",
    "CREATE TABLE IF NOT EXISTS openinfra_request_events ("
    "id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
    "engine_id TEXT NOT NULL, "
    "ts BIGINT NOT NULL, "
    "model TEXT, "
    "input_len BIGINT, "
    "output_len BIGINT, "
    "cache_len BIGINT, "
    "uncached_len BIGINT, "
    "ttft_s DOUBLE PRECISION, "
    "queue_s DOUBLE PRECISION, "
    "prefill_s DOUBLE PRECISION, "
    "tpot_s DOUBLE PRECISION, "
    "e2e_s DOUBLE PRECISION, "
    "decode_tps DOUBLE PRECISION, "
    "spec_accept_len DOUBLE PRECISION, "
    "spec_accept_rate DOUBLE PRECISION, "
    "batch_size BIGINT"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_openinfra_request_events_engine_ts "
    "ON openinfra_request_events (engine_id, ts DESC)",
    "CREATE TABLE IF NOT EXISTS openinfra_gpu_rollups ("
    "grain BIGINT NOT NULL, "
    "bucket_ts BIGINT NOT NULL, "
    "gpu_id TEXT NOT NULL, "
    "samples INTEGER NOT NULL, "
    f"{_metrics(_GPU_ROLLUP)}, "
    "PRIMARY KEY (grain, bucket_ts, gpu_id)"
    ")",
    "CREATE TABLE IF NOT EXISTS openinfra_engine_rollups ("
    "grain BIGINT NOT NULL, "
    "bucket_ts BIGINT NOT NULL, "
    "engine_id TEXT NOT NULL, "
    "samples INTEGER NOT NULL, "
    f"{_metrics(_ENGINE_ROLLUP)}, "
    "first_ts BIGINT, "
    "last_ts BIGINT, "
    "PRIMARY KEY (grain, bucket_ts, engine_id)"
    ")",
    "CREATE TABLE IF NOT EXISTS openinfra_cpu_rollups ("
    "grain BIGINT NOT NULL, "
    "bucket_ts BIGINT NOT NULL, "
    "node_id TEXT NOT NULL, "
    "samples INTEGER NOT NULL, "
    f"{_metrics(_CPU_ROLLUP)}, "
    "PRIMARY KEY (grain, bucket_ts, node_id)"
    ")",
)


def upgrade() -> None:
    """Add the OpenInfra tables."""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Keep the tables: unread by earlier code, they are harmless there.

    ``upgrade`` adds them with ``IF NOT EXISTS``, so going back up after a
    downgrade still works, with the metrics collected meanwhile.
    """
