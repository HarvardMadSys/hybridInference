"""Check actual Markdown syntax and tracked-file selection, without network IO."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from ops.ci import check_docs_links as check

if TYPE_CHECKING:
    from pathlib import Path


def test_local_files_references_and_encoded_spaces(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(check, "REPO", tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "my file.md").touch()
    (tmp_path / "image.png").touch()
    page = docs / "index.md"
    page.write_text(
        "[inline](my%20file.md#section)\n\n"
        "[reference][target]\n\n[target]: <my file.md>\n\n"
        "![image](../image.png)\n\n"
        "[self](#heading) [web](https://example.test/no-file) "
        "[email](mailto:test@example.test)\n\n"
        "```markdown\n[code example](does-not-exist.md)\n```\n"
    )
    assert check.missing_links(page) == []


def test_missing_links_and_images_report_their_source_blocks(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(check, "REPO", tmp_path)
    page = tmp_path / "index.md"
    page.write_text(
        "# Test\n\n[missing](absent.md)\n\n![missing](absent.png)\n\n"
        "[reference][ref]\n\n[ref]: missing-reference.md\n"
    )
    assert check.missing_links(page) == [
        "index.md:3: absent.md",
        "index.md:5: absent.png",
        "index.md:7: missing-reference.md",
    ]


def test_cli_checks_all_tracked_markdown_but_ignores_untracked_files(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(check, "REPO", tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "README.md").write_text("[guide](guide.mdx)\n")
    (tmp_path / "guide.mdx").write_text("Guide\n")
    (tmp_path / "untracked.md").write_text("[missing](absent.md)\n")
    subprocess.run(["git", "add", "README.md", "guide.mdx"], cwd=tmp_path, check=True)
    assert check.main() == 0
    subprocess.run(["git", "add", "untracked.md"], cwd=tmp_path, check=True)
    assert check.main() == 1


def test_removing_a_linked_file_fails_the_check(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(check, "REPO", tmp_path)
    page = tmp_path / "index.md"
    target = tmp_path / "target.md"
    page.write_text("[target](target.md)\n")
    target.touch()
    assert check.missing_links(page) == []
    target.unlink()
    assert check.missing_links(page) == ["index.md:1: target.md"]
