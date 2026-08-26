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


def test_index_path_resolves_to_the_overlay_in_this_checkout():
    # The index is distribution content: the default must resolve into the
    # overlay, never into the serving package. The runnable example is skipped
    # by its EXAMPLE_OVERLAY marker, leaving the one real overlay unambiguous.
    assert DEFAULT_INDEX_PATH.parts[-4:] == (
        "freeinference",
        "content",
        "rag",
        "docs_index.json",
    )
    assert "serving" not in DEFAULT_INDEX_PATH.parts


def test_path_resolution_names_no_distribution():
    """Upstream must not reach into one deployment's overlay by name.

    Hardcoding it would work here and only here — a clone ships no overlay, and
    a second distribution would silently keep reading the first one's corpus.

    Scoped to the path constants: ``RAG_GATEWAY_BASE_URL`` still carries a
    default host, which belongs to the site-identity chain in #1060 rather than
    to this change.
    """
    from serving.rag import config as rag_config

    source = Path(rag_config.__file__).read_text()
    path_lines = [
        line
        for line in source.splitlines()
        if "distribution" in line.lower() or "content" in line.lower()
    ]
    assert path_lines, "the overlay resolution should be findable by these words"
    offenders = [line.strip() for line in path_lines if "freeinference" in line.lower()]
    assert not offenders, (
        f"resolve the overlay through _distribution_root() rather than naming one: {offenders}"
    )


def test_a_tree_with_no_overlay_resolves_to_nothing(tmp_path, monkeypatch):
    """A plain clone has no corpus, and RAG is expected to degrade to 503."""
    from serving.rag import config as rag_config

    monkeypatch.delenv("DISTRIBUTION_CONFIG_PATH", raising=False)
    monkeypatch.setattr(rag_config, "_REPO_ROOT", tmp_path)
    assert rag_config._distribution_root() is None


def test_an_ambiguous_tree_picks_neither(tmp_path, monkeypatch):
    """Two overlays and no manifest: guessing would serve the wrong docs."""
    from serving.rag import config as rag_config

    (tmp_path / "distributions" / "alpha").mkdir(parents=True)
    (tmp_path / "distributions" / "beta").mkdir(parents=True)
    monkeypatch.delenv("DISTRIBUTION_CONFIG_PATH", raising=False)
    monkeypatch.setattr(rag_config, "_REPO_ROOT", tmp_path)
    assert rag_config._distribution_root() is None


def test_a_declared_manifest_wins(tmp_path, monkeypatch):
    """A deployment that named its manifest has already answered the question."""
    from serving.rag import config as rag_config

    manifest = tmp_path / "distributions" / "acme" / "distribution.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("schema_version: 1\n")
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    assert rag_config._distribution_root() == manifest.parent


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
