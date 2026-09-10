#!/usr/bin/env python3
"""Check local link destinations in every tracked Markdown document.

Parse Markdown so examples inside code fences are not mistaken for links.
Network URLs and fragments are outside this check; Sphinx validates the
developer site's cross-references when the site builds.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

REPO = Path(__file__).resolve().parents[2]


def missing_links(path: Path) -> list[str]:
    """Return local destinations that do not exist relative to their document."""
    problems = []
    for block in MarkdownIt().parse(path.read_text(encoding="utf-8")):
        for token in block.children or []:
            target = token.attrGet("href") or token.attrGet("src")
            if not target:
                continue
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            if not (path.parent / unquote(parsed.path)).exists():
                line = block.map[0] + 1 if block.map else 1
                problems.append(f"{path.relative_to(REPO)}:{line}: {target}")
    return problems


def main() -> int:
    """Check the same tracked files locally and in CI."""
    names = (
        subprocess.check_output(["git", "ls-files", "-z", "*.md", "*.mdx"], cwd=REPO)
        .decode()
        .split("\0")
    )
    paths = [REPO / name for name in names if name]
    problems = [problem for path in paths for problem in missing_links(path)]
    if problems:
        print("Missing local documentation links:\n" + "\n".join(problems))
        return 1
    print(f"Local links OK: {len(paths)} Markdown documents checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
