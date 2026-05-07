.PHONY: help format lint test test-verbose test-cov setup-dev clean check all \
       docker-volumes up down restart ps logs build

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
COMPOSE := docker compose -f deploy/docker/docker-compose.yml --env-file .env
DOCKER_VOLUMES := hybridinference_postgres_data \
                  hybridinference_alertmanager_data hybridinference_alert_log_data

docker-volumes:  ## Create external Docker volumes required by production compose
	@for volume in $(DOCKER_VOLUMES); do \
		if ! docker volume inspect "$$volume" >/dev/null 2>&1; then \
			echo "$(YELLOW)Creating Docker volume $$volume...$(RESET)"; \
			docker volume create "$$volume" >/dev/null; \
		fi; \
	done

up: docker-volumes  ## Start all services
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

build: docker-volumes  ## Rebuild images and restart (or: make build s=backend)
ifdef s
	$(COMPOSE) up -d --build $(s)
else
	$(COMPOSE) up -d --build
endif
