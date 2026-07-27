"""Environment-driven configuration for the docs RAG assistant.

Everything is read from environment variables with sensible defaults so the
prototype can be pointed at a different corpus, index, or model without code
changes. Defaults resolve relative to the repo root so it works out of the box.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _repo_root_for(path: Path) -> Path:
    """Best-effort repo root, used only for the ingest-time corpus default.

    In the repo tree this file is ``apps/backend/serving/rag/config.py`` so the
    repo root is ``parents[4]``. Inside the Docker image the tree is flattened to
    ``/app/serving/rag/config.py`` (only 4 parents), where ``parents[4]`` raises
    ``IndexError`` and crashes the whole app at import. Fall back to the package
    dir there: in the container the index default resolves via the app root
    instead (see :func:`_default_index_path`), so the fallback never affects
    requests, and the offline ingest CLI always runs from the repo tree.
    """
    parents = path.parents
    return parents[4] if len(parents) > 4 else path.parent


_REPO_ROOT = _repo_root_for(Path(__file__).resolve())

DEFAULT_CORPUS_DIR = (
    _REPO_ROOT / "distributions" / "freeinference" / "content" / "docs" / "docs" / "source"
)
# The FreeInference prebuilt index is distribution content: it lives in the
# overlay, not in the neutral serving package (and is no longer baked into the
# backend image).
_OVERLAY_INDEX = Path("distributions") / "freeinference" / "content" / "rag" / "docs_index.json"


def _default_index_path() -> Path:
    """Resolve the overlay index for both the repo tree and the container.

    In a repository checkout the overlay sits at the repo root. In the Docker
    image the code tree is flattened to ``/app`` and the overlay is
    bind-mounted at ``/app/distributions`` (deploy/docker/docker-compose.yml),
    so the app root — two levels above this package — is the base there.
    Prefer whichever candidate exists; fall back to the repo-root candidate so
    error messages point at the canonical location. A missing index is not
    fatal: the ``/v1/rag/chat`` handler degrades to 503 until one is supplied.
    ``RAG_INDEX_PATH`` overrides this default entirely.
    """
    app_root = Path(__file__).resolve().parents[2]
    candidates = (_REPO_ROOT / _OVERLAY_INDEX, app_root / _OVERLAY_INDEX)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


DEFAULT_INDEX_PATH = _default_index_path()

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
    # Serving-time: the gateway the /v1/rag/chat handler calls (as a user, over
    # HTTP) for query embedding and answer generation, so those requests flow
    # through the standard handlers and get logged / cost / quota-tracked. A
    # self-call to the gateway's own address; ``api_key`` must be a valid user
    # API key (unset => the endpoint returns 503).
    api_base_url: str
    api_key: str
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
        api_base_url=os.getenv("RAG_API_BASE_URL", "http://localhost:8080/v1"),
        api_key=os.getenv("RAG_API_KEY", ""),
        gateway_base_url=os.getenv("RAG_GATEWAY_BASE_URL", "https://freeinference.org/v1"),
        gateway_api_key=os.getenv("RAG_GATEWAY_API_KEY", os.getenv("LOCAL_API_KEY", "")),
    )
