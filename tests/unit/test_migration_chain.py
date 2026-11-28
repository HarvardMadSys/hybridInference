"""How the gateway reads its chain of revisions at startup: from the files, as Alembic would."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from migrations import MIGRATIONS_DIR, compat

VERSIONS = MIGRATIONS_DIR / "versions"


def _revision(revision: str, down_revision: str | None, extra: str = "") -> str:
    """The header lines ``script.py.mako`` writes, then *extra*."""
    return (
        f"revision = {revision!r}\n"
        f"down_revision = {down_revision!r}\n"
        "branch_labels = None\n"
        "depends_on = None\n"
        f"{extra}"
    )


def _write(directory, files: dict[str, str]) -> None:
    for name, source in files.items():
        (directory / name).write_text(source)


def test_the_chain_read_from_the_files_is_alembics():
    chain = compat._literal_chain(VERSIONS)

    assert chain is not None, "a revision here is not one code_chain reads without Alembic"
    assert chain == compat._alembic_chain()
    assert compat.code_chain() == chain


def test_reading_the_chain_loads_neither_alembic_nor_sqlalchemy():
    """Every start reads the chain; Alembic would leave its modules and SQLAlchemy's behind."""
    code = (
        "import sys; from migrations.compat import code_chain; code_chain(); "
        "print(sorted({m.partition('.')[0] for m in sys.modules} & {'alembic', 'mako', 'sqlalchemy'}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(MIGRATIONS_DIR.parent)},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert result.stdout.strip() == "[]"


def test_a_line_of_revisions_is_read_oldest_first(tmp_path):
    _write(
        tmp_path,
        {
            "0001_base.py": _revision("0001_base", None),
            "0002_then.py": f'"""Docstring."""\nfrom alembic import op\n{_revision("0002_then", "0001_base")}',
            "0003_last.py": _revision("0003_last", "0002_then").replace(
                "revision = ", "revision: str = ", 1
            ),
            # Not revisions, to Alembic either.
            "__init__.py": "",
            ".#0004_lock.py": "not python",
            "notes.txt": _revision("0004_notes", "0003_last"),
        },
    )
    (tmp_path / "0005_dir.py").mkdir()

    assert compat._literal_chain(tmp_path) == ("0001_base", "0002_then", "0003_last")


@pytest.mark.parametrize(
    "files",
    [
        pytest.param({}, id="no revisions"),
        pytest.param(
            {
                "a.py": _revision("a", None),
                "b.py": _revision("b", "a"),
                "c.py": _revision("c", "a"),
                "d.py": _revision("d", None).replace("None\nbranch", "('b', 'c')\nbranch", 1),
            },
            id="merge",
        ),
        pytest.param(
            {
                "a.py": _revision("a", None),
                "b.py": _revision("b", "a"),
                "c.py": _revision("c", "a"),
            },
            id="two heads",
        ),
        pytest.param({"a.py": _revision("a", None), "b.py": _revision("b", None)}, id="two bases"),
        pytest.param(
            {"a.py": _revision("a", None), "b.py": _revision("b", "x")}, id="unknown parent"
        ),
        pytest.param(
            {
                "a.py": _revision("a", None),
                "b.py": _revision("b", "c"),
                "c.py": _revision("c", "b"),
            },
            id="loop",
        ),
        pytest.param(
            {"a.py": _revision("a", None), "b.py": _revision("a", None)}, id="same id twice"
        ),
        pytest.param(
            {
                "a.py": _revision("a", None).replace(
                    "branch_labels = None", "branch_labels = ('x',)"
                )
            },
            id="branch label",
        ),
        pytest.param(
            {"a.py": _revision("a", None).replace("depends_on = None", "depends_on = 'z'")},
            id="dependency",
        ),
        pytest.param(
            {"a.py": _revision("a", None).replace("'a'", "'a' + 'b'", 1)}, id="computed id"
        ),
        pytest.param(
            {"a.py": _revision("a", None, "if True:\n    revision = 'b'\n")},
            id="rebound in a block",
        ),
        pytest.param({"a.py": _revision("a", None, "revision = 'b'\n")}, id="assigned twice"),
        pytest.param(
            {"a.py": _revision("a", None, "from x import down_revision\n")}, id="imported"
        ),
        pytest.param({"a.py": f"from x import *\n{_revision('a', None)}"}, id="star import"),
        pytest.param({"a.py": _revision("a", None, "def revision():\n    pass\n")}, id="function"),
        pytest.param({"a.py": "revision = 'a'\n"}, id="no down_revision"),
        pytest.param({"a.py": _revision("a-1", None)}, id="id alembic refuses"),
        pytest.param({"a.py": _revision("a", None, "def broken(:\n")}, id="does not parse"),
    ],
)
def test_anything_but_a_plain_line_is_left_to_alembic(tmp_path, files):
    _write(tmp_path, files)

    assert compat._literal_chain(tmp_path) is None


def test_a_missing_directory_is_left_to_alembic(tmp_path):
    assert compat._literal_chain(tmp_path / "versions") is None


def test_alembic_reads_what_the_files_do_not_say_plainly(monkeypatch):
    monkeypatch.setattr(compat, "_literal_chain", lambda versions: None)
    monkeypatch.setattr(compat, "_alembic_chain", lambda: ("from_alembic",))

    assert compat.code_chain() == ("from_alembic",)
