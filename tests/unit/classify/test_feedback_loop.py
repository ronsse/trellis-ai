"""Tests for the feedback loop that applies noise tags."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trellis.classify.feedback import apply_noise_tags
from trellis.mutate.retention import RetentionCriteria, resolve_candidates
from trellis.retrieve.strategies import KeywordSearch
from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.stores.registry import StoreRegistry
from trellis.stores.sqlite.document import SQLiteDocumentStore


@pytest.fixture
def doc_store(tmp_path: Path):
    store = SQLiteDocumentStore(tmp_path / "docs.db")
    yield store
    store.close()


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    """A full registry, for the tests that reach retention's resolver."""
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


class TestApplyNoiseTags:
    """apply_noise_tags updates signal_quality on noise candidates."""

    def test_marks_noise_candidates(self, doc_store: SQLiteDocumentStore) -> None:
        d1 = doc_store.put(
            None,
            "noisy content",
            {"content_tags": {"domain": ["api"], "signal_quality": "standard"}},
        )
        d2 = doc_store.put(
            None,
            "good content",
            {"content_tags": {"domain": ["api"], "signal_quality": "high"}},
        )

        updated = apply_noise_tags([d1], doc_store)

        doc1 = doc_store.get(d1)
        doc2 = doc_store.get(d2)
        assert doc1 is not None
        assert doc1["metadata"]["content_tags"]["signal_quality"] == "noise"
        assert doc2 is not None
        assert doc2["metadata"]["content_tags"]["signal_quality"] == "high"
        assert updated == 1

    def test_skips_nonexistent_items(self, doc_store: SQLiteDocumentStore) -> None:
        updated = apply_noise_tags(["nonexistent-id"], doc_store)
        assert updated == 0

    def test_empty_candidates_noop(self, doc_store: SQLiteDocumentStore) -> None:
        updated = apply_noise_tags([], doc_store)
        assert updated == 0

    def test_creates_content_tags_if_missing(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        d1 = doc_store.put(None, "content without tags", {})
        updated = apply_noise_tags([d1], doc_store)

        doc = doc_store.get(d1)
        assert doc is not None
        assert doc["metadata"]["content_tags"]["signal_quality"] == "noise"
        assert updated == 1

    def test_preserves_other_tags(self, doc_store: SQLiteDocumentStore) -> None:
        d1 = doc_store.put(
            None,
            "tagged content",
            {
                "content_tags": {
                    "domain": ["data-pipeline"],
                    "content_type": "code",
                    "signal_quality": "standard",
                },
            },
        )
        apply_noise_tags([d1], doc_store)

        doc = doc_store.get(d1)
        assert doc is not None
        tags = doc["metadata"]["content_tags"]
        assert tags["domain"] == ["data-pipeline"]
        assert tags["content_type"] == "code"
        assert tags["signal_quality"] == "noise"


class TestNoiseTaggingPreservesRecency:
    """A demotion is a verdict about the row, not an edit to it (#406).

    ``apply_noise_tags`` writes ``signal_quality`` and two stamps derived
    from effectiveness analysis and hands the row's own content straight
    back — metadata-only — but omitted ``preserve_updated_at``, so every
    demotion re-dated the document it demoted.

    ``updated_at`` has two readers and this writer reaches **both**, which
    is what makes it worth more than the one stamp assertion its sibling
    sites get:

    * ``KeywordSearch`` decays relevance off the column. Latent *today* only
      because ``retrieve.noise.exclude_noise`` drops the item at the collect
      seam — but that boundary is deliberately default-pass and invertible
      (``tag_filters={"signal_quality": {"in": ["noise"]}}`` for curation
      tooling), and a later ``classify refresh`` revising the facet returns
      the row to service still carrying the falsified stamp.
    * ``mutate.retention._classify_document``'s ``lifecycle_states`` age gate
      reads it too, and *nothing* masks that one. ``apply_noise_tags``
      applies no lifecycle filter, so a ``superseded`` row is an ordinary
      candidate for demotion — and a demotion that re-dates it shields it
      from the very prune whose whole criterion is age.
    """

    @staticmethod
    def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Bind the SQLite document store's clock to a mutable holder.

        Exact stamps, rather than two wall-clock reads that merely *tend* to
        differ. Patched by module path, so on a non-SQLite store it would
        succeed and do nothing — and the stamp assertion would pass
        vacuously, since a preserved stamp equals itself either way. The two
        consequence tests are what fail loudly in that case.
        """
        holder: dict[str, Any] = {"now": datetime.now(UTC)}
        monkeypatch.setattr(
            "trellis.stores.sqlite.document.utc_now", lambda: holder["now"]
        )
        return holder

    def test_demotion_keeps_the_prior_updated_at(
        self, doc_store: SQLiteDocumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fails against the un-fixed call site, which re-stamps the row."""
        clock = self._fake_clock(monkeypatch)
        now = clock["now"]

        clock["now"] = now - timedelta(days=365)
        doc_store.put(
            "stale",
            "a year-old note about widget calibration",
            {"content_tags": {"signal_quality": "standard"}},
        )
        before = doc_store.get("stale")["updated_at"]

        clock["now"] = now
        assert apply_noise_tags(["stale"], doc_store) == 1

        doc = doc_store.get("stale")
        assert doc is not None
        # The demotion landed...
        assert doc["metadata"]["content_tags"]["signal_quality"] == "noise"
        # ...and the row does not claim to have been modified by it.
        assert doc["updated_at"] == before

    def test_a_demoted_document_does_not_outrank_a_fresh_one(
        self, doc_store: SQLiteDocumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The keyword-axis consequence, once the noise boundary is inverted.

        Both documents carry byte-identical content, so their FTS base ranks
        are equal by construction and recency is the only variable left.
        Un-fixed, demoting the year-old note stamps it with the demotion's
        own clock and the two come back level — so a curation tool that asks
        for noise items on purpose is handed a year-old row presented as the
        freshest thing it has.

        A *margin* is asserted, not a bare ordering: ``_apply_recency_decay``
        re-reads ``datetime.now(UTC)`` **per item**, so identical stamps still
        separate by nanoseconds in result order and a plain ``old < new``
        passes against the bug whenever the fresh row is scored first (#411
        shipped one that did).
        """
        clock = self._fake_clock(monkeypatch)
        now = clock["now"]
        body = "Widget calibration runs at sixty hertz."

        clock["now"] = now - timedelta(days=365)
        doc_store.put("old-doc", body, {})
        clock["now"] = now
        doc_store.put("new-doc", body, {})

        apply_noise_tags(["old-doc"], doc_store)

        # Half-life pinned at the call site rather than inherited from
        # ``DEFAULT_RECENCY_HALF_LIFE_DAYS``: retuning that global has nothing
        # to do with demotion, but at 365.0 the ratio rises past 0.5 and this
        # regression assertion would read as "#406 is back". Costs no
        # fix-sensitivity — under the bug both stamps are identical and the
        # ratio is 1.0 at every half-life.
        scores = {
            item.item_id: item.relevance_score
            for item in KeywordSearch(
                doc_store, recency_half_life_days=30.0
            ).search("calibration", limit=10)
        }
        assert set(scores) == {"old-doc", "new-doc"}, scores
        ratio = scores["old-doc"] / scores["new-doc"]
        assert ratio < 0.5, scores
        # Demoted by age, not annihilated: ``strategies.RECENCY_FLOOR`` (0.3)
        # is the floor a year-old row lands on. Coupled to that constant on
        # purpose — dropping it below 0.25 should fail here and be argued for.
        assert ratio > 0.25, scores

    def test_a_demoted_superseded_row_still_ages_out_of_retention(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The **second** reader, which nothing masks.

        ``retention.prune`` with ``lifecycle_states=["superseded"]`` means
        "archive superseded rows older than N days", and
        ``_classify_document`` implements *older* as
        ``updated_at or created_at``. ``superseded`` is the one lifecycle
        state that reaches that line — ``archived`` and ``current`` both
        return above it — and ``apply_noise_tags`` neither filters on
        lifecycle nor changes it, so it is free to re-date exactly the rows
        the gate measures.

        Un-fixed, demoting a year-old superseded note reset its age to zero
        and shielded it from the prune for a further 30 days: the criterion
        measured *time since the demotion* rather than the age it claims to.

        ``noise_documents`` is off deliberately. With it on, the noise branch
        selects the row first and returns before the age gate — the very
        masking this test exists to get past — which is why the reason code
        is asserted and not just the id.
        """
        docs = registry.knowledge.document_store
        clock = self._fake_clock(monkeypatch)
        now = clock["now"]

        clock["now"] = now - timedelta(days=365)
        docs.put(
            "stale",
            "a year-old note that has since been replaced",
            {
                LIFECYCLE_KEY: {"state": "superseded"},
                "content_tags": {"signal_quality": "standard"},
            },
        )

        clock["now"] = now
        assert apply_noise_tags(["stale"], docs) == 1

        report = resolve_candidates(
            RetentionCriteria(
                noise_documents=False,
                lifecycle_states=["superseded"],
                older_than_days=30,
            ),
            registry,
        )

        assert [(c.item_id, c.reason_code) for c in report.candidates] == [
            ("stale", "lifecycle_stale")
        ]
