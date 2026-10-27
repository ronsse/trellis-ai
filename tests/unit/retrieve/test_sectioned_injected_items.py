"""``build_sectioned`` emits ``injected_items[]`` and its readers price it (#958).

A sectioned ``PACK_ASSEMBLED`` row used to carry ``sections`` and no
per-item rows, so every reader of ``injected_items`` was a flat-only reader by
construction. These tests run a real :class:`PackBuilder` into a real event
log and read the row back through each reader that changed, so a reader that
still assumed "rows means flat" fails on the actual payload, not a hand-built
one.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from trellis.feedback.attribution import (
    is_sectioned_payload,
    lookup_pack_item_ids,
    lookup_pack_items_by_strategy,
    served_item_ids,
)
from trellis.learning.judged_outcomes import summarize_judged_outcomes
from trellis.learning.pack_observations import derive_selection_efficiency
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.pack_replay import replay_graph_selection
from trellis.retrieve.pack_value import summarize_pack_value
from trellis.retrieve.strategies import SearchStrategy
from trellis.retrieve.telemetry import analyze_pack_telemetry
from trellis.schemas.pack import PackItem, SectionRequest
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis_api.routes.explore import _is_attributable, _is_sectioned

_ITEM_SPECS = [
    ("doc-alpha", 0.91, "reference", "Rotate the signing key before the audit window."),
    ("doc-bravo", 0.74, "domain_knowledge", "The ingest worker retries with backoff."),
    ("doc-charlie", 0.52, "reference", "Schema migrations run one table at a time."),
]

_SECTIONS = [
    SectionRequest(
        name="reference",
        retrieval_affinities=["reference"],
        max_items=2,
        max_tokens=900,
    ),
    SectionRequest(
        name="domain",
        retrieval_affinities=["domain_knowledge"],
        max_items=1,
        max_tokens=2600,
    ),
]


def _items() -> list[PackItem]:
    return [
        PackItem(
            item_id=item_id,
            item_type="document",
            excerpt=body,
            relevance_score=score,
            metadata={
                "content_tags": {"retrieval_affinity": [affinity]},
                "source_strategy": "keyword",
                "graph_selection": "caller_seeded",
            },
        )
        for item_id, score, affinity, body in _ITEM_SPECS
    ]


@pytest.fixture
def event_log(tmp_path: Path) -> Iterator[SQLiteEventLog]:
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def _builder(event_log: SQLiteEventLog, rate: float = 0.0) -> PackBuilder:
    strategy = MagicMock(spec=SearchStrategy)
    strategy.name = "keyword"
    strategy.search.return_value = _items()
    return PackBuilder(strategies=[strategy], event_log=event_log, holdout_rate=rate)


def _payload(event_log: SQLiteEventLog, pack_id: str) -> dict[str, Any]:
    events = event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, entity_id=pack_id, limit=5
    )
    assert len(events) == 1
    return dict(events[0].payload)


def _sectioned_payload(event_log: SQLiteEventLog) -> tuple[str, dict[str, Any]]:
    pack = _builder(event_log).build_sectioned("rotate keys", sections=_SECTIONS)
    return pack.pack_id, _payload(event_log, pack.pack_id)


class TestEmit:
    def test_rows_run_sections_in_request_order_with_section_local_rank(
        self, event_log: SQLiteEventLog
    ) -> None:
        _, payload = _sectioned_payload(event_log)

        assert [
            (row["section"], row["item_id"], row["rank"])
            for row in payload["injected_items"]
        ] == [
            ("reference", "doc-alpha", 1),
            ("reference", "doc-charlie", 2),
            ("domain", "doc-bravo", 1),
        ]

    def test_an_item_id_appears_once_per_pack(self, event_log: SQLiteEventLog) -> None:
        _, payload = _sectioned_payload(event_log)
        ids = [row["item_id"] for row in payload["injected_items"]]
        assert len(ids) == len(set(ids))
        assert ids == [
            item_id
            for section in payload["sections"]
            for item_id in section["item_ids"]
        ]

    def test_a_row_is_the_flat_row_plus_section(
        self, event_log: SQLiteEventLog
    ) -> None:
        flat = _builder(event_log).build("rotate keys")
        flat_row = _payload(event_log, flat.pack_id)["injected_items"][0]
        _, payload = _sectioned_payload(event_log)

        for row in payload["injected_items"]:
            assert set(row) - {"section"} == set(flat_row)
        # The caller-seeded stamp #950 added is the one the issue wanted legible.
        assert {row["graph_selection"] for row in payload["injected_items"]} == {
            "caller_seeded"
        }

    def test_no_injected_item_ids_rides_beside_the_rows(
        self, event_log: SQLiteEventLog
    ) -> None:
        """That list gates the noise pass and advisory entity correlation."""
        pack_id, payload = _sectioned_payload(event_log)
        assert "injected_item_ids" not in payload
        assert lookup_pack_item_ids(event_log, pack_id) == []

    def test_a_withheld_pack_emits_no_rows_and_no_new_holdout_key(
        self, event_log: SQLiteEventLog
    ) -> None:
        pack = _builder(event_log, rate=1.0).build_sectioned(
            "rotate keys", sections=_SECTIONS
        )
        payload = _payload(event_log, pack.pack_id)

        assert payload["injected_items"] == []
        assert payload["holdout_sections"]
        assert "holdout_items" not in payload


class TestPredicate:
    def test_a_sectioned_payload_is_sectioned_whether_or_not_it_has_rows(
        self, event_log: SQLiteEventLog
    ) -> None:
        _, payload = _sectioned_payload(event_log)
        assert is_sectioned_payload(payload) is True
        assert is_sectioned_payload({"sections": []}) is True

    def test_a_flat_payload_is_not(self, event_log: SQLiteEventLog) -> None:
        flat = _builder(event_log).build("rotate keys")
        assert is_sectioned_payload(_payload(event_log, flat.pack_id)) is False
        assert is_sectioned_payload({"injected_items": [{"item_id": "a"}]}) is False
        assert is_sectioned_payload({"sections": "no"}) is False


class TestReaders:
    def test_served_item_ids_reads_the_rows(self, event_log: SQLiteEventLog) -> None:
        _, payload = _sectioned_payload(event_log)
        assert served_item_ids(payload) == {"doc-alpha", "doc-charlie", "doc-bravo"}

    def test_strategy_lookup_counts_every_section(
        self, event_log: SQLiteEventLog
    ) -> None:
        pack_id, _ = _sectioned_payload(event_log)
        by_strategy = lookup_pack_items_by_strategy(event_log, pack_id)
        assert {k: sorted(v) for k, v in by_strategy.items()} == {
            "keyword": ["doc-alpha", "doc-bravo", "doc-charlie"]
        }

    def test_pack_value_counts_the_pack_as_attributable(
        self, event_log: SQLiteEventLog
    ) -> None:
        pack_id, _ = _sectioned_payload(event_log)
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="mcp",
            entity_id=pack_id,
            payload={
                "pack_id": pack_id,
                "helpful_item_ids": ["doc-alpha"],
                "unhelpful_item_ids": [],
                "rating": 0.9,
                "success": True,
            },
        )
        report = summarize_pack_value(event_log, days=30)

        assert report.flat_packs == 1
        assert report.sectioned_packs_excluded == 0
        assert not any("sectioned" in note for note in report.notes)

    def test_judged_outcomes_counts_a_sectioned_pack_with_rows(
        self, event_log: SQLiteEventLog
    ) -> None:
        _sectioned_payload(event_log)
        report = summarize_judged_outcomes(event_log)
        assert report.flat_packs == 1
        assert report.sectioned_packs_excluded == 0

    def test_telemetry_ignores_the_rows_but_says_so(
        self, event_log: SQLiteEventLog
    ) -> None:
        _sectioned_payload(event_log)
        report = analyze_pack_telemetry(event_log, days=30)

        assert report.total_packs == 1
        assert report.total_injected_items == 0
        assert report.strategy_contributions == []
        assert any("sectioned" in note for note in report.notes)

    def test_selection_efficiency_is_unobserved_without_rejected_items(
        self, event_log: SQLiteEventLog
    ) -> None:
        _, payload = _sectioned_payload(event_log)
        assert derive_selection_efficiency(payload) is None

    def test_the_explorer_flags_it_sectioned_and_attributable(
        self, event_log: SQLiteEventLog
    ) -> None:
        _, payload = _sectioned_payload(event_log)
        assert _is_sectioned(payload) is True
        assert _is_attributable(payload) is True
        historic = {"sections": [{"name": "reference", "item_ids": ["a"]}]}
        assert _is_sectioned(historic) is True
        assert _is_attributable(historic) is False


class TestFlatOnlyReadersSkipIt:
    def test_graph_selection_replay_prices_no_sectioned_serving(
        self, event_log: SQLiteEventLog
    ) -> None:
        """A replay re-runs a flat walk from ``budget_trace``; a sectioned pack
        has none, so its rows must not be priced as if it did."""
        pack_id, _ = _sectioned_payload(event_log)
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="mcp",
            entity_id=pack_id,
            payload={
                "pack_id": pack_id,
                "helpful_item_ids": ["doc-alpha"],
                "unhelpful_item_ids": [],
                "rating": 0.9,
                "success": True,
            },
        )
        report = replay_graph_selection(event_log, graph_selection="caller_seeded")

        assert report.attributed_packs == 0
        assert report.matched_servings == 0
        assert report.helpful_lost == 0
