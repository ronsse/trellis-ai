"""A promoted precedent carries source content and a graph route.

Before this, a promotion minted a node whose description was a generated
statistics sentence and whose ``precedent_applies_to`` edges came from
``candidate["target_entity_ids"]`` — empty for every candidate, because the
scorer fills that from ``PACK_ASSEMBLED.seed_entity_ids`` and no writer of
``PACK_ASSEMBLED`` ever set the key. A flat pack reaches a graph node only
through a seeded expansion or the query-independent recency window, so such
a precedent aged out of the window and was never served.

The chain below runs the real parts end to end on SQLite stores: pack builds
through :func:`~trellis.retrieve.builder_factory.build_pack_builder`, graded
feedback, the scorer, the promotion payloads, the governed submit, and a
later pack. Clutter nodes newer than the precedent are written so the
recency window alone cannot be what reaches it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.unit.learning._registry_fixture import build_seeded_registry
from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import record_feedback
from trellis.learning import (
    PROMOTE_RECOMMENDATIONS,
    analyze_learning_observations,
    build_learning_observations_from_event_log,
    build_learning_promotion_payloads,
    submit_learning_promotion,
)
from trellis.learning.scoring import (
    PRECEDENT_SOURCE_EXCERPT_MAX_CHARS,
    SOURCE_EXCERPT_INCLUDED,
)
from trellis.mutate import build_curate_executor
from trellis.retrieve.builder_factory import build_pack_builder, build_strategies
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import GRAPH_SEED_IDS_KEY, SearchStrategy
from trellis.schemas.pack import PackItem
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis.stores.sqlite.event_log import SQLiteEventLog

_ENTITY = "domain:billing"
_DOC = "doc:billing-runbook"
_DOC_BODY = (
    "Billing runbook. Retry failed invoice jobs from the dead letter queue "
    "before paging anyone."
)
_FAMILY = "billing_ops"
#: Names ``domain:billing`` (resolves to a live node), so the graph axis is seeded.
_SEEDED_INTENT = "billing runbook"
#: Names nothing the seed extractor can resolve, yet still keyword-matches the doc.
_UNSEEDED_INTENT = "retry invoice jobs"
_CLUTTER_NODES = 15
_LIMIT = 3  # scan window = _LIMIT * 4 = 12 < _CLUTTER_NODES


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    reg = StoreRegistry(stores_dir=stores_dir)
    graph = reg.knowledge.graph_store
    graph.upsert_node(_ENTITY, "domain", {"name": "billing"})
    reg.knowledge.document_store.put(_DOC, _DOC_BODY, {})
    return reg


def _pack_ids(registry: StoreRegistry) -> list[str]:
    events = registry.operational.event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, limit=50
    )
    return [e.entity_id for e in events]


def _serve_and_grade(
    registry: StoreRegistry, tmp_path: Path, *, intent: str, rounds: int = 2
) -> None:
    """Build ``rounds`` packs for ``intent`` and grade the doc helpful on each."""
    builder = build_pack_builder(registry, surface="test")
    for n in range(rounds):
        pack = builder.build(
            intent, run_id=f"run-{n}", intent_family=_FAMILY, limit_per_strategy=_LIMIT
        )
        assert _DOC in [i.item_id for i in pack.items], "the doc must be served"
        record_feedback(
            PackFeedback(
                run_id=f"run-{n}",
                phase="GENERATE",
                intent=intent,
                outcome="success",
                items_served=[i.item_id for i in pack.items],
                items_referenced=[_DOC],
                intent_family=_FAMILY,
            ),
            log_dir=tmp_path / "feedback",
            event_log=registry.operational.event_log,
            pack_id=pack.pack_id,
        )


def _doc_candidate(registry: StoreRegistry) -> dict[str, Any]:
    report = analyze_learning_observations(
        observations=build_learning_observations_from_event_log(
            registry.operational.event_log
        ),
        registry=build_seeded_registry(),
        min_support=2,
    )
    candidate = next(c for c in report["candidates"] if c["item_id"] == _DOC)
    assert candidate["recommendation_type"] in PROMOTE_RECOMMENDATIONS
    return candidate


def _promote(registry: StoreRegistry, candidate: dict[str, Any]) -> str:
    payloads = build_learning_promotion_payloads(
        candidate=candidate,
        promotion_name="Billing runbook",
        rationale="Graded helpful on every billing pack.",
        document_store=registry.knowledge.document_store,
    )
    outcome = submit_learning_promotion(
        build_curate_executor(registry),
        payloads["entity_payload"],
        payloads["edge_payloads"],
        requested_by="test:promote-learning",
        graph_store=registry.knowledge.graph_store,
    )
    assert outcome["status"] == "promoted", outcome
    return str(payloads["entity_id"])


class TestRoute:
    def test_seeded_pack_records_its_seed_and_the_candidate_targets_it(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _serve_and_grade(registry, tmp_path, intent=_SEEDED_INTENT)

        events = registry.operational.event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=10
        )
        assert events
        assert all(e.payload["seed_entity_ids"] == [_ENTITY] for e in events)
        assert _doc_candidate(registry)["target_entity_ids"] == [_ENTITY]

    def test_unseeded_pack_records_no_seed_and_the_candidate_has_no_target(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _serve_and_grade(registry, tmp_path, intent=_UNSEEDED_INTENT)

        events = registry.operational.event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=10
        )
        assert events
        # Present and empty: "ran the recency window", not "unrecorded".
        assert all(e.payload["seed_entity_ids"] == [] for e in events)
        assert _doc_candidate(registry)["target_entity_ids"] == []

    def test_promotion_mints_an_edge_to_each_seed(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _serve_and_grade(registry, tmp_path, intent=_SEEDED_INTENT)
        precedent_id = _promote(registry, _doc_candidate(registry))

        incoming = registry.knowledge.graph_store.get_edges(
            _ENTITY, direction="incoming", edge_type="precedent_applies_to"
        )
        assert [e["source_id"] for e in incoming] == [precedent_id]


class TestContent:
    def test_promotion_embeds_a_bounded_excerpt_of_the_source(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _serve_and_grade(registry, tmp_path, intent=_SEEDED_INTENT)
        precedent_id = _promote(registry, _doc_candidate(registry))

        node = registry.knowledge.graph_store.get_node(precedent_id)
        assert node is not None
        props = node["properties"]
        assert _DOC_BODY in props["description"]
        assert props["source_excerpt_status"] == SOURCE_EXCERPT_INCLUDED
        # The stats sentence stays beside the content.
        assert f"Source item '{_DOC}' showed success_rate=" in props["description"]


class TestReach:
    def test_seeded_pack_serves_the_precedent_and_so_does_an_unseeded_one_by_keyword(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _serve_and_grade(registry, tmp_path, intent=_SEEDED_INTENT)
        precedent_id = _promote(registry, _doc_candidate(registry))
        # Newer than the precedent and more numerous than the window scans.
        for i in range(_CLUTTER_NODES):
            registry.knowledge.graph_store.upsert_node(
                f"clutter:{i}", "concept", {"name": f"clutter {i}"}
            )
        builder = build_pack_builder(registry, surface="test")

        unseeded = builder.build(_UNSEEDED_INTENT, limit_per_strategy=_LIMIT)
        seeded = builder.build(_SEEDED_INTENT, limit_per_strategy=_LIMIT)

        assert precedent_id in [i.item_id for i in seeded.items]
        # This pinned the opposite until the promotion wrote a document row:
        # the graph axis alone cannot reach an unseeded intent once newer nodes
        # fill its recency window, and that is still true (see
        # ``test_the_graph_axis_alone_still_cannot_reach_it_unseeded``). What
        # changed is that the keyword axis now can, through the precedent's
        # own document.
        by_id = {i.item_id: i for i in unseeded.items}
        assert precedent_id in by_id
        assert by_id[precedent_id].metadata["source_strategy"] == "keyword"

    def test_the_graph_axis_alone_still_cannot_reach_it_unseeded(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _serve_and_grade(registry, tmp_path, intent=_SEEDED_INTENT)
        precedent_id = _promote(registry, _doc_candidate(registry))
        for i in range(_CLUTTER_NODES):
            registry.knowledge.graph_store.upsert_node(
                f"clutter:{i}", "concept", {"name": f"clutter {i}"}
            )
        graph_only = [
            s for s in build_strategies(registry).strategies if s.name == "graph"
        ]
        assert len(graph_only) == 1

        items = graph_only[0].search(_UNSEEDED_INTENT, limit=_LIMIT)

        assert precedent_id not in [i.item_id for i in items]


class _StubStrategy(SearchStrategy):
    def __init__(self, strategy_name: str, items: list[PackItem]) -> None:
        self._name = strategy_name
        self._items = items

    @property
    def name(self) -> str:
        return self._name

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[PackItem]:
        return list(self._items)


def _item(item_id: str, **metadata: Any) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt=f"content of {item_id}",
        relevance_score=0.5,
        metadata=metadata,
    )


def test_only_the_graph_axis_is_read_for_seeds(tmp_path: Path) -> None:
    """A document whose stored metadata names the key must not forge a seed."""
    log = SQLiteEventLog(tmp_path / "events.db")
    try:
        builder = PackBuilder(
            strategies=[
                _StubStrategy(
                    "keyword", [_item("d1", **{GRAPH_SEED_IDS_KEY: ["forged"]})]
                ),
                _StubStrategy("graph", [_item("g1", **{GRAPH_SEED_IDS_KEY: ["real"]})]),
            ],
            event_log=log,
        )
        pack = builder.build("anything")
        payload = log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=1)[
            0
        ].payload
    finally:
        log.close()

    assert payload["seed_entity_ids"] == ["real"]
    assert pack.pack_id


class _DocStore:
    """Just the read side ``build_learning_promotion_payloads`` uses."""

    def __init__(
        self, doc: dict[str, Any] | None = None, *, raises: bool = False
    ) -> None:
        self._doc = doc
        self._raises = raises

    def get(self, doc_id: str) -> dict[str, Any] | None:
        if self._raises:
            msg = "store down"
            raise RuntimeError(msg)
        return self._doc


class _MapStore:
    """A read side keyed by doc id, for a chunk whose parent is looked up."""

    def __init__(self, docs: dict[str, dict[str, Any]]) -> None:
        self._docs = docs

    def get(self, doc_id: str) -> dict[str, Any] | None:
        return self._docs.get(doc_id)


_NOISE_META = {"content_tags": {"signal_quality": "noise"}}
_ARCHIVED_META = {"lifecycle": {"state": "archived"}}
_CHUNK_ID = "doc:x#chunk-1"


def _payloads(store: Any, item_id: str = "doc:x") -> dict[str, Any]:
    candidate = {
        "candidate_id": "fam:abc",
        "intent_family": "fam",
        "item_id": item_id,
        "metrics": {"success_rate": 1.0, "retry_rate": None},
    }
    props = build_learning_promotion_payloads(
        candidate=candidate, promotion_name="n", rationale="r", document_store=store
    )["entity_payload"]["properties"]
    assert isinstance(props, dict)
    return props


class TestExcerptDegradation:
    @pytest.mark.parametrize(
        ("store", "status"),
        [
            (None, "no_document_store"),
            (_DocStore(None), "document_not_found"),
            (_DocStore({"content": "   \n  "}), "document_empty"),
            (_DocStore({"content": "---\ntitle: t\n---\n"}), "document_empty"),
            (_DocStore(raises=True), "lookup_failed"),
        ],
    )
    def test_missing_source_keeps_the_statistics_and_records_why(
        self, store: Any, status: str
    ) -> None:
        props = _payloads(store)
        assert props["source_excerpt_status"] == status
        assert "Source excerpt" not in props["description"]
        assert "Source item 'doc:x' showed success_rate=1.0" in props["description"]

    @pytest.mark.parametrize(
        ("store", "item_id"),
        [
            (_DocStore({"content": "noisy body", "metadata": _NOISE_META}), "doc:x"),
            (
                _DocStore({"content": "archived body", "metadata": _ARCHIVED_META}),
                "doc:x",
            ),
            (
                _MapStore(
                    {
                        _CHUNK_ID: {
                            "doc_id": _CHUNK_ID,
                            "content": "chunk body",
                            "metadata": {"parent_doc_id": "doc:x"},
                        },
                        "doc:x": {"content": "parent", "metadata": _ARCHIVED_META},
                    }
                ),
                _CHUNK_ID,
            ),
        ],
        ids=["noise", "archived", "chunk-of-archived-parent"],
    )
    def test_a_source_packs_withhold_is_never_copied(
        self, store: Any, item_id: str
    ) -> None:
        # The graph axis never re-checks a precedent's source, so a copied
        # body would reach packs the collect gates keep it out of.
        props = _payloads(store, item_id)
        assert props["source_excerpt_status"] == "source_withheld"
        assert "Source excerpt" not in props["description"]
        assert "body" not in props["description"]

    def test_a_chunk_of_a_servable_parent_is_copied(self) -> None:
        store = _MapStore(
            {
                _CHUNK_ID: {
                    "doc_id": _CHUNK_ID,
                    "content": "chunk body",
                    "metadata": {"parent_doc_id": "doc:x"},
                },
                "doc:x": {"content": "parent", "metadata": {}},
            }
        )
        props = _payloads(store, _CHUNK_ID)
        assert props["source_excerpt_status"] == SOURCE_EXCERPT_INCLUDED
        assert "chunk body" in props["description"]

    def test_excerpt_is_bounded_single_line_and_drops_frontmatter(self) -> None:
        body = "---\ntitle: secret-title\n---\n" + "\n".join(
            f"line {i} of a long source document." for i in range(200)
        )
        props = _payloads(_DocStore({"content": body}))
        description = props["description"]
        excerpt = description.split("Source excerpt: ", 1)[1].split(" Source item", 1)[
            0
        ]

        assert props["source_excerpt_status"] == SOURCE_EXCERPT_INCLUDED
        assert len(excerpt) <= PRECEDENT_SOURCE_EXCERPT_MAX_CHARS
        assert "\n" not in description
        assert "secret-title" not in description
        assert excerpt.startswith("line 0 of a long source document.")
        # Cut on a clean break with the dropped-size note, not a hard slice.
        assert excerpt.endswith("chars]")
        assert excerpt.rsplit("\u2026", 1)[0].endswith("document.")
