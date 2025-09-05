.PHONY: format lint typecheck test setup-dev

format:
	@echo "Running yapf (google style) and ruff format..."
	ruff format .
	yapf --style=google -r -i serving routing database client utils llama_benchmark

lint:
	@echo "Running ruff and pydocstyle..."
	ruff .
	pydocstyle

typecheck:
	@echo "Running mypy..."
	mypy .

test:
	pytest -q

setup-dev:
	python -m pip install -r requirements.txt
	python -m pip install -r requirements-dev.txt
	pre-commit install
