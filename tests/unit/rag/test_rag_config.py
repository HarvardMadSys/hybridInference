"""Regression tests for serving.rag.config path resolution.

`_REPO_ROOT` used to be computed as ``Path(__file__).resolve().parents[4]`` at
import time. That holds in the repo tree (apps/backend/serving/rag/config.py)
but the Docker image flattens the tree to ``/app/serving/rag/config.py`` — only
4 parents — so ``parents[4]`` raised ``IndexError`` and crashed the entire app
at import, taking staging down with a 502 (the module is imported transitively
by serving.servers.app). The repo root only feeds the ingest-time corpus
default, which serving never reads.
"""

from __future__ import annotations

from pathlib import Path

from serving.rag.config import DEFAULT_INDEX_PATH, _repo_root_for, load_rag_settings


def test_repo_root_survives_flattened_docker_layout():
    # /app/serving/rag/config.py has exactly 4 parents; parents[4] would raise.
    image_path = Path("/app/serving/rag/config.py")
    assert len(image_path.parents) == 4
    # Must not raise, and must fall back to the package dir.
    assert _repo_root_for(image_path) == Path("/app/serving/rag")


def test_repo_root_uses_parents4_in_repo_layout():
    repo_path = Path("/home/dev/hybridInference/apps/backend/serving/rag/config.py")
    assert _repo_root_for(repo_path) == Path("/home/dev/hybridInference")


def test_index_path_is_package_relative():
    # Serving resolves retrieval from the prebuilt index shipped in the package,
    # never from the repo root — so it works identically in dev and container.
    assert DEFAULT_INDEX_PATH.parent.name == "prebuilt"
    assert DEFAULT_INDEX_PATH.parent.parent.name == "rag"


def test_load_rag_settings_does_not_raise():
    settings = load_rag_settings()
    assert settings.index_path.name == "docs_index.json"
    assert settings.embedder_mode in ("gateway", "hash")
