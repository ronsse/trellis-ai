"""Tests for the domain usage analyzer (WP7 Part 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.analyze.domains import NO_DOMAIN_KEY, analyze_domains
from trellis.schemas.enums import TraceSource
from trellis.schemas.trace import Trace, TraceContext
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.trace import SQLiteTraceStore


@pytest.fixture
def stores(tmp_path: Path):
    trace_store = SQLiteTraceStore(tmp_path / "traces.db")
    document_store = SQLiteDocumentStore(tmp_path / "docs.db")
    event_log = SQLiteEventLog(tmp_path / "events.db")
    yield trace_store, document_store, event_log
    trace_store.close()
    document_store.close()
    event_log.close()


def _trace(domain: str | None) -> Trace:
    return Trace(
        source=TraceSource.AGENT,
        intent="do a thing",
        steps=[],
        context=TraceContext(agent_id="agent-1", domain=domain),
    )


def _emit_pack(event_log: SQLiteEventLog, pack_id: str, domain: str | None) -> None:
    event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=pack_id,
        entity_type="pack",
        payload={"domain": domain, "intent": "x"},
    )


def _emit_feedback(event_log: SQLiteEventLog, pack_id: str, *, success: bool) -> None:
    event_log.emit(
        EventType.FEEDBACK_RECORDED,
        source="test",
        entity_id=pack_id,
        entity_type="pack",
        payload={"pack_id": pack_id, "success": success},
    )


def _by_domain(report) -> dict[str, object]:
    return {row.domain: row for row in report.domains}


class TestAnalyzeDomains:
    def test_empty_stores_yield_empty_report(self, stores) -> None:
        trace_store, document_store, event_log = stores
        report = analyze_domains(trace_store, document_store, event_log)
        assert report.domains == []

    def test_trace_and_document_counts(self, stores) -> None:
        trace_store, document_store, event_log = stores
        trace_store.append(_trace("payments"))
        trace_store.append(_trace("payments"))
        trace_store.append(_trace("backend"))
        # content_tags.domain (canonical) is a list.
        document_store.put("d1", "doc one", {"content_tags": {"domain": ["payments"]}})
        # flat metadata.domain (simple ingest path) is a string.
        document_store.put("d2", "doc two", {"domain": "backend"})

        report = analyze_domains(trace_store, document_store, event_log)
        rows = _by_domain(report)

        assert rows["payments"].trace_count == 2
        assert rows["payments"].document_count == 1
        assert rows["backend"].trace_count == 1
        assert rows["backend"].document_count == 1

    def test_none_row_for_missing_domain(self, stores) -> None:
        trace_store, document_store, event_log = stores
        trace_store.append(_trace(None))
        document_store.put("d1", "no domain doc", {"author": "alice"})

        report = analyze_domains(trace_store, document_store, event_log)
        rows = _by_domain(report)

        assert NO_DOMAIN_KEY in rows
        assert rows[NO_DOMAIN_KEY].trace_count == 1
        assert rows[NO_DOMAIN_KEY].document_count == 1

    def test_multi_domain_document_counts_each(self, stores) -> None:
        trace_store, document_store, event_log = stores
        document_store.put(
            "d1", "spans two", {"content_tags": {"domain": ["payments", "backend"]}}
        )
        report = analyze_domains(trace_store, document_store, event_log)
        rows = _by_domain(report)
        assert rows["payments"].document_count == 1
        assert rows["backend"].document_count == 1

    def test_packs_served_and_success_rate(self, stores) -> None:
        trace_store, document_store, event_log = stores
        # payments: 3 packs, 2 graded (1 success, 1 failure) -> 50%.
        _emit_pack(event_log, "p1", "payments")
        _emit_pack(event_log, "p2", "payments")
        _emit_pack(event_log, "p3", "payments")
        _emit_feedback(event_log, "p1", success=True)
        _emit_feedback(event_log, "p2", success=False)
        # backend: 1 pack, 1 graded success -> 100%.
        _emit_pack(event_log, "p4", "backend")
        _emit_feedback(event_log, "p4", success=True)

        report = analyze_domains(trace_store, document_store, event_log)
        rows = _by_domain(report)

        assert rows["payments"].packs_served == 3
        assert rows["payments"].graded_packs == 2
        assert rows["payments"].graded_successes == 1
        assert rows["payments"].success_rate == pytest.approx(0.5)

        assert rows["backend"].packs_served == 1
        assert rows["backend"].graded_packs == 1
        assert rows["backend"].success_rate == pytest.approx(1.0)

    def test_pack_without_domain_attributed_to_none(self, stores) -> None:
        trace_store, document_store, event_log = stores
        _emit_pack(event_log, "p1", None)
        _emit_feedback(event_log, "p1", success=True)
        report = analyze_domains(trace_store, document_store, event_log)
        rows = _by_domain(report)
        assert rows[NO_DOMAIN_KEY].packs_served == 1
        assert rows[NO_DOMAIN_KEY].graded_packs == 1

    def test_ungraded_pack_has_none_success_rate(self, stores) -> None:
        trace_store, document_store, event_log = stores
        _emit_pack(event_log, "p1", "payments")
        report = analyze_domains(trace_store, document_store, event_log)
        rows = _by_domain(report)
        assert rows["payments"].packs_served == 1
        assert rows["payments"].graded_packs == 0
        assert rows["payments"].success_rate is None

    def test_sorted_by_document_count_desc(self, stores) -> None:
        trace_store, document_store, event_log = stores
        document_store.put("d1", "a", {"domain": "low"})
        document_store.put("d2", "b", {"domain": "high"})
        document_store.put("d3", "c", {"domain": "high"})
        report = analyze_domains(trace_store, document_store, event_log)
        assert report.domains[0].domain == "high"

    def test_payload_shape(self, stores) -> None:
        trace_store, document_store, event_log = stores
        document_store.put("d1", "a", {"domain": "payments"})
        report = analyze_domains(trace_store, document_store, event_log, days=14)
        payload = report.to_payload()
        assert payload["status"] == "ok"
        assert payload["days"] == 14
        assert isinstance(payload["domains"], list)
        row = payload["domains"][0]
        assert set(row) == {
            "domain",
            "document_count",
            "trace_count",
            "packs_served",
            "graded_packs",
            "graded_successes",
            "success_rate",
        }
