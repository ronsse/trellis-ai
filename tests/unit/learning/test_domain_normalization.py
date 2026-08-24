"""Tests for domain-vocabulary normalization (#321).

The design of this analyzer was set by measurement against a real corpus, not
by reasoning, and the two things it *refuses* to do are the load-bearing ones.
So most of this file pins refusals: co-occurrence must not generate a merge on
its own, and token containment must be exact. Both rules exist because the
permissive version was built first and measured proposing
``playwright -> hunting``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trellis.learning.domain_normalization import (
    NOTE_COMPETING_CANONICAL,
    NOTE_LEXICAL_ONLY,
    NOTE_NO_GAIN,
    PARAM_COMPONENT_ID,
    RECOMMENDED_SEED_VALUES,
    REQUIRED_PARAM_KEYS,
    SIGNAL_COOCCURRENCE,
    SIGNAL_LEXICAL,
    DomainAliasCandidate,
    analyze_domain_alias_candidates,
    apply_normalization,
    compute_candidate_id,
    normalize_domain_tags,
    revoke_normalization,
    tag_tokens,
)
from trellis.ops import ParameterRegistry
from trellis.schemas.classification import SHADOW_TAGS_KEY, ShadowTags
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis_cli.analyze import _InMemoryParameterStore


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


def _registry(**overrides: Any) -> ParameterRegistry:
    values = dict(RECOMMENDED_SEED_VALUES)
    values.update(overrides)
    store = _InMemoryParameterStore()
    store.put(
        ParameterSet(
            scope=ParameterScope(component_id=PARAM_COMPONENT_ID),
            values=values,
            source="test",
        )
    )
    return ParameterRegistry(store=store)


def _seed(store: SQLiteDocumentStore, doc_id: str, *domains: str) -> None:
    """Store one document whose shadow record carries ``domains``."""
    store.put(
        doc_id,
        f"content for {doc_id}",
        {SHADOW_TAGS_KEY: ShadowTags(domain=list(domains)).model_dump(mode="json")},
    )


def _seed_canonical(store: SQLiteDocumentStore, tag: str, count: int) -> None:
    """Push ``tag`` over the canonical-support threshold."""
    for i in range(count):
        _seed(store, f"{tag}-canon-{i}", tag)


def _analyze(document_store, event_log, **kwargs: Any) -> list[DomainAliasCandidate]:
    kwargs.setdefault("registry", _registry())
    kwargs.setdefault("emit_events", False)
    return analyze_domain_alias_candidates(
        document_store=document_store, event_log=event_log, **kwargs
    )


class TestTokenisation:
    def test_separators_are_noise(self) -> None:
        assert tag_tokens("deer-hunting") == tag_tokens("deer_hunting")
        assert tag_tokens("Real-Estate") == frozenset({"real", "estate"})

    def test_containment_is_token_exact(self) -> None:
        """``hunt`` is not ``hunting``, and that distinction is the whole gate."""
        assert not tag_tokens("hunting") <= tag_tokens("scavenger-hunt")
        assert tag_tokens("hunting") <= tag_tokens("budget-hunting")


class TestGeneration:
    def test_lexical_containment_surfaces_a_merge(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        _seed_canonical(document_store, "hunting", 20)
        _seed(document_store, "d-alias", "budget-hunting")

        candidates = _analyze(document_store, event_log)

        assert [(c.alias, c.canonical) for c in candidates] == [
            ("budget-hunting", "hunting")
        ]
        assert SIGNAL_LEXICAL in candidates[0].signals

    def test_scavenger_hunt_is_not_a_spelling_of_hunting(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        """The trap. A shared *stem* is not a shared subject."""
        _seed_canonical(document_store, "hunting", 20)
        for i in range(7):
            _seed(document_store, f"scav-{i}", "scavenger-hunt")

        assert _analyze(document_store, event_log) == []

    def test_cooccurrence_alone_never_generates_a_merge(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        """Measured regression: this shape proposed ``playwright -> hunting``.

        A tag that appears on *every* document of a dominant subject is
        topically associated with it, not a spelling of it. Co-occurrence
        cannot tell those apart, so it corroborates and never generates.
        """
        for i in range(20):
            _seed(document_store, f"h-{i}", "hunting", "playwright")

        assert _analyze(document_store, event_log) == []

    def test_cooccurrence_corroborates_a_lexical_match(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        _seed_canonical(document_store, "hunting", 20)
        for i in range(4):
            _seed(document_store, f"both-{i}", "hunting", "deer-hunting")

        candidate = _analyze(document_store, event_log)[0]
        assert candidate.signals == (SIGNAL_LEXICAL, SIGNAL_COOCCURRENCE)
        assert candidate.cooccurrence_rate == 1.0
        assert not candidate.is_lexical_only

    def test_uncorroborated_spelling_is_flagged_not_dropped(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        """Excluding these removed every merge that had anything to gain."""
        _seed_canonical(document_store, "hunting", 20)
        _seed(document_store, "d1", "budget-hunting", "unrelated-topic")
        _seed(document_store, "d2", "hunting", "another-topic")

        candidate = next(
            c
            for c in _analyze(document_store, event_log)
            if c.alias == "budget-hunting"
        )
        assert candidate.is_lexical_only
        assert candidate.cooccurrence_documents == 0
        assert NOTE_LEXICAL_ONLY in candidate.notes

    def test_a_canonical_is_never_proposed_as_an_alias(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        """Merging two established tags is a different, riskier decision."""
        _seed_canonical(document_store, "planning", 20)
        _seed_canonical(document_store, "tax-planning", 20)

        assert [c.alias for c in _analyze(document_store, event_log)] == []


class TestEvidence:
    def test_documents_gained_excludes_documents_already_carrying_both(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        _seed_canonical(document_store, "hunting", 20)
        _seed(document_store, "both", "hunting", "deer-hunting")
        _seed(document_store, "alias-only", "deer-hunting")

        candidate = _analyze(document_store, event_log)[0]
        assert candidate.alias_documents == 2
        assert candidate.cooccurrence_documents == 1
        assert candidate.documents_gained == 1

    def test_a_merge_that_changes_nothing_says_so(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        _seed_canonical(document_store, "hunting", 20)
        _seed(document_store, "both", "hunting", "deer-hunting")

        candidate = _analyze(document_store, event_log)[0]
        assert candidate.documents_gained == 0
        assert NOTE_NO_GAIN in candidate.notes

    def test_cross_cutting_alias_names_its_competing_subject(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        """``tax-planning`` is both tax and planning; either merge loses one."""
        _seed_canonical(document_store, "tax", 20)
        _seed_canonical(document_store, "planning", 20)
        _seed(document_store, "d1", "tax-planning")

        candidate = _analyze(document_store, event_log)[0]
        assert candidate.competing_canonicals in (("planning",), ("tax",))
        assert NOTE_COMPETING_CANONICAL in candidate.notes

    def test_every_candidate_warns_that_domain_hard_excludes(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        _seed_canonical(document_store, "hunting", 20)
        _seed(document_store, "d1", "budget-hunting")

        assert any(
            "hard-exclude" in n for n in _analyze(document_store, event_log)[0].notes
        )


class TestConstraints:
    def test_missing_threshold_raises_rather_than_defaulting(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        values = dict(RECOMMENDED_SEED_VALUES)
        del values[REQUIRED_PARAM_KEYS[0]]
        store = _InMemoryParameterStore()
        store.put(
            ParameterSet(
                scope=ParameterScope(component_id=PARAM_COMPONENT_ID),
                values=values,
                source="test",
            )
        )
        with pytest.raises(KeyError, match="missing required"):
            _analyze(document_store, event_log, registry=ParameterRegistry(store=store))

    def test_an_out_of_range_threshold_raises(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        with pytest.raises(ValueError, match="must be in"):
            _analyze(
                document_store,
                event_log,
                registry=_registry(domain_min_cooccurrence=1.5),
            )

    def test_the_analyzer_filters_its_own_prior_writes(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        """An alias the operator already mapped is never re-proposed."""
        _seed_canonical(document_store, "hunting", 20)
        _seed(document_store, "d1", "budget-hunting")

        assert _analyze(document_store, event_log) != []
        assert (
            _analyze(
                document_store, event_log, known_aliases={"budget-hunting": "hunting"}
            )
            == []
        )

    def test_the_document_store_is_never_written(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        _seed_canonical(document_store, "hunting", 20)
        _seed(document_store, "d1", "budget-hunting")
        before = document_store.get("d1")

        _analyze(document_store, event_log)

        assert document_store.get("d1") == before

    def test_dry_run_emits_nothing(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        _seed_canonical(document_store, "hunting", 20)
        _seed(document_store, "d1", "budget-hunting")

        _analyze(document_store, event_log, emit_events=False)

        assert event_log.get_events(event_type=EventType.DOMAIN_ALIAS_CANDIDATE) == []

    def test_a_surfaced_candidate_emits_one_event(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        _seed_canonical(document_store, "hunting", 20)
        _seed(document_store, "d1", "budget-hunting")

        _analyze(document_store, event_log, emit_events=True)

        events = event_log.get_events(event_type=EventType.DOMAIN_ALIAS_CANDIDATE)
        assert len(events) == 1
        assert events[0].payload["alias"] == "budget-hunting"
        assert events[0].payload["canonical"] == "hunting"

    def test_the_event_carries_no_example_item_ids(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        """Same disclosure rule as ``TAG_KEYWORD_CANDIDATE``."""
        _seed_canonical(document_store, "hunting", 20)
        _seed(document_store, "secret-doc", "budget-hunting")

        _analyze(document_store, event_log, emit_events=True)

        payload = event_log.get_events(event_type=EventType.DOMAIN_ALIAS_CANDIDATE)[
            0
        ].payload
        assert "example_item_ids" not in payload
        assert payload["example_count"] == 1
        assert "secret-doc" not in str(payload)

    def test_a_repeat_run_is_suppressed_by_cooldown(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        _seed_canonical(document_store, "hunting", 20)
        _seed(document_store, "d1", "budget-hunting")
        now = datetime(2026, 8, 24, tzinfo=UTC)

        first = _analyze(document_store, event_log, emit_events=True, now=now)
        second = _analyze(
            document_store,
            event_log,
            emit_events=True,
            now=now + timedelta(days=1),
        )

        assert len(first) == 1
        assert second == []

    def test_an_empty_corpus_returns_nothing(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        assert _analyze(document_store, event_log) == []


class TestNormalizeDomainTags:
    def test_unmapped_tags_pass_through(self) -> None:
        """A merge list, never an allow-list."""
        assert normalize_domain_tags(["ai", "novel-tag"], {"ai": "computing"}) == [
            "computing",
            "novel-tag",
        ]

    def test_merged_tags_deduplicate(self) -> None:
        assert normalize_domain_tags(
            ["hunting", "deer-hunting"], {"deer-hunting": "hunting"}
        ) == ["hunting"]

    def test_order_is_preserved_on_first_appearance(self) -> None:
        assert normalize_domain_tags(["b", "a", "b"], None) == ["b", "a"]

    def test_a_cycle_terminates(self) -> None:
        """One-step rewrite, so an accidental cycle cannot hang the caller."""
        assert normalize_domain_tags(["a"], {"a": "b", "b": "a"}) == ["b"]

    def test_no_map_is_identity(self) -> None:
        assert normalize_domain_tags(["a", "b"], None) == ["a", "b"]


class TestApplyAndRevoke:
    @staticmethod
    def _candidate(alias: str, canonical: str) -> DomainAliasCandidate:
        return DomainAliasCandidate(
            alias=alias,
            canonical=canonical,
            alias_documents=1,
            canonical_documents=20,
            corpus_documents=21,
            cooccurrence_documents=0,
            cooccurrence_rate=0.0,
            neighbor_overlap=0.0,
            shared_tokens=(),
            signals=(SIGNAL_LEXICAL,),
            competing_canonicals=(),
            documents_gained=1,
            candidate_id=compute_candidate_id(alias, canonical),
        )

    def test_apply_then_revoke_restores_the_original(self) -> None:
        original = {"handwritten": "ai"}
        candidates = [self._candidate("budget-hunting", "hunting")]

        applied = apply_normalization(original, candidates)
        assert applied.domain_aliases == {
            "handwritten": "ai",
            "budget-hunting": "hunting",
        }
        assert revoke_normalization(applied.domain_aliases, applied) == original

    def test_revoke_never_removes_more_than_apply_added(self) -> None:
        """An operator's own alias survives a revoke that duplicates it."""
        original = {"budget-hunting": "hunting"}
        applied = apply_normalization(
            original, [self._candidate("budget-hunting", "hunting")]
        )

        assert applied.added == ()
        assert applied.skipped == (("budget-hunting", "hunting"),)
        assert revoke_normalization(applied.domain_aliases, applied) == original

    def test_an_existing_alias_is_never_repointed(self) -> None:
        """The operator's mapping is the authority."""
        applied = apply_normalization(
            {"tax-planning": "tax"}, [self._candidate("tax-planning", "planning")]
        )
        assert applied.domain_aliases == {"tax-planning": "tax"}

    def test_revoking_an_operator_repointed_alias_is_a_no_op(self) -> None:
        applied = apply_normalization({}, [self._candidate("a", "b")])
        repointed = {"a": "c"}
        assert revoke_normalization(repointed, applied) == repointed


class TestPromotionMinesNormalizedLabels:
    """The point of the whole exercise.

    Fragmentation does not merely make ``domain`` an unusable filter — it also
    starves the promotion analyzer, because a keyword's support is split
    across every spelling the model invented and may clear the floor on none
    of them. Normalizing before mining is what reconnects the two halves.
    """

    def test_a_rule_hidden_by_fragmentation_surfaces_once_merged(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        from trellis.learning.tag_evolution import (
            PARAM_COMPONENT_ID as TAG_COMPONENT,
        )
        from trellis.learning.tag_evolution import (
            RECOMMENDED_SEED_VALUES as TAG_SEEDS,
        )
        from trellis.learning.tag_evolution import analyze_tag_keyword_candidates

        # Six documents about the same subject, labelled three different ways,
        # against fourteen that are not — so the tag has a base rate to beat.
        # (A corpus where *every* document carries the tag has no achievable
        # lift by construction, and the analyzer correctly surfaces nothing.)
        spellings = ["hunting", "budget-hunting", "hunting-options"]
        for i in range(6):
            document_store.put(
                f"d{i}",
                "elk rifle season draw application deadline",
                {
                    SHADOW_TAGS_KEY: ShadowTags(domain=[spellings[i % 3]]).model_dump(
                        mode="json"
                    )
                },
            )
        for i in range(14):
            document_store.put(
                f"other-{i}",
                "quarterly invoice reconciliation ledger balance",
                {
                    SHADOW_TAGS_KEY: ShadowTags(domain=["finance"]).model_dump(
                        mode="json"
                    )
                },
            )

        params = _InMemoryParameterStore()
        params.put(
            ParameterSet(
                scope=ParameterScope(component_id=TAG_COMPONENT),
                values={
                    **TAG_SEEDS,
                    "tag_keyword_min_support": 5,
                    "tag_keyword_min_corpus": 5,
                },
                source="test",
            )
        )
        registry = ParameterRegistry(store=params)

        def run(aliases: dict[str, str] | None) -> list[str]:
            return [
                c.tag
                for c in analyze_tag_keyword_candidates(
                    document_store=document_store,
                    event_log=event_log,
                    registry=registry,
                    domain_aliases=aliases,
                    emit_events=False,
                )
            ]

        # Split three ways, no spelling reaches the support floor of 5.
        assert "hunting" not in run(None)

        # Merged, the subject has six documents and its rules surface. The
        # distractor tag surfaces either way — this is about `hunting` going
        # from invisible to promotable, not about the rest of the corpus.
        assert "hunting" in run(
            {"budget-hunting": "hunting", "hunting-options": "hunting"}
        )


class TestAspectTags:
    """Tags that name a mode of engagement are never merge destinations.

    ``planning`` is not a subject some documents are about — it is something
    you do *to* a subject. ``estate-planning`` is about estates and
    ``trip-planning`` is about travel; collapsing both into ``planning`` keeps
    the mode and discards the half that identifies the document.

    This has to be declared. A structural detector was built and measured
    against a real corpus: it rated ``hunting`` (67%) and ``architecture``
    (80%) as more modifier-like than ``planning`` (30%), because 701 tokens in
    that vocabulary happen to stand alone as their own tag, so ``budget`` and
    ``deer`` read as independent subjects. Subject-vs-aspect is semantic, and
    the corpus does not carry it.
    """

    def test_an_aspect_is_never_a_merge_destination(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        _seed_canonical(document_store, "planning", 20)
        _seed(document_store, "d1", "estate-planning")

        assert _analyze(document_store, event_log) != []
        assert _analyze(document_store, event_log, aspect_tags={"planning"}) == []

    def test_an_alias_redirects_to_the_subject_not_the_aspect(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        """``project-planning`` is a project, not a planning."""
        _seed_canonical(document_store, "planning", 20)
        _seed_canonical(document_store, "project", 20)
        _seed(document_store, "d1", "project-planning")

        candidate = next(
            c
            for c in _analyze(document_store, event_log, aspect_tags={"planning"})
            if c.alias == "project-planning"
        )
        assert candidate.canonical == "project"

    def test_an_aspect_tag_is_still_a_usable_tag(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        """Declaring an aspect stops merges into it; it deletes nothing."""
        _seed_canonical(document_store, "planning", 20)
        _seed(document_store, "d1", "estate-planning")

        _analyze(document_store, event_log, aspect_tags={"planning"})

        stored = document_store.get("planning-canon-0")
        assert stored["metadata"][SHADOW_TAGS_KEY]["domain"] == ["planning"]

    def test_an_aspect_no_longer_counts_as_a_competing_subject(
        self, document_store: SQLiteDocumentStore, event_log: SQLiteEventLog
    ) -> None:
        """``tax-planning`` is not cross-cutting once planning is an aspect —
        it is ``tax``, qualified."""
        _seed_canonical(document_store, "tax", 20)
        _seed_canonical(document_store, "planning", 20)
        _seed(document_store, "d1", "tax-planning")

        candidate = _analyze(document_store, event_log, aspect_tags={"planning"})[0]
        assert candidate.canonical == "tax"
        assert candidate.competing_canonicals == ()
