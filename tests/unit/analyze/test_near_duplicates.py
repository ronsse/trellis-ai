"""The near-duplicates measurement: population, the MinHash subtraction, read-only.

Ingest dedup is lexical (an exact hash plus character-trigram MinHash), so a
reworded document passes it. These tests seed real SQLite stores with hand-set
vectors and pin what the analyzer counts: which documents are in the
population and why the rest are not, that a pair the shipped MinHash predicate
already catches is reported as lexical and never as residue, the per-threshold
counts and clusters, the document cap, and that nothing is written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.unit.analyze import _near_duplicates_seed as seed
from trellis.analyze.near_duplicates import (
    DEFAULT_THRESHOLDS,
    STATUS_EMPTY,
    STATUS_OK,
    NearDuplicatesReport,
    ThresholdResult,
    summarize_near_duplicates,
)
from trellis.classify.dedup.minhash import MinHashIndex
from trellis.stores.registry import StoreRegistry


@pytest.fixture
def stores(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _run(stores: StoreRegistry, **kwargs: Any) -> NearDuplicatesReport:
    return summarize_near_duplicates(
        stores.knowledge.document_store, stores.knowledge.vector_store, **kwargs
    )


def _at(report: NearDuplicatesReport, threshold: float) -> ThresholdResult:
    [row] = [r for r in report.thresholds if r.threshold == threshold]
    return row


def _residue_ids(row: ThresholdResult) -> set[frozenset[str]]:
    return {frozenset((p.doc_id, p.other_doc_id)) for p in row.residue_sample}


class TestPopulation:
    def test_every_document_is_counted_in_exactly_one_class(
        self, stores: StoreRegistry
    ) -> None:
        seed.seed_main(stores)

        report = _run(stores)

        # The chunk is never listed; the other ten whole documents are.
        assert report.documents_considered == 10
        assert report.skipped.archived == 1
        assert report.skipped.superseded == 1
        assert report.skipped.noise == 1
        assert report.skipped.unreadable == 0
        assert report.no_embedding == 1
        assert report.embedded == 6
        assert (
            report.embedded
            + report.no_embedding
            + report.embedding_unusable
            + report.skipped.total
        ) == report.documents_considered
        assert report.status == STATUS_OK

    def test_archived_superseded_and_noise_never_reach_a_pair(
        self, stores: StoreRegistry
    ) -> None:
        seed.seed_main(stores)

        report = _run(stores)

        named = {
            doc_id
            for row in report.thresholds
            for pair in (*row.residue_sample, *row.lexical_sample)
            for doc_id in (pair.doc_id, pair.other_doc_id)
        }
        assert named.isdisjoint({"arch", "sup", "noisy", "bare"})
        assert not any("#chunk-" in doc_id for doc_id in named)
        # Only the one paraphrase pair: the archived copy of para-a's vector
        # and the noise copy of para-b's are both out.
        assert _at(report, 0.95).residue_pairs == 1

    def test_an_empty_store_is_empty_not_ok(self, stores: StoreRegistry) -> None:
        report = _run(stores)

        assert report.status == STATUS_EMPTY
        assert report.empty_reason
        assert all(row.pairs == 0 for row in report.thresholds)


class TestPredicate:
    def test_a_pair_minhash_catches_is_lexical_not_residue(
        self, stores: StoreRegistry
    ) -> None:
        # The differential: the shipped index itself must call this pair a
        # duplicate, or the test proves nothing about the subtraction.
        index = MinHashIndex()
        index.add("mem:lex-a", seed.LEX_A)
        index.add("kb:para-a", seed.PARA_A)
        match = index.find_duplicate(seed.LEX_B)
        assert match is not None
        assert match[0] == "mem:lex-a"
        assert index.find_duplicate(seed.PARA_B) is None
        seed.seed_main(stores)

        row = _at(_run(stores), 0.95)

        assert row.lexical_pairs == 1
        assert {p.doc_id for p in row.lexical_sample} | {
            p.other_doc_id for p in row.lexical_sample
        } == {"mem:lex-a", "mem:lex-b"}
        assert frozenset(("mem:lex-a", "mem:lex-b")) not in _residue_ids(row)
        assert _residue_ids(row) == {frozenset(("kb:para-a", "kb:para-b"))}

    def test_counts_per_threshold(self, stores: StoreRegistry) -> None:
        seed.seed_main(stores)

        report = _run(stores)

        assert tuple(r.threshold for r in report.thresholds) == DEFAULT_THRESHOLDS
        low, high = _at(report, 0.90), _at(report, 0.95)
        assert (low.pairs, low.lexical_pairs, low.residue_pairs) == (3, 1, 2)
        assert (high.pairs, high.lexical_pairs, high.residue_pairs) == (2, 1, 1)
        assert low.documents_in_residue == 4
        assert (low.clusters, low.largest_cluster) == (2, 2)
        assert [p.cosine for p in low.residue_sample] == [0.97, 0.92]
        assert report.basis.minhash_threshold == 0.85

    def test_thresholds_are_overridable(self, stores: StoreRegistry) -> None:
        seed.seed_main(stores)

        report = _run(stores, thresholds=(0.99,))

        [row] = report.thresholds
        assert (row.threshold, row.pairs) == (0.99, 0)

    def test_clusters_are_connected_components(self, stores: StoreRegistry) -> None:
        seed.put(stores, "a", seed.PARA_A, seed.unit(0))
        seed.put(stores, "b", seed.PARA_B, seed.unit(0, 0.97))
        seed.put(stores, "c", seed.MID_A, seed.unit(0, 0.995))
        seed.put(stores, "d", seed.MID_B, seed.unit(6))
        seed.put(stores, "e", seed.OTHER, seed.unit(6, 0.93))

        row = _at(_run(stores), 0.90)

        assert row.residue_pairs == 4
        assert (row.clusters, row.largest_cluster) == (2, 3)
        assert row.documents_in_residue == 5

    def test_breakdown_by_source_system_then_id_prefix(
        self, stores: StoreRegistry
    ) -> None:
        seed.seed_main(stores)

        row = _at(_run(stores), 0.90)

        by = {g.group: g.documents_in_residue for g in row.by_group}
        assert by == {"notes": 2, "kb": 2}


class TestCap:
    def test_the_cap_bounds_the_population_and_is_reported(
        self, stores: StoreRegistry
    ) -> None:
        seed.seed_main(stores)

        report = _run(stores, max_docs=3)

        assert report.documents_considered == 3
        assert report.cap_hit is True
        assert report.max_docs == 3
        assert any("cap" in note for note in report.notes)

    def test_no_cap_hit_when_everything_fits(self, stores: StoreRegistry) -> None:
        seed.seed_main(stores)

        assert _run(stores).cap_hit is False


class TestReportsItsLimits:
    def test_the_report_states_what_it_cannot_tell(self, stores: StoreRegistry) -> None:
        seed.seed_main(stores)

        text = " ".join(_run(stores).cannot_tell).lower()

        for phrase in ("contradiction", "outcome", "unembedded", "uncalibrated"):
            assert phrase in text, phrase


_READS = {
    "get",
    "query",
    "list_documents",
    "count",
    "dimensions",
}


class _ReadOnly:
    """Forwards only the read methods; any other attribute fails the test."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name not in _READS:
            pytest.fail(f"near-duplicates touched {name!r}, which is not a read")
        return getattr(self._inner, name)


class TestReadOnly:
    def test_only_read_methods_are_called_and_no_row_changes(
        self, stores: StoreRegistry
    ) -> None:
        seed.seed_main(stores)
        docs, vecs = stores.knowledge.document_store, stores.knowledge.vector_store
        before = (docs.count(), vecs.count(), docs.get("kb:para-a"))

        report = summarize_near_duplicates(_ReadOnly(docs), _ReadOnly(vecs))  # type: ignore[arg-type]

        assert report.embedded == 6
        assert (docs.count(), vecs.count(), docs.get("kb:para-a")) == before
