"""Shared bulk-write input validators.

Both :class:`~trellis.stores.base.graph.GraphStore` and
:class:`~trellis.stores.base.vector.VectorStore` bulk methods share
the same per-row "required key is present and non-None" pre-pass.
Factoring it here keeps the message format (``method[i]: missing
required key 'foo'``) consistent across every backend so callers
that ``except ValueError`` and parse the index don't need backend-
specific branches.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def validate_bulk_required_keys(
    items: list[dict[str, Any]],
    required_keys: Iterable[str],
    method_name: str,
) -> None:
    """Raise ``ValueError`` on the first row missing any required key.

    A key counts as "missing" if it's absent from the row OR present
    with a ``None`` value — bulk callers must ship every required
    field with a real value, since downstream Cypher/SQL writes have
    NOT NULL constraints on the same columns.

    Args:
        items: Bulk payload — each entry is a row dict.
        required_keys: Keys that must be present + non-None on every
            row. Iteration order determines which key the error names
            when multiple are missing.
        method_name: Caller's public method name (e.g.
            ``"upsert_bulk"``, ``"upsert_edges_bulk"``). Embedded in
            the error message so the row index is unambiguous.

    Raises:
        ValueError: ``"{method_name}[{i}]: missing required key {key!r}"``
            on the first offending row.
    """
    for i, spec in enumerate(items):
        for key in required_keys:
            if key not in spec or spec[key] is None:
                msg = f"{method_name}[{i}]: missing required key {key!r}"
                raise ValueError(msg)
