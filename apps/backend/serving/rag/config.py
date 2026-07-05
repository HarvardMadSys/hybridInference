"""Environment-driven configuration for the docs RAG assistant.

Everything is read from environment variables with sensible defaults so the
prototype can be pointed at a different corpus, index, or model without code
changes. Defaults resolve relative to the repo root so it works out of the box.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# apps/backend/serving/rag/config.py -> parents[4] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[4]

DEFAULT_CORPUS_DIR = _REPO_ROOT / "docs" / "free_inference" / "docs" / "source"
# Package-relative so the prebuilt index ships inside the Docker image (which
# COPYs apps/backend/serving/) and resolves identically in dev and container.
DEFAULT_INDEX_PATH = Path(__file__).resolve().parent / "prebuilt" / "docs_index.json"

# Valid embedder modes. "gateway" routes through the OpenAI-compatible gateway;
# "hash" is a deterministic offline fallback for dev/CI (poor retrieval quality).
EMBEDDER_MODES = ("gateway", "hash")


@dataclass(frozen=True)
class RagSettings:
    """Resolved RAG configuration for one process."""

    corpus_dir: Path
    index_path: Path
    embedder_mode: str
    embed_model: str
    chat_model: str
    top_k: int
    chunk_max_chars: int
    chunk_overlap_chars: int
    max_tokens: int
    temperature: float
    # Used only by the ingest CLI when embedder_mode == "gateway": the
    # OpenAI-compatible base URL + key of the gateway to embed against.
    gateway_base_url: str
    gateway_api_key: str


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def load_rag_settings() -> RagSettings:
    """Build :class:`RagSettings` from the environment."""
    mode = os.getenv("RAG_EMBEDDER", "gateway").strip().lower()
    if mode not in EMBEDDER_MODES:
        mode = "gateway"
    return RagSettings(
        corpus_dir=Path(os.getenv("RAG_CORPUS_DIR", str(DEFAULT_CORPUS_DIR))),
        index_path=Path(os.getenv("RAG_INDEX_PATH", str(DEFAULT_INDEX_PATH))),
        embedder_mode=mode,
        embed_model=os.getenv("RAG_EMBED_MODEL", "bge-m3"),
        chat_model=os.getenv("RAG_CHAT_MODEL", "qwen3.6-35b"),
        top_k=_int_env("RAG_TOP_K", 4),
        chunk_max_chars=_int_env("RAG_CHUNK_MAX_CHARS", 1200),
        chunk_overlap_chars=_int_env("RAG_CHUNK_OVERLAP_CHARS", 150),
        max_tokens=_int_env("RAG_MAX_TOKENS", 1024),
        temperature=_float_env("RAG_TEMPERATURE", 0.3),
        gateway_base_url=os.getenv("RAG_GATEWAY_BASE_URL", "https://freeinference.org/v1"),
        gateway_api_key=os.getenv("RAG_GATEWAY_API_KEY", os.getenv("LOCAL_API_KEY", "")),
    )
