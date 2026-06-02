"""Configure the Qwen3.6 throughput benchmark.

Central, immutable configuration for the Qwen3.6-35B-A3B-FP8 throughput
benchmark. Every other module in `benchmark/` reads from here so the test
matrix and paths are defined exactly once.

Usage:
    from benchmark import config
    print(config.MODEL_REPO, config.MAX_MODEL_LEN)
"""

from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# Model
MODEL_REPO: Final[str] = "Qwen/Qwen3.6-35B-A3B-FP8"
MODEL_DIR: Final[Path] = Path("/netscratch/juncheng/models/Qwen3.6-35B-A3B-FP8")

# Server config (held constant across engines)
MAX_MODEL_LEN: Final[int] = 135168  # 132K — fits 128K input + output headroom
GPU_MEMORY_UTILIZATION: Final[float] = 0.90
TENSOR_PARALLEL_SIZE: Final[int] = 1
GPU_INDEX: Final[int] = 1

# Engines and their assigned ports
ENGINES: Final[tuple[str, ...]] = ("vllm", "sglang", "trtllm")
ENGINE_PORT: Final[dict[str, int]] = {
    "vllm": 8000,
    "sglang": 8001,
    "trtllm": 8002,
}

# Test matrix — prefill
PREFILL_INPUT_LENS: Final[tuple[int, ...]] = (1024, 4096, 16384, 65536, 131072)
PREFILL_OUTPUT_LEN: Final[int] = 1
PREFILL_CONCURRENCY: Final[int] = 1

# Test matrix — decode
DECODE_INPUT_LEN: Final[int] = 1024
DECODE_OUTPUT_LEN: Final[int] = 1024
DECODE_CONCURRENCIES: Final[tuple[int, ...]] = (1, 4, 16, 64, 128)

# Statistics
WARMUP_REQUESTS: Final[int] = 30
MEASUREMENT_REQUESTS: Final[int] = 200
MIN_MEASUREMENT_SECONDS: Final[int] = 60
NUM_REPEATS: Final[int] = 3

# Output paths
RESULTS_DIR: Final[Path] = REPO_ROOT / "results"
STATE_DIR: Final[Path] = RESULTS_DIR / ".state"
PLOTS_DIR: Final[Path] = RESULTS_DIR / "plots"
SUMMARY_CSV: Final[Path] = RESULTS_DIR / "summary.csv"
LESSONS_MD: Final[Path] = REPO_ROOT / "docs" / "internal" / "lessons.md"
