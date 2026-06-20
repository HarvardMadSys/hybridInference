"""Configure the MiniMax-M2.7 throughput benchmark.

Central, immutable configuration for the MiniMax-M2.7 (native FP8) throughput
benchmark on 2x NVIDIA H200. Every other module reads from here so the test
matrix, GPU pinning, and paths are defined exactly once.

Adapted from the qwen3.6 benchmark with three structural changes:
  * Tensor parallel size 2 across GPUs 2 and 3 (GPU 1 deliberately avoided;
    2 and 3 share NUMA node 1 and are NVLink-connected).
  * The model ships natively in FP8 (block-quantized e4m3, ~230 GB), so it
    leaves only ~13 GB/GPU for KV cache. We run an FP8 KV cache and trim the
    sweep ranges accordingly (no 128K prefill, no 128-way concurrency).
  * Engines run as native processes in isolated venvs (no Docker on this
    shared host).

Usage:
    import config
    print(config.MODEL_REPO, config.MAX_MODEL_LEN)
"""

from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# Model — official MiniMax-M2.7, native FP8 (float8_e4m3fn, weight_block 128x128)
MODEL_REPO: Final[str] = "MiniMaxAI/MiniMax-M2.7"
MODEL_DIR: Final[Path] = Path("/netscratch/juncheng/models/MiniMax-M2.7-FP8")

# Server config (held constant across engines)
# Weights are ~115 GB/GPU at TP=2; at GMU 0.92 that leaves ~13 GB/GPU for the
# KV pool. An FP8 KV cache (~124 KiB/token) keeps the sweep ranges below feasible.
MAX_MODEL_LEN: Final[int] = 40960  # 40K — fits 32K input + output headroom
GPU_MEMORY_UTILIZATION: Final[float] = 0.92
TENSOR_PARALLEL_SIZE: Final[int] = 2
GPU_DEVICES: Final[str] = "2,3"  # avoid GPU 1; 2+3 share NUMA node 1, NVLink (NV6)
KV_CACHE_DTYPE: Final[str] = "fp8"  # vLLM: "fp8"; sglang maps to "fp8_e4m3"

# Native engine venvs (Docker is unavailable on this shared host)
VENV_DIR: Final[Path] = Path("/netscratch/juncheng/venvs")

# Engines and their assigned ports
ENGINES: Final[tuple[str, ...]] = ("vllm", "sglang")
ENGINE_PORT: Final[dict[str, int]] = {
    "vllm": 8000,
    "sglang": 8001,
}

# Test matrix — prefill (trimmed from qwen3.6's 128K ceiling to fit the KV pool)
PREFILL_INPUT_LENS: Final[tuple[int, ...]] = (1024, 4096, 16384, 32768)
PREFILL_OUTPUT_LEN: Final[int] = 1
PREFILL_CONCURRENCY: Final[int] = 1

# Test matrix — decode (concurrency capped at 32: 32 x 2K tokens fits FP8 KV)
DECODE_INPUT_LEN: Final[int] = 1024
DECODE_OUTPUT_LEN: Final[int] = 1024
DECODE_CONCURRENCIES: Final[tuple[int, ...]] = (1, 4, 16, 32)

# Statistics
WARMUP_REQUESTS: Final[int] = 30
MEASUREMENT_REQUESTS: Final[int] = 200
MIN_MEASUREMENT_SECONDS: Final[int] = 60
NUM_REPEATS: Final[int] = 1

# Server startup can be slow: first load reads ~230 GB of weights over NFS.
SERVER_HEALTH_TIMEOUT_S: Final[int] = 2400

# Output paths
RESULTS_DIR: Final[Path] = REPO_ROOT / "results"
STATE_DIR: Final[Path] = RESULTS_DIR / ".state"
PLOTS_DIR: Final[Path] = RESULTS_DIR / "plots"
SUMMARY_CSV: Final[Path] = RESULTS_DIR / "summary.csv"
