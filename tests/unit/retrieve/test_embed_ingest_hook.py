"""Tests for the shared post-ingest document->vector embedding hook.

The hook is fail-soft and feature-flagged. These tests cover the
contract guarantees the wiring depends on:

* flag off      -> returns ``None`` and touches nothing.
* flag on       -> embeds and upserts a vector keyed by ``doc_id``.
* unavailable   -> missing embedding_fn / vector store no-ops with a
                   reason instead of failing the ingest.
* failure       -> caught + logged, returns an error summary, never raises.
* row shape     -> ``build_vector_row`` carries the ``content`` excerpt
                   SemanticSearch renders, the doc metadata, and a
                   recency stamp.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trellis.retrieve.embed_ingest_hook import (
    EMBED_INPUT_CHAR_CAP,
    EMBED_ON_INGEST_FLAG,
    VECTOR_METADATA_EXCERPT_CHARS,
    build_vector_row,
    embed_on_ingest_enabled,
    run_embed_on_ingest,
)

_EMBEDDING = [0.1, 0.2, 0.3]


def _registry(
    *,
    embedding_fn: object = "default",
    vector_store: object = "default",
) -> MagicMock:
    """Registry double with configurable embedder / vector store."""
    registry = MagicMock()
    registry.embedding_fn = (
        (lambda text: list(_EMBEDDING)) if embedding_fn == "default" else embedding_fn
    )
    if vector_store == "default":
        registry.knowledge.vector_store = MagicMock()
    else:
        registry.knowledge.vector_store = vector_store
    return registry


class TestFlag:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(EMBED_ON_INGEST_FLAG, raising=False)
        assert embed_on_ingest_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "On"])
    def test_truthy_spellings(self, monkeypatch: pytest.MonkeyPatch, val: str) -> None:
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, val)
        assert embed_on_ingest_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_falsy_spellings(self, monkeypatch: pytest.MonkeyPatch, val: str) -> None:
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, val)
        assert embed_on_ingest_enabled() is False


class TestBuildVectorRow:
    def test_row_shape(self) -> None:
        row = build_vector_row(
            "doc-1",
            "some content",
            {"domain": "backend"},
            lambda text: list(_EMBEDDING),
        )
        assert row["item_id"] == "doc-1"
        assert row["vector"] == _EMBEDDING
        assert row["metadata"]["doc_id"] == "doc-1"
        assert row["metadata"]["content"] == "some content"
        assert row["metadata"]["domain"] == "backend"
        assert row["metadata"]["created_at"]  # recency stamp present

    def test_embed_input_capped(self) -> None:
        seen: list[str] = []

        def embedder(text: str) -> list[float]:
            seen.append(text)
            return list(_EMBEDDING)

        build_vector_row("doc-1", "x" * (EMBED_INPUT_CHAR_CAP + 500), None, embedder)
        assert len(seen[0]) == EMBED_INPUT_CHAR_CAP

    def test_metadata_content_excerpt_capped(self) -> None:
        row = build_vector_row(
            "doc-1",
            "y" * (VECTOR_METADATA_EXCERPT_CHARS + 500),
            None,
            lambda text: list(_EMBEDDING),
        )
        assert len(row["metadata"]["content"]) == VECTOR_METADATA_EXCERPT_CHARS

    def test_explicit_created_at_wins_over_stamp(self) -> None:
        row = build_vector_row(
            "doc-1",
            "content",
            None,
            lambda text: list(_EMBEDDING),
            created_at="2026-01-01T00:00:00+00:00",
        )
        assert row["metadata"]["created_at"] == "2026-01-01T00:00:00+00:00"

    def test_document_metadata_created_at_not_clobbered(self) -> None:
        row = build_vector_row(
            "doc-1",
            "content",
            {"created_at": "2025-06-01T00:00:00+00:00"},
            lambda text: list(_EMBEDDING),
        )
        assert row["metadata"]["created_at"] == "2025-06-01T00:00:00+00:00"


class TestHook:
    def test_flag_off_returns_none_and_touches_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(EMBED_ON_INGEST_FLAG, raising=False)
        registry = _registry()
        assert run_embed_on_ingest(registry, "d1", "content", source="t") is None
        registry.knowledge.vector_store.upsert.assert_not_called()

    def test_flag_on_upserts_vector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        summary = run_embed_on_ingest(
            registry, "d1", "content", {"domain": "backend"}, source="t"
        )
        assert summary == {"embedded": True, "dimensions": len(_EMBEDDING)}
        registry.knowledge.vector_store.upsert.assert_called_once()
        kwargs = registry.knowledge.vector_store.upsert.call_args.kwargs
        assert kwargs["item_id"] == "d1"
        assert kwargs["vector"] == _EMBEDDING
        assert kwargs["metadata"]["content"] == "content"
        assert kwargs["metadata"]["domain"] == "backend"

    def test_empty_content_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        summary = run_embed_on_ingest(registry, "d1", "   ", source="t")
        assert summary == {"embedded": False, "reason": "empty_content"}
        registry.knowledge.vector_store.upsert.assert_not_called()

    def test_missing_embedding_fn_noops(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry(embedding_fn=None)
        summary = run_embed_on_ingest(registry, "d1", "content", source="t")
        assert summary is not None
        assert summary["embedded"] is False
        registry.knowledge.vector_store.upsert.assert_not_called()

    def test_missing_vector_store_noops(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry(vector_store=None)
        summary = run_embed_on_ingest(registry, "d1", "content", source="t")
        assert summary is not None
        assert summary["embedded"] is False

    def test_embedder_failure_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")

        def broken(text: str) -> list[float]:
            msg = "embedder down"
            raise RuntimeError(msg)

        registry = _registry(embedding_fn=broken)
        summary = run_embed_on_ingest(registry, "d1", "content", source="t")
        assert summary == {"embedded": False, "reason": "embedder down"}
        registry.knowledge.vector_store.upsert.assert_not_called()

    def test_embedder_resolve_failure_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bad TRELLIS_EMBEDDING_FN path raises at property-resolve time —
        that too must never fail the ingest."""
        from unittest.mock import PropertyMock

        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        type(registry).embedding_fn = PropertyMock(
            side_effect=RuntimeError("no module named 'typo'")
        )
        summary = run_embed_on_ingest(registry, "d1", "content", source="t")
        assert summary is not None
        assert summary["embedded"] is False
        assert "typo" in summary["reason"]

    def test_upsert_failure_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        registry.knowledge.vector_store.upsert.side_effect = RuntimeError("db down")
        summary = run_embed_on_ingest(registry, "d1", "content", source="t")
        assert summary is not None
        assert summary["embedded"] is False
        assert "db down" in summary["reason"]
