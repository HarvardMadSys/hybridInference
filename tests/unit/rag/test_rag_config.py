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

from serving.config import app_config
from serving.rag import config as rag_config
from serving.rag.config import _repo_root_for, load_rag_settings
from tests.fixtures.app_config_store import row


def test_repo_root_survives_flattened_docker_layout():
    # /app/serving/rag/config.py has exactly 4 parents; parents[4] would raise.
    image_path = Path("/app/serving/rag/config.py")
    assert len(image_path.parents) == 4
    # Must not raise, and must fall back to the package dir.
    assert _repo_root_for(image_path) == Path("/app/serving/rag")


def test_repo_root_uses_parents4_in_repo_layout():
    repo_path = Path("/home/dev/hybridInference/apps/backend/serving/rag/config.py")
    assert _repo_root_for(repo_path) == Path("/home/dev/hybridInference")


def test_index_path_resolves_under_distributions_in_this_checkout(monkeypatch):
    # The index is distribution content: the default must resolve under
    # distributions/, never into the serving package. Since the FreeInference
    # overlay moved to its own repository (and the runnable example is skipped
    # by its EXAMPLE_OVERLAY marker), this checkout has no overlay, and the
    # resolver's documented no-overlay fallback is the distributions root.
    # Recomputed rather than read from the import-time constant: a host `.env`
    # naming DISTRIBUTION_CONFIG_PATH taints that constant with another tree.
    from serving.rag import config as rag_config

    monkeypatch.delenv("DISTRIBUTION_CONFIG_PATH", raising=False)
    assert rag_config._distribution_root() is None
    resolved = rag_config._default_index_path()
    assert resolved.parts[-4:] == (
        "distributions",
        "content",
        "rag",
        "docs_index.json",
    )
    assert "serving" not in resolved.parts


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


def test_ingest_cli_builds_settings_with_all_required_fields(tmp_path, monkeypatch):
    # Regression: RagSettings grew required api_base_url/api_key fields (#911)
    # and the ingest CLI's manual construction missed them, so every
    # `make rag-ingest` run died with a TypeError before reaching embedding.
    from serving.rag.ingest import main

    # No database here: the CLI would otherwise try to load stored settings.
    monkeypatch.setenv("DB_ENABLED", "false")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "hello.md").write_text("# Hello\n\nSome documentation text.\n")
    out = tmp_path / "index.json"
    assert main(["--corpus", str(corpus), "--out", str(out), "--embedder", "hash"]) == 0
    assert out.exists()


# --- "Empty uses ..." --------------------------------------------------------
#
# The console stores "" when an administrator clears a field and saves. For the
# settings whose description says what an empty value uses, that must mean the
# fallback: Path("") is the working directory, and an empty ingest key is no key.


def _stored(**values: str) -> None:
    app_config._apply({key: row(key, value) for key, value in values.items()}, boot=True)


def test_a_cleared_index_or_corpus_path_uses_the_overlays(monkeypatch):
    _stored(RAG_INDEX_PATH="", RAG_CORPUS_DIR="  ")

    settings = load_rag_settings()
    assert settings.index_path == rag_config.DEFAULT_INDEX_PATH
    assert settings.corpus_dir == rag_config.DEFAULT_CORPUS_DIR
    assert str(settings.index_path) != "."

    app_config.reset_state()
    monkeypatch.setenv("RAG_INDEX_PATH", "")  # an empty .env line, no row
    assert load_rag_settings().index_path == rag_config.DEFAULT_INDEX_PATH


def test_a_cleared_ingest_key_uses_local_api_key(monkeypatch):
    monkeypatch.setenv("LOCAL_API_KEY", "local-proxy-key")
    _stored(RAG_GATEWAY_API_KEY="")

    assert load_rag_settings().gateway_api_key == "local-proxy-key"


def test_a_value_still_wins_over_the_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_API_KEY", "local-proxy-key")
    _stored(
        RAG_GATEWAY_API_KEY="ingest-key",
        RAG_INDEX_PATH=str(tmp_path / "index.json"),
        RAG_CORPUS_DIR=str(tmp_path / "docs"),
    )

    settings = load_rag_settings()
    assert settings.gateway_api_key == "ingest-key"
    assert settings.index_path == tmp_path / "index.json"
    assert settings.corpus_dir == tmp_path / "docs"


def test_an_empty_assistant_key_still_disables_it(monkeypatch):
    """RAG_API_KEY promises no fallback: empty disables the assistant."""
    monkeypatch.setenv("LOCAL_API_KEY", "local-proxy-key")
    _stored(RAG_API_KEY="")

    assert load_rag_settings().api_key == ""
