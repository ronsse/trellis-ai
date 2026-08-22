"""Tests for the serving-boundary metadata filter."""

from __future__ import annotations

from trellis.retrieve.servable import (
    NON_SERVABLE_METADATA_KEYS,
    servable_metadata,
)
from trellis.schemas.classification import SHADOW_TAGS_KEY


class TestServableMetadata:
    def test_strips_shadow_tags(self) -> None:
        out = servable_metadata({"title": "T", SHADOW_TAGS_KEY: {"domain": ["x"]}})
        assert out == {"title": "T"}

    def test_deny_list_is_narrow(self) -> None:
        """Servable-by-default: a new key must not need registering to be served.

        Guards against the deny-list quietly becoming an allow-list.
        """
        assert {SHADOW_TAGS_KEY} == NON_SERVABLE_METADATA_KEYS

    def test_passes_everything_else_through(self) -> None:
        meta = {
            "title": "T",
            "content_tags": {"domain": ["ops"]},
            "auto_importance": 0.5,
            "source_system": "dbt",
            "a_key_invented_tomorrow": 1,
        }
        assert servable_metadata(meta) == meta

    def test_never_mutates_the_caller_mapping(self) -> None:
        meta = {"title": "T", SHADOW_TAGS_KEY: {"domain": ["x"]}}
        servable_metadata(meta)
        assert SHADOW_TAGS_KEY in meta

    def test_returns_a_copy_even_on_the_fast_path(self) -> None:
        """A strategy splats the result into a PackItem; it must not alias.

        The no-strip path is the common one, so it is also the one where an
        accidental alias would go unnoticed.
        """
        meta = {"title": "T"}
        out = servable_metadata(meta)
        assert out == meta
        assert out is not meta

    def test_none_and_empty(self) -> None:
        assert servable_metadata(None) == {}
        assert servable_metadata({}) == {}
