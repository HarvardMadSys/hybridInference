"""Sphinx configuration for the FreeInference user documentation.

This module configures Sphinx extensions, HTML theme, and source parsers
used to build the user-facing documentation. This documentation focuses
on helping users get started and use the FreeInference API.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

project = "FreeInference"
copyright = "2025-2026, Harvard System Lab"
author = "Harvard System Lab"
release = "0.1.0"

extensions = [
    "myst_parser",
    "sphinx.ext.intersphinx",
]

myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "tasklist",
]

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

exclude_patterns = []

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]

html_theme_options = {
    "navigation_depth": 4,
    "collapse_navigation": False,
    "sticky_navigation": True,
    "includehidden": True,
    "titles_only": False,
}

html_css_files = [
    "custom.css",
]

html_extra_path = []


def setup(app):

    def create_nojekyll(app, exception):
        if exception is None and app.builder.name == "html":
            nojekyll_path = Path(app.outdir) / ".nojekyll"
            nojekyll_path.touch()

    app.connect("build-finished", create_nojekyll)
