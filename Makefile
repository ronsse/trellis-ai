.DEFAULT_GOAL := help

.PHONY: help install install-dev lint format typecheck test check clean build publish-check verify-wheel openapi openapi-check

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Install package
	uv pip install -e .

install-dev: ## Install package with dev dependencies
	uv pip install -e ".[dev]"
	python -m pre_commit install

lint: ## Run linting
	ruff check src/ tests/

format: ## Format code
	ruff format src/ tests/
	ruff check --fix src/ tests/

typecheck: ## Run type checking
	mypy src/

test: ## Run tests
	pytest tests/ -v

check: lint typecheck test ## Run all checks (lint + typecheck + test)

clean: ## Clean build artifacts
	rm -rf dist/ build/ *.egg-info .mypy_cache .ruff_cache .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

build: clean ## Build sdist + wheel into dist/
	python -m build

verify-wheel: build ## Build and inspect wheel contents (sanity-check before tagging)
	@echo "--- wheel contents ---"
	@unzip -l dist/*.whl
	@echo
	@echo "--- sdist contents ---"
	@tar -tzf dist/*.tar.gz | head -50

publish-check: build ## Build and run twine check on the artifacts
	python -m twine check dist/*

openapi: ## Regenerate docs/api/v1.yaml from the live FastAPI app
	python scripts/generate_openapi.py

openapi-check: ## Verify docs/api/v1.yaml matches the live FastAPI app (CI)
	@python scripts/generate_openapi.py > /dev/null
	@git diff --exit-code docs/api/v1.yaml \
		|| (echo ""; \
		    echo "docs/api/v1.yaml is out of date. Run 'make openapi' and commit the diff."; \
		    exit 1)
