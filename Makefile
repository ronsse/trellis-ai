.DEFAULT_GOAL := help

.PHONY: help install install-dev lint format typecheck test check clean

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
