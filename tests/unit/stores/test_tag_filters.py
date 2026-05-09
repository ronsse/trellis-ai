"""Unit tests for the shared content_tags operator parser.

Pins the contract that SQLite and Postgres document stores both rely
on. The DSL accepts exactly one shape — a single-key operator dict —
and rejects everything else loudly. Silent no-ops are the failure
mode the DSL was designed to eliminate.
"""

from __future__ import annotations

import pytest

from trellis.stores.base.tag_filters import normalize_facet_filter


class TestOperatorDictForm:
    """The four supported operators."""

    def test_in_returns_in_with_values(self) -> None:
        assert normalize_facet_filter({"in": ["a", "b"]}) == (
            "in",
            ["a", "b"],
        )

    def test_not_in_returns_not_in_with_values(self) -> None:
        assert normalize_facet_filter({"not_in": ["noise"]}) == (
            "not_in",
            ["noise"],
        )

    def test_eq_normalizes_to_single_element_in(self) -> None:
        """``eq`` is sugar — it rewrites to single-element ``in`` so
        backends only have to handle two operator shapes."""
        assert normalize_facet_filter({"eq": "high"}) == ("in", ["high"])

    def test_ne_normalizes_to_single_element_not_in(self) -> None:
        assert normalize_facet_filter({"ne": "noise"}) == (
            "not_in",
            ["noise"],
        )

    def test_in_with_empty_list_returns_none(self) -> None:
        """Empty operator value list is the documented opt-out at
        runtime — used by callers that compute an allowlist that may
        end up empty. Returning ``None`` makes the calling code drop
        the facet from the WHERE clause."""
        assert normalize_facet_filter({"in": []}) is None

    def test_not_in_with_empty_list_returns_none(self) -> None:
        """Empty ``not_in`` is degenerate — there's nothing being
        excluded — so the facet should be dropped, not pinned at
        'match everything tagged'."""
        assert normalize_facet_filter({"not_in": []}) is None

    def test_in_with_single_element(self) -> None:
        assert normalize_facet_filter({"in": ["solo"]}) == ("in", ["solo"])


class TestRejectedShapes:
    """Inputs that aren't single-key operator dicts must raise. Bare
    lists, bare scalars, and ``None`` were all silently accepted in an
    earlier permissive design — that silent acceptance is exactly the
    bug surface this DSL was designed to eliminate."""

    def test_bare_list_raises(self) -> None:
        """The legacy implicit-``in`` shape is no longer accepted —
        callers must spell ``{"in": [...]}`` explicitly."""
        with pytest.raises(ValueError, match="single-key operator dict"):
            normalize_facet_filter(["a", "b"])

    def test_empty_list_raises(self) -> None:
        with pytest.raises(ValueError, match="single-key operator dict"):
            normalize_facet_filter([])

    def test_bare_scalar_string_raises(self) -> None:
        with pytest.raises(ValueError, match="single-key operator dict"):
            normalize_facet_filter("high")

    def test_bare_scalar_int_raises(self) -> None:
        with pytest.raises(ValueError, match="single-key operator dict"):
            normalize_facet_filter(42)

    def test_none_raises(self) -> None:
        with pytest.raises(ValueError, match="single-key operator dict"):
            normalize_facet_filter(None)


class TestValidationErrors:
    """Malformed operator dicts surface as ``ValueError``."""

    def test_unknown_operator_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown operator"):
            normalize_facet_filter({"contains": ["x"]})

    def test_multiple_keys_raise(self) -> None:
        with pytest.raises(ValueError, match="exactly one key"):
            normalize_facet_filter({"in": ["a"], "not_in": ["b"]})

    def test_zero_keys_raise(self) -> None:
        """Empty dict is a degenerate operator dict — surface, don't
        silently drop."""
        with pytest.raises(ValueError, match="exactly one key"):
            normalize_facet_filter({})

    def test_in_with_non_list_payload_raises(self) -> None:
        with pytest.raises(ValueError, match="'in' requires a list"):
            normalize_facet_filter({"in": "high"})

    def test_not_in_with_non_list_payload_raises(self) -> None:
        with pytest.raises(ValueError, match="'not_in' requires a list"):
            normalize_facet_filter({"not_in": "noise"})

    def test_eq_with_list_payload_raises(self) -> None:
        """Reject ``eq`` with a list — keeps the contract crisp.
        Callers wanting set semantics should spell ``in``."""
        with pytest.raises(ValueError, match="'eq' requires a scalar"):
            normalize_facet_filter({"eq": ["a", "b"]})

    def test_ne_with_list_payload_raises(self) -> None:
        with pytest.raises(ValueError, match="'ne' requires a scalar"):
            normalize_facet_filter({"ne": ["noise"]})

    def test_eq_with_dict_payload_raises(self) -> None:
        """Nested dict on a scalar operator is never meaningful."""
        with pytest.raises(ValueError, match="'eq' requires a scalar"):
            normalize_facet_filter({"eq": {"nested": "thing"}})
