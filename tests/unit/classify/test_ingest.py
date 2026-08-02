"""Tests for classify-on-write (``trellis.classify.ingest``).

Covers the flag gating, the tag/importance shape returned for the document
metadata, the deliberate ``domain``-drop safety (the only hard-excluding
facet), and the fail-soft contract (a classifier error must never surface).
"""

from __future__ import annotations

import pytest

from trellis.classify.ingest import (
    CLASSIFY_METADATA_KEYS,
    CLASSIFY_ON_INGEST_FLAG,
    build_ingest_classifier,
    classify_for_ingest,
    classify_on_ingest_enabled,
)


class TestFlag:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(CLASSIFY_ON_INGEST_FLAG, raising=False)
        assert classify_on_ingest_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "On"])
    def test_truthy_spellings_enable(
        self, monkeypatch: pytest.MonkeyPatch, val: str
    ) -> None:
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, val)
        assert classify_on_ingest_enabled() is True

    @pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "nope"])
    def test_falsy_spellings_disable(
        self, monkeypatch: pytest.MonkeyPatch, val: str
    ) -> None:
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, val)
        assert classify_on_ingest_enabled() is False


class TestClassifyForIngest:
    def test_returns_content_tags_and_importance(self) -> None:
        pipeline = build_ingest_classifier()
        out = classify_for_ingest(
            pipeline,
            "def f():\n    # configuration yaml settings\n    pass\n",
            source_system="obsidian",
            doc_id="corpus:obsidian:x",
        )
        assert set(out) == {"content_tags", "auto_importance"}
        ct = out["content_tags"]
        # signal_quality always resolves (default "standard"); freshness stamps
        # are set so reclassify_stale won't immediately re-touch the row.
        assert ct["signal_quality"]
        assert ct["classified_at"]
        assert ct["importance_scored_at"]
        assert isinstance(out["auto_importance"], float)

    def test_classify_metadata_keys_matches_return_shape(self) -> None:
        """The exported key tuple must track what this function actually
        returns. Callers that relocate a classified document's tags key off
        the constant — corpus sync propagates them to the chunk documents,
        which are the retrievable unit — so a facet added here without
        updating the tuple would silently stop reaching them."""
        pipeline = build_ingest_classifier()
        out = classify_for_ingest(
            pipeline, "## Heading\n\nSome prose about deployment pipelines.\n"
        )
        assert set(out) == set(CLASSIFY_METADATA_KEYS)

    def test_drops_domain_facet_by_default(self) -> None:
        pipeline = build_ingest_classifier()
        # This content is confidently domain-tagged "infrastructure" (see the
        # include_domain test). Auto-classification must NOT persist it: a
        # personal note that happens to mention kubernetes would otherwise be
        # hard-excluded from every other domain-scoped query.
        out = classify_for_ingest(
            pipeline,
            "kubernetes deployment infra helm terraform",
            source_system="obsidian",
        )
        assert out["content_tags"]["domain"] == []

    def test_include_domain_retains_facet(self) -> None:
        pipeline = build_ingest_classifier()
        out = classify_for_ingest(
            pipeline,
            "kubernetes deployment infra helm terraform",
            source_system="obsidian",
            include_domain=True,
        )
        # Proves the drop above is real: the classifier DID assign a domain.
        assert out["content_tags"]["domain"]

    def test_fail_soft_on_classifier_error(self) -> None:
        class _Boom:
            def classify(self, *args: object, **kwargs: object) -> None:
                msg = "classifier exploded"
                raise RuntimeError(msg)

        # A classification error must never propagate — ingest continues untagged.
        out = classify_for_ingest(_Boom(), "content", doc_id="corpus:x:1")
        assert out == {}
