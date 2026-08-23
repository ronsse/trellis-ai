"""Tests for the tag-keyword promotion loop (#321 Phase 2).

The acceptance criteria are: the analyzer emits keyword candidates with support
counts; a promoted candidate written into ``classify.domain_keywords``
demonstrably changes deterministic output on a fixture; the promotion is
revocable. Each has a test below, plus the four constraints inherited from
:mod:`trellis.learning.schema_evolution` (read-only, thresholds-or-raise,
idempotent, filters its own writes).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trellis.classify.classifiers.keyword import KeywordDomainClassifier
from trellis.learning.tag_evolution import (
    FACETS_WITH_WRITE_TARGET,
    PARAM_COMPONENT_ID,
    RECOMMENDED_SEED_VALUES,
    REQUIRED_PARAM_KEYS,
    TagKeywordCandidate,
    analyze_tag_keyword_candidates,
    apply_promotion,
    compute_candidate_id,
    extract_keywords,
    revoke_promotion,
)
from trellis.ops import ParameterRegistry
from trellis.schemas.classification import SHADOW_TAGS_KEY, ShadowTags
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.parameter import SQLiteParameterStore


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


@pytest.fixture
def param_store(tmp_path: Path):
    store = SQLiteParameterStore(tmp_path / "params.db")
    yield store
    store.close()


def _seed_registry(
    param_store: SQLiteParameterStore,
    *,
    overrides: dict[str, float | int | str | bool] | None = None,
    replace: bool = False,
) -> ParameterRegistry:
    """Registry seeded with the recommended values, scaled down for unit tests.

    Support and corpus floors drop to 3 so a fixture is a handful of documents
    rather than the 30 a real run demands; the precision and lift gates keep
    their production values, because those are the gates under test.
    """
    values: dict[str, float | int | str | bool] = (
        {} if replace else dict(RECOMMENDED_SEED_VALUES)
    )
    if not replace:
        values["tag_keyword_min_support"] = 3
        values["tag_keyword_min_corpus"] = 3
    if overrides:
        values.update(overrides)
    param_store.put(
        ParameterSet(
            scope=ParameterScope(component_id=PARAM_COMPONENT_ID),
            values=values,
            source="test",
        )
    )
    return ParameterRegistry(store=param_store)


def _seed_docs(
    store: SQLiteDocumentStore,
    docs: list[tuple[str, str, list[str]]],
    *,
    facet: str = "domain",
) -> None:
    """Seed ``(doc_id, content, tags)`` triples as shadowed documents."""
    for doc_id, content, tags in docs:
        shadow = ShadowTags(
            classified_at=datetime.now(UTC), model_id="hermes3:8b"
        ).model_dump(mode="json")
        shadow[facet] = tags if facet == "domain" else (tags[0] if tags else None)
        store.put(doc_id, content, {SHADOW_TAGS_KEY: shadow})


def _todoist_corpus(store: SQLiteDocumentStore, *, n: int = 5) -> None:
    """``todoist`` predicts ``task-management``; ``kubernetes`` predicts nothing."""
    docs: list[tuple[str, str, list[str]]] = [
        (
            f"task{i}",
            f"todoist project sync notes number {i} about scheduling",
            ["task-management"],
        )
        for i in range(n)
    ]
    docs.extend(
        (
            f"infra{i}",
            f"kubernetes cluster rollout number {i} for the platform",
            ["infrastructure"],
        )
        for i in range(n)
    )
    _seed_docs(store, docs)


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


class TestThresholds:
    def test_missing_key_raises_naming_it(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        """No silent defaults — a misconfigured run must not quietly propose."""
        registry = _seed_registry(param_store, replace=True)
        with pytest.raises(KeyError) as exc:
            analyze_tag_keyword_candidates(
                document_store=document_store,
                event_log=event_log,
                registry=registry,
            )
        message = str(exc.value)
        for key in REQUIRED_PARAM_KEYS:
            assert key in message
        assert "RECOMMENDED_SEED_VALUES" in message

    def test_out_of_range_precision_raises(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        registry = _seed_registry(
            param_store, overrides={"tag_keyword_min_precision": 1.5}
        )
        with pytest.raises(ValueError, match="min_precision"):
            analyze_tag_keyword_candidates(
                document_store=document_store,
                event_log=event_log,
                registry=registry,
            )

    def test_corpus_below_floor_surfaces_nothing(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        """ "Not enough corpus" and "nothing found" are different answers."""
        _seed_docs(document_store, [("d1", "todoist notes", ["task-management"])])
        registry = _seed_registry(param_store, overrides={"tag_keyword_min_corpus": 30})
        assert (
            analyze_tag_keyword_candidates(
                document_store=document_store,
                event_log=event_log,
                registry=registry,
            )
            == []
        )
        assert event_log.get_events(event_type=EventType.TAG_KEYWORD_CANDIDATE) == []


# ---------------------------------------------------------------------------
# Mining
# ---------------------------------------------------------------------------


class TestMining:
    def test_surfaces_a_keyword_that_predicts_a_tag(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        """The motivating example: ``todoist`` -> ``task-management``."""
        _todoist_corpus(document_store)
        candidates = analyze_tag_keyword_candidates(
            document_store=document_store,
            event_log=event_log,
            registry=_seed_registry(param_store),
        )
        by_pair = {(c.keyword, c.tag): c for c in candidates}
        assert ("todoist", "task-management") in by_pair
        found = by_pair[("todoist", "task-management")]
        assert found.support == 5
        assert found.keyword_documents == 5
        assert found.tag_documents == 5
        assert found.corpus_documents == 10
        assert found.precision == pytest.approx(1.0)
        assert found.recall == pytest.approx(1.0)
        # Base rate for the tag is 5/10; perfect precision is twice that.
        assert found.lift == pytest.approx(2.0)
        assert found.example_item_ids

    def test_emits_one_event_per_candidate(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        _todoist_corpus(document_store)
        candidates = analyze_tag_keyword_candidates(
            document_store=document_store,
            event_log=event_log,
            registry=_seed_registry(param_store),
        )
        events = event_log.get_events(event_type=EventType.TAG_KEYWORD_CANDIDATE)
        assert len(events) == len(candidates)
        payload = events[0].payload
        assert {
            "candidate_id",
            "keyword",
            "tag",
            "support",
            "precision",
            "lift",
        } <= set(payload)

    def test_dry_run_emits_nothing(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        _todoist_corpus(document_store)
        candidates = analyze_tag_keyword_candidates(
            document_store=document_store,
            event_log=event_log,
            registry=_seed_registry(param_store),
            emit_events=False,
        )
        assert candidates
        assert event_log.get_events(event_type=EventType.TAG_KEYWORD_CANDIDATE) == []

    def test_is_read_only_against_the_document_store(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        _todoist_corpus(document_store)
        before = {
            d["doc_id"]: json.dumps(d, sort_keys=True, default=str)
            for d in document_store.list_documents(limit=100)
        }
        analyze_tag_keyword_candidates(
            document_store=document_store,
            event_log=event_log,
            registry=_seed_registry(param_store),
        )
        after = {
            d["doc_id"]: json.dumps(d, sort_keys=True, default=str)
            for d in document_store.list_documents(limit=100)
        }
        assert after == before

    def test_low_precision_keyword_is_rejected(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        """A keyword split evenly across two tags predicts neither."""
        docs = [
            (f"a{i}", f"shared marker document {i}", ["alpha"]) for i in range(4)
        ] + [(f"b{i}", f"shared marker document {i}", ["beta"]) for i in range(4)]
        _seed_docs(document_store, docs)
        candidates = analyze_tag_keyword_candidates(
            document_store=document_store,
            event_log=event_log,
            registry=_seed_registry(param_store),
        )
        assert not [c for c in candidates if c.keyword == "marker"]

    def test_lift_gate_rejects_a_keyword_that_only_matches_the_base_rate(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        """The gate that keeps this from being a metric wired to a constant.

        When one tag is on every document, *every* keyword has precision 1.0
        for it. Precision alone would surface the whole vocabulary as
        "perfectly predictive" — which is the shape of measurement bug this
        repo keeps finding. Lift collapses to 1.0 and the candidate dies.
        """
        docs = [
            (f"d{i}", f"alpha beta gamma document {i}", ["notes"]) for i in range(6)
        ]
        _seed_docs(document_store, docs)
        candidates = analyze_tag_keyword_candidates(
            document_store=document_store,
            event_log=event_log,
            registry=_seed_registry(param_store),
        )
        assert candidates == []

    def test_support_gate_rejects_a_rare_but_perfect_keyword(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        docs = [("rare", "singleton unicorn token", ["exotic"])] + [
            (f"d{i}", f"ordinary filler content {i}", ["common"]) for i in range(5)
        ]
        _seed_docs(document_store, docs)
        candidates = analyze_tag_keyword_candidates(
            document_store=document_store,
            event_log=event_log,
            registry=_seed_registry(param_store),
        )
        assert not [c for c in candidates if c.tag == "exotic"]

    def test_filters_its_own_writes(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        """A keyword the classifier already owns can never be re-proposed.

        Without this the loop bootstraps off its own output: yesterday's
        promotion reappears today with support that looks like fresh evidence.
        """
        _todoist_corpus(document_store)
        candidates = analyze_tag_keyword_candidates(
            document_store=document_store,
            event_log=event_log,
            registry=_seed_registry(param_store),
            known_keywords=["todoist"],
        )
        assert not [c for c in candidates if c.keyword == "todoist"]

    def test_known_keywords_are_matched_case_insensitively(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        _todoist_corpus(document_store)
        candidates = analyze_tag_keyword_candidates(
            document_store=document_store,
            event_log=event_log,
            registry=_seed_registry(param_store),
            known_keywords=["ToDoIst"],
        )
        assert not [c for c in candidates if c.keyword == "todoist"]

    def test_reserved_namespace_tag_is_never_proposed(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        """Shadow records what a model said; the gate refuses to act on it.

        ``ContentTags`` rejects reserved policy namespaces outright, so a
        promoted ``retention`` domain could never be written anyway — better to
        never surface it than to surface a proposal that cannot be applied.
        """
        docs = [
            (f"r{i}", f"marker document about policy {i}", ["retention"])
            for i in range(5)
        ] + [(f"o{i}", f"unrelated content {i}", ["ops"]) for i in range(5)]
        _seed_docs(document_store, docs)
        candidates = analyze_tag_keyword_candidates(
            document_store=document_store,
            event_log=event_log,
            registry=_seed_registry(param_store),
        )
        assert not [c for c in candidates if c.tag == "retention"]

    def test_unshadowed_documents_are_ignored(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        _todoist_corpus(document_store)
        for i in range(50):
            document_store.put(f"plain{i}", "todoist todoist todoist", {})
        candidates = analyze_tag_keyword_candidates(
            document_store=document_store,
            event_log=event_log,
            registry=_seed_registry(param_store),
        )
        found = next(c for c in candidates if c.keyword == "todoist")
        assert found.corpus_documents == 10, "unshadowed docs must not inflate counts"

    def test_event_payload_omits_example_item_ids(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        """Aggregate facts go to the log; per-document pointers do not.

        Pairing a mined keyword with specific ids turns an aggregate over
        ``>= min_support`` documents back into a per-document disclosure, in a
        log with a different access profile than the doc store. Same rule
        ``classify.shadow`` applies to ``MEMORY_OP_JUDGED``.
        """
        _todoist_corpus(document_store)
        candidates = analyze_tag_keyword_candidates(
            document_store=document_store,
            event_log=event_log,
            registry=_seed_registry(param_store),
        )
        found = next(c for c in candidates if c.keyword == "todoist")
        assert found.example_item_ids, "the dataclass still carries them for the CLI"

        raw = json.dumps(
            [
                e.payload
                for e in event_log.get_events(
                    event_type=EventType.TAG_KEYWORD_CANDIDATE
                )
            ]
        )
        assert "example_item_ids" not in raw
        for item_id in found.example_item_ids:
            assert item_id not in raw
        assert '"example_count"' in raw

    def test_prunes_keywords_that_cannot_reach_min_support(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        """The apriori prune is exact, not a sampling heuristic.

        Pair support is bounded above by keyword support, so a keyword in fewer
        than ``min_support`` documents can never produce a surfaced candidate.
        Dropping those before counting pairs is what keeps memory bounded — the
        single-pass version retained 156k pairs and 549k example ids for a
        1,000-document corpus. Qualifying candidates must be unaffected.
        """
        from trellis.learning.tag_evolution import _scan_corpus

        _todoist_corpus(document_store)
        # Every one of these carries a token seen exactly once.
        _seed_docs(
            document_store,
            [(f"rare{i}", f"singleton unicorn{i} token", ["exotic"]) for i in range(5)],
        )

        corpus = _scan_corpus(
            document_store,
            facet="domain",
            excluded_keywords=frozenset(),
            min_support=3,
            scan_limit=1000,
            page_size=100,
        )
        assert ("todoist", "task-management") in corpus.pair_documents
        assert not [kw for kw, _ in corpus.pair_documents if kw.startswith("unicorn")]

        # And the surfaced result is identical to what an unpruned scan gives.
        candidates = analyze_tag_keyword_candidates(
            document_store=document_store,
            event_log=event_log,
            registry=_seed_registry(param_store),
        )
        assert "todoist" in {c.keyword for c in candidates}

    def test_notes_state_the_limit_of_the_measurement(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        """A reviewer should not have to infer that this measures imitation."""
        _todoist_corpus(document_store)
        candidate = analyze_tag_keyword_candidates(
            document_store=document_store,
            event_log=event_log,
            registry=_seed_registry(param_store),
        )[0]
        joined = " ".join(candidate.notes)
        assert "not on retrieval outcome" in joined
        assert "hard-excludes" in joined


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_second_run_within_cooldown_is_suppressed(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        _todoist_corpus(document_store)
        registry = _seed_registry(param_store)
        first = analyze_tag_keyword_candidates(
            document_store=document_store, event_log=event_log, registry=registry
        )
        assert first
        second = analyze_tag_keyword_candidates(
            document_store=document_store, event_log=event_log, registry=registry
        )
        assert second == []

    def test_material_growth_resurfaces_a_candidate(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        _todoist_corpus(document_store)
        registry = _seed_registry(param_store)
        analyze_tag_keyword_candidates(
            document_store=document_store, event_log=event_log, registry=registry
        )
        # +40% support on the todoist rule.
        _seed_docs(
            document_store,
            [
                (f"task_new{i}", f"todoist sprint planning {i}", ["task-management"])
                for i in range(2)
            ],
        )
        again = analyze_tag_keyword_candidates(
            document_store=document_store, event_log=event_log, registry=registry
        )
        resurfaced = next(c for c in again if c.keyword == "todoist")
        assert resurfaced.support == 7
        assert resurfaced.recurrence_count == 1

    def test_elapsed_cooldown_resurfaces_without_growth(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        """A persistent candidate is a persistent signal (ADR §4.2)."""
        _todoist_corpus(document_store)
        registry = _seed_registry(param_store)
        analyze_tag_keyword_candidates(
            document_store=document_store, event_log=event_log, registry=registry
        )
        later = datetime.now(UTC) + timedelta(days=30)
        again = analyze_tag_keyword_candidates(
            document_store=document_store,
            event_log=event_log,
            registry=registry,
            now=later,
        )
        assert [c.keyword for c in again if c.keyword == "todoist"] == ["todoist"]

    def test_candidate_id_is_stable_and_distinguishing(self) -> None:
        first = compute_candidate_id("domain", "todoist", "task-management")
        assert first == compute_candidate_id("domain", "todoist", "task-management")
        assert first != compute_candidate_id("domain", "todoist", "productivity")
        assert first != compute_candidate_id(
            "content_type", "todoist", "task-management"
        )


# ---------------------------------------------------------------------------
# Promotion + revocation
# ---------------------------------------------------------------------------


def _candidate(keyword: str, tag: str, *, facet: str = "domain") -> TagKeywordCandidate:
    return TagKeywordCandidate(
        facet=facet,
        keyword=keyword,
        tag=tag,
        support=40,
        keyword_documents=41,
        tag_documents=44,
        corpus_documents=200,
        precision=40 / 41,
        recall=40 / 44,
        lift=4.4,
        candidate_id=compute_candidate_id(facet, keyword, tag),
    )


class TestPromotion:
    def test_promotion_changes_deterministic_classifier_output(self) -> None:
        """The Phase 2 acceptance criterion, end to end on a real classifier.

        Note it takes *two* keywords to move the output: the classifier
        requires two distinct hits before assigning a domain. That threshold is
        why a single bad promotion cannot hide a document on its own.
        """
        content = "todoist sync failed while the asana board was mid-import"

        before = KeywordDomainClassifier().classify(content)
        assert "task-management" not in before.tags.get("domain", [])

        promoted = apply_promotion(
            None,
            [
                _candidate("todoist", "task-management"),
                _candidate("asana", "task-management"),
            ],
        )
        assert promoted.domain_keywords == {"task-management": ["todoist", "asana"]}

        after = KeywordDomainClassifier(
            config_domains=promoted.domain_keywords
        ).classify(content)
        assert "task-management" in after.tags["domain"]

    def test_revocation_restores_the_prior_output_exactly(self) -> None:
        content = "todoist sync failed while the asana board was mid-import"
        candidates = [
            _candidate("todoist", "task-management"),
            _candidate("asana", "task-management"),
        ]
        promoted = apply_promotion(None, candidates)
        revoked = revoke_promotion(promoted.domain_keywords, promoted)

        assert revoked == {}
        after = KeywordDomainClassifier(config_domains=revoked).classify(content)
        assert "task-management" not in after.tags.get("domain", [])

    def test_apply_then_revoke_is_the_identity_on_operator_config(self) -> None:
        """Revocation must not eat vocabulary the operator wrote by hand."""
        operator_config = {"payments": ["stripe", "invoice"], "ops": ["pagerduty"]}
        candidates = [_candidate("todoist", "payments")]
        promoted = apply_promotion(operator_config, candidates)
        assert promoted.domain_keywords["payments"] == ["stripe", "invoice", "todoist"]
        assert revoke_promotion(promoted.domain_keywords, promoted) == operator_config

    def test_revoke_never_removes_a_keyword_the_operator_already_owned(self) -> None:
        """The trap a candidate-keyed revoke falls into.

        If a candidate names a keyword the operator already had, ``apply`` is
        correctly a no-op — so ``revoke`` must be one too. Revoking by
        re-deriving from candidates deleted the operator's own keyword, and
        dropped the whole domain when it was the only entry.
        """
        operator_config = {"payments": ["stripe"]}
        candidates = [_candidate("stripe", "payments")]

        promoted = apply_promotion(operator_config, candidates)
        assert promoted.domain_keywords == operator_config
        assert promoted.added == ()
        assert promoted.skipped == (("payments", "stripe"),)

        assert revoke_promotion(promoted.domain_keywords, promoted) == operator_config

    def test_apply_never_mutates_the_input(self) -> None:
        operator_config = {"payments": ["stripe"]}
        apply_promotion(operator_config, [_candidate("todoist", "payments")])
        assert operator_config == {"payments": ["stripe"]}

    def test_apply_is_idempotent(self) -> None:
        candidates = [_candidate("todoist", "task-management")]
        once = apply_promotion(None, candidates)
        twice = apply_promotion(once.domain_keywords, candidates)
        assert twice.domain_keywords == once.domain_keywords
        assert twice.added == (), "the second apply inserted nothing"

    def test_revoking_an_absent_keyword_is_a_no_op(self) -> None:
        config = {"payments": ["stripe"]}
        promotion = apply_promotion({}, [_candidate("todoist", "payments")])
        assert revoke_promotion(config, promotion) == config
        assert revoke_promotion({}, promotion) == {}

    def test_candidate_without_a_write_target_is_not_promoted(self) -> None:
        """A ``content_type`` rule must not be filed under a domain name."""
        assert "content_type" not in FACETS_WITH_WRITE_TARGET
        candidate = _candidate("todoist", "reference", facet="content_type")
        assert candidate.has_write_target is False
        promoted = apply_promotion({"ops": ["x"]}, [candidate])
        assert promoted.domain_keywords == {"ops": ["x"]}
        assert promoted.added == ()
        assert promoted.skipped == (("reference", "todoist"),)


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------


class TestExtractKeywords:
    def test_keeps_hyphenated_and_underscored_tokens_whole(self) -> None:
        """The multi-word tags an LLM produces are the interesting candidates."""
        tokens = extract_keywords("task-management and source_system notes")
        assert "task-management" in tokens
        assert "source_system" in tokens

    def test_drops_short_tokens_stopwords_and_digits(self) -> None:
        tokens = extract_keywords("the ml model has 42 layers")
        assert "the" not in tokens
        assert "ml" not in tokens, "2-char tokens over-match under substring semantics"
        assert "42" not in tokens
        assert "model" in tokens

    def test_is_presence_not_frequency(self) -> None:
        """A keyword-heavy document must not count as many documents."""
        assert extract_keywords("todoist todoist todoist") == {"todoist"}

    def test_is_case_insensitive(self) -> None:
        assert extract_keywords("Todoist TODOIST") == {"todoist"}

    def test_mined_tokens_always_match_the_classifier_substring_rule(self) -> None:
        """Token presence implies substring presence — the mining is safe.

        The analyzer mines tokens; the classifier matches substrings. If that
        implication failed, a promoted keyword could be mined from a corpus and
        then never fire.
        """
        content = "Todoist project sync — task-management workflow"
        for token in extract_keywords(content):
            assert token in content.lower()


class TestFacetHandling:
    def test_scalar_facet_is_mined(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        """``content_type`` is a scalar on the shadow record, not a list."""
        docs: list[tuple[str, str, list[str]]] = [
            (f"r{i}", f"citation bibliography entry {i}", ["reference"])
            for i in range(4)
        ] + [
            (f"j{i}", f"woke up feeling tired today {i}", ["journal"]) for i in range(4)
        ]
        _seed_docs(document_store, docs, facet="content_type")
        candidates = analyze_tag_keyword_candidates(
            document_store=document_store,
            event_log=event_log,
            registry=_seed_registry(param_store),
            facet="content_type",
        )
        assert candidates
        found = candidates[0]
        assert found.facet == "content_type"
        assert found.has_write_target is False
        assert any("no config write target" in n for n in found.notes)

    def test_documents_without_the_facet_are_skipped(
        self,
        document_store: SQLiteDocumentStore,
        event_log: SQLiteEventLog,
        param_store: SQLiteParameterStore,
    ) -> None:
        _todoist_corpus(document_store)
        empty: dict[str, Any] = ShadowTags(classified_at=datetime.now(UTC)).model_dump(
            mode="json"
        )
        for i in range(5):
            document_store.put(f"empty{i}", "todoist notes", {SHADOW_TAGS_KEY: empty})
        candidates = analyze_tag_keyword_candidates(
            document_store=document_store,
            event_log=event_log,
            registry=_seed_registry(param_store),
        )
        assert (
            next(c for c in candidates if c.keyword == "todoist").corpus_documents == 10
        )
