.PHONY: help format lint test test-verbose test-cov setup-dev clean check all \
       sync-subscriptions up down restart ps logs build \
       staging-up staging-down staging-restart staging-ps staging-logs staging-build

# Default target
.DEFAULT_GOAL := help

# Allow overriding uv run flags, e.g.:
#   make lint UV_RUN="uv run --active"
UV_RUN ?= uv run

# Frontend directory
FRONTEND_DIR := frontend

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
	$(UV_RUN) ruff check --fix .
	@echo "$(GREEN)OK Code formatted$(RESET)"

lint:  ## Run linters (ruff, pydocstyle)
	@echo "$(YELLOW)Running linters...$(RESET)"
	$(UV_RUN) ruff check --no-fix .
	$(UV_RUN) pydocstyle
	@echo "$(GREEN)OK Linting passed$(RESET)"

test:  ## Run unit/integration tests (exclude external and db-dependent)
	@echo "$(YELLOW)Running tests (not external, not dbtest)...$(RESET)"
	$(UV_RUN) pytest -q -m "not external and not dbtest"
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
	$(UV_RUN) pytest -q -m "not external"
	@echo "$(GREEN)OK All tests passed$(RESET)"

test-e2e: ## Run external/E2E tests (may require local server)
	$(UV_RUN) pytest -m external -vv

check: lint test  ## Run all checks (lint, test)
	@echo "$(GREEN)OK All checks passed$(RESET)"

all: format check  ## Format code and run all checks

setup-dev:  ## Set up development environment
	@echo "$(YELLOW)Setting up development environment...$(RESET)"
	@# Init git submodules (e.g. docs/free_inference)
	git submodule update --init --recursive
	@# Create venv if it doesn't exist; keep idempotent
	[ -d .venv ] || uv venv -p 3.10
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
	cd $(FRONTEND_DIR) && npm run test -- --run
	@echo "$(GREEN)OK Frontend tests passed$(RESET)"

frontend-check: frontend-lint frontend-type-check frontend-test  ## Run all frontend checks
	@echo "$(GREEN)OK All frontend checks passed$(RESET)"

# Combined targets
check-all: lint test frontend-check  ## Run all checks (backend + frontend)
	@echo "$(GREEN)OK All checks passed (backend + frontend)$(RESET)"

all-with-frontend: format check-all  ## Format and check everything (backend + frontend)

# ─── Docker / Production ─────────────────────────────────────────────────────
COMPOSE := docker compose -f infrastructure/docker/docker-compose.yml --env-file .env
STAGING_COMPOSE := docker compose -f infrastructure/docker/docker-compose.staging.yml --env-file .env

sync-subscriptions:  ## Import CLI OAuth credentials for subscription adapters
	@mkdir -p var/data
	@echo "$(YELLOW)Syncing subscription credentials...$(RESET)"
	@if [ -f "$$HOME/.codex/auth.json" ]; then \
		$(UV_RUN) python scripts/import_codex_auth.py \
			&& echo "$(GREEN)  codex: imported$(RESET)" \
			|| echo "$(YELLOW)  codex: import FAILED (see error above)$(RESET)"; \
	else \
		echo "  codex: skipped (~/.codex/auth.json not found; run codex --login)"; \
	fi
	@if [ -f "$$HOME/.claude/.credentials.json" ] || [ -f "$$HOME/.claude/credentials.json" ] || [ -f "$$HOME/.claude/auth.json" ]; then \
		$(UV_RUN) python scripts/import_claude_auth.py \
			&& echo "$(GREEN)  claude: imported$(RESET)" \
			|| echo "$(YELLOW)  claude: import FAILED (see error above)$(RESET)"; \
	else \
		echo "  claude: skipped (~/.claude/ credentials not found; run claude login)"; \
	fi

up: sync-subscriptions  ## Start all services
	$(COMPOSE) up -d

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

build:  ## Rebuild images and restart (or: make build s=backend)
ifdef s
	$(COMPOSE) up -d --build $(s)
else
	$(COMPOSE) up -d --build
endif

# ─── Docker / Staging ────────────────────────────────────────────────────────
staging-up:  ## Start the full staging stack
	$(STAGING_COMPOSE) pull
	$(STAGING_COMPOSE) build backend frontend
	$(STAGING_COMPOSE) up -d

staging-down:  ## Stop the full staging stack
	$(STAGING_COMPOSE) down

staging-restart:  ## Restart staging services (or: make staging-restart s=backend)
ifdef s
	$(STAGING_COMPOSE) restart $(s)
else
	$(STAGING_COMPOSE) restart
endif

staging-ps:  ## Show running staging services
	$(STAGING_COMPOSE) ps

staging-logs:  ## Tail staging logs (or: make staging-logs s=backend)
ifdef s
	$(STAGING_COMPOSE) logs -f $(s)
else
	$(STAGING_COMPOSE) logs -f --tail=500
endif

staging-build:  ## Rebuild staging images and restart (or: make staging-build s=backend)
ifdef s
	$(STAGING_COMPOSE) up -d --build $(s)
else
	$(STAGING_COMPOSE) up -d --build
endif
