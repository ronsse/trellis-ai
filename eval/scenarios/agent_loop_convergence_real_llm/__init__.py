"""Agent-loop convergence with a real LLM + real embedder.

See README.md and scenario.py for details. This is a fork of
``agent_loop_convergence`` that adds:

- LLM-generated entity summaries (Moonshot/Kimi via OpenAI-compat).
- OpenAI text-embedding-3-small embeddings for all docs.
- ``SemanticSearch`` strategy alongside ``KeywordSearch`` in the
  PackBuilder.
- Cost / latency / call-count telemetry surfaced in the report metrics.

The synthetic baseline at ``eval/scenarios/agent_loop_convergence``
remains unchanged so per-seed diff comparisons stay meaningful.
"""
