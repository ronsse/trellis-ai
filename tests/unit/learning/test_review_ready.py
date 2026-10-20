"""The pure half of ``trellis curate review-ready``.

The predicate itself is pinned in ``test_scoring.py``; here are the pieces
this module adds around it. Ids are synthetic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.learning.review_ready import (
    REFUSED_ALREADY_PROMOTED,
    REFUSED_CONTRADICTED,
    REFUSED_NOT_READY,
    approval_refusal,
    build_review_rows,
    make_promoted_probe,
    noise_families_by_item,
)
from trellis.learning.scoring import precedent_entity_id_for_candidate
from trellis.mutate import Operation
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.graph import SQLiteGraphStore


def _cand(
    candidate_id: str,
    item_id: str,
    *,
    family: str = "f1",
    rec: str = "promote_guidance",
    helpful: int = 1,
    unhelpful: int = 0,
    targets: Any = (),
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "intent_family": family,
        "recommendation_type": rec,
        "item_id": item_id,
        "target_entity_ids": targets,
        "metrics": {"times_served": 2, "success_rate": 1.0},
        "citation_evidence": {
            "helpful_count": helpful,
            "unhelpful_count": unhelpful,
        },
    }


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "entity_id": "precedent://learning/x",
        "already_promoted": False,
        "cross_family_noise": False,
        "noise_intent_families": [],
    }
    row.update(overrides)
    return row


class TestNoiseFamilies:
    def test_collects_every_family_that_calls_the_item_noise(self) -> None:
        found = noise_families_by_item(
            [
                _cand("f2:i", "doc:i", family="f2", rec="investigate_noise"),
                _cand("f3:i", "doc:i", family="f3", rec="investigate_noise"),
                _cand("f1:i", "doc:i", family="f1"),  # promote arm: not noise
                _cand("f1:j", "doc:j", rec="investigate_noise", family="f1"),
            ]
        )
        assert found == {"doc:i": ["f2", "f3"], "doc:j": ["f1"]}

    def test_a_blank_item_id_is_not_a_key(self) -> None:
        assert noise_families_by_item([_cand("f:x", "", rec="investigate_noise")]) == {}


class TestBuildRows:
    def test_non_mapping_entries_and_non_ready_candidates_are_skipped(self) -> None:
        rows = build_review_rows(
            ["junk", _cand("f1:a", "doc:a"), _cand("f1:b", "doc:b", unhelpful=1)],  # type: ignore[list-item]
            document_store=None,
            is_promoted=lambda _eid: False,
        )
        assert [r["candidate_id"] for r in rows] == ["f1:a"]
        assert rows[0]["source_excerpt_status"] == "no_document_store"

    def test_flagged_rows_sort_after_clean_and_promoted_last(self) -> None:
        promoted_entity = precedent_entity_id_for_candidate("f1:p")
        rows = build_review_rows(
            [
                _cand("f1:p", "doc:p", helpful=9),
                _cand("f1:n", "doc:n", helpful=5),
                _cand("f2:n", "doc:n", family="f2", rec="investigate_noise", helpful=0),
                _cand("f1:c", "doc:c", helpful=1),
            ],
            document_store=None,
            is_promoted=lambda eid: eid == promoted_entity,
        )
        assert [r["candidate_id"] for r in rows] == ["f1:c", "f1:n", "f1:p"]

    def test_target_count_ignores_a_non_list(self) -> None:
        (row,) = build_review_rows(
            [_cand("f1:a", "doc:a", targets="not-a-list")],
            document_store=None,
            is_promoted=lambda _eid: False,
        )
        assert row["target_entity_count"] == 0


class TestApprovalRefusal:
    def test_unknown_row_is_not_ready(self) -> None:
        refusal = approval_refusal("f:x", None, force_contradicted=True)
        assert refusal is not None
        assert refusal["status"] == REFUSED_NOT_READY

    def test_already_promoted_is_reported_before_contradicted(self) -> None:
        refusal = approval_refusal(
            "f:x",
            _row(
                already_promoted=True,
                cross_family_noise=True,
                noise_intent_families=["g"],
            ),
            force_contradicted=False,
        )
        assert refusal is not None
        assert refusal["status"] == REFUSED_ALREADY_PROMOTED

    def test_force_lifts_only_the_contradiction(self) -> None:
        contradicted = _row(cross_family_noise=True, noise_intent_families=["g"])
        refusal = approval_refusal("f:x", contradicted, force_contradicted=False)
        assert refusal is not None
        assert refusal["status"] == REFUSED_CONTRADICTED
        assert approval_refusal("f:x", contradicted, force_contradicted=True) is None
        promoted = _row(already_promoted=True)
        assert approval_refusal("f:x", promoted, force_contradicted=True) is not None

    def test_a_clean_row_has_no_refusal(self) -> None:
        assert approval_refusal("f:x", _row(), force_contradicted=False) is None


class TestPromotedProbe:
    @pytest.fixture
    def stores(self, tmp_path: Path) -> Any:
        events = SQLiteEventLog(tmp_path / "events.db")
        graph = SQLiteGraphStore(tmp_path / "graph.db")
        yield events, graph
        events.close()
        graph.close()

    def test_a_precedent_node_counts_and_another_node_type_does_not(
        self, stores: Any
    ) -> None:
        events, graph = stores
        graph.upsert_node("precedent://learning/a", "precedent", {})
        graph.upsert_node("precedent://learning/b", "entity", {})
        probe = make_promoted_probe(events, graph)
        assert probe("precedent://learning/a") is True
        assert probe("precedent://learning/b") is False
        assert probe("precedent://learning/missing") is False

    def test_a_successful_promote_event_counts_without_a_graph_node(
        self, stores: Any
    ) -> None:
        """The event scan is the signal a busy graph cannot lose, and the one
        ``auto-promote-candidates`` uses; a failed promote event is not one."""
        events, graph = stores
        for entity_id, status in (
            ("precedent://learning/by-event", "success"),
            ("precedent://learning/failed", "failed"),
        ):
            events.emit(
                EventType.MUTATION_EXECUTED,
                source="test",
                entity_id=entity_id,
                payload={
                    "operation": Operation.PRECEDENT_PROMOTE.value,
                    "status": status,
                },
            )
        probe = make_promoted_probe(events, graph)
        assert graph.get_node("precedent://learning/by-event") is None
        assert probe("precedent://learning/by-event") is True
        assert probe("precedent://learning/failed") is False
