.DEFAULT_GOAL := help

.PHONY: help setup install install-dev lint format typecheck test check clean build publish-check verify-wheel hooks hooks-run fix openapi openapi-check secrets-check docker-build env-check

# Git-derived PEP 440 version for the working tree, e.g. 0.9.0.dev157+gd9939e8ee.
# The Docker build context excludes .git, so this is the only way an image can
# know which commit it was built from — see the ARG in Dockerfile.
BUILD_VERSION := $(shell git describe --tags --abbrev=9 --dirty=.dirty --always \
	| sed -E 's/^v//; s/-([0-9]+)-g/.dev\1+g/')

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

setup: install-dev hooks ## One-shot first-time setup: install [dev] deps + register git hooks
	@echo "Setup complete. You can now run: make check"

install: ## Install package
	uv pip install -e .

install-dev: ## Install package with dev deps (mirrors CI)
	uv pip install -e ".[dev]"

hooks: ## Install pre-commit git hooks (safe to re-run)
	python -m pre_commit install --install-hooks
	@echo "Hooks registered. They run automatically on `git commit`."

hooks-run: ## Run pre-commit across the whole repo (not just staged files)
	python -m pre_commit run --all-files

fix: ## Auto-fix everything pre-commit can fix (ruff format + ruff --fix + whitespace)
	python -m pre_commit run --all-files || true
	@echo "Any remaining failures above need manual attention."

# Both gates below claim to "match CI". Nothing checked that until #398: the
# shared venv drifted onto a Python version CI never runs the gates on, and
# onto a ruff one patch behind the pin, so `make lint` and `make typecheck`
# reported on a different gate from the one that decides the PR — `mypy src/`
# aborting having checked ZERO files in src/, which several agents read as
# clean. A target nobody invokes cannot catch that, so `env-check` is a
# PREREQUISITE of both gates rather than something to remember.
#
# `format` is gated too, since #498. It was the one ruff invocation outside
# the gate, and it is the invocation that WRITES: a drifted `ruff format`
# rewrites every .py file under src/ and tests/ (774 of them at 3f6dd2d) to a
# formatter version CI will then check them against. The damage is caught
# rather than shipped -- `ruff format --check` inside the gated `lint` is what
# catches it -- but "caught" there means an unrelated PR arrives carrying a
# tree-wide reformat, which is the largest diff a version difference can buy
# for the least reason.
#
# Deliberately NOT a prerequisite of `test`: tests.yml runs the full
# 3.11/3.12/3.13 matrix, so a test run on 3.12 is a legitimate thing to do
# and demanding the gate version there would be false.
#
# TRELLIS_ALLOW_ENV_DRIFT=1 downgrades it to a warning for deliberate work in
# a known-drifted environment. Only 1/true/yes/on turn it on; anything else
# (0, false, no, unset) leaves the gate enforcing -- see #498, where =0 used
# to enable it.
env-check: ## Verify this environment runs the same gates as CI (#398)
	python scripts/check_tool_pins.py --check-env

lint: env-check ## Run linting (matches CI: lint rules + formatting)
	ruff check src/ tests/
	ruff format --check src/ tests/

format: env-check ## Format code
	ruff format src/ tests/
	ruff check --fix src/ tests/

typecheck: env-check ## Run type checking
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

docker-build: ## Build the API image stamped with the working tree's git sha
	docker build --build-arg TRELLIS_BUILD_VERSION="$(BUILD_VERSION)" -t trellis-ai .
	@echo "Built trellis-ai stamped as $(BUILD_VERSION)"

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

# eval-phase-a target moved to the trellis-evals repo (2026-07-12) with the
# rest of the evaluation program; see its TODO.md for the Phase A wiring note.
