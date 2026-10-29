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

from tests.unit.learning._registry_fixture import build_seeded_registry
from trellis.feedback.attribution import (
    is_sectioned_payload,
    lookup_pack_item_ids,
    lookup_pack_items_by_strategy,
    served_item_ids,
)
from trellis.learning import analyze_learning_observations
from trellis.learning.judged_outcomes import summarize_judged_outcomes
from trellis.learning.pack_observations import (
    build_learning_observations_from_event_log,
    derive_selection_efficiency,
)
from trellis.ops import ParameterRegistry
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.pack_replay import replay_graph_selection
from trellis.retrieve.pack_value import summarize_pack_value
from trellis.retrieve.strategies import SearchStrategy
from trellis.retrieve.telemetry import analyze_pack_telemetry
from trellis.schemas.pack import PackItem, PackSection, SectionRequest
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
def learning_registry() -> ParameterRegistry:
    return build_seeded_registry()


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
    pack = _builder(event_log).build_sectioned(
        "rotate keys", sections=_SECTIONS, clearance="restricted"
    )
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
        flat = _builder(event_log).build("rotate keys", clearance="restricted")
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
            "rotate keys", sections=_SECTIONS, clearance="restricted"
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
        flat = _builder(event_log).build("rotate keys", clearance="restricted")
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

    def test_the_note_is_true_of_a_pack_that_never_had_rows(
        self, event_log: SQLiteEventLog
    ) -> None:
        """A pack written before #958 has ``sections`` and no rows at all."""
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="pack_builder",
            entity_id="pack-historic",
            payload={"sections": [{"name": "reference", "item_ids": ["a"]}]},
        )
        report = analyze_pack_telemetry(event_log, days=30)

        assert report.total_packs == 1
        (note,) = [n for n in report.notes if "sectioned" in n]
        assert "before #958" in note
        assert "no injected" in note

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


def _named(name: str, affinity: str, max_items: int) -> SectionRequest:
    return SectionRequest(
        name=name,
        retrieval_affinities=[affinity],
        max_items=max_items,
        max_tokens=2600,
    )


class TestSameNamedSections:
    """Section names are caller-supplied and nothing makes them unique.

    The builder used to key its per-section selections by name, so a second
    ``dup`` overwrote the first's items and then both passed the "best
    section" test, serving one item twice and dropping another. One
    unhelpful citation of the doubled item then joined as two.
    """

    def _same_named_pack(self, event_log: SQLiteEventLog) -> tuple[Any, str]:
        pack = _builder(event_log).build_sectioned(
            "rotate keys",
            sections=[
                _named("dup", "reference", 2),
                _named("dup", "domain_knowledge", 1),
            ],
            clearance="restricted",
        )
        return pack, pack.pack_id

    def test_each_same_named_section_keeps_its_own_items(
        self, event_log: SQLiteEventLog
    ) -> None:
        pack, _ = self._same_named_pack(event_log)

        assert [s.name for s in pack.sections] == ["dup", "dup"]
        assert [[i.item_id for i in s.items] for s in pack.sections] == [
            ["doc-alpha", "doc-charlie"],
            ["doc-bravo"],
        ]
        assert [s.retrieval_report.candidates_found for s in pack.sections] == [2, 1]

    def test_item_ids_are_unique_across_the_emitted_rows(
        self, event_log: SQLiteEventLog
    ) -> None:
        _, pack_id = self._same_named_pack(event_log)
        rows = _payload(event_log, pack_id)["injected_items"]

        ids = [row["item_id"] for row in rows]
        assert sorted(ids) == ["doc-alpha", "doc-bravo", "doc-charlie"]
        assert len(ids) == len(set(ids))

    def test_an_item_two_same_named_sections_both_match_is_served_once(
        self, event_log: SQLiteEventLog
    ) -> None:
        pack = _builder(event_log).build_sectioned(
            "rotate keys",
            sections=[_named("dup", "reference", 1), _named("dup", "reference", 1)],
            clearance="restricted",
        )
        rows = _payload(event_log, pack.pack_id)["injected_items"]

        assert [row["item_id"] for row in rows] == ["doc-alpha"]
        assert [len(s.items) for s in pack.sections] == [1, 0]

    def test_the_row_helper_keeps_the_first_row_of_a_shared_item(self) -> None:
        """The invariant holds structurally, not only because the loop above
        happens to dedupe: hand-built sections that share an item."""
        shared = _items()[0]
        sections = [
            PackSection(name="first", items=[shared]),
            PackSection(name="second", items=[shared, _items()[1]]),
        ]

        rows = PackBuilder._sectioned_injected_item_rows(sections)

        assert [(row["section"], row["item_id"]) for row in rows] == [
            ("first", "doc-alpha"),
            ("second", "doc-bravo"),
        ]

    def test_one_unhelpful_citation_joins_as_one(
        self, event_log: SQLiteEventLog, learning_registry: ParameterRegistry
    ) -> None:
        """The harm, end to end: the demotion screen needs two unhelpful
        citations, so a doubled row must not turn one into two."""
        _, pack_id = self._same_named_pack(event_log)
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="mcp",
            entity_id=pack_id,
            payload={
                "pack_id": pack_id,
                "helpful_item_ids": ["doc-alpha"],
                "unhelpful_item_ids": ["doc-bravo"],
                "rating": 0.4,
                "success": False,
            },
        )

        observations = build_learning_observations_from_event_log(event_log, days=30)
        verdicts = [
            item["cited_unhelpful"]
            for obs in observations
            for item in obs["items"]
            if item["item_id"] == "doc-bravo"
        ]
        assert verdicts == [True]

        report = analyze_learning_observations(
            observations=observations, registry=learning_registry, min_support=1
        )
        bravo = [
            c
            for c in report["candidates"]
            if c.get("item_id") == "doc-bravo" and "citation_evidence" in c
        ]
        assert bravo, report["candidates"]
        assert {c["citation_evidence"]["unhelpful_count"] for c in bravo} == {1}
