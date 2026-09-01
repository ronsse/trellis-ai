"""Tests for shadow-mode tagging (#321 Phase 1).

The acceptance criterion is a *negative* one — "a shadow pass over N documents
persists LLM tags that provably do not appear in any served pack" — so the
core of this file is the leak-proof slate: the shadow key is structurally
unreachable from tag filtering, the live tags are byte-identical after a pass,
and a real pack assembled over a shadowed store contains none of the shadow
vocabulary.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

from trellis.classify.protocol import (
    BOTH_MODES,
    ClassificationContext,
    ClassificationResult,
)
from trellis.classify.shadow import (
    PROTECTED_LIVE_KEYS,
    REASON_NO_SIGNAL,
    SHADOW_TAGS_KEY,
    compare_shadow_to_live,
    shadow_classify_item,
    shadow_classify_stale,
)
from trellis.retrieve.strategies import _apply_recency_decay
from trellis.schemas.classification import (
    DOCUMENT_FORM_KEY,
    ContentTags,
    ShadowTags,
    shadow_verdict,
)
from trellis.schemas.memory_op import JudgedOpType, MemoryOpJudgedPayload
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog


class FakeClassifier:
    """A :class:`Classifier` stand-in returning canned facet maps.

    Stands in for :class:`~trellis.classify.classifiers.llm.LLMFacetClassifier`
    so the pass is exercised without a model. The canned tags use the
    *enrichment* vocabulary on purpose (``reference`` / ``research``), which is
    what makes these tests cover the vocabulary collision rather than a
    convenient sanitised version of it.
    """

    def __init__(
        self,
        tags: dict[str, list[str]] | None = None,
        *,
        confidence: float = 0.9,
        name: str = "llm_facet",
    ) -> None:
        self._tags = tags if tags is not None else {}
        self._confidence = confidence
        self._name = name
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def allowed_modes(self) -> frozenset[str]:
        return BOTH_MODES

    def classify(
        self,
        content: str,
        *,
        context: ClassificationContext | None = None,
    ) -> ClassificationResult:
        self.calls.append(content)
        return ClassificationResult(
            tags=dict(self._tags),
            confidence=self._confidence,
            classifier_name=self._name,
        )


@pytest.fixture
def document_store(tmp_path: Path):
    store = SQLiteDocumentStore(tmp_path / "docs.db")
    yield store
    store.close()


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def _live_tags(**overrides: Any) -> dict[str, Any]:
    """A realistic live tag set, as classify-on-write would have written it."""
    tags = ContentTags(
        domain=[],
        content_type="procedure",
        signal_quality="standard",
        classified_by=["structural"],
        classified_at=datetime.now(UTC),
        classified_mode="ingestion",
        importance_scored_at=datetime.now(UTC),
    ).model_dump(mode="json")
    tags.update(overrides)
    return tags


def _seed(
    store: SQLiteDocumentStore,
    doc_id: str,
    content: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> str:
    return store.put(doc_id, content, metadata or {})


# ---------------------------------------------------------------------------
# The leak-proof slate — the Phase 1 acceptance criterion
# ---------------------------------------------------------------------------


class TestShadowIsInvisibleToRetrieval:
    def test_shadow_key_is_not_a_content_tags_facet(self) -> None:
        """The shadow key is unreachable from tag filtering by construction.

        Every tag filter both document backends build is rooted at the JSON
        path ``$.content_tags.<facet>``. A sibling *top-level* key can never be
        addressed by that path no matter what facet name a caller passes — so
        the guarantee is structural, not a matter of remembering to exclude it.
        """
        assert not SHADOW_TAGS_KEY.startswith("content_tags.")
        assert SHADOW_TAGS_KEY != "content_tags"
        # A caller passing the shadow key as a *facet* addresses
        # `$.content_tags.content_tags_shadow`, which no document has.
        assert f"$.content_tags.{SHADOW_TAGS_KEY}" != f"$.{SHADOW_TAGS_KEY}"

    def test_shadow_pass_never_touches_live_tags(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """Live tags and importance are byte-identical after a shadow pass."""
        live = _live_tags()
        _seed(
            document_store,
            "d1",
            "dbt models run nightly in the warehouse",
            metadata={"content_tags": live, "auto_importance": 0.42, "title": "T"},
        )
        before = json.dumps(document_store.get("d1")["metadata"], sort_keys=True)

        outcome = shadow_classify_item(
            "d1",
            classifier=FakeClassifier(
                {"domain": ["todoist"], "content_type": ["reference"]}
            ),
            document_store=document_store,
        )
        assert outcome.written is True

        after_meta = document_store.get("d1")["metadata"]
        assert after_meta["content_tags"] == live
        assert after_meta["auto_importance"] == 0.42
        # The only difference is the added shadow key.
        after_without_shadow = {
            k: v for k, v in after_meta.items() if k != SHADOW_TAGS_KEY
        }
        assert json.dumps(after_without_shadow, sort_keys=True) == before

    def test_protected_key_absent_before_stays_absent(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """A shadow pass never *introduces* a live tag key on an untagged doc."""
        _seed(document_store, "d1", "some content about airflow dags", metadata={})
        shadow_classify_item(
            "d1",
            classifier=FakeClassifier({"domain": ["data"]}),
            document_store=document_store,
        )
        meta = document_store.get("d1")["metadata"]
        for key in PROTECTED_LIVE_KEYS:
            assert key not in meta

    def test_shadow_tags_do_not_appear_in_a_served_pack(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """End-to-end: assemble a real pack over a shadowed store.

        The strongest form of the acceptance criterion — not "we did not add
        the key to the pack builder" but "the assembled pack, serialised whole,
        contains none of the shadow vocabulary".
        """
        from trellis.retrieve.pack_builder import PackBuilder
        from trellis.retrieve.strategies import KeywordSearch

        _seed(
            document_store,
            "d1",
            "kubernetes cluster deploy notes for the platform team",
            metadata={"content_tags": _live_tags(), "title": "Deploy notes"},
        )
        shadow_classify_item(
            "d1",
            classifier=FakeClassifier(
                {
                    "domain": ["yellowstone-national-park", "rv-parking"],
                    "content_type": ["journal"],
                }
            ),
            document_store=document_store,
        )
        # Confirm the shadow record really is on the row we are about to serve.
        assert SHADOW_TAGS_KEY in document_store.get("d1")["metadata"]

        pack = PackBuilder(strategies=[KeywordSearch(document_store)]).build(
            "kubernetes deploy"
        )
        assert pack.items, "fixture must actually retrieve, else the test is vacuous"

        served = json.dumps(pack.model_dump(mode="json"))
        for leaked in (
            SHADOW_TAGS_KEY,
            "yellowstone-national-park",
            "rv-parking",
            "journal",
        ):
            assert leaked not in served, f"{leaked!r} leaked into a served pack"

    def test_shadow_pass_does_not_move_recency_scoring(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """The *write* must not perturb the row either, not just the read.

        ``updated_at`` feeds ``KeywordSearch``'s recency decay, and a plain
        ``put`` stamps it with now. Before ``preserve_updated_at``, a
        whole-corpus shadow pass reset every document to "brand new" — a
        365-day-old document's recency multiplier went 0.30 -> 1.0 — flattening
        recency ordering across the entire store. Retrieval moved, invisibly to
        a test that only checked the shadow values stayed out of the pack.
        """
        _seed(document_store, "aged", "kubernetes deploy notes", metadata={})
        aged_stamp = (datetime.now(UTC) - timedelta(days=365)).isoformat()
        document_store._conn.execute(
            "UPDATE documents SET created_at=?, updated_at=? WHERE doc_id='aged'",
            (aged_stamp, aged_stamp),
        )
        document_store._conn.commit()

        before = document_store.get("aged")["updated_at"]
        shadow_classify_stale(
            classifier=FakeClassifier({"domain": ["x"], "content_type": ["notes"]}),
            document_store=document_store,
        )
        after = document_store.get("aged")

        assert SHADOW_TAGS_KEY in after["metadata"], "the pass must have run"
        assert str(after["updated_at"]) == str(before)
        # The decay multiplier the score is built from is therefore unchanged.
        assert _apply_recency_decay(1.0, after["updated_at"]) == pytest.approx(
            _apply_recency_decay(1.0, before)
        )

    def test_shadow_domain_does_not_scope_a_domain_filtered_query(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """A shadow ``domain`` neither includes nor excludes on a scoped query.

        The failure this guards is the #282 shape in reverse: if shadow tags
        were readable by the tag filter, a shadowed document would start
        matching (or missing) domain-scoped queries the moment the pass ran.
        """
        _seed(
            document_store,
            "d1",
            "terraform vpc module notes",
            metadata={"content_tags": _live_tags()},
        )
        shadow_classify_item(
            "d1",
            classifier=FakeClassifier({"domain": ["infrastructure"]}),
            document_store=document_store,
        )
        # Scoped to a domain the *shadow* claims: the document still passes
        # only via the default-pass rule for its empty live domain, exactly as
        # it did before the shadow pass.
        hits = document_store.search(
            "terraform",
            filters={"content_tags": {"domain": {"in": ["infrastructure"]}}},
        )
        assert [h["doc_id"] for h in hits] == ["d1"]
        # And scoped to an unrelated domain it also still passes — because the
        # live domain is empty (default-pass), not because the shadow matched.
        hits_other = document_store.search(
            "terraform",
            filters={"content_tags": {"domain": {"in": ["frontend"]}}},
        )
        assert [h["doc_id"] for h in hits_other] == ["d1"]


# ---------------------------------------------------------------------------
# The record itself
# ---------------------------------------------------------------------------


class TestShadowRecord:
    def test_open_vocabulary_content_type_is_preserved(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """``reference`` survives — the value ``ContentTags`` would reject.

        This is the whole reason shadow tags are not a ``ContentTags``: nine of
        the ten enrichment classifications fail that model's ``Literal``.
        """
        with pytest.raises(Exception):  # noqa: B017
            ContentTags(content_type="reference")  # type: ignore[arg-type]

        _seed(document_store, "d1", "content", metadata={})
        outcome = shadow_classify_item(
            "d1",
            classifier=FakeClassifier({"content_type": ["reference"]}),
            document_store=document_store,
        )
        assert outcome.shadow is not None
        assert outcome.shadow["content_type"] == "reference"
        stored = document_store.get("d1")["metadata"][SHADOW_TAGS_KEY]
        assert ShadowTags.model_validate(stored).content_type == "reference"

    def test_provenance_is_recorded(self, document_store: SQLiteDocumentStore) -> None:
        _seed(document_store, "d1", "content", metadata={})
        shadow_classify_item(
            "d1",
            classifier=FakeClassifier({"domain": ["x"]}, confidence=0.75),
            document_store=document_store,
            model_id="hermes3:8b",
        )
        record = ShadowTags.model_validate(
            document_store.get("d1")["metadata"][SHADOW_TAGS_KEY]
        )
        assert record.model_id == "hermes3:8b"
        assert record.classified_by == ["llm_facet"]
        assert record.confidence == pytest.approx(0.75)
        assert record.classified_at is not None

    def test_out_of_band_llm_keys_are_dropped(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """``_auto_importance`` / ``_auto_summary`` are scores and prose.

        ``LLMFacetClassifier`` smuggles them through the tag map; they must not
        land in ``custom``, which feeds the promotion analyzer.
        """
        _seed(document_store, "d1", "content", metadata={})
        shadow_classify_item(
            "d1",
            classifier=FakeClassifier(
                {
                    "domain": ["x"],
                    "_auto_importance": ["0.8"],
                    "_auto_summary": ["a summary sentence"],
                }
            ),
            document_store=document_store,
        )
        record = ShadowTags.model_validate(
            document_store.get("d1")["metadata"][SHADOW_TAGS_KEY]
        )
        assert record.custom == {}

    def test_out_of_band_only_result_is_no_signal_not_an_empty_record(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        """A truthy tag map that reduces to nothing must count as no signal.

        ``LLMFacetClassifier`` adds ``_auto_importance`` / ``_auto_summary``
        independently of the facets, so a model that summarises but classifies
        nothing returns a non-empty map carrying no tags. Writing that would
        persist an empty record, count it as written, and — since
        ``_needs_shadow`` only asks whether a record exists — mark the document
        judged forever, inflating the coverage number this pass produces.
        """
        _seed(document_store, "d1", "content", metadata={})
        outcome = shadow_classify_item(
            "d1",
            classifier=FakeClassifier(
                {"_auto_importance": ["0.8"], "_auto_summary": ["a summary"]}
            ),
            document_store=document_store,
            event_log=event_log,
        )
        assert outcome.written is False
        assert outcome.reason == REASON_NO_SIGNAL
        assert SHADOW_TAGS_KEY not in document_store.get("d1")["metadata"]
        # The verdict is still logged — coverage is the point.
        events = event_log.get_events(event_type=EventType.MEMORY_OP_JUDGED)
        assert len(events) == 1
        assert events[0].payload["decision"] == "unclassified"

    def test_batch_re_judges_a_document_that_produced_no_signal(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """No record written means the next pass tries again, as it should."""
        _seed(document_store, "d1", "content", metadata={})
        empty = FakeClassifier({"_auto_summary": ["s"]})
        shadow_classify_stale(classifier=empty, document_store=document_store)
        assert len(empty.calls) == 1
        shadow_classify_stale(classifier=empty, document_store=document_store)
        assert len(empty.calls) == 2, "a no-signal document is not marked judged"

    def test_dry_run_persists_nothing(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        _seed(document_store, "d1", "content", metadata={})
        outcome = shadow_classify_item(
            "d1",
            classifier=FakeClassifier({"domain": ["x"]}),
            document_store=document_store,
            event_log=event_log,
            dry_run=True,
        )
        assert outcome.written is True
        assert outcome.shadow is not None
        assert SHADOW_TAGS_KEY not in document_store.get("d1")["metadata"]
        assert event_log.get_events(event_type=EventType.MEMORY_OP_JUDGED) == []

    def test_missing_document(self, document_store: SQLiteDocumentStore) -> None:
        outcome = shadow_classify_item(
            "nope",
            classifier=FakeClassifier({"domain": ["x"]}),
            document_store=document_store,
        )
        assert outcome.written is False
        assert outcome.shadow is None


# ---------------------------------------------------------------------------
# The training-pair event
# ---------------------------------------------------------------------------


class TestJudgedEvent:
    def test_emits_leak_safe_training_pair(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        content = "a document about parking an rv near yellowstone"
        _seed(document_store, "d1", content, metadata={})
        shadow_classify_item(
            "d1",
            classifier=FakeClassifier(
                {
                    "domain": ["yellowstone-national-park"],
                    "content_type": ["journal"],
                }
            ),
            document_store=document_store,
            event_log=event_log,
            model_id="hermes3:8b",
        )
        events = event_log.get_events(event_type=EventType.MEMORY_OP_JUDGED)
        assert len(events) == 1
        payload = MemoryOpJudgedPayload.model_validate(events[0].payload)
        assert payload.op_type is JudgedOpType.CLASSIFICATION
        assert payload.model_id == "hermes3:8b"
        assert payload.decision == "journal"
        assert payload.subject_ref.ref_id == "d1"
        assert payload.input_digest.length == len(content)

    def test_event_carries_no_content_and_no_domain_tags(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        """The open-vocabulary tags reveal subject matter — keep them off the log.

        The event log has a different access/retention profile than the doc
        store, which is why the content-revealing half of a shadow verdict
        lives on the document instead.
        """
        content = "notes about parking an rv near yellowstone national park"
        _seed(document_store, "d1", content, metadata={})
        shadow_classify_item(
            "d1",
            classifier=FakeClassifier(
                {"domain": ["yellowstone-national-park"], "content_type": ["journal"]}
            ),
            document_store=document_store,
            event_log=event_log,
        )
        raw = json.dumps(
            event_log.get_events(event_type=EventType.MEMORY_OP_JUDGED)[0].payload
        )
        assert "yellowstone" not in raw
        assert "rv" not in raw.replace("serve", "")  # substring-safe check
        assert content not in raw

    def test_no_signal_still_emits_a_verdict(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        """ "The model produced nothing" is the coverage signal, not a non-event."""
        _seed(document_store, "d1", "content", metadata={})
        outcome = shadow_classify_item(
            "d1",
            classifier=FakeClassifier({}),
            document_store=document_store,
            event_log=event_log,
        )
        assert outcome.written is False
        events = event_log.get_events(event_type=EventType.MEMORY_OP_JUDGED)
        assert len(events) == 1
        assert events[0].payload["decision"] == "unclassified"
        assert SHADOW_TAGS_KEY not in document_store.get("d1")["metadata"]

    def test_emit_failure_does_not_roll_back_the_record(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        class BrokenLog:
            def emit(self, *args: Any, **kwargs: Any) -> None:
                msg = "log down"
                raise RuntimeError(msg)

        _seed(document_store, "d1", "content", metadata={})
        outcome = shadow_classify_item(
            "d1",
            classifier=FakeClassifier({"domain": ["x"]}),
            document_store=document_store,
            event_log=BrokenLog(),  # type: ignore[arg-type]
        )
        assert outcome.written is True
        assert SHADOW_TAGS_KEY in document_store.get("d1")["metadata"]


# ---------------------------------------------------------------------------
# Batch pass
# ---------------------------------------------------------------------------


class TestBatchPass:
    def test_shadows_every_unshadowed_document(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        for i in range(3):
            _seed(document_store, f"d{i}", f"content number {i}", metadata={})
        result = shadow_classify_stale(
            classifier=FakeClassifier({"domain": ["x"]}),
            document_store=document_store,
        )
        assert result.scanned == 3
        assert result.written == 3
        assert sorted(result.item_ids_written) == ["d0", "d1", "d2"]

    def test_default_skips_already_shadowed(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """The default is "never judged", not a staleness window.

        Each item costs a model call, so re-judging an unchanged document with
        an unchanged model is spend with no measurement attached.
        """
        _seed(document_store, "d1", "content", metadata={})
        classifier = FakeClassifier({"domain": ["x"]})
        shadow_classify_stale(classifier=classifier, document_store=document_store)
        assert len(classifier.calls) == 1

        result = shadow_classify_stale(
            classifier=classifier, document_store=document_store
        )
        assert result.written == 0
        assert result.skipped_fresh == 1
        assert len(classifier.calls) == 1, "no second model call"

    def test_max_age_days_rejudges_stale_records(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        stale = ShadowTags(
            domain=["old"],
            classified_at=datetime.now(UTC) - timedelta(days=90),
        ).model_dump(mode="json")
        _seed(document_store, "d1", "content", metadata={SHADOW_TAGS_KEY: stale})

        result = shadow_classify_stale(
            classifier=FakeClassifier({"domain": ["fresh"]}),
            document_store=document_store,
            max_age_days=30,
        )
        assert result.written == 1
        record = document_store.get("d1")["metadata"][SHADOW_TAGS_KEY]
        assert record["domain"] == ["fresh"]

    def test_malformed_shadow_record_is_treated_as_absent(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        _seed(document_store, "d1", "content", metadata={SHADOW_TAGS_KEY: "legacy"})
        result = shadow_classify_stale(
            classifier=FakeClassifier({"domain": ["x"]}),
            document_store=document_store,
        )
        assert result.written == 1

    def test_skips_empty_content(self, document_store: SQLiteDocumentStore) -> None:
        _seed(document_store, "d1", "", metadata={})
        result = shadow_classify_stale(
            classifier=FakeClassifier({"domain": ["x"]}),
            document_store=document_store,
        )
        assert result.skipped_missing_content == 1
        assert result.written == 0

    def test_classifier_failure_is_fail_soft_per_document(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """One unreachable model call must not abort a whole-store pass."""
        for i in range(3):
            _seed(document_store, f"d{i}", f"content {i}", metadata={})

        calls: list[str] = []

        class FlakyClassifier(FakeClassifier):
            def classify(
                self,
                content: str,
                *,
                context: ClassificationContext | None = None,
            ) -> ClassificationResult:
                calls.append(content)
                if content == "content 1":
                    msg = "model unreachable"
                    raise RuntimeError(msg)
                return super().classify(content, context=context)

        result = shadow_classify_stale(
            classifier=FlakyClassifier({"domain": ["x"]}),
            document_store=document_store,
        )
        assert result.errors == 1
        assert result.written == 2
        assert len(calls) == 3, "the pass continued past the failure"

    def test_limit_and_paging(self, document_store: SQLiteDocumentStore) -> None:
        for i in range(5):
            _seed(document_store, f"d{i}", f"content {i}", metadata={})
        result = shadow_classify_stale(
            classifier=FakeClassifier({"domain": ["x"]}),
            document_store=document_store,
            limit=2,
            page_size=1,
        )
        assert result.scanned == 2
        assert result.written == 2

    def test_dry_run_batch_writes_nothing(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        _seed(document_store, "d1", "content", metadata={})
        result = shadow_classify_stale(
            classifier=FakeClassifier({"domain": ["x"]}),
            document_store=document_store,
            dry_run=True,
        )
        assert result.written == 1
        assert SHADOW_TAGS_KEY not in document_store.get("d1")["metadata"]


# ---------------------------------------------------------------------------
# Comparison query
# ---------------------------------------------------------------------------


class TestCompareShadowToLive:
    def test_reports_coverage_gain_on_content_type(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """The measured motivation: the LLM produces a class where we produce none."""
        live_without_class = _live_tags(content_type=None)
        _seed(
            document_store,
            "d1",
            "c1",
            metadata={
                "content_tags": live_without_class,
                SHADOW_TAGS_KEY: ShadowTags(content_type="reference").model_dump(
                    mode="json"
                ),
            },
        )
        report = compare_shadow_to_live(document_store=document_store)
        assert report.with_shadow == 1
        assert report.per_facet["content_type"].live_missing == 1
        assert report.per_facet["content_type"].comparable == 0
        assert report.per_facet["content_type"].agreement_rate is None

    def test_agreement_and_disagreement_are_counted_separately(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        _seed(
            document_store,
            "agree",
            "c",
            metadata={
                "content_tags": _live_tags(content_type="procedure"),
                SHADOW_TAGS_KEY: ShadowTags(content_type="procedure").model_dump(
                    mode="json"
                ),
            },
        )
        _seed(
            document_store,
            "differ",
            "c",
            metadata={
                "content_tags": _live_tags(content_type="procedure"),
                SHADOW_TAGS_KEY: ShadowTags(content_type="reference").model_dump(
                    mode="json"
                ),
            },
        )
        report = compare_shadow_to_live(document_store=document_store)
        facet = report.per_facet["content_type"]
        assert facet.agreed == 1
        assert facet.disagreed == 1
        assert facet.agreement_rate == pytest.approx(0.5)

    def test_empty_live_domain_counts_as_absent_not_disagreement(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """``domain: []`` is what classify-on-write writes, deliberately.

        Counting it as "live said something different" would report near-total
        disagreement on the one facet where the live side has, by design, said
        nothing at all.
        """
        _seed(
            document_store,
            "d1",
            "c",
            metadata={
                "content_tags": _live_tags(domain=[]),
                SHADOW_TAGS_KEY: ShadowTags(domain=["task-management"]).model_dump(
                    mode="json"
                ),
            },
        )
        report = compare_shadow_to_live(document_store=document_store)
        facet = report.per_facet["domain"]
        assert facet.disagreed == 0
        assert facet.live_missing == 1

    def test_scalar_domain_shape_is_tolerated(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """A flat ``domain: "payments"`` must not shred into per-character tags."""
        _seed(
            document_store,
            "d1",
            "c",
            metadata={
                "content_tags": {"domain": "payments"},
                SHADOW_TAGS_KEY: ShadowTags(domain=["payments"]).model_dump(
                    mode="json"
                ),
            },
        )
        report = compare_shadow_to_live(document_store=document_store)
        assert report.per_facet["domain"].agreed == 1

    def test_counts_out_of_vocabulary_content_types(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """The vocabulary collision, made countable."""
        for i, value in enumerate(["reference", "reference", "documentation"]):
            _seed(
                document_store,
                f"d{i}",
                "c",
                metadata={
                    SHADOW_TAGS_KEY: ShadowTags(content_type=value).model_dump(
                        mode="json"
                    )
                },
            )
        report = compare_shadow_to_live(document_store=document_store)
        # `documentation` is the single value the two vocabularies share.
        assert report.out_of_vocabulary_content_types == {"reference": 2}

    def test_unshadowed_documents_are_scanned_but_not_compared(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        _seed(document_store, "d1", "c", metadata={"content_tags": _live_tags()})
        report = compare_shadow_to_live(document_store=document_store)
        assert report.scanned == 1
        assert report.with_shadow == 0
        assert report.comparisons == []

    def test_per_document_comparison_rows(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        _seed(
            document_store,
            "d1",
            "c",
            metadata={
                "content_tags": _live_tags(content_type="procedure"),
                SHADOW_TAGS_KEY: ShadowTags(
                    content_type="reference", domain=["ops"]
                ).model_dump(mode="json"),
            },
        )
        report = compare_shadow_to_live(
            document_store=document_store, collect_comparisons=True
        )
        assert len(report.comparisons) == 1
        row = report.comparisons[0]
        assert row.item_id == "d1"
        assert row.agreements["content_type"] is False
        assert row.agreements["domain"] is None  # live domain empty

    def test_per_document_rows_are_off_by_default(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """Retaining a row per document is O(corpus); a caller must ask for it."""
        _seed(
            document_store,
            "d1",
            "c",
            metadata={
                SHADOW_TAGS_KEY: ShadowTags(domain=["x"]).model_dump(mode="json")
            },
        )
        report = compare_shadow_to_live(document_store=document_store)
        assert report.with_shadow == 1
        assert report.comparisons == []

    def test_collect_comparisons_false_keeps_aggregates_only(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        _seed(
            document_store,
            "d1",
            "c",
            metadata={
                SHADOW_TAGS_KEY: ShadowTags(domain=["x"]).model_dump(mode="json")
            },
        )
        report = compare_shadow_to_live(
            document_store=document_store, collect_comparisons=False
        )
        assert report.with_shadow == 1
        assert report.comparisons == []


class TestVocabularySeam:
    """The shadow pass must read the verdict the LLM classifier actually writes.

    #321 was written when :class:`LLMFacetClassifier` filed its
    ``auto_class`` under ``content_type``. #324 moved it to
    :data:`~trellis.schemas.classification.DOCUMENT_FORM_KEY`, correctly — the
    closed ``ContentTags.content_type`` ``Literal`` rejects nine of the ten
    enrichment values, so the two vocabularies cannot share a key. Both changes
    were green, because neither suite crossed the seam: this file's
    ``FakeClassifier`` hand-wrote ``content_type`` tags the real classifier no
    longer emits.

    The consequence was silent and total. Every judged document recorded
    ``decision="unclassified"`` on its ``MEMORY_OP_JUDGED`` event however
    confident the model was — #264's training-pair substrate wired to a
    constant, which is the failure class this repo keeps finding.

    So these tests derive the tag keys from the **real** classifier rather than
    restating them. A future rename breaks the test instead of the measurement.
    """

    @staticmethod
    def _real_llm_facet_tags() -> dict[str, Any]:
        """Drive the real classifier with a stub model; return its tag map."""
        from types import SimpleNamespace

        from trellis.classify.classifiers.llm import LLMFacetClassifier

        class _StubEnrichment:
            async def enrich(self, content: str, *, title: str = "") -> Any:
                return SimpleNamespace(
                    success=True,
                    auto_tags=["postgres", "infrastructure"],
                    auto_class="reference",
                    auto_importance=0.8,
                    auto_summary="notes on connection pooling",
                    tag_confidence=0.82,
                    class_confidence=0.91,
                    error=None,
                )

        classifier = LLMFacetClassifier(_StubEnrichment())  # type: ignore[arg-type]
        return classifier.classify("pgbouncer sits in front of postgres").tags

    def test_real_classifier_does_not_emit_content_type(self) -> None:
        """Pins the seam itself, so the tests below cannot pass vacuously."""
        tags = self._real_llm_facet_tags()
        assert "content_type" not in tags
        assert tags[DOCUMENT_FORM_KEY] == ["reference"]

    def test_llm_verdict_reaches_the_judged_event(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        _seed(document_store, "d1", "pgbouncer sits in front of postgres")
        shadow_classify_item(
            "d1",
            classifier=FakeClassifier(self._real_llm_facet_tags()),
            document_store=document_store,
            event_log=event_log,
            model_id="hermes3:8b",
        )
        events = event_log.get_events(event_type=EventType.MEMORY_OP_JUDGED)
        payload = MemoryOpJudgedPayload.model_validate(events[0].payload)
        assert payload.decision == "reference"

    def test_verdict_prefers_the_modelled_facet(self) -> None:
        """A classifier that emits both is read on ``content_type``."""
        tags = ShadowTags(content_type="pattern", custom={DOCUMENT_FORM_KEY: ["notes"]})
        assert tags.verdict == "pattern"

    def test_verdict_falls_back_to_document_form(self) -> None:
        assert (
            ShadowTags(custom={DOCUMENT_FORM_KEY: ["research"]}).verdict == "research"
        )

    def test_verdict_is_none_when_the_model_said_nothing(self) -> None:
        """``None`` must mean *the model produced no label*, not *unread key*."""
        assert ShadowTags(domain=["postgres"]).verdict is None

    def test_out_of_vocabulary_counts_the_real_record_shape(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """The collision counter must see the key the LLM actually writes.

        ``out_of_vocabulary_content_types`` is the one measurement that
        justifies :class:`ShadowTags` existing — "promoting shadow
        ``content_type`` wholesale would mean adopting a different taxonomy".
        Reading the raw facet counted **zero** collisions on a production
        corpus that measured 924/991, because the LLM files its label under
        ``document_form``.
        """
        for doc_id, form in (
            ("d1", "reference"),
            ("d2", "research"),
            ("d3", "documentation"),
        ):
            _seed(
                document_store,
                doc_id,
                "c",
                metadata={
                    SHADOW_TAGS_KEY: ShadowTags(
                        custom={DOCUMENT_FORM_KEY: [form]}
                    ).model_dump(mode="json")
                },
            )
        report = compare_shadow_to_live(document_store=document_store)
        # `documentation` is the single value the two vocabularies share.
        assert report.out_of_vocabulary_content_types == {
            "reference": 1,
            "research": 1,
        }

    def test_shadow_verdict_survives_a_scalar_facet_value(self) -> None:
        """A bare string, not a list — the #282 shape that shreds under set()."""
        assert shadow_verdict({"custom": {DOCUMENT_FORM_KEY: "notes"}}) == "notes"


# ---------------------------------------------------------------------------
# #421 — the model call sits between the read and the write
# ---------------------------------------------------------------------------


class RacingClassifier(FakeClassifier):
    """A classifier that performs a store write from inside ``classify``.

    That is where the race actually lives: ``shadow_classify_item`` reads the
    row, spends ~1.6 s in the model, then writes. Driving the concurrent write
    from the classifier puts it in exactly that window rather than
    approximating it with an out-of-band ``put``.
    """

    def __init__(self, side_effect, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._side_effect = side_effect
        self._fired = False

    def classify(
        self,
        content: str,
        *,
        context: ClassificationContext | None = None,
    ) -> ClassificationResult:
        if not self._fired:
            self._fired = True
            self._side_effect()
        return super().classify(content, context=context)


class TestShadowSurvivesAConcurrentWrite:
    """A whole-corpus shadow pass opens the #421 window once per document.

    The module's stated guarantee is that shadowing "does not perturb the
    row". Writing back a pre-model snapshot perturbs it in the strongest way
    available — it reverts content *and* the live tags
    :data:`PROTECTED_LIVE_KEYS` exists to protect — and, because the write
    correctly preserves ``updated_at``, leaves no trace at all.
    """

    _TAGS: ClassVar[dict[str, list[str]]] = {
        "domain": ["postgres"],
        "content_type": ["reference"],
    }

    def test_the_concurrent_content_survives_and_the_record_still_lands(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        _seed(document_store, "d1", "the original body", metadata={"title": "T"})

        def concurrent_write() -> None:
            document_store.put(
                "d1", "the concurrent body", {"title": "T", "written_by": "other"}
            )

        outcome = shadow_classify_item(
            "d1",
            classifier=RacingClassifier(concurrent_write, tags=self._TAGS),
            document_store=document_store,
        )

        assert outcome.written is True
        stored = document_store.get("d1")
        assert stored["content"] == "the concurrent body"
        assert stored["metadata"]["written_by"] == "other"
        assert stored["metadata"][SHADOW_TAGS_KEY]["domain"] == ["postgres"]

    def test_live_tags_written_during_the_model_call_are_not_reverted(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """The guarantee this module makes, against the writer that breaks it.

        A concurrent classify refresh is the likeliest second writer on a
        shadowed store, and it touches precisely the keys
        :data:`PROTECTED_LIVE_KEYS` names. Splatting the pre-model snapshot
        put them back to their pre-refresh values — the shadow pass silently
        undoing the live tagging path.
        """
        _seed(document_store, "d1", "body", metadata={"content_tags": _live_tags()})
        fresh = _live_tags(signal_quality="noise")

        def concurrent_tag_write() -> None:
            doc = document_store.get("d1")
            document_store.put(
                "d1", doc["content"], {**doc["metadata"], "content_tags": fresh}
            )

        shadow_classify_item(
            "d1",
            classifier=RacingClassifier(concurrent_tag_write, tags=self._TAGS),
            document_store=document_store,
        )

        live = document_store.get("d1")["metadata"]["content_tags"]
        assert live["signal_quality"] == "noise"

    def test_the_race_is_counted_on_the_outcome_and_the_batch(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        _seed(document_store, "d1", "the original body")
        outcome = shadow_classify_item(
            "d1",
            classifier=RacingClassifier(
                lambda: document_store.put("d1", "rewritten", {}), tags=self._TAGS
            ),
            document_store=document_store,
        )
        assert outcome.stale_snapshot is True

        _seed(document_store, "d2", "the original body")
        result = shadow_classify_stale(
            classifier=RacingClassifier(
                lambda: document_store.put("d2", "rewritten", {}), tags=self._TAGS
            ),
            document_store=document_store,
        )
        assert result.stale_snapshot == 1
        assert result.written == 1

    def test_the_counter_reads_zero_when_nothing_raced(
        self, document_store: SQLiteDocumentStore
    ) -> None:
        """Both arms asserted so neither can drift to a constant."""
        _seed(document_store, "d1", "body")
        outcome = shadow_classify_item(
            "d1",
            classifier=FakeClassifier(self._TAGS),
            document_store=document_store,
        )
        assert outcome.written is True
        assert outcome.stale_snapshot is False

        _seed(document_store, "d2", "body")
        result = shadow_classify_stale(
            classifier=FakeClassifier(self._TAGS), document_store=document_store
        )
        assert result.stale_snapshot == 0

    def test_a_document_deleted_during_the_model_call_is_not_resurrected(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        """A ``put`` would re-insert it with pre-model content.

        Counted as an error, beside the identical concurrent-delete case the
        function already handles one branch up — a document we were asked to
        judge and did not is not a skip. No ``MEMORY_OP_JUDGED`` is emitted:
        the event's subject pointer would name a row that no longer exists.
        """
        _seed(document_store, "d1", "body")
        outcome = shadow_classify_item(
            "d1",
            classifier=RacingClassifier(
                lambda: document_store.delete("d1"), tags=self._TAGS
            ),
            document_store=document_store,
            event_log=event_log,
        )

        assert outcome.written is False
        assert document_store.get("d1") is None
        assert event_log.get_events(event_type=EventType.MEMORY_OP_JUDGED) == []

    def test_the_write_still_preserves_updated_at(
        self,
        document_store: SQLiteDocumentStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Re-reading must not have quietly turned this into a normal write.

        Asserted against the seeded stamp as well as against itself, so the
        ``fake_document_clock`` vacuity caveat cannot make it pass silently.
        """
        from tests.document_recency import fake_document_clock

        clock = fake_document_clock(monkeypatch)
        now = clock["now"]
        clock["now"] = now - timedelta(days=365)
        _seed(document_store, "d1", "body")
        before = document_store.get("d1")["updated_at"]
        assert before == (now - timedelta(days=365)).isoformat()

        clock["now"] = now
        shadow_classify_item(
            "d1",
            classifier=FakeClassifier(self._TAGS),
            document_store=document_store,
        )
        assert document_store.get("d1")["updated_at"] == before
