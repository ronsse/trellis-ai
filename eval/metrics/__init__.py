"""Per-scenario metric helpers.

Composable metric functions that scenarios import. Anything that's
clearly Trellis-internal (pack quality dimensions, advisory
effectiveness) stays in ``src/trellis/`` and is *called* from here, not
re-implemented.
"""
