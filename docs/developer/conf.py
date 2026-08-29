"""Sphinx configuration for the HybridInference developer documentation.

The site is plain MyST Markdown -- no page uses autodoc, so nothing here
imports the application. That keeps the build hermetic: `sphinx-build` needs
only Sphinx, myst-parser and the theme, and makes no network requests, so an
offline contributor gets the same result as CI.
"""

# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

import os
import shutil
import subprocess
import sys
import tempfile

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = "HybridInference"
copyright = "2026, The HybridInference contributors"
author = "The HybridInference contributors"
release = "0.1.0"

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    "myst_parser",  # Support for Markdown files
]

# MyST parser configuration
myst_enable_extensions = [
    "colon_fence",  # ::: fences
    "deflist",  # Definition lists
    "tasklist",  # Task lists
]

# Generate implicit anchors for h1-h3 headings so GitHub-style in-page links
# (`[text](#some-heading)`) resolve. Without this the Markdown renders fine on
# GitHub but Sphinx reports `myst.xref_missing`, which fails the build (CI and
# the published site both run `sphinx-build -W`).
myst_heading_anchors = 3

# -- Translations ------------------------------------------------------------
# The docs are written in English and translated with Sphinx's gettext
# workflow: `make docs-gettext` extracts one catalog per page, a translator
# fills in the `msgstr` entries, and `make docs-lang DOCS_LANG=<code>` builds
# that language. Every string without a translation falls back to the English
# source, so a partly-translated language still builds a complete site -- and
# when an English paragraph changes, its `msgid` changes with it, gettext marks
# the old translation `fuzzy`, and the page falls back to English rather than
# serving a translation that no longer matches. See the Translations section of
# contributing.md.
language = os.environ.get("DOCS_LANGUAGE", "en").strip() or "en"
locale_dirs = ["locale"]

# One catalog per source file rather than one per directory: a pull request
# then shows which page a translation touches, and a renamed page renames its
# catalog with it.
gettext_compact = False

# Languages the *published* site offers, as `code:endonym` pairs, in the order
# they appear in the switcher. The **first** entry is the root language: it is
# published at the site root (`/routing.html`) and every other language goes in
# a subdirectory named for its code (`/zh_CN/routing.html`). That layout keeps
# every URL the English site has ever had.
#
# The default is deliberately not empty. The published site is built by a
# single `sphinx-build` whose command line lives in the hosting project's
# settings rather than in this repository, so there is no env var to set at
# publish time. Carrying the list here is what makes a translation ship: the
# root-language build emits every other language beneath its own output
# directory (`_build_nested_languages` below), so one invocation produces the
# whole site.
#
# Set `DOCS_LANGUAGES="en:English"` to build the root language alone.
_languages = []
for _entry in os.environ.get("DOCS_LANGUAGES", "en:English,zh_CN:简体中文").split(","):
    _code, _, _label = _entry.strip().partition(":")
    # A code reaches an href, so accept only the shape a locale code has.
    if _code.replace("_", "").isalnum() and _label.strip():
        _languages.append({"code": _code, "label": _label.strip()})
if len(_languages) < 2:
    # One language needs no switcher, and a malformed value must not render a
    # half-built one.
    _languages = []

templates_path = ["_templates"]
exclude_patterns = []

# Source file suffixes
source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]

# RTD theme options
html_theme_options = {
    "navigation_depth": 4,
    "collapse_navigation": False,
    "sticky_navigation": True,
    "includehidden": True,
    "titles_only": False,
}

# Custom CSS files
html_css_files = [
    "custom.css",
]

# The root language is the first declared one; the switcher needs it to know
# whether the page it is rendering sits at the site root or one level down.
_root_language = _languages[0]["code"] if _languages else language

html_context = {
    "doc_languages": _languages,
    "doc_root_language": _root_language,
}

# Set in the child processes below so a nested build does not nest again.
_NESTED_ENV = "DOCS_NESTED_BUILD"


def _build_nested_languages(app, exception):
    """Build every non-root language into ``<outdir>/<code>/``.

    Sphinx builds one language per invocation, and the published site gets
    exactly one invocation that this repository does not control. Running the
    other languages from ``build-finished`` is what lets that single command
    publish all of them.

    Deliberately strict: the child build inherits ``-W``, so a translation that
    breaks the build fails the parent too rather than silently publishing a
    stale or half-rendered page. Doctrees go to a temporary directory so the
    published tree contains only HTML.
    """
    if exception is not None:
        return
    if os.environ.get(_NESTED_ENV) == "1":
        return
    # Only the root-language build nests. A `-D language=zh_CN` build is either
    # a translator iterating on one language or the child process below.
    if app.config.language != _root_language:
        return

    nested = [entry["code"] for entry in _languages if entry["code"] != _root_language]
    if not nested:
        return

    env = {**os.environ, _NESTED_ENV: "1"}
    scratch = tempfile.mkdtemp(prefix="sphinx-i18n-")
    try:
        for code in nested:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "sphinx",
                    "-b",
                    "html",
                    str(app.srcdir),
                    os.path.join(str(app.outdir), code),
                    "-d",
                    os.path.join(scratch, code),
                    "-D",
                    f"language={code}",
                    "-W",
                    "--keep-going",
                    "-q",
                ],
                env=env,
                check=True,
            )
            print(f"  translated site: {os.path.join(str(app.outdir), code)}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def setup(app):
    """Register the nested-language build."""
    app.connect("build-finished", _build_nested_languages)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
