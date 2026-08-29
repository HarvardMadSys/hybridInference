#!/usr/bin/env python3
"""Fail when a translated docs page has silently reverted to English.

A translation does not break loudly. Sphinx falls back to the English source
for any string it cannot translate, so a stale catalog, a missing catalog or a
translation Sphinx refuses to apply all produce a page that builds clean under
``-W`` and reads as English. Nothing else in CI notices.

Five checks, each aimed at one way that happens:

``fuzzy``
    Editing an English sentence changes its ``msgid``, which is the lookup key,
    so gettext marks the old translation ``fuzzy`` and Sphinx stops using it.
    That paragraph reverts to English. Any fuzzy entry is a defect: either
    update the translation or delete it.

``missing catalog``
    A page added without running ``make docs-translate`` has no catalog at all,
    so the whole page is English in every language.

``stale``
    The commonest one, and the quietest: editing an English sentence without
    running ``make docs-translate``. The ``msgid`` is the lookup key, so the
    edited sentence no longer matches any entry and renders in English -- with
    no ``fuzzy`` marker anywhere, because nothing re-merged the catalog. Caught
    by comparing the freshly extracted ``.pot`` templates against the catalogs.

``block start``
    Sphinx re-parses each ``msgstr``. One that opens with an unescaped block
    marker -- ``1. ``, ``- ``, ``# ``, ``> `` -- parses as a list or a heading
    instead of the inline text the source had, the structures no longer match,
    and Sphinx drops the translation without a warning. A numbered heading is
    the common case; escape the marker (``1\\. ``) to keep it.

``structure``
    Markup that survives in English but not in the translation -- an emphasis
    run that stopped being recognised next to CJK punctuation, a heading Sphinx
    dropped because the translation re-parsed as a list, a link target someone
    translated along with its label. The rendered structure diverges while the
    build stays green, so compare the built trees directly.

Run it against the tree ``make docs`` produces::

    python ops/ci/check_docs_translations.py docs/build/html docs/developer/locale \\
        --gettext-dir docs/gettext

Stdlib only: the docs CI job installs Sphinx and nothing else.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

# A .po string literal, tolerating escaped quotes. Naive `"[^"]*"` splits an
# entry that contains \" and silently mis-reads the rest of the file.
_PO_STRING = r'"(?:[^"\\]|\\.)*"'
_PO_ENTRY = re.compile(rf"((?:#[^\n]*\n)*)msgid ((?:{_PO_STRING}\n)+)msgstr ((?:{_PO_STRING}\n)+)")

# The rendered body, excluding the theme's chrome: the sidebar and footer carry
# their own translated strings, and comparing them would report every page.
#
# Sliced by index rather than matched as a balanced element. A non-greedy
# `<div itemprop="articleBody".*?</div>` stops at the first nested close, which
# on these pages is a few hundred lines in — it silently compared about a
# twelfth of each page and passed everything.
_BODY_START = 'itemprop="articleBody"'
_BODY_END = "<footer"


def _body(text: str) -> str:
    """Return the article body, or the whole document when the theme changes."""
    start = text.find(_BODY_START)
    if start < 0:
        return text
    end = text.find(_BODY_END, start)
    return text[start:end] if end > start else text[start:]


_TAGS = re.compile(r"<(code|strong|em|a|li|table|h1|h2|h3)[ >]")
_CODE_TEXT = re.compile(r"<code[^>]*>(.*?)</code>", re.S)
_HREF = re.compile(r'href="([^"]+)"')


def _po_entries(text: str) -> list[tuple[str, str, str]]:
    """Return ``(comments, msgid, msgstr)`` for every entry in a catalog."""
    out = []
    for match in _PO_ENTRY.finditer(text):
        parts = []
        for chunk in match.group(2), match.group(3):
            joined = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', chunk))
            parts.append(joined.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\"))
        out.append((match.group(1), parts[0], parts[1]))
    return out


def check_catalogs(locale_dir: Path) -> list[str]:
    """Report fuzzy entries, which Sphinx silently renders as English."""
    problems = []
    for po in sorted(locale_dir.rglob("*.po")):
        for comments, msgid, _ in _po_entries(po.read_text(encoding="utf-8")):
            if not msgid or "fuzzy" not in comments:
                continue
            excerpt = " ".join(msgid.split())[:70]
            problems.append(
                f"{po}: fuzzy entry renders as English -- {excerpt!r}\n"
                f"    Update or delete the translation, then drop the `#, fuzzy` line."
            )
    return problems


def check_coverage(build_dir: Path, locale_dir: Path, languages: list[str]) -> list[str]:
    """Report pages a language has no catalog for, which render fully in English."""
    problems = []
    pages = {p.stem for p in build_dir.glob("*.html")} - {"genindex", "search", "py-modindex"}
    for lang in languages:
        messages = locale_dir / lang / "LC_MESSAGES"
        if not messages.is_dir():
            problems.append(f"{messages}: no catalogs for a language the site publishes")
            continue
        have = {p.stem for p in messages.glob("*.po")}
        for page in sorted(pages - have):
            problems.append(
                f"{messages}/{page}.po: missing, so the whole page renders in English\n"
                f"    Run `make docs-translate DOCS_LANG={lang}`."
            )
    return problems


# A msgstr that starts with one of these re-parses as a block construct.
_BLOCK_START = re.compile(r"^(?:\d+[.)] |[-*+] |#{1,6} |> )")


def check_block_starts(locale_dir: Path) -> list[str]:
    """Report translations Sphinx will drop because they re-parse as a block."""
    problems = []
    for po in sorted(locale_dir.rglob("*.po")):
        for _, msgid, msgstr in _po_entries(po.read_text(encoding="utf-8")):
            if not msgid or not msgstr:
                continue
            # No msgid exemption. A numbered heading's own msgid starts with
            # `1. ` too -- gettext extracts the heading text verbatim -- and
            # exempting it is exactly what lets the broken translation through.
            # A genuine list contributes no marker to its msgid: gettext
            # extracts the item's text, not the `- ` in front of it.
            if _BLOCK_START.match(msgstr):
                excerpt = " ".join(msgstr.split())[:60]
                problems.append(
                    f"{po}: translation starts with a block marker, so Sphinx "
                    f"drops it and renders the English -- {excerpt!r}\n"
                    f"    Escape it, e.g. `1\\. ` for a numbered heading."
                )
    return problems


def check_staleness(gettext_dir: Path, locale_dir: Path, languages: list[str]) -> list[str]:
    """Report source strings no catalog entry matches, which render in English."""
    problems = []
    for pot in sorted(gettext_dir.rglob("*.pot")):
        wanted = {msgid for _, msgid, _ in _po_entries(pot.read_text(encoding="utf-8")) if msgid}
        for lang in languages:
            po = locale_dir / lang / "LC_MESSAGES" / f"{pot.stem}.po"
            if not po.exists():
                continue  # reported by check_coverage
            have = {msgid for _, msgid, _ in _po_entries(po.read_text(encoding="utf-8")) if msgid}
            for msgid in sorted(wanted - have):
                excerpt = " ".join(msgid.split())[:70]
                problems.append(
                    f"{po}: no entry for a string this page now contains, so it "
                    f"renders in English -- {excerpt!r}\n"
                    f"    Run `make docs-translate DOCS_LANG={lang}` and translate the new entry."
                )
    return problems


def _profile(path: Path) -> tuple[Counter, Counter, Counter]:
    """Return the markup fingerprint of a built page's body."""
    source = _body(path.read_text(encoding="utf-8", errors="ignore"))
    return (
        Counter(_TAGS.findall(source)),
        Counter(_CODE_TEXT.findall(source)),
        Counter(_HREF.findall(source)),
    )


def check_structure(build_dir: Path, languages: list[str]) -> list[str]:
    """Report pages whose translation renders different markup than the source."""
    problems = []
    for lang in languages:
        lang_dir = build_dir / lang
        if not lang_dir.is_dir():
            problems.append(f"{lang_dir}: the build produced no such language directory")
            continue
        for source_page in sorted(build_dir.glob("*.html")):
            translated = lang_dir / source_page.name
            if not translated.exists():
                problems.append(f"{translated}: missing from the {lang} build")
                continue
            want, want_code, want_href = _profile(source_page)
            got, got_code, got_href = _profile(translated)
            for label, a, b in (
                ("markup tags", want, got),
                ("inline-code literals", want_code, got_code),
                ("link targets", want_href, got_href),
            ):
                if a == b:
                    continue
                lost = a - b
                gained = b - a
                detail = []
                if lost:
                    detail.append(f"only in en: {dict(list(lost.items())[:4])}")
                if gained:
                    detail.append(f"only in {lang}: {dict(list(gained.items())[:4])}")
                problems.append(
                    f"{translated}: {label} differ from the English page -- " + "; ".join(detail)
                )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build_dir", type=Path, help="root of the built site (English at its top)")
    parser.add_argument("locale_dir", type=Path, help="docs/developer/locale")
    parser.add_argument(
        "--gettext-dir",
        type=Path,
        help="docs/gettext, as produced by `make docs-gettext`. Without it the "
        "staleness check is skipped, and an English edit that was never merged "
        "into the catalogs goes unreported.",
    )
    args = parser.parse_args()

    if not args.build_dir.is_dir():
        print(f"error: {args.build_dir} does not exist -- run `make docs` first", file=sys.stderr)
        return 2
    if not args.locale_dir.is_dir():
        print(f"error: {args.locale_dir} does not exist", file=sys.stderr)
        return 2

    # The languages the build actually published, which is what conf.py's
    # DOCS_LANGUAGES produced. Taking them from the tree rather than from the
    # environment keeps this honest about what was built.
    languages = sorted(
        d.name
        for d in args.build_dir.iterdir()
        if d.is_dir() and (args.locale_dir / d.name).is_dir()
    )
    if not languages:
        print("No translated language directories in the build; nothing to check.")
        return 0

    problems = (
        check_catalogs(args.locale_dir)
        + check_block_starts(args.locale_dir)
        + check_coverage(args.build_dir, args.locale_dir, languages)
        + check_structure(args.build_dir, languages)
    )
    if args.gettext_dir and args.gettext_dir.is_dir():
        problems += check_staleness(args.gettext_dir, args.locale_dir, languages)
    elif args.gettext_dir:
        print(
            f"error: {args.gettext_dir} does not exist -- run `make docs-gettext`",
            file=sys.stderr,
        )
        return 2

    if problems:
        print(f"{len(problems)} translation problem(s):\n", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print(
            "\nSee the Translations section of docs/developer/contributing.md.",
            file=sys.stderr,
        )
        return 1

    scope = "" if args.gettext_dir else " (staleness unchecked)"
    print(f"Translations OK: {', '.join(languages)} match the English build{scope}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
