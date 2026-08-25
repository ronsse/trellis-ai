"""Vector-column unwrapping is driver-shape agnostic.

``pgvector`` is not pinned, and its psycopg adapter changed shape between
releases: 0.4.x hands back a plain ``list``, 0.5.0 hands back a
``pgvector.Vector`` that implements neither ``__iter__`` nor ``__len__``.
The original ``list(row[1])`` therefore raised
``TypeError: 'Vector' object is not iterable`` on any environment that
resolved the newer wheel — which is what happened when the trellis
containers were rebuilt onto 0.5.0 while the host editable install stayed
on 0.4.2: ``get()`` worked from the CLI and raised inside the containers.

The normaliser therefore lives in the dependency-free ``stores.base.vector``
module beside its inverse ``format_vector_literal``, so these tests run
everywhere — including CI, which does not install the postgres extras and
would otherwise skip exactly the regression they exist to catch.
"""

from __future__ import annotations

import pytest

from trellis.stores.base.vector import as_float_list


class _VectorLike:
    """Stands in for ``pgvector.Vector``: has ``to_list``, is not iterable."""

    def __init__(self, values: list[float]) -> None:
        self._values = values

    def to_list(self) -> list[float]:
        return list(self._values)


class TestAsFloatList:
    def test_plain_list_passes_through(self) -> None:
        assert as_float_list([1.0, 2.0, 3.0]) == [1.0, 2.0, 3.0]

    def test_vector_object_is_unwrapped_via_to_list(self) -> None:
        assert as_float_list(_VectorLike([1.0, 2.0, 3.0])) == [1.0, 2.0, 3.0]

    def test_vector_object_is_not_iterable(self) -> None:
        """Guards the assumption that made the old implementation fail."""
        with pytest.raises(TypeError):
            list(_VectorLike([1.0, 2.0]))

    def test_ints_are_coerced_to_float(self) -> None:
        assert as_float_list([1, 2]) == [1.0, 2.0]

    def test_other_iterables_still_work(self) -> None:
        assert as_float_list((1.0, 2.0)) == [1.0, 2.0]
