"""Scenario 5.2 — synthetic traces end-to-end.

Generate synthetic agent traces with known ground-truth entities,
ingest them, mirror minimal entity extraction into the graph + document
stores, then build packs for follow-up queries and score them with
``evaluate_pack()``. Closes the retrieval-quality regression-detection
loop.

See ``scenario.py`` for the run entry point.
"""
