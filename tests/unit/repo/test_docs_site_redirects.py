"""The published site's redirect table has to keep pointing at real pages.

Cloudflare Pages answers an unmatched path with the site index and HTTP 200,
not a 404. That is why `docs/developer/_extra/_redirects` exists: a page that
gets renamed keeps its old address working instead of quietly resolving to the
home page. But it is also why a mistake in that file is invisible — a rule
whose target no longer exists redirects a reader from one silent 200 to
another, and nothing in the docs build notices, because Sphinx never reads it.

So the rules are checked here instead:

- every target resolves to a page this repository actually builds;
- no source shadows a live page, which would make that page unreachable;
- every rename rule has its `/zh_CN/` twin, so a translated reader following an
  old link is not the only one who lands nowhere;
- `404.html` names no relative asset, the single property that lets one file
  render correctly at every depth Pages serves it from;
- `conf.py` still declares `html_extra_path`, without which neither file is
  published at all and everything above is theory.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[3] / "docs" / "developer"
EXTRA = DOCS / "_extra"
REDIRECTS = EXTRA / "_redirects"
NOT_FOUND = EXTRA / "404.html"

# The language trees the site publishes beneath the root, from conf.py's
# DOCS_LANGUAGES default. A redirect into one of these is a redirect to the
# same source page, just translated.
LANG_PREFIXES = ("/zh_CN/",)


def _pages() -> set[str]:
    """Every page slug the docs build produces, from its source files."""
    return {p.stem for p in DOCS.iterdir() if p.suffix in {".md", ".rst"}}


def _rules() -> list[tuple[str, str, str]]:
    """(source, target, status) for each rule, comments and blanks dropped."""
    rules = []
    for line in REDIRECTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        assert len(parts) == 3, f"malformed rule: {line!r}"
        rules.append((parts[0], parts[1], parts[2]))
    return rules


def _slug(path: str) -> str | None:
    """The page slug a site path names, or None when it names no single page."""
    for prefix in LANG_PREFIXES:
        if path.startswith(prefix):
            path = "/" + path[len(prefix) :]
            break
    if path in {"/", ""} or ":splat" in path or path.endswith("*"):
        return None
    return path.lstrip("/").removesuffix(".html")


def test_redirect_file_is_present_and_wired() -> None:
    assert REDIRECTS.is_file(), f"{REDIRECTS} is missing"
    assert NOT_FOUND.is_file(), f"{NOT_FOUND} is missing"
    conf = (DOCS / "conf.py").read_text(encoding="utf-8")
    assert re.search(r"^html_extra_path\s*=\s*\[\"_extra\"\]", conf, re.M), (
        "conf.py no longer copies _extra/ into the build, so _redirects and "
        "404.html are not published and every rule below is inert"
    )


@pytest.mark.parametrize("source,target,status", _rules())
def test_every_target_is_a_page_we_build(source: str, target: str, status: str) -> None:
    assert status in {"301", "302", "308"}, f"{source}: unexpected status {status}"
    slug = _slug(target)
    if slug is None:
        return
    assert slug in _pages(), (
        f"{source} redirects to {target}, but no docs/developer/{slug}.md|.rst "
        "exists — the reader lands on the site index with a 200"
    )


@pytest.mark.parametrize("source,target,status", _rules())
def test_no_source_shadows_a_live_page(source: str, target: str, status: str) -> None:
    slug = _slug(source)
    if slug is None:
        return
    assert slug not in _pages(), (
        f"{source} is redirected away, but docs/developer/{slug} is a page this "
        "repository still builds — the redirect makes it unreachable"
    )


def test_rename_rules_cover_the_translated_tree() -> None:
    """A rename breaks the translated URL exactly as it breaks the English one."""
    sources = {source for source, _, _ in _rules()}
    # Only rename rules -- ones that carry an old page slug to a new one. A
    # language-code guess (`/zh` -> `/zh_CN/`) names no page on either side and
    # is already the rule that serves the translated tree.
    renames = [
        source
        for source, target, _ in _rules()
        if _slug(source) is not None and _slug(target) is not None
    ]
    missing = [
        s
        for s in renames
        if not s.startswith(LANG_PREFIXES)
        and not any(f"{prefix}{s.lstrip('/')}" in sources for prefix in LANG_PREFIXES)
    ]
    assert not missing, f"rename rules with no /zh_CN/ twin: {missing}"


def test_404_page_carries_no_relative_asset() -> None:
    """Pages serves this file at the requested depth, so relative paths break."""
    html = NOT_FOUND.read_text(encoding="utf-8")
    refs = re.findall(r'(?:href|src)="([^"]+)"', html)
    assert refs, "no links found — the 404 page should at least offer a way home"
    relative = [r for r in refs if not r.startswith(("/", "http://", "https://", "mailto:", "#"))]
    assert not relative, (
        f"404.html references {relative} relatively; served from /a/b/c it would "
        "resolve against that path. Use absolute or external URLs only."
    )
