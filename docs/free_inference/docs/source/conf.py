"""Sphinx configuration for the FreeInference user documentation.

This module configures Sphinx extensions, HTML theme, and source parsers
used to build the user-facing documentation. This documentation focuses
on helping users get started and use the FreeInference API.
"""

# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

import sys
from pathlib import Path

# Add the imported documentation root to sys.path for autodoc.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = "FreeInference"
copyright = "2025-2026, Harvard System Lab"
author = "Harvard System Lab"
release = "0.1.0"

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    "myst_parser",  # Support for Markdown files
    "sphinx.ext.intersphinx",  # Link to other project's documentation
]

# MyST parser configuration
myst_enable_extensions = [
    "colon_fence",  # ::: fences
    "deflist",  # Definition lists
    "tasklist",  # Task lists
]

# Generate implicit anchors for headings (h1-h3) so in-page links such as
# ``[Manual setup](#manual-setup)`` resolve. Without this, MyST emits
# ``xref_missing`` warnings that fail the Cloudflare Pages build (Sphinx runs
# with warnings treated as errors).
myst_heading_anchors = 3

# Intersphinx mapping - link to Python docs for better references
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

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

# Emit a .nojekyll marker for static hosts that understand it. Cloudflare Pages
# does not require the marker, but keeping it is harmless and preserves the
# standalone docs build behavior.
html_extra_path = []


def setup(app):
    """Create a .nojekyll marker after successful HTML builds."""

    def create_nojekyll(app, exception):
        """Create .nojekyll file in build output directory."""
        if exception is None and app.builder.name == "html":
            nojekyll_path = Path(app.outdir) / ".nojekyll"
            nojekyll_path.touch()

    app.connect("build-finished", create_nojekyll)
