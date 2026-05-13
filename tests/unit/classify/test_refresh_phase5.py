"""C2 Phase 5 — telemetry-failure tests for `trellis.classify.refresh`.

Pins the 3 GRACEFUL-DEGRADATION sites in
``src/trellis/classify/refresh.py``:

* L287 — malformed stored tags → ``_default_context_builder`` returns
  ``existing_tags=None`` and logs ``existing_tags_malformed`` at warning
  level with ``exc_info`` (rubric: corrupt rows must remain observable).
* L316 — malformed ``classified_at`` → ``_parse_classified_at`` returns
  None (caller-required signal) and logs ``classified_at_parse_failed``.
* L341 — ``TAGS_REFRESHED`` emit fails → reclassify_item still returns
  refreshed outcome; failure logged via ``logger.exception``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs

from trellis.classify.pipeline import ClassifierPipeline
from trellis.classify.protocol import ClassificationContext, ClassificationResult
from trellis.classify.refresh import (
    _default_context_builder,
    _parse_classified_at,
    reclassify_item,
)


@pytest.fixture
def log_output() -> Iterator[list[dict]]:
    saved = structlog.get_config()
    structlog.configure(
        wrapper_class=structlog.BoundLogger,
        processors=saved.get("processors", []),
    )
    try:
        with capture_logs() as cap:
            yield cap
    finally:
        structlog.configure(**saved)


def _events_with_key(cap: list[dict], event_key: str) -> list[dict]:
    return [e for e in cap if e.get("event") == event_key]


class _StubClassifier:
    def __init__(self, tags: dict[str, list[str]]) -> None:
        self._tags = tags

    @property
    def name(self) -> str:
        return "stub"

    def classify(
        self,
        content: str,
        *,
        context: ClassificationContext | None = None,
    ) -> ClassificationResult:
        return ClassificationResult(
            tags=self._tags,
            confidence=1.0,
            classifier_name="stub",
        )


class _InMemoryDocStore:
    def __init__(self) -> None:
        self._docs: dict[str, dict[str, Any]] = {}

    def put(self, doc_id, content, metadata=None):
        self._docs[doc_id] = {
            "doc_id": doc_id,
            "content": content,
            "metadata": metadata or {},
        }
        return doc_id

    def get(self, doc_id):
        doc = self._docs.get(doc_id)
        if doc is None:
            return None
        return {
            "doc_id": doc["doc_id"],
            "content": doc["content"],
            "metadata": dict(doc.get("metadata") or {}),
        }

    def list_documents(self, *, limit=50, offset=0):
        return list(self._docs.values())[offset : offset + limit]


class TestMalformedExistingTagsGraceful:
    """L287 — malformed ContentTags must not break context build."""

    def test_returns_context_with_none_existing_tags_and_logs(
        self,
        log_output: list[dict],
    ) -> None:
        # ContentTags requires structured fields; pass a junk dict to
        # trigger ValidationError inside model_validate.
        doc: dict[str, Any] = {
            "doc_id": "doc-1",
            "metadata": {
                "content_tags": {"domain": "not-a-list"},  # type: ignore[dict-item]
                "title": "X",
            },
        }
        ctx = _default_context_builder(doc)

        # Primary op succeeded: a ClassificationContext is returned with
        # existing_tags=None.
        assert isinstance(ctx, ClassificationContext)
        assert ctx.existing_tags is None
        assert ctx.title == "X"

        events = _events_with_key(log_output, "existing_tags_malformed")
        assert events, log_output
        assert events[0].get("log_level") == "warning"


class TestParseClassifiedAtGraceful:
    """L316 — malformed classified_at returns None + logs."""

    def test_none_input_returns_none_silently(
        self,
        log_output: list[dict],
    ) -> None:
        assert _parse_classified_at(None) is None
        assert _events_with_key(log_output, "classified_at_parse_failed") == []

    def test_malformed_string_returns_none_and_logs(
        self,
        log_output: list[dict],
    ) -> None:
        assert _parse_classified_at("totally-bogus") is None
        events = _events_with_key(log_output, "classified_at_parse_failed")
        assert events, log_output
        assert events[0].get("log_level") == "warning"
        assert events[0].get("raw") == "totally-bogus"


class TestTagsRefreshedEmitFailureGraceful:
    """L341 — TAGS_REFRESHED emit failure must not roll back the write."""

    def test_emit_failure_does_not_break_refresh(
        self,
        log_output: list[dict],
    ) -> None:
        class _BoomEventLog:
            def emit(self, *_args, **_kwargs):
                msg = "event log down"
                raise RuntimeError(msg)

        store = _InMemoryDocStore()
        store.put("doc-1", "some content about retry patterns", {})
        pipeline = ClassifierPipeline(
            classifiers=[
                _StubClassifier(
                    {"domain": ["engineering"], "content_type": ["procedure"]}
                )
            ]
        )

        outcome = reclassify_item(
            "doc-1",
            pipeline=pipeline,
            document_store=store,
            event_log=_BoomEventLog(),
        )

        # Primary op: tags were refreshed AND persisted to the doc store
        # despite the event-log emit failure.
        assert outcome.refreshed is True
        persisted = store.get("doc-1")
        assert persisted is not None
        assert "content_tags" in persisted["metadata"]

        events = _events_with_key(log_output, "tags_refreshed_emit_failed")
        assert events, log_output
        assert events[0].get("log_level") == "error"
