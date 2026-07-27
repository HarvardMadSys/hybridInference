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


def test_index_path_resolves_to_overlay():
    # The FreeInference index is distribution content: in a repo checkout the
    # default must resolve into the overlay, never into the serving package.
    assert DEFAULT_INDEX_PATH.parts[-5:] == (
        "distributions",
        "freeinference",
        "content",
        "rag",
        "docs_index.json",
    )
    assert "serving" not in DEFAULT_INDEX_PATH.parts


def test_load_rag_settings_does_not_raise():
    settings = load_rag_settings()
    assert settings.index_path.name == "docs_index.json"
    assert settings.embedder_mode in ("gateway", "hash")


def test_ingest_cli_builds_settings_with_all_required_fields(tmp_path):
    # Regression: RagSettings grew required api_base_url/api_key fields (#911)
    # and the ingest CLI's manual construction missed them, so every
    # `make rag-ingest` run died with a TypeError before reaching embedding.
    from serving.rag.ingest import main

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "hello.md").write_text("# Hello\n\nSome documentation text.\n")
    out = tmp_path / "index.json"
    assert main(["--corpus", str(corpus), "--out", str(out), "--embedder", "hash"]) == 0
    assert out.exists()
