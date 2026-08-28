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

# -- Optional analytics ------------------------------------------------------
# Off unless whoever publishes the site opts in with their own Statcounter
# ids, the same arrangement the console uses
# (`apps/frontend/src/config/branding.ts`). A shipped default would report
# every third-party build's traffic into one project's analytics account.
# `_templates/layout.html` emits nothing when the project id is empty.
_statcounter_project_id = os.environ.get("DOCS_STATCOUNTER_PROJECT_ID", "").strip()
_statcounter_security_key = os.environ.get("DOCS_STATCOUNTER_SECURITY_KEY", "").strip()

# Both values are interpolated into a <script> body and an image URL, so accept
# only the shapes Statcounter issues (a numeric project id, an alphanumeric
# security key). Anything else is dropped, which turns analytics off.
if not _statcounter_project_id.isdigit():
    _statcounter_project_id = ""
if not _statcounter_security_key.isalnum():
    _statcounter_security_key = ""

html_context = {
    "statcounter_project_id": _statcounter_project_id,
    "statcounter_security_key": _statcounter_security_key,
}
