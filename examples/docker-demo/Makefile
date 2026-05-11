# Minimal end-to-end demo for a query-engine agent against Trellis.
#
# Usage from the repo root:
#
#     make -C examples/docker-demo demo
#
# Or from this directory:
#
#     make demo
#
# What it does:
#
#   1. Verifies Trellis is installed (skips the install step if so).
#   2. Runs the sample query agent script, which:
#        a. Loads the cold-start fixture (dbt manifest + OpenLineage)
#           via the extractor path — same code path a real deployment
#           uses.
#        b. Searches for customer-related entities.
#        c. Prints the cross-database routing properties an agent would
#           use to dispatch SQL queries.
#        d. Sketches the feedback loop closing.
#
# Total runtime: < 60 seconds on a laptop.
#
# Why no Docker? In-memory ASGI shim (trellis.testing.in_memory_client)
# achieves the same "git clone, run, see the loop close" goal with much
# less ceremony. A real docker-compose stack (Trellis API container +
# Postgres + agent sidecar) is a v2 follow-up — see ../../docs/agent-guide/
# quickstart-query-agent.md for the deployment-shape walkthrough.

REPO_ROOT := $(realpath ../..)
PYTHON ?= python

.PHONY: demo install check

demo: check
	@echo ">>> Running sample query agent..."
	$(PYTHON) $(REPO_ROOT)/examples/docker-demo/sample_query_agent.py

check:
	@$(PYTHON) -c "import trellis" 2>/dev/null || ( \
	  echo "!! Trellis is not installed in the current Python environment."; \
	  echo "   Run: pip install -e $(REPO_ROOT)"; \
	  exit 1 )

install:
	$(PYTHON) -m pip install -e "$(REPO_ROOT)[dev]"
