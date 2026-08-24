"""Config-seeded domain keyword map (WP7 Part 1)."""

from __future__ import annotations

import pytest

from trellis.classify.classifiers.keyword import (
    KeywordDomainClassifier,
    build_domain_keyword_map,
)
from trellis.classify.factory import (
    build_ingestion_pipeline,
    effective_domain_aliases,
    effective_domain_aspects,
)


class TestBuildDomainKeywordMap:
    def test_defaults_present_without_overrides(self) -> None:
        merged = build_domain_keyword_map()
        # A built-in default survives.
        assert "data-pipeline" in merged
        assert "dbt" in merged["data-pipeline"]

    def test_config_adds_new_domain(self) -> None:
        merged = build_domain_keyword_map(
            config_domains={"payments": ["stripe", "invoice", "chargeback"]}
        )
        assert merged["payments"] == ["stripe", "invoice", "chargeback"]
        # Defaults still present alongside the new one.
        assert "infrastructure" in merged

    def test_config_overrides_builtin_on_collision(self) -> None:
        merged = build_domain_keyword_map(
            config_domains={"api": ["custom-only-keyword"]}
        )
        assert merged["api"] == ["custom-only-keyword"]

    def test_extra_domains_merge_last(self) -> None:
        """extra_domains wins over config on key collision (config < extra)."""
        merged = build_domain_keyword_map(
            config_domains={"api": ["from-config"]},
            extra_domains={"api": ["from-extra"]},
        )
        assert merged["api"] == ["from-extra"]

    def test_reserved_name_in_config_rejected(self) -> None:
        with pytest.raises(ValueError, match="reserved namespace 'sensitivity'"):
            build_domain_keyword_map(config_domains={"sensitivity": ["x"]})

    def test_reserved_namespace_prefix_in_config_rejected(self) -> None:
        with pytest.raises(ValueError, match="reserved namespace 'regulatory'"):
            build_domain_keyword_map(config_domains={"regulatory:gdpr": ["x"]})

    def test_reserved_name_in_extra_rejected(self) -> None:
        with pytest.raises(ValueError, match="reserved namespace 'lifecycle'"):
            build_domain_keyword_map(extra_domains={"lifecycle": ["x"]})


class TestKeywordClassifierConfigDomains:
    def test_config_only_domain_is_assigned(self) -> None:
        """A domain defined ONLY via config_domains gets assigned at classify."""
        classifier = KeywordDomainClassifier(
            config_domains={"payments": ["stripe", "invoice", "chargeback"]}
        )
        result = classifier.classify(
            "We reconcile each stripe invoice and resolve every chargeback."
        )
        assert "payments" in result.tags.get("domain", [])

    def test_config_then_extra_precedence_at_classify_time(self) -> None:
        classifier = KeywordDomainClassifier(
            config_domains={"widgets": ["alpha", "beta"]},
            extra_domains={"widgets": ["gamma", "delta"]},
        )
        # Content matching the extra (winning) keyword list assigns the domain.
        result = classifier.classify("The gamma and delta widgets shipped today.")
        assert "widgets" in result.tags.get("domain", [])
        # Content matching only the overridden config list does NOT.
        result2 = classifier.classify("The alpha and beta items shipped today.")
        assert "widgets" not in result2.tags.get("domain", [])

    def test_reserved_config_domain_rejected_via_constructor(self) -> None:
        with pytest.raises(ValueError, match="reserved namespace"):
            KeywordDomainClassifier(config_domains={"jurisdiction": ["x"]})


class TestBuildIngestionPipeline:
    def test_no_config_uses_defaults(self) -> None:
        pipeline = build_ingestion_pipeline()
        result = pipeline.classify("dbt spark warehouse etl pipeline")
        assert "data-pipeline" in result.tags.get("domain", [])

    def test_config_domain_assigned_in_ingestion_mode(self) -> None:
        pipeline = build_ingestion_pipeline(
            {"domain_keywords": {"payments": ["stripe", "invoice", "chargeback"]}}
        )
        assert pipeline.mode == "ingestion"
        result = pipeline.classify("stripe invoice chargeback reconciliation")
        assert "payments" in result.tags.get("domain", [])

    def test_malformed_domain_keywords_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a mapping"):
            build_ingestion_pipeline({"domain_keywords": ["not", "a", "mapping"]})

    def test_non_string_keyword_list_rejected(self) -> None:
        with pytest.raises(ValueError, match="list of keyword strings"):
            build_ingestion_pipeline({"domain_keywords": {"payments": [1, 2, 3]}})

    def test_reserved_config_domain_rejected_at_build(self) -> None:
        with pytest.raises(ValueError, match="reserved namespace"):
            build_ingestion_pipeline({"domain_keywords": {"retention": ["x"]}})


class TestEffectiveDomainAliases:
    """The ``classify.domain_aliases`` block (#321 normalization).

    Fails loudly on a malformed block for the same reason
    ``domain_keywords`` does: the map decides which documents a domain-scoped
    query can see, so silently declining to merge is worse than refusing to
    start.
    """

    def test_absent_block_merges_nothing(self) -> None:
        assert effective_domain_aliases(None) == {}
        assert effective_domain_aliases({}) == {}
        assert effective_domain_aliases({"domain_keywords": {"a": ["b"]}}) == {}

    def test_a_well_formed_block_round_trips(self) -> None:
        assert effective_domain_aliases(
            {"domain_aliases": {"budget-hunting": "hunting"}}
        ) == {"budget-hunting": "hunting"}

    def test_a_non_mapping_block_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a mapping"):
            effective_domain_aliases({"domain_aliases": ["budget-hunting"]})

    def test_a_non_string_canonical_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty canonical"):
            effective_domain_aliases({"domain_aliases": {"a": ["b"]}})

    def test_a_self_mapping_is_rejected(self) -> None:
        """A no-op that reads as intent."""
        with pytest.raises(ValueError, match="maps to itself"):
            effective_domain_aliases({"domain_aliases": {"hunting": "hunting"}})

    def test_a_chain_is_rejected(self) -> None:
        """Merging is one step; accepting a chain would promise a rewrite
        ``normalize_domain_tags`` deliberately does not perform."""
        with pytest.raises(ValueError, match="both an alias and a merge destination"):
            effective_domain_aliases({"domain_aliases": {"a": "b", "b": "c"}})


class TestEffectiveDomainAspects:
    """Activity nouns that may never be a merge destination.

    A shipped default is defensible here where an inferred one was not:
    ``planning`` describes what you are doing *to* a subject in any
    vocabulary, so the list is generic English rather than corpus-specific.
    But whether a given noun is an activity or a subject still depends on the
    corpus, so both directions are overridable.
    """

    def test_the_builtin_list_applies_with_no_config(self) -> None:
        assert "planning" in effective_domain_aspects(None)
        assert "optimization" in effective_domain_aspects({})

    def test_plausible_subjects_are_left_out_of_the_builtins(self) -> None:
        """Conservative on purpose — these are real subjects somewhere."""
        builtins = effective_domain_aspects(None)
        for plausible in ("automation", "migration", "testing", "negotiation"):
            assert plausible not in builtins

    def test_config_adds_to_the_builtins(self) -> None:
        aspects = effective_domain_aspects({"domain_aspects": ["automation"]})
        assert "automation" in aspects
        assert "planning" in aspects

    def test_config_can_reclaim_a_builtin_as_a_subject(self) -> None:
        """``documentation`` is an activity in a vault, a subject to a docs team."""
        aspects = effective_domain_aspects({"domain_subjects": ["documentation"]})
        assert "documentation" not in aspects
        assert "planning" in aspects

    def test_a_tag_declared_both_ways_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="appear in both"):
            effective_domain_aspects(
                {"domain_aspects": ["x"], "domain_subjects": ["x"]}
            )

    def test_a_non_list_block_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            effective_domain_aspects({"domain_aspects": {"planning": True}})

    def test_a_malformed_subjects_block_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            effective_domain_aspects({"domain_subjects": "documentation"})

    def test_a_non_string_entry_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            effective_domain_aspects({"domain_aspects": ["planning", 3]})
