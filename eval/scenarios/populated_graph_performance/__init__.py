"""Scenario 5.3 — populated-graph performance baseline.

Loads a populated synthetic graph into every reachable backend, runs a
fixed query mix, records p50/p95/p99 latency per query type per
backend, plus vector recall@10 against a brute-force baseline.

See ``scenario.py`` for the run entry point and the README for the
judgment calls.
"""
