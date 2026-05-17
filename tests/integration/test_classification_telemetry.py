"""End-to-end ``CLASSIFICATION_DEGRADED`` telemetry — regression guard.

Asserts the production wiring path:

* :func:`~trellis.classify.classifiers.llm.build_llm_facet_classifier`
  threads ``registry.operational.event_log`` into
  :class:`~trellis.classify.classifiers.llm.LLMFacetClassifier`.
* When the wrapped :class:`~trellis_workers.enrichment.service.EnrichmentService`
  fails, the classifier emits
  :attr:`~trellis.stores.base.event_log.EventType.CLASSIFICATION_DEGRADED`
  to the same EventLog backend the rest of the operational plane writes to.

Without this guard, production callers can construct ``LLMFacetClassifier``
directly with the bare constructor (``event_log=None``), the emit silently
no-ops, and the new telemetry is dormant in deployed systems even though
:mod:`tests.unit.classify.test_llm` proves the emit path itself works.

Module-level ``pytestmark = []`` overrides the parent
``tests/integration/conftest.py`` mark (``pytest.mark.neo4j``) because
this test is fully SQLite-only — wiring it under the neo4j marker would
make it skip in default CI runs, defeating the regression-guard purpose.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.classify.classifiers.llm import (
    LLMFacetClassifier,
    build_llm_facet_classifier,
)
from trellis.classify.protocol import ClassificationContext
from trellis.llm import LLMResponse, Message
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_workers.enrichment.service import EnrichmentService

# Override the parent conftest's ``[pytest.mark.neo4j]`` — this test
# uses only SQLite-backed stores and must run in the default selection.
pytestmark: list[Any] = []


class _FailingLLM:
    """LLM stub that always raises — drives EnrichmentService into
    ``success=False`` so the classifier exercises the degraded path.

    Matches the FakeLLM shape from ``tests/unit/classify/test_llm_classifier.py``
    but inverts the contract (always raises). The broad ``except`` in
    :meth:`EnrichmentService.enrich` will catch this and return
    ``EnrichmentResult(success=False, error=...)``.
    """

    async def generate(
        self,
        *,
        messages: list[Message],
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse:
        msg = "simulated LLM outage"
        raise RuntimeError(msg)


@pytest.fixture
def sqlite_registry(tmp_path: Path):
    """Plane-split SQLite registry — same shape as
    ``tests/unit/eval/test_agent_loop_convergence_degraded.py``.

    Uses the plane-split config shape to make sure the builder reads
    ``registry.operational.event_log`` (not a flat ``event_log`` legacy
    fallback) — that's the path production code uses through
    :meth:`StoreRegistry.from_config_dir`.
    """
    config = {
        "knowledge": {
            "graph": {"backend": "sqlite"},
            "vector": {"backend": "sqlite"},
            "document": {"backend": "sqlite"},
            "blob": {"backend": "local"},
        },
        "operational": {
            "trace": {"backend": "sqlite"},
            "event_log": {"backend": "sqlite"},
        },
    }
    with StoreRegistry(config=config, stores_dir=tmp_path) as registry:
        yield registry


class TestBuilderWiresEventLog:
    """The builder threads the registry's EventLog into the classifier."""

    def test_builder_returns_llm_facet_classifier(self, sqlite_registry) -> None:
        svc = EnrichmentService(llm=_FailingLLM())
        classifier = build_llm_facet_classifier(sqlite_registry, enrichment_service=svc)
        assert isinstance(classifier, LLMFacetClassifier)
        assert classifier.name == "llm_facet"

    def test_builder_threads_operational_event_log(self, sqlite_registry) -> None:
        """The classifier's ``_event_log`` is the registry's EventLog.

        Sanity check on the wiring seam itself — the end-to-end test
        below is the real regression guard, but this catches the
        builder regressing to ``event_log=None`` even if some later
        change broke the emit path.
        """
        svc = EnrichmentService(llm=_FailingLLM())
        classifier = build_llm_facet_classifier(sqlite_registry, enrichment_service=svc)
        # _event_log is private but load-bearing for this contract;
        # a public ``event_log`` property would be the right shape if
        # we ever need this externally. For now: the regression guard
        # is end-to-end (test_enrichment_failure_emits_degraded_event).
        assert classifier._event_log is sqlite_registry.operational.event_log


class TestEndToEndDegradedEmit:
    """The end-to-end regression guard.

    Constructs the full production-shaped chain — registry, LLM,
    EnrichmentService, classifier via the builder — drives an
    enrichment failure, and asserts ``CLASSIFICATION_DEGRADED``
    landed in the operational EventLog with the documented payload.
    """

    async def test_enrichment_failure_emits_degraded_event(
        self, sqlite_registry
    ) -> None:
        svc = EnrichmentService(llm=_FailingLLM())
        classifier = build_llm_facet_classifier(sqlite_registry, enrichment_service=svc)

        ctx = ClassificationContext(node_id="prod-node-7", title="ops runbook")
        result = await classifier.classify_async(
            "anything — LLM will raise", context=ctx
        )

        # Sentinel result — telemetry is additive, classifier still
        # degrades gracefully per the documented contract.
        assert result.needs_llm_review is True
        assert result.confidence == 0.0
        assert result.classifier_name == "llm_facet"

        # The end-to-end regression guard: the event landed in the
        # registry's operational EventLog.
        event_log = sqlite_registry.operational.event_log
        events = event_log.get_events(event_type=EventType.CLASSIFICATION_DEGRADED)
        assert len(events) == 1, (
            f"expected exactly one CLASSIFICATION_DEGRADED event, "
            f"got {len(events)}: {events}"
        )

        event = events[0]
        assert event.source == "llm_facet"
        assert event.entity_id == "prod-node-7"

        payload = event.payload
        assert payload["classifier_id"] == "llm_facet"
        assert payload["upstream_failure_kind"] == "enrichment_failure"
        assert payload["subject_entity_id"] == "prod-node-7"
        assert payload["degraded_to"] == "needs_llm_review"

    async def test_bare_constructor_silently_no_ops(self, sqlite_registry) -> None:
        """Confirms the bug this builder exists to prevent.

        If a future change makes some production caller construct
        ``LLMFacetClassifier`` directly (bypassing the builder), the
        ``event_log`` kwarg defaults to ``None`` and the emit silently
        no-ops. The unit suite proves the emit path works in isolation;
        this regression guard proves the wiring matters: same registry,
        same EnrichmentService, no builder => zero events.
        """
        svc = EnrichmentService(llm=_FailingLLM())
        bare_classifier = LLMFacetClassifier(enrichment_service=svc)

        result = await bare_classifier.classify_async("anything")
        assert result.needs_llm_review is True

        event_log = sqlite_registry.operational.event_log
        events = event_log.get_events(event_type=EventType.CLASSIFICATION_DEGRADED)
        assert events == [], (
            "bare constructor must not write to the registry's EventLog — "
            "if it does, the optional-event-log contract has regressed"
        )
