"""The index freshness check: cheap, offline, and bound to the builder.

The check exists so that "is the committed index still current?" can be asked
on every push instead of only when someone pays for a rebuild. These tests pin
the two properties that make that true — it never reaches an embedder, and it
reads chunk identity through the same function the builder uses — because a
check that drifts from the builder is worse than no check at all.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from serving.rag import ingest
from serving.rag.config import RagSettings, load_rag_settings


def _settings(tmp_path, **overrides) -> RagSettings:
    corpus = tmp_path / "corpus"
    corpus.mkdir(exist_ok=True)
    base = replace(
        load_rag_settings(),
        corpus_dir=corpus,
        index_path=tmp_path / "docs_index.json",
        embedder_mode="hash",
        embed_model="hash-256",
    )
    return replace(base, **overrides) if overrides else base


def _write(settings: RagSettings, name: str, body: str) -> None:
    (settings.corpus_dir / name).write_text(body, encoding="utf-8")


def _build(settings: RagSettings) -> None:
    ingest.build_index(settings).save(settings.index_path)


def test_freshly_built_index_is_current(tmp_path, capsys):
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    _build(settings)

    assert ingest.check_index(settings) == 0
    assert "index current" in capsys.readouterr().out


def test_a_corpus_edit_drifts_the_index_and_names_the_chunk(tmp_path, capsys):
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    _build(settings)

    _write(settings, "models.md", "# Models\n\nglm-5.3 and glm-5.3-flash reason.\n")

    assert ingest.check_index(settings) == 1
    assert "models.md" in capsys.readouterr().err


def test_a_new_corpus_file_drifts_the_index(tmp_path):
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    _build(settings)

    _write(settings, "quickstart.md", "# Quick start\n\nGet a key.\n")

    assert ingest.check_index(settings) == 1


def test_changed_chunk_settings_drift_the_index(tmp_path):
    """The check must read the same knobs the builder reads.

    Chunk size and overlap decide chunk identity just as much as the corpus
    does. A check that hardcoded them would pass while the next rebuild
    produced something else — the exact shape of rot this guard is here to
    prevent.
    """
    settings = _settings(tmp_path, chunk_max_chars=400, chunk_overlap_chars=50)
    _write(settings, "models.md", "# Models\n\n" + ("paragraph text. " * 60 + "\n\n") * 4)
    _build(settings)
    assert ingest.check_index(settings) == 0

    resized = replace(settings, chunk_max_chars=200)
    assert ingest.check_index(resized) == 1


def test_an_offline_built_index_drifts_against_gateway_settings(tmp_path, capsys):
    """The case worth catching cheaply: hash vectors where bge-m3 was asked for.

    ``embed_model`` is deliberately not compared — the hash embedder hardcodes
    its own name, so that check would fire on every offline build. Mode is
    settings-derived on both sides, so it is exact.
    """
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    _build(settings)

    assert ingest.check_index(replace(settings, embedder_mode="gateway")) == 1
    assert "embedder mode drift" in capsys.readouterr().err


def test_missing_index_is_reported_apart_from_drift(tmp_path, capsys):
    """Exit 2, not 1: a caller must not answer an unreadable index by rebuilding.

    freeInference's RAG Index workflow branches on this exit code. Collapsing
    "unreadable" into "drifted" would turn a broken checkout into an expensive
    rebuild and a committed index built from who-knows-what.
    """
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")

    assert ingest.check_index(settings) == 2
    assert "index unreadable" in capsys.readouterr().err

    settings.index_path.write_text("{not json", encoding="utf-8")
    assert ingest.check_index(settings) == 2


def test_check_never_reaches_an_embedder(tmp_path, monkeypatch):
    """No gateway, no key, no network — that is the whole point of the check."""
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    _build(settings)

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("check_index built an embedder")

    monkeypatch.setattr(ingest, "build_ingest_embedder", explode)
    assert ingest.check_index(settings) == 0

    _write(settings, "models.md", "# Models\n\nchanged.\n")
    assert ingest.check_index(settings) == 1


def test_build_and_check_share_one_chunker(tmp_path, monkeypatch):
    """Both paths must go through ``chunk_corpus``; neither may re-implement it."""
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    _build(settings)

    calls: list[str] = []
    real = ingest.chunk_corpus

    def counting(config):
        calls.append("called")
        return real(config)

    monkeypatch.setattr(ingest, "chunk_corpus", counting)
    ingest.check_index(settings)
    ingest.build_index(settings)
    assert len(calls) == 2


def test_cli_check_flag_returns_the_exit_code(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    _build(settings)

    argv = [
        "--check",
        "--corpus",
        str(settings.corpus_dir),
        "--out",
        str(settings.index_path),
        "--embedder",
        "hash",
        "--embed-model",
        settings.embed_model,
    ]
    assert ingest.main(argv) == 0

    _write(settings, "models.md", "# Models\n\nchanged.\n")
    assert ingest.main(argv) == 1


def test_cli_check_does_not_write_the_index(tmp_path):
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    _build(settings)
    before = settings.index_path.read_bytes()

    _write(settings, "models.md", "# Models\n\nchanged.\n")
    ingest.main(
        [
            "--check",
            "--corpus",
            str(settings.corpus_dir),
            "--out",
            str(settings.index_path),
            "--embedder",
            "hash",
            "--embed-model",
            settings.embed_model,
        ]
    )

    assert settings.index_path.read_bytes() == before


def test_empty_corpus_still_raises_the_guidance_error(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(FileNotFoundError, match="RAG_CORPUS_DIR"):
        ingest.check_index(settings)
