# Minimal makefile for Sphinx documentation
#
# `make html` here is equivalent to `make docs` at the repo root; both build
# the developer guide the same way CI does.

# You can set these variables from the command line, and also from the
# environment. UV_RUN matches the repo-root Makefile, so
# `make html UV_RUN="uv run --active"` works the same way.
UV_RUN        ?= uv run
SPHINXBUILD   ?= $(UV_RUN) sphinx-build
# Warnings are errors, and --keep-going reports every one instead of stopping
# at the first. CI (.github/workflows/ci.yml, "Docs Build") uses the same
# flags, so a build that is clean here is clean there.
SPHINXOPTS    ?= -W --keep-going
SOURCEDIR     = developer
BUILDDIR      = build

# Put it first so that "make" without argument is like "make help".
help:
	@$(SPHINXBUILD) -M help "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

.PHONY: help Makefile

# Catch-all target: route all unknown targets to Sphinx using the new
# "make mode" option.  $(O) is meant as a shortcut for $(SPHINXOPTS).
%: Makefile
	@$(SPHINXBUILD) -M $@ "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)
