"""Backend-agnostic contract tests for stores.

See ``docs/design/adr-canonical-graph-layer.md`` for the rationale.
The base class :class:`GraphStoreContractTests` defines tests that
every ``GraphStore`` backend must pass; per-backend modules subclass
it and provide a fixture that yields a fresh store instance.
"""
