.PHONY: help format lint test test-verbose test-cov setup-dev clean check all \
       docker-volumes up down restart ps logs build smoke \
       demo demo-smoke demo-down demo-reset _require-demo

# Default target
.DEFAULT_GOAL := help

# Allow overriding uv run flags, e.g.:
#   make lint UV_RUN="uv run --active"
UV_RUN ?= uv run
PYTHON_VERSION ?= 3.12

# Frontend directory
FRONTEND_DIR := apps/frontend

# Colors for terminal output
RESET := \033[0m
BOLD := \033[1m
GREEN := \033[32m
YELLOW := \033[33m
BLUE := \033[34m

help:  ## Show this help message
	@echo "$(BOLD)Available targets:$(RESET)"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(BLUE)%-15s$(RESET) %s\n", $$1, $$2}'

format:  ## Format code with ruff (configured for Google style)
	@echo "$(YELLOW)Running formatter...$(RESET)"
	$(UV_RUN) ruff format .
	$(UV_RUN) ruff check --fix --unsafe-fixes .
	@echo "$(GREEN)OK Code formatted$(RESET)"

lint:  ## Run linters (ruff format check, ruff lint, pydocstyle)
	@echo "$(YELLOW)Running linters...$(RESET)"
	$(UV_RUN) ruff format --check .
	$(UV_RUN) ruff check --no-fix .
	$(UV_RUN) pydocstyle --match-dir='^((?!(tests|\.venv|node_modules|apps/frontend|ops|docs)).)*$$'
	@echo "$(GREEN)OK Linting passed$(RESET)"

test:  ## Run unit/integration tests (exclude external and db-dependent)
	@echo "$(YELLOW)Running tests (not external, not dbtest)...$(RESET)"
	@# -n auto --dist loadfile: parallelize across cores at file granularity
	@# (same isolation unit as CI's file-based shards); ~3x faster wall time.
	$(UV_RUN) pytest -q -m "not external and not dbtest" -n auto --dist loadfile
	@echo "$(GREEN)OK Tests passed$(RESET)"

test-verbose: ## Run tests with verbose output (exclude external and db-dependent)
	$(UV_RUN) pytest -vv -m "not external and not dbtest"

test-cov:  ## Run tests with coverage (exclude external and db-dependent)
	$(UV_RUN) pytest -m "not external and not dbtest" --cov=. --cov-report=term-missing --cov-report=html

test-db:  ## Run tests that require PostgreSQL (set TEST_DB_* env vars)
	@echo "$(YELLOW)Running database-dependent tests...$(RESET)"
	$(UV_RUN) pytest -vv -m "dbtest"
	@echo "$(GREEN)OK Database tests passed$(RESET)"

test-all:  ## Run all tests except external (includes db-dependent)
	@echo "$(YELLOW)Running all tests (not external)...$(RESET)"
	$(UV_RUN) pytest -q -m "not external" -n auto --dist loadfile
	@echo "$(GREEN)OK All tests passed$(RESET)"

test-e2e: ## Run external/E2E tests (may require local server)
	$(UV_RUN) pytest -m external -vv

rag-ingest:  ## Build the docs RAG index (real bge-m3; RAG_EMBEDDER=hash for offline)
	@echo "$(YELLOW)Building docs RAG index...$(RESET)"
	PYTHONPATH=apps/backend $(UV_RUN) python -m serving.rag.ingest --embedder $${RAG_EMBEDDER:-gateway}
	@echo "$(GREEN)OK RAG index built$(RESET)"

check: lint test  ## Run all checks (lint, test)
	@echo "$(GREEN)OK All checks passed$(RESET)"

all: format check  ## Format code and run all checks

setup-dev:  ## Set up development environment
	@echo "$(YELLOW)Setting up development environment...$(RESET)"
	@# Init git submodules (e.g. llm-prober)
	git submodule update --init --recursive
	@# Create venv if it doesn't exist; fail fast if an existing venv uses a different Python minor.
	@if [ -x .venv/bin/python ]; then \
		current="$$(.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"; \
		if [ "$$current" != "$(PYTHON_VERSION)" ]; then \
			echo "Existing .venv uses Python $$current, expected $(PYTHON_VERSION). Remove .venv or rerun with PYTHON_VERSION=$$current."; \
			exit 1; \
		fi; \
	else \
		uv venv -p $(PYTHON_VERSION); \
	fi
	@# Install package in editable mode
	uv pip install -e .
	@# Install requirements.txt if it exists
	[ -f requirements.txt ] && uv pip install -r requirements.txt || true
	@# Sync development dependencies from pyproject.toml
	uv sync --group dev
	$(UV_RUN) pre-commit install
	@echo "$(GREEN)✓ Development environment ready$(RESET)"

clean:  ## Clean build artifacts and cache
	@echo "$(YELLOW)Cleaning up...$(RESET)"
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name ".coverage" -delete
	rm -rf htmlcov/ .pytest_cache/ .mypy_cache/ .ruff_cache/
	@echo "$(GREEN)OK Cleanup complete$(RESET)"

# Frontend targets
frontend-install:  ## Install frontend dependencies
	@echo "$(YELLOW)Installing frontend dependencies...$(RESET)"
	cd $(FRONTEND_DIR) && npm ci
	@echo "$(GREEN)OK Frontend dependencies installed$(RESET)"

frontend-lint:  ## Run ESLint on frontend code
	@echo "$(YELLOW)Running frontend linter...$(RESET)"
	cd $(FRONTEND_DIR) && npm run lint
	@echo "$(GREEN)OK Frontend linting passed$(RESET)"

frontend-type-check:  ## Run TypeScript type checking
	@echo "$(YELLOW)Running TypeScript type check...$(RESET)"
	cd $(FRONTEND_DIR) && npm run type-check
	@echo "$(GREEN)OK Type checking passed$(RESET)"

frontend-test:  ## Run frontend tests
	@echo "$(YELLOW)Running frontend tests...$(RESET)"
	cd $(FRONTEND_DIR) && npm run test
	@echo "$(GREEN)OK Frontend tests passed$(RESET)"

frontend-check: frontend-lint frontend-type-check frontend-test  ## Run all configured frontend checks
	@echo "$(GREEN)OK All configured frontend checks passed$(RESET)"

# Combined targets
check-all: lint test frontend-check  ## Run all checks (backend + frontend)
	@echo "$(GREEN)OK All checks passed (backend + frontend)$(RESET)"

all-with-frontend: format check-all  ## Format and check everything (backend + frontend)

# ─── Docker / Production ─────────────────────────────────────────────────────
# A deployment's public identity — site name, links, CORS, and the console's
# build-time values — lives in its distribution overlay, because the upstream
# compose defaults name no deployment. These have to reach `make build` too,
# not just the deploy scripts: the console's identity is baked in as build
# args, so a rebuild without them ships an unbranded frontend. `.env` stays
# last so per-host overrides still win, and secrets stay only in `.env`.
# Which deployment's identity `make up` and `make build` compile in.
#
# Discovered by default, because the runbooks tell operators to run these by
# hand on the server (docs/developer/deployment.md, adding-models.md), and a
# rebuild that quietly dropped the identity would publish an unbranded console
# from a routine command. Discovery keeps that working with no change to any
# machine.
#
# The cost is that this repository still contains the overlay, so a clone gets
# it too. That is an artifact of the split being unfinished — once the overlay
# lives elsewhere, discovery finds nothing and every clone is neutral without
# anything here changing. Until then it is announced rather than silent, and
# `DISTRIBUTION=none` opts out:
#
#   make up                          # discovers the overlay, and says so
#   make up DISTRIBUTION=none        # your own gateway, named after nobody
#   make up DISTRIBUTION=<name>       # pick one when several are present
#
# distributions/<name>/deploy/<file>.env -> <name>, deduplicated.
#
# An EXAMPLE_OVERLAY regular file marks a teaching artifact. Such overlays are
# available by name but never auto-selected as deployments. `test -f` matches
# the backend fallback's Path.is_file() check.
_ALL_DISTRIBUTION_DIRS := $(sort $(foreach f,$(wildcard distributions/*/deploy/*.env),$(word 2,$(subst /, ,$(f)))))
_EXAMPLE_DISTRIBUTION_DIRS := $(sort $(foreach d,$(_ALL_DISTRIBUTION_DIRS),\
  $(if $(shell test -f 'distributions/$(d)/EXAMPLE_OVERLAY' && echo yes),$(d),)))
_DISTRIBUTION_DIRS := $(filter-out $(_EXAMPLE_DISTRIBUTION_DIRS),$(_ALL_DISTRIBUTION_DIRS))
ifeq ($(words $(_DISTRIBUTION_DIRS)),1)
DISTRIBUTION ?= $(_DISTRIBUTION_DIRS)
else ifeq ($(words $(_DISTRIBUTION_DIRS)),0)
DISTRIBUTION ?=
else
# Picking the alphabetically first of several would compile one deployment's
# identity into another's console, and say nothing while doing it.
DISTRIBUTION ?= $(error Several distributions carry deploy/*.env ($(_DISTRIBUTION_DIRS)). Name one: make $(MAKECMDGOALS) DISTRIBUTION=<name>, or DISTRIBUTION=none)
endif
# These are outputs of DISTRIBUTION selection, never independent inputs.  The
# initial values also close the neutral/empty-selector branch against command-
# line injection.
override DISTRIBUTION_PATH :=
override DISTRIBUTION_ENV_FILES :=
override _IS_EXAMPLE :=
ifeq ($(DISTRIBUTION),none)
else ifneq ($(DISTRIBUTION),)
# DISTRIBUTION_PATH is derived state, not a second selector.  In particular,
# `DISTRIBUTION=example DISTRIBUTION_PATH=distributions/<deployment>` must not
# combine the example lifecycle with a deployment's Compose/env files.
override DISTRIBUTION_PATH := $(wildcard distributions/$(DISTRIBUTION))
ifeq ($(DISTRIBUTION_PATH),)
$(error DISTRIBUTION=$(DISTRIBUTION) matches no distributions/$(DISTRIBUTION))
endif
override _DISTRIBUTION_ENV_PATHS := $(wildcard $(DISTRIBUTION_PATH)/deploy/*.env)
override DISTRIBUTION_ENV_FILES := $(patsubst %,--env-file %,$(_DISTRIBUTION_ENV_PATHS))
ifeq ($(DISTRIBUTION_ENV_FILES),)
$(error DISTRIBUTION=$(DISTRIBUTION) matches no $(DISTRIBUTION_PATH)/deploy/*.env)
endif
# Derive the internal branch from the selected overlay, not from dotenv values.
override _IS_EXAMPLE := $(shell test -f '$(DISTRIBUTION_PATH)/EXAMPLE_OVERLAY' && echo yes)
ifeq ($(_IS_EXAMPLE),)
$(info Using distribution '$(DISTRIBUTION)' — its identity is compiled into the console. DISTRIBUTION=none for a neutral stack.)
endif
endif
# Deploy scripts may append environment files for a deployment-specific
# environment, such as staging. These are inserted after the shared
# distribution files and before `.env`, preserving host-local overrides.
COMPOSE_EXTRA_ENV_FILES ?=
COMPOSE_EXTRA_ENV_ARGS := $(patsubst %,--env-file %,$(wildcard $(COMPOSE_EXTRA_ENV_FILES)))
# The cloud-agent runner overlay was removed at H4: agents run from their own
# repository and their own deployment, so this stack no longer has an opt-in
# that attaches `backend` to an agent network.
COMPOSE_FILE_ARGS := -f deploy/docker/docker-compose.yml
DISTRIBUTION_COMPOSE_FILE := $(if $(DISTRIBUTION_PATH),$(wildcard $(DISTRIBUTION_PATH)/deploy/docker-compose.yml),)
ifneq ($(DISTRIBUTION_COMPOSE_FILE),)
COMPOSE_FILE_ARGS += -f $(DISTRIBUTION_COMPOSE_FILE)
endif
# Standalone cloud agent on the same host (freeinference-cloud-agent). Opting
# in attaches the console to that stack's network so the `/agents` rewrites
# from #1206 can resolve `web` and `control-plane`; without it they resolve
# nothing and the console answers 500 for a page it is configured to serve.
# Off by default because the network is `external` — naming one that does not
# exist fails every compose command on every other deployment.
ifeq ($(CLOUD_AGENT_NETWORK),1)
COMPOSE_FILE_ARGS += -f deploy/docker/docker-compose.cloud-agent.yml
endif
# A host-local `.env` remains last whenever it exists, preserving its override
# precedence. Public runnable examples deliberately need no secret file, so do
# not hand Compose a path that is absent on a fresh clone.
LOCAL_ENV_ARGS := $(if $(wildcard .env),--env-file .env,)
# The example is deterministic even in an operator checkout that already has a
# deployment .env. Shell variables still outrank every --env-file in Compose,
# which is the explicit escape hatch documented for a real upstream.
ifeq ($(_IS_EXAMPLE),yes)
LOCAL_ENV_ARGS :=
endif
COMPOSE := docker compose $(COMPOSE_FILE_ARGS) $(DISTRIBUTION_ENV_FILES) $(COMPOSE_EXTRA_ENV_ARGS) $(LOCAL_ENV_ARGS)
# The full local demo is a third Compose layer on top of the runnable example.
# Keep COMPOSE above untouched: `make up DISTRIBUTION=example` is the Stage 1
# contract, while only the explicit demo targets opt into Postgres + frontend.
DEMO_COMPOSE_FILE := $(if $(DISTRIBUTION_PATH),$(wildcard $(DISTRIBUTION_PATH)/deploy/docker-compose.demo.yml),)
DEMO_COMPOSE_FILE_ARG := $(if $(DEMO_COMPOSE_FILE),-f $(DEMO_COMPOSE_FILE),)
# The teaching lifecycle never inherits optional production profiles from a
# caller's shell. `env` also keeps this usable as full_smoke's argv-based
# recreate command, where a bare `NAME=value` token would not be executable.
DEMO_COMPOSE := env COMPOSE_PROFILES= docker compose $(COMPOSE_FILE_ARGS) $(DEMO_COMPOSE_FILE_ARG) $(DISTRIBUTION_ENV_FILES) $(COMPOSE_EXTRA_ENV_ARGS) $(LOCAL_ENV_ARGS)
DOCKER_VOLUMES := hybridinference_postgres_data

docker-volumes:  ## Create external Docker volumes required by production compose
	@for volume in $(DOCKER_VOLUMES); do \
		if ! docker volume inspect "$$volume" >/dev/null 2>&1; then \
			echo "$(YELLOW)Creating Docker volume $$volume...$(RESET)"; \
			docker volume create "$$volume" >/dev/null; \
		fi; \
	done

# The runnable tutorial proves the backend routing chain, not the production
# frontend/database stack. Start its fake to healthy first, then use --no-deps
# so backend's production Postgres dependency stays stopped while DB_ENABLED is
# false. The production branch retains its external-volume prerequisite.
ifeq ($(_IS_EXAMPLE),yes)
up:  ## Start all services
	$(COMPOSE) up -d --wait example-provider
	$(COMPOSE) up -d --no-deps backend
else
up: docker-volumes
	$(COMPOSE) up -d
endif

# The port has one source of truth: the distribution's own env file, which is
# also the file Compose reads to publish it. Deriving the smoke URL from the
# same place means editing the distribution — the thing this repository teaches
# people to do — cannot leave `make smoke` probing a port nobody published. A
# shell override still outranks both, and Compose honours it too.
_DIST_ENV_FILES := $(if $(DISTRIBUTION_PATH),$(wildcard $(DISTRIBUTION_PATH)/deploy/*.env),)
_DIST_BACKEND_PORT := $(if $(_DIST_ENV_FILES),$(shell sed -n 's/^BACKEND_PORT=//p' $(_DIST_ENV_FILES) | tail -n 1),)
_DIST_FRONTEND_PORT := $(if $(_DIST_ENV_FILES),$(shell sed -n 's/^FRONTEND_PORT=//p' $(_DIST_ENV_FILES) | tail -n 1),)
_DIST_DEMO_PROVIDER_SERVICE := $(if $(_DIST_ENV_FILES),$(shell sed -n 's/^DEMO_PROVIDER_SERVICE=//p' $(_DIST_ENV_FILES) | tail -n 1),)
BACKEND_PORT ?= $(if $(_DIST_BACKEND_PORT),$(_DIST_BACKEND_PORT),8080)
FRONTEND_PORT ?= $(if $(_DIST_FRONTEND_PORT),$(_DIST_FRONTEND_PORT),3001)
DEMO_PROVIDER_SERVICE ?= $(_DIST_DEMO_PROVIDER_SERVICE)
SMOKE_BASE_URL ?= http://localhost:$(BACKEND_PORT)
DEMO_BASE_URL ?= http://localhost:$(FRONTEND_PORT)
SMOKE_TIMEOUT ?= 120
SMOKE_PYTHON ?= python3
SMOKE_SCRIPT := $(DISTRIBUTION_PATH)/smoke.py
DEMO_SMOKE_SCRIPT := $(DISTRIBUTION_PATH)/full_smoke.py
DEMO_SMOKE_STATE_ARG := $(if $(DEMO_SMOKE_STATE_FILE),--write-reset-state "$(DEMO_SMOKE_STATE_FILE)",)
DEMO_SMOKE_EXPECT_STATE_ARG := $(if $(DEMO_SMOKE_EXPECT_STATE_FILE),--expect-existing-state "$(DEMO_SMOKE_EXPECT_STATE_FILE)",)

smoke:  ## Verify the selected distribution's running stack
	@test -f "$(SMOKE_SCRIPT)" || (echo "No smoke script for DISTRIBUTION=$(DISTRIBUTION)"; exit 1)
	$(SMOKE_PYTHON) "$(SMOKE_SCRIPT)" --base-url "$(SMOKE_BASE_URL)" --timeout "$(SMOKE_TIMEOUT)"

# Stage 2 deliberately reuses Stage 1's project and provider. --no-recreate
# pins that promise: Compose's divergence check can otherwise decide an
# unchanged service needs recreating (observed in CI whenever the provider
# image was built without cache) and silently restart it mid-tutorial. Only
# backend is recreated in place, picking up DB/auth.
_require-demo:
	@test -n "$(DISTRIBUTION_PATH)" || (echo "The demo requires an explicit distribution"; exit 1)
	@test -f "$(DEMO_COMPOSE_FILE)" || (echo "No demo Compose overlay for DISTRIBUTION=$(DISTRIBUTION)"; exit 1)

demo: _require-demo  ## Upgrade the runnable example in place to the full local stack
	$(DEMO_COMPOSE) up -d --no-recreate --wait $(DEMO_PROVIDER_SERVICE) postgres
	$(DEMO_COMPOSE) up -d --no-deps --force-recreate --wait backend
	$(DEMO_COMPOSE) up -d --no-deps --no-recreate --wait frontend

# full_smoke keeps the original JWT and API key in memory while this Compose
# command recreates backend, then proves both still work afterwards. CI can
# opt into a private outside-checkout reset-proof file for a later reset check.
demo-smoke: _require-demo  ## Verify the full demo through its frontend origin
	@test -f "$(DEMO_SMOKE_SCRIPT)" || (echo "No full smoke script for DISTRIBUTION=$(DISTRIBUTION)"; exit 1)
	$(SMOKE_PYTHON) "$(DEMO_SMOKE_SCRIPT)" --base-url "$(DEMO_BASE_URL)" --timeout "$(SMOKE_TIMEOUT)" $(DEMO_SMOKE_STATE_ARG) $(DEMO_SMOKE_EXPECT_STATE_ARG) --recreate-command $(DEMO_COMPOSE) up -d --no-deps --force-recreate --wait backend

demo-down: _require-demo  ## Stop the full demo while preserving its database
	$(DEMO_COMPOSE) down

demo-reset: _require-demo  ## Stop the full demo and delete its local database
	$(DEMO_COMPOSE) down --volumes --remove-orphans

down:  ## Stop all services
	$(COMPOSE) down

restart:  ## Restart all services (or: make restart s=backend)
ifdef s
	$(COMPOSE) restart $(s)
else
	$(COMPOSE) restart
endif

ps:  ## Show running services
	$(COMPOSE) ps

logs:  ## Tail logs (or: make logs s=backend)
ifdef s
	$(COMPOSE) logs -f $(s)
else
	$(COMPOSE) logs -f --tail=500
endif

# Keep rebuild semantics aligned with `up`: rebuilding the tutorial must not
# unexpectedly turn it into the production full stack.
ifeq ($(_IS_EXAMPLE),yes)
build:  ## Rebuild images and restart (or: make build s=backend)
	$(COMPOSE) up -d --wait example-provider
ifdef s
ifneq ($(filter-out backend example-provider,$(s)),)
$(error The runnable example can build only backend or example-provider)
endif
	$(COMPOSE) up -d --build --no-deps $(s)
else
	$(COMPOSE) up -d --build --no-deps backend
endif
else
build: docker-volumes
ifdef s
	$(COMPOSE) up -d --build $(s)
else
	$(COMPOSE) up -d --build
endif
endif
