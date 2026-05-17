.DEFAULT_GOAL := help

.PHONY: help setup install install-dev lint format typecheck test check clean build publish-check verify-wheel hooks hooks-run fix openapi openapi-check secrets-check eval-phase-a

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

setup: install-dev hooks ## One-shot first-time setup: install [dev] deps + register git hooks
	@echo "Setup complete. You can now run: make check"

install: ## Install package
	uv pip install -e .

install-dev: ## Install package with dev + vectors deps (mirrors CI)
	uv pip install -e ".[dev,vectors]"

hooks: ## Install pre-commit git hooks (safe to re-run)
	python -m pre_commit install --install-hooks
	@echo "Hooks registered. They run automatically on `git commit`."

hooks-run: ## Run pre-commit across the whole repo (not just staged files)
	python -m pre_commit run --all-files

fix: ## Auto-fix everything pre-commit can fix (ruff format + ruff --fix + whitespace)
	python -m pre_commit run --all-files || true
	@echo "Any remaining failures above need manual attention."

lint: ## Run linting
	ruff check src/ tests/ eval/

format: ## Format code
	ruff format src/ tests/ eval/
	ruff check --fix src/ tests/ eval/

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

# ---------------------------------------------------------------------------
# Eval / secrets — see docs/design/plan-real-corpus-eval.md
# ---------------------------------------------------------------------------

secrets-check: ## Verify op:// references in .env resolve (no secrets printed)
	@op run --env-file=.env -- python -c "import os, hashlib; \
keys = ['MOONSHOT_API_KEY', 'OPENAI_API_KEY']; \
[print(f'{k}: ' + (f'len={len(os.environ[k])}, sha256_first6={hashlib.sha256(os.environ[k].encode()).hexdigest()[:6]}' if os.environ.get(k) else 'NOT SET')) for k in keys]"

eval-phase-a: ## Run scenario 5.4 with live Moonshot/Kimi via op-resolved secrets (forward-ref: Phase A wiring not yet landed)
	@echo "NOTE: --provider moonshot flag is not yet implemented on eval.runner."
	@echo "This target documents the target shape. Wire the flag as part of Phase A,"
	@echo "then this target becomes runnable. See plan-real-corpus-eval.md §5.1."
	op run --env-file=.env -- python -m eval.runner \
		--scenario agent_loop_convergence \
		--provider moonshot \
		--rounds 100 \
		--feedback-batch-size 10
