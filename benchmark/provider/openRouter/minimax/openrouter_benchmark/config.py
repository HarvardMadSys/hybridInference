"""Configure the MiniMax-M2.5 OpenRouter provider benchmark.

Central, immutable configuration for benchmarking MiniMax-M2.5 across all
OpenRouter providers. Every other module reads from here so the test matrix
and paths are defined exactly once.
"""

from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[5]

MODEL_ID: Final[str] = "MiniMaxAI/MiniMax-M2.5"
BASE_URL: Final[str] = "https://openrouter.ai/api/v1"
OPENROUTER_API_KEY_ENV: Final[str] = "OPENROUTER_API_KEY"

PROVIDERS: Final[list[str]] = [
    "deepinfra",
    "fireworks",
    "together-ai",
    "featherless",
    "chutes",
    "ollama",
]

PROVIDER_COLORS: Final[dict[str, str]] = {
    "deepinfra": "#1f77b4",
    "fireworks": "#ff7f0e",
    "together-ai": "#2ca02c",
    "featherless": "#d62728",
    "chutes": "#9467bd",
    "ollama": "#8c564b",
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
