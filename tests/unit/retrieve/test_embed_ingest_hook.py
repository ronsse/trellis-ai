"""Tests for the shared post-ingest document->vector embedding hook.

The hook is fail-soft and feature-flagged. These tests cover the
contract guarantees the wiring depends on:

* flag off      -> returns ``None`` and touches nothing.
* flag on       -> embeds and upserts a vector keyed by ``doc_id``.
* unavailable   -> missing embedding_fn / vector store no-ops with a
                   reason instead of failing the ingest.
* failure       -> caught + logged, returns an error summary, never raises.
* resolve loud  -> an embedder that fails to *resolve* (config error)
                   warns once per distinct (type, setting) per process,
                   not once per document, and the returned reason names
                   only the type and setting, never the exception text.
* row shape     -> ``build_vector_row`` carries the ``content`` excerpt
                   SemanticSearch renders — boundary-cut and size-marked
                   here, the last stage holding the full document — plus
                   the doc metadata and a recency stamp.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trellis.core.elision import format_char_count
from trellis.errors import ConfigError
from trellis.retrieve.embed_ingest_hook import (
    EMBED_INPUT_CHAR_CAP,
    EMBED_ON_INGEST_FLAG,
    VECTOR_METADATA_EXCERPT_CHARS,
    build_vector_row,
    embed_on_ingest_enabled,
    run_embed_on_ingest,
)
from trellis.retrieve.excerpts import EXCERPT_ELLIPSIS

# The once-per-cause dedup cache in ``_warn_resolve_failed_once``
# is reset by the central autouse fixture in tests/conftest.py
# (``_reset_embedder_resolve_failure_log_cache``), which isolates it across
# every test file, not just this one.

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
        assert len(row["metadata"]["content"]) <= VECTOR_METADATA_EXCERPT_CHARS

    def test_metadata_excerpt_is_cut_on_a_boundary_and_marked(self) -> None:
        """The semantic path's cut happens here, where the full doc is (#310).

        ``SemanticSearch`` renders ``PackItem.excerpt`` straight from this
        metadata and never sees the document row, so a raw slice stored
        here is a mid-word cut no later stage can repair — and only this
        stage knows how much it dropped.
        """
        content = (
            "The mutation executor validates every command before it "
            "reaches a store. " + "policy gate words " * 200
        )
        row = build_vector_row("doc-1", content, None, lambda text: list(_EMBEDDING))
        excerpt = row["metadata"]["content"]

        assert len(excerpt) <= VECTOR_METADATA_EXCERPT_CHARS
        body, _, note = excerpt.partition(EXCERPT_ELLIPSIS)
        assert note.strip() == f"[+{format_char_count(len(content) - len(body))} chars]"
        assert content.startswith(body)
        assert content[len(body) : len(body) + 1].isspace(), "cut mid-word"

    def test_short_content_is_stored_verbatim(self) -> None:
        """No cut, no marker — short memories are unchanged."""
        row = build_vector_row(
            "doc-1", "a terse gotcha", None, lambda text: list(_EMBEDDING)
        )
        assert row["metadata"]["content"] == "a terse gotcha"

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
        that too must never fail the ingest, and the exception's own
        message text must never reach the summary (Q8: describe, don't
        quote — a resolve failure can be raised by arbitrary imported
        code, so the message is untrusted)."""
        from unittest.mock import PropertyMock

        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        type(registry).embedding_fn = PropertyMock(
            side_effect=RuntimeError("no module named 'typo'")
        )
        summary = run_embed_on_ingest(registry, "d1", "content", source="t")
        assert summary == {"embedded": False, "reason": "RuntimeError"}
        assert "typo" not in summary["reason"]

    def test_embedder_resolve_failure_reason_names_the_setting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``ConfigError`` names the setting to fix; the reason carries
        it, still without the message text."""
        from unittest.mock import PropertyMock

        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        type(registry).embedding_fn = PropertyMock(
            side_effect=ConfigError(
                "no OPENAI_API_KEY set", setting="embeddings.provider"
            )
        )
        summary = run_embed_on_ingest(registry, "d1", "content", source="t")
        assert summary == {
            "embedded": False,
            "reason": "ConfigError: embeddings.provider",
        }

    def test_embedder_resolve_failure_ignores_a_foreign_unhashable_setting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a ``ConfigError``'s own ``.setting`` is trusted.

        The resolve path can run arbitrary imported code (a bad
        ``TRELLIS_EMBEDDING_FN`` target), so a non-``ConfigError`` exception
        that happens to carry a same-named ``.setting`` attribute — here
        deliberately unhashable — must not reach the
        ``functools.cache``-keyed warning, which would raise ``TypeError:
        unhashable type: 'list'`` and escape the fail-soft contract, nor the
        returned ``reason``.
        """
        from unittest.mock import PropertyMock

        class _ForeignError(RuntimeError):
            def __init__(self) -> None:
                super().__init__("boom")
                self.setting = ["unhashable", "list"]

        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        type(registry).embedding_fn = PropertyMock(side_effect=_ForeignError())

        summary = run_embed_on_ingest(registry, "d1", "content", source="t")

        assert summary == {"embedded": False, "reason": "_ForeignError"}

    def test_embedder_resolve_failure_logged_once_per_distinct_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A persistently-broken embedder fails every ingest the same way.

        Two ingests must produce exactly ONE warning record, not a
        traceback per document — the pre-fix behaviour used
        ``logger.exception`` unconditionally, so this fails at main with
        two ``error`` records instead of one ``warning`` record."""
        from unittest.mock import PropertyMock

        from structlog.testing import capture_logs

        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        type(registry).embedding_fn = PropertyMock(
            side_effect=ConfigError("missing extra", setting="llm-openai")
        )

        with capture_logs() as logs:
            first = run_embed_on_ingest(registry, "d1", "content", source="t")
            second = run_embed_on_ingest(registry, "d2", "content", source="t")

        assert first == {"embedded": False, "reason": "ConfigError: llm-openai"}
        assert second == {"embedded": False, "reason": "ConfigError: llm-openai"}
        matching = [
            entry
            for entry in logs
            if entry["event"] == "embed_on_ingest_resolve_failed"
            and entry["component"] == "embedding_fn"
        ]
        assert len(matching) == 1
        assert matching[0]["log_level"] == "warning"
        assert matching[0]["error_type"] == "ConfigError"
        assert matching[0]["setting"] == "llm-openai"

    def test_embedder_resolve_failure_log_never_carries_the_message_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Describe, don't quote (Q8) binds the LOG line, not only the
        returned ``reason``. A mutant that logs ``setting or str(exc)``
        instead of ``setting`` alone survives every other test in this
        class because they all check ``reason`` or the dedup count, never
        the logged ``setting`` value against the message text — and with
        a ``setting`` present, ``setting or str(exc)`` is indistinguishable
        from ``setting`` alone, so the exception here deliberately carries
        none."""
        from unittest.mock import PropertyMock

        from structlog.testing import capture_logs

        sentinel = "SENTINEL-leak-marker-9f3c"
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        type(registry).embedding_fn = PropertyMock(side_effect=RuntimeError(sentinel))

        with capture_logs() as logs:
            run_embed_on_ingest(registry, "d1", "content", source="t")

        matching = [
            entry
            for entry in logs
            if entry["event"] == "embed_on_ingest_resolve_failed"
            and entry["component"] == "embedding_fn"
        ]
        assert len(matching) == 1
        assert matching[0]["setting"] is None
        for value in matching[0].values():
            assert sentinel not in str(value)

    def test_embedder_resolve_failure_logs_again_for_a_different_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deduping is per-cause, not a global silence switch: a second,
        distinct resolve failure in the same process still warns."""
        from unittest.mock import PropertyMock

        from structlog.testing import capture_logs

        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        type(registry).embedding_fn = PropertyMock(
            side_effect=ConfigError("bad path", setting="TRELLIS_EMBEDDING_FN")
        )

        with capture_logs() as logs:
            run_embed_on_ingest(registry, "d1", "content", source="t")
            type(registry).embedding_fn = PropertyMock(
                side_effect=RuntimeError("different cause")
            )
            run_embed_on_ingest(registry, "d2", "content", source="t")

        matching = [
            entry
            for entry in logs
            if entry["event"] == "embed_on_ingest_resolve_failed"
            and entry["component"] == "embedding_fn"
        ]
        assert len(matching) == 2
        assert {m["error_type"] for m in matching} == {"ConfigError", "RuntimeError"}

    def test_embedder_resolve_failure_logs_again_for_a_different_setting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same exception type, different ``setting`` — still two distinct
        causes, so still two warnings. Dedup keys on BOTH (type, setting);
        a mutant that keys on type alone would wrongly swallow the
        second."""
        from unittest.mock import PropertyMock

        from structlog.testing import capture_logs

        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        type(registry).embedding_fn = PropertyMock(
            side_effect=ConfigError("bad path", setting="TRELLIS_EMBEDDING_FN")
        )

        with capture_logs() as logs:
            run_embed_on_ingest(registry, "d1", "content", source="t")
            type(registry).embedding_fn = PropertyMock(
                side_effect=ConfigError("no key", setting="embeddings.provider")
            )
            run_embed_on_ingest(registry, "d2", "content", source="t")

        matching = [
            entry
            for entry in logs
            if entry["event"] == "embed_on_ingest_resolve_failed"
            and entry["component"] == "embedding_fn"
        ]
        assert len(matching) == 2
        assert {m["setting"] for m in matching} == {
            "TRELLIS_EMBEDDING_FN",
            "embeddings.provider",
        }

    def test_upsert_failure_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        registry.knowledge.vector_store.upsert.side_effect = RuntimeError("db down")
        summary = run_embed_on_ingest(registry, "d1", "content", source="t")
        assert summary is not None
        assert summary["embedded"] is False
        assert "db down" in summary["reason"]


class TestVectorStoreResolve:
    """``registry.knowledge.vector_store`` raising at resolve time (not
    merely being ``None``) must be exactly as fail-soft as a broken
    ``embedding_fn`` resolve (follow-up 3 from #830).

    Before this fix, ``run_embed_on_ingest`` read the vector store with
    ``getattr(registry.knowledge, "vector_store", None)``: the ``getattr``
    default only ever absorbs an ``AttributeError`` raised by the lookup
    itself, so a real backend's ``ConfigError``/``BackendNotInstalledError``
    (or any other exception) from *inside* the property getter propagated
    straight out of the hook — after the document had already been
    durably stored by the caller. Every production caller (MCP
    ``save_memory``, the mutate ``"soft"`` handler, the corpus-sync ingest
    paths, the dbt-manifest CLI ingest) invokes the hook unwrapped, so that
    raise reached the caller's response for a write that had already
    landed, inviting a retry of an already-stored document.
    """

    def test_working_embedder_broken_vector_store_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import PropertyMock

        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        type(registry.knowledge).vector_store = PropertyMock(
            side_effect=RuntimeError("no module named 'typo'")
        )
        summary = run_embed_on_ingest(registry, "d1", "content", source="t")
        assert summary == {"embedded": False, "reason": "RuntimeError"}
        assert "typo" not in summary["reason"]

    def test_vector_store_resolve_failure_reason_names_the_setting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import PropertyMock

        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        type(registry.knowledge).vector_store = PropertyMock(
            side_effect=ConfigError(
                "unknown vector backend 'bogus'", setting="vector.backend"
            )
        )
        summary = run_embed_on_ingest(registry, "d1", "content", source="t")
        assert summary == {
            "embedded": False,
            "reason": "ConfigError: vector.backend",
        }

    def test_vector_store_resolve_failure_ignores_a_foreign_unhashable_setting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import PropertyMock

        class _ForeignError(RuntimeError):
            def __init__(self) -> None:
                super().__init__("boom")
                self.setting = ["unhashable", "list"]

        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        type(registry.knowledge).vector_store = PropertyMock(
            side_effect=_ForeignError()
        )

        summary = run_embed_on_ingest(registry, "d1", "content", source="t")

        assert summary == {"embedded": False, "reason": "_ForeignError"}

    def test_vector_store_resolve_failure_logged_once_per_distinct_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two ingests against a persistently-broken vector store must
        produce exactly ONE warning record — the pre-fix hook raised
        instead of returning at all, so this fails on main with a
        propagated ``ConfigError`` rather than two summaries."""
        from unittest.mock import PropertyMock

        from structlog.testing import capture_logs

        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        type(registry.knowledge).vector_store = PropertyMock(
            side_effect=ConfigError("down", setting="vector.backend")
        )

        with capture_logs() as logs:
            first = run_embed_on_ingest(registry, "d1", "content", source="t")
            second = run_embed_on_ingest(registry, "d2", "content", source="t")

        assert first == {"embedded": False, "reason": "ConfigError: vector.backend"}
        assert second == {"embedded": False, "reason": "ConfigError: vector.backend"}
        matching = [
            entry
            for entry in logs
            if entry["event"] == "embed_on_ingest_resolve_failed"
            and entry["component"] == "vector_store"
        ]
        assert len(matching) == 1
        assert matching[0]["log_level"] == "warning"
        assert matching[0]["error_type"] == "ConfigError"
        assert matching[0]["setting"] == "vector.backend"

    def test_vector_store_resolve_failure_log_never_carries_the_message_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import PropertyMock

        from structlog.testing import capture_logs

        sentinel = "SENTINEL-leak-marker-9f3c-vs"
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        type(registry.knowledge).vector_store = PropertyMock(
            side_effect=RuntimeError(sentinel)
        )

        with capture_logs() as logs:
            run_embed_on_ingest(registry, "d1", "content", source="t")

        matching = [
            entry
            for entry in logs
            if entry["event"] == "embed_on_ingest_resolve_failed"
            and entry["component"] == "vector_store"
        ]
        assert len(matching) == 1
        assert matching[0]["setting"] is None
        for value in matching[0].values():
            assert sentinel not in str(value)

    def test_vector_store_resolve_failure_logs_again_for_a_different_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import PropertyMock

        from structlog.testing import capture_logs

        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        type(registry.knowledge).vector_store = PropertyMock(
            side_effect=ConfigError("bad uri", setting="vector.uri")
        )

        with capture_logs() as logs:
            run_embed_on_ingest(registry, "d1", "content", source="t")
            type(registry.knowledge).vector_store = PropertyMock(
                side_effect=RuntimeError("different cause")
            )
            run_embed_on_ingest(registry, "d2", "content", source="t")

        matching = [
            entry
            for entry in logs
            if entry["event"] == "embed_on_ingest_resolve_failed"
            and entry["component"] == "vector_store"
        ]
        assert len(matching) == 2
        assert {m["error_type"] for m in matching} == {"ConfigError", "RuntimeError"}

    def test_embedder_and_vector_store_failures_with_the_same_cause_both_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``component`` must be part of the dedup cache key, not only the
        log payload.

        An embedder resolve failure and a vector_store resolve failure
        that share the exact same ``(error_type, setting)`` — here both a
        bare ``RuntimeError`` with no ``setting`` — are still two distinct
        causes an operator needs to see. A mutant that drops ``component``
        from the ``functools.cache`` key would let the first call's cache
        entry silently swallow the second component's warning.
        """
        from unittest.mock import PropertyMock

        from structlog.testing import capture_logs

        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        registry = _registry()
        type(registry).embedding_fn = PropertyMock(
            side_effect=RuntimeError("embedder cause")
        )

        other_registry = _registry()
        type(other_registry.knowledge).vector_store = PropertyMock(
            side_effect=RuntimeError("vector store cause")
        )

        with capture_logs() as logs:
            run_embed_on_ingest(registry, "d1", "content", source="t")
            run_embed_on_ingest(other_registry, "d2", "content", source="t")

        matching = [
            entry
            for entry in logs
            if entry["event"] == "embed_on_ingest_resolve_failed"
        ]
        assert len(matching) == 2
        assert {m["component"] for m in matching} == {"embedding_fn", "vector_store"}

    def test_vector_store_resolve_failure_does_not_reach_embedder_or_upsert(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The embedder must never run when the vector store can't be
        resolved — there is nowhere to upsert the result."""
        from unittest.mock import PropertyMock

        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        calls: list[str] = []

        def embedder(text: str) -> list[float]:
            calls.append(text)
            return list(_EMBEDDING)

        registry = _registry(embedding_fn=embedder)
        type(registry.knowledge).vector_store = PropertyMock(
            side_effect=ConfigError("down", setting="vector.backend")
        )

        summary = run_embed_on_ingest(registry, "d1", "content", source="t")

        assert summary == {"embedded": False, "reason": "ConfigError: vector.backend"}
        assert calls == []
