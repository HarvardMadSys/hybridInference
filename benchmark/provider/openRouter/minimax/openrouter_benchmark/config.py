"""Configure the MiniMax-M2.5 OpenRouter provider benchmark.

Central, immutable configuration for benchmarking MiniMax-M2.5 across all
OpenRouter providers. Every other module reads from here so the test matrix
and paths are defined exactly once.
"""

from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[5]

MODEL_ID: Final[str] = "minimax/minimax-m2.5"
BASE_URL: Final[str] = "https://openrouter.ai/api/v1"
OPENROUTER_API_KEY_ENV: Final[str] = "OPENROUTER_API_KEY"

PROVIDERS: Final[list[str]] = [
    "DeepInfra",
    "Chutes",
    "Inceptron",
    "Parasail",
    "SambaNova",
    "Friendli",
    "Mara",
    "Minimax",
    "Novita",
    "SiliconFlow",
    "AkashML",
]

PROVIDER_COLORS: Final[dict[str, str]] = {
    "DeepInfra": "#1f77b4",
    "Chutes": "#ff7f0e",
    "Inceptron": "#2ca02c",
    "Parasail": "#d62728",
    "SambaNova": "#9467bd",
    "Friendli": "#8c564b",
    "Mara": "#e377c2",
    "Minimax": "#7f7f7f",
    "Novita": "#bcbd22",
    "SiliconFlow": "#17becf",
    "AkashML": "#aec7e8",
}

INPUT_LENS: Final[tuple[int, ...]] = (1024, 4096, 16384)
OUTPUT_LEN: Final[int] = 256
CONCURRENCIES: Final[tuple[int, ...]] = (1, 4, 16, 64)
NUM_REPEATS: Final[int] = 3

RESULTS_DIR: Final[Path] = REPO_ROOT / "results" / "minimax_m2_5_openrouter"
RAW_CSV: Final[Path] = RESULTS_DIR / "raw.csv"
STATE_DIR: Final[Path] = RESULTS_DIR / ".state"
PLOTS_DIR: Final[Path] = RESULTS_DIR / "plots"
SUMMARY_CSV: Final[Path] = RESULTS_DIR / "summary.csv"
