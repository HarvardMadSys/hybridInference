"""The index freshness check: cheap, offline, and bound to the builder.

The check exists so that "is the committed index still current?" can be asked
on every push instead of only when someone pays for a rebuild. These tests pin
the two properties that make that true — it never reaches an embedder, and it
reads chunk identity through the same function the builder uses — because a
check that drifts from the builder is worse than no check at all.
"""

from __future__ import annotations

import json
import math
import os
import random
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from serving.rag import ingest
from serving.rag.config import EMBEDDER_MODES, RagSettings, load_rag_settings
from serving.rag.embedder import build_ingest_embedder, recorded_model_name
from serving.rag.pipeline import format_context, sources_payload
from serving.rag.store import VectorStore, index_document_problem


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


# --- "current" must mean servable -------------------------------------------
#
# `--check` returning 0 makes freeInference's workflow skip the rebuild. If the
# committed index is one `VectorStore.load` rejects, that verdict leaves the
# gateway holding a file it cannot parse and nothing scheduled to replace it.


def _mangle(settings: RagSettings, edit) -> None:
    data = json.loads(settings.index_path.read_text(encoding="utf-8"))
    edit(data)
    settings.index_path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("field", ["source", "title", "embedding"])
def test_a_record_missing_a_field_load_needs_is_unreadable(tmp_path, field):
    """id and text are what the check compares; they are not what load reads."""
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    _build(settings)
    _mangle(settings, lambda d: d["records"][0].pop(field))

    assert ingest.check_index(settings) == 2
    assert _run_check(settings).returncode == 2
    with pytest.raises((KeyError, ValueError, TypeError)):
        VectorStore.load(settings.index_path)


@pytest.mark.parametrize(
    "edit, says",
    [
        (lambda d: d.update(dim=d["dim"] + 1), "!= declared dim"),
        (lambda d: d.pop("dim"), "declared dim is None"),
        (lambda d: d.update(dim=0), "declared dim is 0"),
        (lambda d: d.update(dim="1024"), "declared dim is '1024'"),
        (lambda d: d["records"][0].update(embedding="not-a-list"), "non-list embedding"),
    ],
    ids=["dim-disagrees", "dim-missing", "dim-zero", "dim-not-an-int", "embedding-not-a-list"],
)
def test_an_index_load_would_reject_is_unreadable(tmp_path, capsys, edit, says):
    """The message is asserted, not just the code.

    A bad ``dim`` is caught twice over — the per-record length comparison
    reports it even with the explicit check removed — so only the wording
    distinguishes "this index declares no dimension" from "record 7 is the
    wrong length". Pinning the code alone would leave that guard untested and
    free to rot.
    """
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    _build(settings)
    _mangle(settings, edit)

    assert ingest.check_index(settings) == 2
    assert says in capsys.readouterr().err
    assert _run_check(settings).returncode == 2


def test_every_index_the_check_calls_current_can_be_searched(tmp_path):
    """The invariant, stated as what actually has to work.

    "Loadable" was too weak: an index of strings, nulls, NaN or booleans loads
    fine and only fails — or worse, silently misranks — at query time. What
    ``current`` has to mean is that a query can be scored against this index.
    """
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    (settings.corpus_dir / "quickstart.md").write_text("# Quick\n\nKey.\n", encoding="utf-8")
    _build(settings)

    assert ingest.check_index(settings) == 0
    store = VectorStore.load(settings.index_path)
    assert len(store.records) == len(ingest.chunk_corpus(settings))

    results = store.search([0.1] * store.dim, top_k=len(store.records))
    assert len(results) == len(store.records)
    assert all(math.isfinite(score) for _, score in results)


@pytest.mark.parametrize(
    "embedding, says",
    [
        (lambda dim: [0.0] * dim, "zero-norm"),
        (lambda dim: [-0.0] * dim, "zero-norm"),
        (lambda dim: [1e308] * dim, "squared norm that overflows"),
        (lambda dim: [10**400] * dim, "too large to score"),
    ],
    ids=["all-zero", "negative-zero", "norm-overflow", "int-too-large"],
)
def test_a_vector_no_query_could_use_is_unreadable(tmp_path, capsys, embedding, says):
    """Finite components are not enough; the vector has to be usable.

    ``[1e308] * dim`` has every component finite and scores every query ``nan``
    once the squares are summed. ``[0.0] * dim`` scores a finite 0.0 and is
    simply unreachable forever — no crash, no signal, just a chunk the index
    claims to hold and can never return.
    """
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    _build(settings)
    dim = json.loads(settings.index_path.read_text(encoding="utf-8"))["dim"]
    _mangle(settings, lambda d: d["records"][0].update(embedding=embedding(dim)))

    assert ingest.check_index(settings) == 2
    assert says in capsys.readouterr().err
    assert _run_check(settings).returncode == 2
    with pytest.raises(ValueError):
        VectorStore.load(settings.index_path)


@pytest.mark.parametrize(
    "value, says",
    [
        ("x", "non-numeric"),
        (None, "non-numeric"),
        ([0.1], "non-numeric"),
        ({"a": 1}, "non-numeric"),
        (True, "non-numeric"),
        (float("nan"), "non-finite"),
        (float("inf"), "non-finite"),
        (float("-inf"), "non-finite"),
    ],
    ids=["string", "null", "nested-list", "object", "bool", "nan", "inf", "-inf"],
)
def test_an_embedding_a_query_cannot_be_scored_against_is_unreadable(tmp_path, capsys, value, says):
    """Shape was checked, contents were not — and contents are what scoring uses.

    ``[True] * dim`` is the nastiest of these: it loads, it scores, and it
    returns a confident 1.0 against anything. NaN and Infinity are next: no
    exception at all, every score ``nan``, ranking silently meaningless.
    """
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    _build(settings)
    dim = json.loads(settings.index_path.read_text(encoding="utf-8"))["dim"]
    _mangle(settings, lambda d: d["records"][0].update(embedding=[value] * dim))

    assert ingest.check_index(settings) == 2
    assert says in capsys.readouterr().err
    assert _run_check(settings).returncode == 2
    with pytest.raises(ValueError):
        VectorStore.load(settings.index_path)


# --- a model change is drift, not a detail ----------------------------------


def test_a_gateway_embed_model_change_is_drift(tmp_path, capsys):
    """Vectors from different models are not comparable, so this must rebuild.

    In gateway mode the builder records `settings.embed_model` verbatim, so an
    index built for one model and settings asking for another is stale even
    though every chunk id and text still matches.
    """
    settings = _settings(tmp_path, embedder_mode="gateway", embed_model="bge-m3")
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    # Build offline, then relabel as a gateway/bge-m3 index so only the model
    # question is under test.
    _build(replace(settings, embedder_mode="hash", embed_model="hash-256"))
    _mangle(settings, lambda d: d.update(embedder_mode="gateway", embed_model="bge-m3"))
    assert ingest.check_index(settings) == 0

    assert ingest.check_index(replace(settings, embed_model="bge-m4")) == 1
    assert "embed model drift" in capsys.readouterr().err


@pytest.mark.parametrize("mode", sorted(EMBEDDER_MODES))
def test_recorded_model_name_matches_the_embedder_that_gets_built(mode):
    """The check's expectation and the builder's behaviour must not drift.

    `check_index` cannot construct a GatewayHTTPEmbedder to ask — that opens an
    HTTP client — so it asks `recorded_model_name` instead. This pins the two
    together for every mode that exists.
    """
    embedder = build_ingest_embedder(
        mode=mode,
        embed_model="bge-m3",
        gateway_base_url="http://localhost:9/v1",
        gateway_api_key="unused",
    )
    assert embedder.model == recorded_model_name(mode=mode, embed_model="bge-m3")
    assert embedder.mode == mode


# --- The invariant, searched for rather than enumerated ----------------------
#
# Three rounds of review found three gaps here, each of the same kind: the
# check validated the shapes someone had thought of. Enumerating cases is
# always a step behind. What follows searches the space the validator ACCEPTS
# for a document that breaks the thing "current" is supposed to promise.
#
# Deterministic on purpose — a seeded generator, no new dependency, the same
# corpus every run — so a failure is reproducible rather than a CI flake.

_HOSTILE_SCALARS = [
    "ok",
    "",
    "\x00",
    "🙂",
    0,
    1,
    -1,
    True,
    False,
    None,
    [],
    [0.1],
    {},
    {"a": 1},
]

_HOSTILE_COMPONENTS = [
    0.0,
    -0.0,
    1.0,
    -1.0,
    1e-320,
    5e-324,  # subnormals
    1e308,
    -1e308,
    5e307,
    1.7976931348623157e308,  # finite, squares overflow
    float("nan"),
    float("inf"),
    float("-inf"),
    10**200,
    10**400,
    -(10**400),  # unbounded Python ints
    True,
    False,
    "0.1",
    None,
    [0.1],
    {"a": 1},
]


def _hostile_documents(base: dict, seed: int, count: int):
    """Yield documents built by pushing hostile values into a valid index."""
    rng = random.Random(seed)
    fields = ["id", "text", "source", "title", "embedding"]
    for _ in range(count):
        doc = json.loads(json.dumps(base))
        index = rng.randrange(len(doc["records"]))
        record = doc["records"][index]
        field = rng.choice(fields)
        # Deleting matters as much as replacing: a first version of this
        # generator only ever substituted values, so it never produced a record
        # missing `source`, and could not see that guard removed.
        kind = rng.choice(["replace", "replace", "replace", "delete"])
        if kind == "delete":
            record.pop(field, None)
        elif field == "embedding" and rng.random() < 0.15:
            # Whole-vector rewrites: 1-3 poisoned positions cannot produce an
            # all-zero embedding, and an all-zero one is scoreable, finite, and
            # permanently unretrievable.
            record["embedding"] = rng.choice(
                [
                    [0.0] * len(record["embedding"]),
                    [-0.0] * len(record["embedding"]),
                    [],
                    record["embedding"][:-1],
                    record["embedding"] + [0.1],
                ]
            )
        elif field == "embedding" and rng.random() < 0.7:
            embedding = list(record["embedding"])
            for _ in range(rng.randint(1, 3)):
                embedding[rng.randrange(len(embedding))] = rng.choice(_HOSTILE_COMPONENTS)
            record["embedding"] = embedding
        elif field == "embedding":
            record["embedding"] = rng.choice(_HOSTILE_SCALARS)
        else:
            record[field] = rng.choice(_HOSTILE_SCALARS)
        if rng.random() < 0.10:
            doc["records"][index] = rng.choice(_HOSTILE_SCALARS)
        if rng.random() < 0.10:
            doc["records"] = rng.choice([[], {}, "records", None, doc["records"]])
        if rng.random() < 0.15:
            doc["dim"] = rng.choice([0, -1, None, "1024", 1.5, True, doc["dim"] + 1])
        yield doc


def test_anything_the_validator_accepts_can_actually_be_served(tmp_path):
    """`index_document_problem(d) is None` must imply the index works.

    "Works" is load, score every record finitely, and render a sources payload
    — the three things a `/v1/rag/chat` request does. Each of the three bugs
    this file has guarded against would fail here without being named: an
    unloadable record, a `nan` score, a non-string `source` reaching the
    payload.
    """
    settings = _settings(tmp_path)
    _write(settings, "models.md", "# Models\n\nglm-5.3 reasons.\n")
    (settings.corpus_dir / "quickstart.md").write_text("# Quick\n\nKey.\n", encoding="utf-8")
    _build(settings)
    base = json.loads(settings.index_path.read_text(encoding="utf-8"))

    accepted = 0
    for doc in _hostile_documents(base, seed=20260828, count=600):
        if index_document_problem(doc) is not None:
            continue
        accepted += 1
        settings.index_path.write_text(json.dumps(doc), encoding="utf-8")

        store = VectorStore.load(settings.index_path)
        query = [0.1 + 0.001 * position for position in range(store.dim)]
        results = store.search(query, top_k=len(store.records))
        assert all(math.isfinite(score) for _, score in results), (
            f"validator accepted a document scoring non-finitely: {doc['records']}"
        )
        # `cosine_similarity` answers a length mismatch or a zero-norm vector
        # with a finite 0.0 — no crash, and no way for that record to ever be
        # retrieved. "Scores finitely" is too weak a promise on its own; every
        # record has to be reachable. A dense query scores exactly 0.0 against
        # a well-formed record only by accident this generator cannot arrange.
        assert all(score != 0.0 for _, score in results), (
            f"validator accepted a document with an unreachable record: {doc['records']}"
        )
        for payload in sources_payload(results):
            assert isinstance(payload["source"], str)
            assert isinstance(payload["title"], str)
        assert isinstance(format_context(results), str)

    # A generator that never produces an accepted document would assert nothing.
    assert accepted >= 20, f"only {accepted} of 600 documents were accepted"
