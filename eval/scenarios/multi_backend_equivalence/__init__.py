"""Scenario 5.1 — multi-backend equivalence.

Generate the same synthetic graph, ingest into every configured
backend, run a fixed query mix, diff results. Surfaces any drift
between SQLite / Postgres / Neo4j on identical input.

See ``scenario.py`` for the run entry point and the README for the
judgment calls (set vs ordered diffs, vector recall@k tolerance, etc.).
"""
