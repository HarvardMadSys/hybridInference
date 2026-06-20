"""Configure the DeepSeek-V4-Flash throughput benchmark.

Central, immutable configuration for the DeepSeek-V4-Flash (FP8) throughput
benchmark on 4x NVIDIA H200, served by vLLM with a 2-way tensor-parallel x
2-way pipeline-parallel layout (TP=2, PP=2 -> 4 GPUs).

Adapted from the minimax-m2.7 benchmark:
  * Single engine (vLLM); FP8 weights (sgl-project build, ~294 GB).
  * 4 GPUs via TP=2 x PP=2 rather than a single TP group. Weights are
    ~74 GB/GPU, leaving ample room for KV cache (DeepSeek MLA KV is compact).
  * vLLM runs natively in an isolated venv (no Docker on this shared host).

Usage:
    import config
    print(config.MODEL_REPO, config.TENSOR_PARALLEL_SIZE, config.PIPELINE_PARALLEL_SIZE)
"""

from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# Model — DeepSeek-V4-Flash, FP8 (sgl-project build, DeepseekV4ForCausalLM)
MODEL_REPO: Final[str] = "sgl-project/DeepSeek-V4-Flash-FP8"
MODEL_DIR: Final[Path] = Path("/netscratch/juncheng/models/DeepSeek-V4-Flash-FP8")
# Name the server registers and clients/genai-perf send (vLLM --served-model-name).
SERVED_NAME: Final[str] = "deepseek-v4-flash"

# Server config
MAX_MODEL_LEN: Final[int] = 40960  # 40K — fits 32K input + output headroom
GPU_MEMORY_UTILIZATION: Final[float] = 0.90
TENSOR_PARALLEL_SIZE: Final[int] = 2
PIPELINE_PARALLEL_SIZE: Final[int] = 2
GPU_DEVICES: Final[str] = "0,1,2,3"  # 4 GPUs: TP=2 x PP=2
# DeepSeek-V4 FlashMLA FP8 layout requires an FP8 KV cache (it rejects "auto").
KV_CACHE_DTYPE: Final[str] = "fp8"

# Native engine venv (Docker is unavailable on this shared host)
VENV_DIR: Final[Path] = Path("/netscratch/juncheng/venvs")

# Single engine for this run
ENGINES: Final[tuple[str, ...]] = ("vllm",)
ENGINE_PORT: Final[dict[str, int]] = {"vllm": 8000}

# Test matrix — prefill
PREFILL_INPUT_LENS: Final[tuple[int, ...]] = (1024, 4096, 16384, 32768)
PREFILL_OUTPUT_LEN: Final[int] = 1
PREFILL_CONCURRENCY: Final[int] = 1

# Test matrix — decode
DECODE_INPUT_LEN: Final[int] = 1024
DECODE_OUTPUT_LEN: Final[int] = 1024
DECODE_CONCURRENCIES: Final[tuple[int, ...]] = (1, 4, 16, 32)

# Statistics
WARMUP_REQUESTS: Final[int] = 30
MEASUREMENT_REQUESTS: Final[int] = 200
MIN_MEASUREMENT_SECONDS: Final[int] = 60
NUM_REPEATS: Final[int] = 1

# Server startup can be slow: first load reads ~294 GB of weights over NFS, and
# pipeline parallelism adds an extra warmup/graph-capture pass.
SERVER_HEALTH_TIMEOUT_S: Final[int] = 2400

# Output paths
RESULTS_DIR: Final[Path] = REPO_ROOT / "results"
STATE_DIR: Final[Path] = RESULTS_DIR / ".state"
PLOTS_DIR: Final[Path] = RESULTS_DIR / "plots"
SUMMARY_CSV: Final[Path] = RESULTS_DIR / "summary.csv"
