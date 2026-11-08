"""The session-repeats measurement: classes, window, and the builder's predicate.

A session's later pack can serve a *different* member of a near-duplicate
cluster an earlier pack already served, because session dedup is by item id and
content hash (#1208). These tests seed real SQLite stores and pin what the
analyzer counts: each class with its estimated tokens, the window, held-out and
sectioned packs, the opaque session handle, and that the near-duplicate verdict
is the pack builder's own (a differential against
``PackBuilder._semantic_dedup_tracked`` and the factory's default config).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from tests.unit.analyze import _session_repeats_seed as seed
from trellis.analyze.session_repeats import (
    STATUS_EMPTY,
    STATUS_OK,
    TOP_SESSIONS,
    SessionRepeatsReport,
    session_hash,
    summarize_session_repeats,
)
from trellis.retrieve.builder_factory import build_pack_builder
from trellis.retrieve.pack_builder import PackBuilder, SemanticDedupConfig
from trellis.schemas.pack import PackItem
from trellis.stores.base.document import DocumentStore
from trellis.stores.registry import StoreRegistry

SESSION = "sess-main"


@pytest.fixture
def stores(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _run(stores: StoreRegistry, **kwargs: Any) -> SessionRepeatsReport:
    return summarize_session_repeats(
        stores.operational.event_log,
        stores.knowledge.document_store,
        now=seed.NOW,
        **kwargs,
    )


def _minutes(n: int) -> datetime:
    return seed.T0 + timedelta(minutes=n)


def _seed_main_session(stores: StoreRegistry) -> None:
    """Three packs over 30 minutes: a near-duplicate, a chunk sibling, a repeat."""
    seed.put_doc(stores, "doc-a", seed.BASE)
    seed.put_doc(
        stores,
        "notes#chunk-0",
        seed.CHUNK_0,
        {"parent_doc_id": "notes"},
    )
    seed.put_doc(stores, "doc-other", seed.OTHER)
    seed.put_doc(stores, "doc-a-copy", seed.NEAR)
    seed.put_doc(stores, "doc-x", seed.FRESH)
    seed.put_doc(stores, "notes#chunk-1", seed.CHUNK_1)  # parent from the id scheme
    seed.put_doc(stores, "doc-fresh", seed.THIRD)
    log = stores.operational.event_log
    seed.pack(
        log,
        "p1",
        SESSION,
        _minutes(0),
        [("doc-a", 10), ("notes#chunk-0", 6), ("doc-other", 4)],
    )
    seed.pack(log, "p2", SESSION, _minutes(15), [("doc-a-copy", 12), ("doc-x", 8)])
    seed.pack(
        log,
        "p3",
        SESSION,
        _minutes(30),
        [("notes#chunk-1", 7), ("doc-fresh", 9), ("doc-a", 5), ("gone-doc", 3)],
    )


class TestClasses:
    def test_each_class_count_and_token_estimate(self, stores: StoreRegistry) -> None:
        _seed_main_session(stores)

        report = _run(stores)

        assert report.status == STATUS_OK
        assert report.sessions_multi_pack == 1
        assert report.packs_scanned == 3
        assert report.later_pack_items == 6
        assert (report.exact_repeat.items, report.exact_repeat.estimated_tokens) == (
            1,
            5,
        )
        assert (
            report.near_duplicate.items,
            report.near_duplicate.estimated_tokens,
        ) == (1, 12)
        assert (
            report.chunk_sibling.items,
            report.chunk_sibling.estimated_tokens,
        ) == (1, 7)
        assert (report.fresh.items, report.fresh.estimated_tokens) == (2, 17)
        assert (report.unreadable.items, report.unreadable.estimated_tokens) == (1, 3)
        assert report.near_duplicate_share == pytest.approx(1 / 6)
        assert report.outside_window_items == 0

    def test_every_later_item_lands_in_exactly_one_class(
        self, stores: StoreRegistry
    ) -> None:
        _seed_main_session(stores)

        report = _run(stores)

        classes = (
            report.exact_repeat,
            report.near_duplicate,
            report.chunk_sibling,
            report.fresh,
            report.unreadable,
        )
        assert sum(c.items for c in classes) == report.later_pack_items

    def test_the_first_pack_is_never_judged(self, stores: StoreRegistry) -> None:
        _seed_main_session(stores)

        report = _run(stores)

        # p1 serves three items; only p2 and p3 (2 + 4) are later-pack items.
        assert report.later_pack_items == 6

    def test_a_removed_document_is_unreadable_and_never_guessed(
        self, stores: StoreRegistry
    ) -> None:
        seed.put_doc(stores, "doc-a", seed.BASE)
        log = stores.operational.event_log
        seed.pack(log, "p1", SESSION, _minutes(0), [("doc-a", 4)])
        seed.pack(log, "p2", SESSION, _minutes(5), [("never-stored", 6)])

        report = _run(stores)

        assert report.unreadable.items == 1
        assert report.fresh.items == 0
        assert report.near_duplicate.items == 0
        assert any("no readable document row" in note for note in report.notes)

    def test_a_store_that_raises_reads_as_unreadable(
        self, stores: StoreRegistry
    ) -> None:
        class _Broken:
            def get(self, _doc_id: str) -> None:
                msg = "boom"
                raise RuntimeError(msg)

        log = stores.operational.event_log
        seed.pack(log, "p1", SESSION, _minutes(0), [("doc-a", 4)])
        seed.pack(log, "p2", SESSION, _minutes(5), [("doc-b", 6)])

        report = summarize_session_repeats(
            log, cast("DocumentStore", _Broken()), now=seed.NOW
        )

        assert report.unreadable.items == 1
        assert report.later_pack_items == 1

    def test_a_row_without_a_token_estimate_counts_as_zero(
        self, stores: StoreRegistry
    ) -> None:
        seed.put_doc(stores, "doc-a", seed.BASE)
        seed.put_doc(stores, "doc-x", seed.FRESH)
        log = stores.operational.event_log
        seed.pack(log, "p1", SESSION, _minutes(0), [("doc-a", 4)])
        seed.pack(log, "p2", SESSION, _minutes(5), [("doc-x", None)])

        report = _run(stores)

        assert report.fresh.items == 1
        assert report.fresh.estimated_tokens == 0
        assert report.rows_without_token_estimate == 1


class TestWindowAndPackSelection:
    def _two_packs_apart(self, stores: StoreRegistry, gap_minutes: int) -> None:
        seed.put_doc(stores, "doc-a", seed.BASE)
        seed.put_doc(stores, "doc-a-copy", seed.NEAR)
        log = stores.operational.event_log
        seed.pack(log, "p1", SESSION, _minutes(0), [("doc-a", 4)])
        seed.pack(log, "p2", SESSION, _minutes(gap_minutes), [("doc-a-copy", 6)])

    def test_an_item_outside_the_window_is_not_judged(
        self, stores: StoreRegistry
    ) -> None:
        self._two_packs_apart(stores, 90)

        report = _run(stores, window_minutes=60)

        assert report.later_pack_items == 0
        assert report.outside_window_items == 1
        assert report.near_duplicate.items == 0
        assert report.status == STATUS_EMPTY
        assert report.near_duplicate_share is None

    def test_a_wider_window_judges_the_same_item(self, stores: StoreRegistry) -> None:
        self._two_packs_apart(stores, 90)

        report = _run(stores, window_minutes=120)

        assert report.later_pack_items == 1
        assert report.near_duplicate.items == 1
        assert report.outside_window_items == 0

    def test_a_pack_exactly_at_the_window_edge_is_inside(
        self, stores: StoreRegistry
    ) -> None:
        self._two_packs_apart(stores, 60)

        assert _run(stores, window_minutes=60).near_duplicate.items == 1

    def test_the_default_window_is_the_session_dedup_window(
        self, stores: StoreRegistry
    ) -> None:
        self._two_packs_apart(stores, 10)

        assert _run(stores).window_minutes == 60

    def test_a_held_out_pack_is_dropped(self, stores: StoreRegistry) -> None:
        seed.put_doc(stores, "doc-a", seed.BASE)
        seed.put_doc(stores, "doc-a-copy", seed.NEAR)
        log = stores.operational.event_log
        seed.pack(log, "p1", SESSION, _minutes(0), [("doc-a", 4)])
        seed.pack(log, "p2", SESSION, _minutes(5), [("doc-a-copy", 6)], holdout=True)

        report = _run(stores)

        assert report.packs_scanned == 2
        assert report.packs_held_out == 1
        assert report.sessions_multi_pack == 0
        assert report.near_duplicate.items == 0
        assert report.status == STATUS_EMPTY

    def test_a_sectioned_pack_is_read_through_its_item_rows(
        self, stores: StoreRegistry
    ) -> None:
        seed.put_doc(stores, "doc-a", seed.BASE)
        seed.put_doc(stores, "doc-a-copy", seed.NEAR)
        log = stores.operational.event_log
        seed.pack(log, "p1", SESSION, _minutes(0), [("doc-a", 4)])
        seed.pack(log, "p2", SESSION, _minutes(5), [("doc-a-copy", 6)], sectioned=True)

        report = _run(stores)

        assert report.near_duplicate.items == 1
        assert report.near_duplicate.estimated_tokens == 6

    def test_a_sectioned_pack_without_item_rows_is_counted_not_read(
        self, stores: StoreRegistry
    ) -> None:
        seed.put_doc(stores, "doc-a", seed.BASE)
        log = stores.operational.event_log
        seed.pack(log, "p1", SESSION, _minutes(0), [("doc-a", 4)])
        seed.pack(
            log,
            "p2",
            SESSION,
            _minutes(5),
            [("doc-a", 4)],
            sectioned=True,
            rows=False,
        )

        report = _run(stores)

        assert report.packs_without_rows == 1
        assert report.later_pack_items == 0
        assert any("before #958" in note for note in report.notes)

    def test_a_pack_without_a_session_id_is_ignored(
        self, stores: StoreRegistry
    ) -> None:
        seed.put_doc(stores, "doc-a", seed.BASE)
        seed.put_doc(stores, "doc-a-copy", seed.NEAR)
        log = stores.operational.event_log
        seed.pack(log, "p1", None, _minutes(0), [("doc-a", 4)])
        seed.pack(log, "p2", None, _minutes(5), [("doc-a-copy", 6)])

        report = _run(stores)

        assert report.packs_without_session == 2
        assert report.sessions_multi_pack == 0
        assert report.later_pack_items == 0

    def test_sessions_are_judged_separately(self, stores: StoreRegistry) -> None:
        seed.put_doc(stores, "doc-a", seed.BASE)
        seed.put_doc(stores, "doc-a-copy", seed.NEAR)
        log = stores.operational.event_log
        seed.pack(log, "p1", "sess-one", _minutes(0), [("doc-a", 4)])
        seed.pack(log, "p2", "sess-two", _minutes(5), [("doc-a-copy", 6)])

        report = _run(stores)

        assert report.sessions_multi_pack == 0
        assert report.near_duplicate.items == 0

    def test_an_empty_log_is_empty_not_a_failure(self, stores: StoreRegistry) -> None:
        report = _run(stores)

        assert report.status == STATUS_EMPTY
        assert report.later_pack_items == 0
        assert report.empty_reason


class TestTopSessions:
    def _seed_sessions(self, stores: StoreRegistry, count: int) -> None:
        log = stores.operational.event_log
        for k in range(1, count + 1):
            sid = f"top-{k}"
            seed.put_doc(stores, f"{sid}-a", seed.BASE)
            near_ids = []
            for m in range(k):
                seed.put_doc(stores, f"{sid}-n{m}", seed.NEAR)
                near_ids.append((f"{sid}-n{m}", 2))
            seed.pack(log, f"{sid}-p1", sid, _minutes(0), [(f"{sid}-a", 2)])
            seed.pack(log, f"{sid}-p2", sid, _minutes(5), near_ids)

    def test_the_list_is_capped_and_ordered_by_near_duplicate_count(
        self, stores: StoreRegistry
    ) -> None:
        self._seed_sessions(stores, 7)

        report = _run(stores)

        assert len(report.top_sessions) == TOP_SESSIONS == 5
        assert [row.near_duplicates for row in report.top_sessions] == [7, 6, 5, 4, 3]

    def test_a_session_is_named_only_by_an_opaque_handle(
        self, stores: StoreRegistry
    ) -> None:
        self._seed_sessions(stores, 2)

        report = _run(stores)

        handles = {row.session for row in report.top_sessions}
        assert handles == {session_hash("top-1"), session_hash("top-2")}
        assert all("top-" not in handle for handle in handles)
        assert len(session_hash("top-1")) == 12
        assert session_hash("top-1") != session_hash("top-2")
        assert session_hash("top-1") == session_hash("top-1")

    def test_a_session_with_no_near_duplicate_is_not_ranked(
        self, stores: StoreRegistry
    ) -> None:
        _seed_main_session(stores)
        seed.put_doc(stores, "q-a", seed.OTHER)
        seed.put_doc(stores, "q-b", seed.THIRD)
        log = stores.operational.event_log
        seed.pack(log, "q1", "quiet", _minutes(0), [("q-a", 2)])
        seed.pack(log, "q2", "quiet", _minutes(5), [("q-b", 2)])

        report = _run(stores)

        assert [row.session for row in report.top_sessions] == [session_hash(SESSION)]

    def test_the_json_pair_names_the_item_and_its_sibling(
        self, stores: StoreRegistry
    ) -> None:
        _seed_main_session(stores)

        report = _run(stores)

        [row] = report.top_sessions
        assert (row.packs, row.later_pack_items, row.near_duplicates) == (3, 6, 1)
        [pair] = row.pairs
        assert (pair.item_id, pair.sibling_item_id) == ("doc-a-copy", "doc-a")
        assert pair.similarity >= SemanticDedupConfig().threshold


class TestThePredicateIsTheBuilders:
    """The near-duplicate verdict is the builder's, not a restatement of it."""

    CASES = (
        ("near", seed.NEAR),
        ("loose", seed.LOOSE),
        ("fresh", seed.FRESH),
        ("frontmatter", "---\ntitle: x\n---\n" + seed.NEAR),
        ("short", "see readme"),
    )

    @staticmethod
    def _builder_collapses(candidate: str) -> bool:
        items = [
            PackItem(
                item_id="first", item_type="document", excerpt=seed.BASE,
                relevance_score=0.9,
            ),
            PackItem(
                item_id="second", item_type="document", excerpt=candidate,
                relevance_score=0.5,
            ),
        ]  # fmt: skip
        _kept, rejected = PackBuilder._semantic_dedup_tracked(
            items, SemanticDedupConfig()
        )
        return any(r.item_id == "second" for r in rejected)

    @pytest.mark.parametrize(("label", "candidate"), CASES, ids=[c[0] for c in CASES])
    def test_the_analyzer_agrees_with_the_builder(
        self, stores: StoreRegistry, label: str, candidate: str
    ) -> None:
        seed.put_doc(stores, "first", seed.BASE)
        seed.put_doc(stores, "second", candidate)
        log = stores.operational.event_log
        seed.pack(log, "p1", SESSION, _minutes(0), [("first", 4)])
        seed.pack(log, "p2", SESSION, _minutes(5), [("second", 4)])

        report = _run(stores)

        assert (report.near_duplicate.items == 1) == self._builder_collapses(candidate)
        assert report.later_pack_items == 1, label

    def test_the_loose_pair_is_over_the_looser_threshold_only(
        self, stores: StoreRegistry
    ) -> None:
        """A threshold the builder lacks changes the answer: the mutant's tell."""
        seed.put_doc(stores, "first", seed.BASE)
        seed.put_doc(stores, "second", seed.LOOSE)
        log = stores.operational.event_log
        seed.pack(log, "p1", SESSION, _minutes(0), [("first", 4)])
        seed.pack(log, "p2", SESSION, _minutes(5), [("second", 4)])

        default = _run(stores)
        loose = _run(stores, dedup_config=SemanticDedupConfig(threshold=0.5))

        assert default.near_duplicate.items == 0
        assert default.fresh.items == 1
        assert loose.near_duplicate.items == 1
        assert loose.basis.threshold == 0.5

    def test_the_default_basis_is_what_a_default_built_builder_gets(
        self, stores: StoreRegistry, tmp_path: Path
    ) -> None:
        config_dir = tmp_path / "config"
        data_dir = tmp_path / "data"
        (data_dir / "stores").mkdir(parents=True)
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(f"data_dir: {data_dir}\n")
        registry = StoreRegistry.from_config_dir(
            config_dir=config_dir, data_dir=data_dir
        )

        builder = build_pack_builder(registry, surface="test")
        basis = _run(stores).basis

        config = builder._semantic_dedup
        assert config == SemanticDedupConfig()
        assert config is not None
        assert (
            basis.threshold,
            basis.num_perm,
            basis.num_bands,
            basis.shingle_size,
            basis.min_shingles,
        ) == (
            config.threshold,
            config.num_perm,
            config.num_bands,
            config.shingle_size,
            config.min_shingles,
        )
