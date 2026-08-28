"""The index freshness check: cheap, offline, and bound to the builder.

The check exists so that "is the committed index still current?" can be asked
on every push instead of only when someone pays for a rebuild. These tests pin
the two properties that make that true — it never reaches an embedder, and it
reads chunk identity through the same function the builder uses — because a
check that drifts from the builder is worse than no check at all.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

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


# --- The surface the workflows actually consume -----------------------------
#
# freeInference's RAG Index workflow and its CI both run
# `python -m serving.rag.ingest --check` as a subprocess and branch on the
# process exit code. Everything above calls the functions directly, which
# leaves the translation from return value to exit status untested — and that
# translation is where an exit-2 fault silently becomes "drifted, go spend a
# rebuild". These run the real command line.


def _run_check(settings: RagSettings, **env_extra) -> subprocess.CompletedProcess:
    repo_root = Path(ingest.__file__).resolve().parents[4]
    env = {
        **os.environ,
        "PYTHONPATH": str(repo_root / "apps" / "backend"),
        "RAG_EMBEDDER": settings.embedder_mode,
        "RAG_CORPUS_DIR": str(settings.corpus_dir),
        "RAG_INDEX_PATH": str(settings.index_path),
        **env_extra,
    }
    return subprocess.run(
        [sys.executable, "-m", "serving.rag.ingest", "--check"],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo_root,
    )


def test_subprocess_exit_codes_are_the_contract(tmp_path):
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    _build(settings)
    assert _run_check(settings).returncode == 0

    _write(settings, "models.md", "# Models\n\nglm-5.3 and glm-5.3-flash reason.\n")
    assert _run_check(settings).returncode == 1

    settings.index_path.unlink()
    assert _run_check(settings).returncode == 2


def test_a_corpus_fault_exits_2_not_1(tmp_path):
    """A missing corpus must not read as "drifted".

    Exit 1 tells freeInference's workflow to spend a gateway rebuild, and tells
    a PR author their index is stale. Neither is true when the corpus path is
    simply wrong — which is exactly what a docs-tree reshuffle produces, and
    the RAG Index workflow watches that tree.
    """
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    _build(settings)

    missing = replace(settings, corpus_dir=tmp_path / "gone")
    assert _run_check(missing).returncode == 2

    (settings.corpus_dir / "binary.md").write_bytes(b"# Models\n\n\xff\xfe not utf-8\n")
    assert _run_check(settings).returncode == 2


@pytest.mark.parametrize(
    "payload",
    [
        '["not", "an", "object"]',
        '{"embedder_mode": "hash", "records": {"a": 1}}',
        '{"embedder_mode": "hash", "records": [{"id": "a"}]}',
        '{"embedder_mode": "hash"}',
    ],
    ids=["top-level-list", "records-not-a-list", "record-missing-text", "no-records-key"],
)
def test_an_index_of_the_wrong_shape_exits_2(tmp_path, payload):
    """Valid JSON that is not an index is unreadable, not drifted."""
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    settings.index_path.write_text(payload, encoding="utf-8")

    assert ingest.check_index(settings) == 2
    assert _run_check(settings).returncode == 2


def test_the_cli_reconstructs_every_settings_field(tmp_path, monkeypatch):
    """`main` hand-copies 14 fields into a fresh RagSettings.

    A field added to RagSettings and forgotten here is invisible until the run
    behaves differently from the module's own defaults — the silent-dropped-field
    shape this repository has been bitten by before (#911 was exactly that).
    """
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    _build(settings)

    seen: list[RagSettings] = []
    monkeypatch.setattr(ingest, "check_index", lambda config: seen.append(config) or 0)
    monkeypatch.setenv("RAG_CORPUS_DIR", str(settings.corpus_dir))
    monkeypatch.setenv("RAG_INDEX_PATH", str(settings.index_path))
    ingest.main(["--check"])

    assert seen, "main did not reach check_index"
    assert seen[0] == replace(
        load_rag_settings(), corpus_dir=settings.corpus_dir, index_path=settings.index_path
    )


def test_a_nested_corpus_is_walked(tmp_path):
    """The corpus is `rglob`ed. A Sphinx tree with subdirectories is normal."""
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    nested = settings.corpus_dir / "guides"
    nested.mkdir()
    (nested / "quickstart.md").write_text("# Quick start\n\nGet a key.\n", encoding="utf-8")
    _build(settings)
    assert ingest.check_index(settings) == 0

    (nested / "quickstart.md").write_text("# Quick start\n\nGet two keys.\n", encoding="utf-8")
    assert ingest.check_index(settings) == 1


def test_exit_1_is_never_silent(tmp_path, capsys):
    """Same ids, different order: the id-keyed explanation finds nothing to say.

    Two files sharing a basename collide, because chunk ids are built from
    `path.name`. Exiting 1 with no output sends the reader hunting for content
    that did not change.
    """
    settings = _settings(tmp_path)
    _write(settings, "a.md", "# A\n\nfirst.\n")
    (settings.corpus_dir / "sub").mkdir()
    (settings.corpus_dir / "sub" / "b.md").write_text("# B\n\nsecond.\n", encoding="utf-8")
    _build(settings)

    data = json.loads(settings.index_path.read_text(encoding="utf-8"))
    data["records"] = list(reversed(data["records"]))
    settings.index_path.write_text(json.dumps(data), encoding="utf-8")

    assert ingest.check_index(settings) == 1
    err = capsys.readouterr().err
    assert err.strip(), "exit 1 explained nothing"
    assert "different order" in err
