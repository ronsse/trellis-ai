"""Tests for classify-on-write (``trellis.classify.ingest``).

Covers the flag gating, the tag/importance shape returned for the document
metadata, the deliberate ``domain``-drop safety (the only hard-excluding
facet), and the fail-soft contract (a classifier error must never surface).

``TestClassifyMetadataOnWrite`` covers the single-document seam the MCP and
REST write paths share — the four safety properties in one place, so the
per-path tests only have to prove they call it at the right moment.
"""

from __future__ import annotations

import pytest

import trellis.classify.ingest as ingest_mod
from trellis.classify.ingest import (
    CLASSIFY_ON_INGEST_FLAG,
    build_ingest_classifier,
    classify_for_ingest,
    classify_metadata_on_write,
    classify_on_ingest_enabled,
    get_ingest_classifier,
)


class _BoomPipeline:
    """A pipeline whose every classification raises."""

    def classify(self, *args: object, **kwargs: object) -> None:
        msg = "classifier exploded"
        raise RuntimeError(msg)


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
        # A classification error must never propagate — ingest continues untagged.
        out = classify_for_ingest(_BoomPipeline(), "content", doc_id="corpus:x:1")
        assert out == {}


class TestGetIngestClassifier:
    def test_returns_the_same_pipeline(self) -> None:
        # Built once per process: the MCP server and the REST app write one
        # document per call and must not rebuild the classifiers each time.
        assert get_ingest_classifier() is get_ingest_classifier()


class TestClassifyMetadataOnWrite:
    """The shared MCP/REST seam — the four safety properties, in one place."""

    _CONTENT = "kubernetes deployment infra helm terraform"

    def test_flag_off_returns_metadata_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CLASSIFY_ON_INGEST_FLAG, raising=False)
        meta = {"source": "test"}
        assert classify_metadata_on_write(meta, self._CONTENT) is meta

    def test_flag_on_adds_tags_and_importance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")
        out = classify_metadata_on_write({"source": "test"}, self._CONTENT)
        assert out["source"] == "test"
        assert out["content_tags"]["signal_quality"]
        assert isinstance(out["auto_importance"], float)

    def test_does_not_mutate_the_callers_mapping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")
        meta: dict[str, object] = {"source": "test"}
        classify_metadata_on_write(meta, self._CONTENT)
        assert meta == {"source": "test"}

    def test_drops_domain_facet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")
        out = classify_metadata_on_write({}, self._CONTENT, source_system="obsidian")
        assert out["content_tags"]["domain"] == []

    def test_existing_content_tags_not_clobbered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fill-if-absent: an enrichment-written tag set (which DOES carry a
        # domain) must survive a re-write untouched.
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")
        meta = {"content_tags": {"domain": ["backend"]}}
        assert classify_metadata_on_write(meta, self._CONTENT) is meta

    def test_empty_content_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")
        meta = {"source": "test"}
        assert classify_metadata_on_write(meta, "   ") is meta

    def test_fail_soft_on_classifier_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")
        monkeypatch.setattr(ingest_mod, "_ingest_classifier", _BoomPipeline())
        meta = {"source": "test"}
        assert classify_metadata_on_write(meta, self._CONTENT) is meta

    def test_fail_soft_on_pipeline_build_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> None:
            msg = "no pipeline for you"
            raise RuntimeError(msg)

        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")
        monkeypatch.setattr(ingest_mod, "_ingest_classifier", None)
        monkeypatch.setattr(ingest_mod, "build_ingest_classifier", _boom)
        meta = {"source": "test"}
        assert classify_metadata_on_write(meta, self._CONTENT) is meta

    @pytest.mark.parametrize("bad_metadata", [None, 3, "oops", ["a"]])
    def test_non_mapping_metadata_degrades_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch, bad_metadata: object
    ) -> None:
        # Fail-soft has to be TOTAL, not just late: an API caller can send
        # ``"metadata": null`` and a durable write must not 500 on it. The
        # guards live inside the try for exactly this.
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")
        assert classify_metadata_on_write(bad_metadata, self._CONTENT) is bad_metadata  # type: ignore[arg-type]

    def test_source_system_defaults_to_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``dbt`` is the one source system that yields a retrieval_affinity,
        # so it proves the context reached the classifiers.
        monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")
        out = classify_metadata_on_write({"source_system": "dbt"}, self._CONTENT)
        assert out["content_tags"]["retrieval_affinity"]
