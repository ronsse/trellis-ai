"""``summarize_memory_yield(capture_breakdown=True)``: the session-capture block.

Capture documents are written the way the capture worker's corpus sync leaves
them: a document row with the metadata the worker records (``session_id``,
``subagent``) and a ``MEMORY_STORED`` event whose ``requested_by`` is
``worker:session-capture``. They are written oldest first, so the store's own
``created_at`` orders them. Embeddings are written straight to the vector
store as small invented vectors so which pairs are close is decided by the
fixture, not by an embedder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.analyze.memory_yield import (
    DUP_NEAR_DUPLICATE,
    DUP_SINGLETON,
    DUP_UNCHECKED,
    KIND_BASIS_NONE,
    KIND_BASIS_SESSION_ID,
    KIND_BASIS_SUBAGENT_FLAG,
    KIND_SUBAGENT,
    KIND_TOP_LEVEL,
    KIND_UNKNOWN,
    SESSION_CAPTURE_PATH,
    CaptureBreakdown,
    MemoryYieldReport,
    session_kind,
    summarize_memory_yield,
)
from trellis.core.pack_holdout import HOLDOUT_KEY
from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import record_feedback
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import CommandStatus
from trellis.mutate.evidence_ingest import build_evidence_ingest_command
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _create(
    registry: StoreRegistry,
    doc_id: str,
    *,
    surface: str = SESSION_CAPTURE_PATH,
    metadata: dict[str, Any] | None = None,
    vector: list[float] | None = None,
) -> None:
    if surface == SESSION_CAPTURE_PATH:
        registry.knowledge.document_store.put(
            doc_id, f"invented capture {doc_id}", dict(metadata or {})
        )
        registry.operational.event_log.emit(
            EventType.MEMORY_STORED,
            source=surface,
            entity_id=doc_id,
            entity_type="document",
            payload={"requested_by": surface, "action": "created"},
        )
    else:
        executor = build_curate_executor(
            registry, evidence_embed="none", clearance="restricted"
        )
        result = executor.execute(
            build_evidence_ingest_command(
                content=f"invented note {doc_id}",
                requested_by=surface,
                doc_id=doc_id,
                embed_mode="none",
            )
        )
        assert result.status == CommandStatus.SUCCESS, result.message
    if vector is not None:
        registry.knowledge.vector_store.upsert(doc_id, vector, {})


def _pack(
    registry: StoreRegistry, pack_id: str, item_ids: list[str], *, holdout: bool = False
) -> None:
    payload: dict[str, Any] = {
        "injected_items": [
            {"item_id": i, "item_type": "document", "estimated_tokens": 10, "rank": r}
            for r, i in enumerate(item_ids)
        ]
    }
    if holdout:
        payload[HOLDOUT_KEY] = True
    registry.operational.event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=pack_id,
        entity_type="pack",
        payload=payload,
    )


def _grade(
    registry: StoreRegistry,
    tmp_path: Path,
    pack_id: str,
    *,
    helpful: tuple[str, ...] = (),
    unhelpful: tuple[str, ...] = (),
) -> None:
    record_feedback(
        PackFeedback.from_agent_signal(
            run_id=f"run-{pack_id}",
            rating=0.5,
            helpful_item_ids=helpful,
            unhelpful_item_ids=unhelpful,
            pack_id=pack_id,
        ),
        log_dir=tmp_path / "feedback",
        event_log=registry.operational.event_log,
        pack_id=pack_id,
    )


def seed_captures(registry: StoreRegistry, tmp_path: Path) -> None:
    """Six capture documents and one non-capture neighbour.

    - ``c-sub-1`` / ``c-sub-2``: one sub-agent session, close vectors (a
      same-session pair).
    - ``c-top-1`` (flag) / ``c-top-2`` (session id only): two top-level
      sessions, close vectors (a cross-session pair).
    - ``c-legacy-sub``: no flag, an ``agent-`` session id, a lone vector.
    - ``c-unknown``: no metadata, no vector, served only in a held-out pack.
    - ``other``: another path, close to ``c-sub-1``; outside the population.
    """
    _create(registry, "c-unknown")
    _create(
        registry,
        "c-legacy-sub",
        metadata={"session_id": "agent-b"},
        vector=[0.0, 0.0, 1.0],
    )
    _create(
        registry,
        "c-top-2",
        metadata={"session_id": "s-2"},
        vector=[0.0, 0.999, 0.01],
    )
    _create(
        registry,
        "c-top-1",
        metadata={"session_id": "s-1", "subagent": False},
        vector=[0.0, 1.0, 0.0],
    )
    _create(
        registry,
        "c-sub-2",
        metadata={"session_id": "agent-a", "subagent": True},
        vector=[0.99, 0.01, 0.0],
    )
    _create(
        registry,
        "c-sub-1",
        metadata={"session_id": "agent-a", "subagent": True},
        vector=[1.0, 0.0, 0.0],
    )
    _create(registry, "other", surface="test:other", vector=[1.0, 0.0, 0.0])

    _pack(registry, "p1", ["c-sub-1", "c-top-1", "c-legacy-sub", "other"])
    _grade(
        registry,
        tmp_path,
        "p1",
        helpful=("c-top-1",),
        unhelpful=("c-sub-1", "c-legacy-sub"),
    )
    _pack(registry, "p-held", ["c-unknown"], holdout=True)
    _grade(registry, tmp_path, "p-held", helpful=("c-unknown",))


def _report(registry: StoreRegistry, **kwargs: Any) -> MemoryYieldReport:
    return summarize_memory_yield(
        registry.knowledge.document_store,
        registry.operational.event_log,
        days=30,
        capture_breakdown=True,
        vector_store=registry.knowledge.vector_store,
        **kwargs,
    )


def _block(registry: StoreRegistry, **kwargs: Any) -> CaptureBreakdown:
    block = _report(registry, **kwargs).capture_breakdown
    assert block is not None
    return block


class TestSessionKind:
    @pytest.mark.parametrize(
        ("subagent", "session_id", "expected"),
        [
            (True, "s-1", (KIND_SUBAGENT, KIND_BASIS_SUBAGENT_FLAG)),
            (False, "agent-x", (KIND_TOP_LEVEL, KIND_BASIS_SUBAGENT_FLAG)),
            (None, "agent-x", (KIND_SUBAGENT, KIND_BASIS_SESSION_ID)),
            (None, "s-1", (KIND_TOP_LEVEL, KIND_BASIS_SESSION_ID)),
            (None, None, (KIND_UNKNOWN, KIND_BASIS_NONE)),
        ],
    )
    def test_flag_wins_then_session_id(
        self, subagent: bool | None, session_id: str | None, expected: Any
    ) -> None:
        assert session_kind(subagent, session_id) == expected


class TestBySessionKind:
    def test_split(self, registry: StoreRegistry, tmp_path: Path) -> None:
        seed_captures(registry, tmp_path)
        block = _block(registry)
        rows = {row.group: row for row in block.by_session_kind}
        sub, top, unknown = (
            rows[KIND_SUBAGENT],
            rows[KIND_TOP_LEVEL],
            rows[KIND_UNKNOWN],
        )
        assert (sub.created, sub.served, sub.graded_unhelpful) == (3, 2, 2)
        assert (sub.unhelpful_servings, sub.helpful_servings) == (2, 0)
        assert (top.created, top.served, top.graded_helpful) == (2, 1, 1)
        assert (unknown.created, unknown.served, unknown.never_served) == (1, 0, 1)
        assert block.session_kind_basis == {
            KIND_BASIS_NONE: 1,
            KIND_BASIS_SESSION_ID: 2,
            KIND_BASIS_SUBAGENT_FLAG: 3,
        }

    def test_totals_equal_the_capture_path_row(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        seed_captures(registry, tmp_path)
        report = _report(registry)
        row = next(p for p in report.paths if p.path == SESSION_CAPTURE_PATH)
        block = report.capture_breakdown
        assert block is not None
        totals = block.totals.model_dump(exclude={"group"})
        assert totals == row.model_dump(exclude={"path", "basis"})
        assert sum(r.created for r in block.by_session_kind) == row.created
        assert sum(r.created for r in block.by_duplication) == row.created


class TestByDuplication:
    def test_split_and_clusters(self, registry: StoreRegistry, tmp_path: Path) -> None:
        seed_captures(registry, tmp_path)
        block = _block(registry)
        rows = {row.group: row for row in block.by_duplication}
        near, single, unchecked = (
            rows[DUP_NEAR_DUPLICATE],
            rows[DUP_SINGLETON],
            rows[DUP_UNCHECKED],
        )
        assert (near.created, near.served, near.helpful_servings) == (4, 2, 1)
        assert near.unhelpful_servings == 1
        assert (single.created, single.unhelpful_servings) == (1, 1)
        assert unchecked.created == 1
        dup = block.duplication
        assert (dup.population, dup.probed, dup.over_bound) == (6, 6, 0)
        assert (dup.embedded, dup.no_embedding) == (5, 1)
        assert (dup.pairs, dup.pairs_same_session, dup.pairs_cross_session) == (
            2,
            1,
            1,
        )
        assert (dup.clusters, dup.largest_cluster) == (2, 2)
        assert (dup.clusters_same_session, dup.members_in_same_session_clusters) == (
            1,
            2,
        )
        assert (dup.clusters_cross_session, dup.members_in_cross_session_clusters) == (
            1,
            2,
        )

    def test_no_vector_store_leaves_every_document_unchecked(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        seed_captures(registry, tmp_path)
        block = summarize_memory_yield(
            registry.knowledge.document_store,
            registry.operational.event_log,
            capture_breakdown=True,
        ).capture_breakdown
        assert block is not None
        assert block.duplication.no_vector_store
        rows = {row.group: row for row in block.by_duplication}
        assert rows[DUP_UNCHECKED].created == 6

    def test_bound_probes_the_newest_and_reports_the_rest(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        seed_captures(registry, tmp_path)
        block = _block(registry, capture_max_docs=2)
        dup = block.duplication
        assert (dup.max_docs, dup.probed, dup.over_bound) == (2, 2, 4)
        # The two newest are the same-session sub-agent pair.
        assert (dup.pairs, dup.pairs_same_session) == (1, 1)
        rows = {row.group: row for row in block.by_duplication}
        assert rows[DUP_NEAR_DUPLICATE].created == 2
        assert rows[DUP_SINGLETON].created == 0
        assert rows[DUP_UNCHECKED].created == 4


class TestCross:
    def test_cells(self, registry: StoreRegistry, tmp_path: Path) -> None:
        seed_captures(registry, tmp_path)
        cells = {(c.session_kind, c.duplication): c for c in _block(registry).cross}
        assert len(cells) == 9
        sub_near = cells[(KIND_SUBAGENT, DUP_NEAR_DUPLICATE)]
        assert (sub_near.created, sub_near.served, sub_near.unhelpful_servings) == (
            2,
            1,
            1,
        )
        sub_single = cells[(KIND_SUBAGENT, DUP_SINGLETON)]
        assert (sub_single.created, sub_single.unhelpful_servings) == (1, 1)
        top_near = cells[(KIND_TOP_LEVEL, DUP_NEAR_DUPLICATE)]
        assert (top_near.created, top_near.helpful_servings) == (2, 1)
        assert cells[(KIND_UNKNOWN, DUP_UNCHECKED)].created == 1
        assert sum(c.created for c in cells.values()) == 6


class TestHoldout:
    def test_a_held_out_pack_is_excluded(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        seed_captures(registry, tmp_path)
        report = _report(registry)
        assert report.limits.holdout_packs_dropped == 1
        block = report.capture_breakdown
        assert block is not None
        assert block.totals.helpful_servings == 1  # c-top-1 only, not c-unknown
        unknown = {r.group: r for r in block.by_session_kind}[KIND_UNKNOWN]
        assert (unknown.servings, unknown.helpful_servings) == (0, 0)


class TestFlagOff:
    def test_off_reads_no_vector_and_leaves_the_field_none(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        seed_captures(registry, tmp_path)
        report = summarize_memory_yield(
            registry.knowledge.document_store,
            registry.operational.event_log,
            days=30,
        )
        assert report.capture_breakdown is None
        assert "capture_breakdown" not in report.dump()
        on = _report(registry)
        assert on.dump()["paths"] == report.dump()["paths"]
        assert on.dump()["totals"] == report.dump()["totals"]
