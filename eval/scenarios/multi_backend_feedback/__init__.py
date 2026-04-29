"""Scenario 5.5 — multi-backend feedback loop equivalence.

Runs the agent-loop convergence work (scenario 5.4) against multiple
backend combinations and diffs the loop outputs. Surfaces drift
between SQLite, Postgres, and Neo4j on the EventLog-driven
effectiveness + advisory fitness paths.

See ``scenario.py`` for the run entry point.
"""
