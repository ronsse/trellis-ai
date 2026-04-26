"""Unit tests for the synthetic-traces scenario.

Exercises the full ingest → populate → score pipeline against an
in-memory SQLite registry. No live backends.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from eval.generators.trace_generator import (
    DOMAIN_TEMPLATES,
    GeneratedCorpus,
    generate_corpus,
)
from eval.scenarios.synthetic_traces.scenario import (
    DEFAULT_PROFILE_NAME,
    WEIGHTED_REGRESS_THRESHOLD,
    run,
)

from trellis.stores.registry import StoreRegistry


@pytest.fixture
def sqlite_registry(tmp_path: Path):
    config = {
        "knowledge": {
            "graph": {"backend": "sqlite"},
            "vector": {"backend": "sqlite"},
            "document": {"backend": "sqlite"},
            "blob": {"backend": "local"},
        },
        "operational": {
            "trace": {"backend": "sqlite"},
            "event_log": {"backend": "sqlite"},
        },
    }
    with StoreRegistry(config=config, stores_dir=tmp_path) as registry:
        yield registry


def test_corpus_is_deterministic() -> None:
    a = generate_corpus(seed=7, traces_per_domain=4, entities_per_trace=2)
    b = generate_corpus(seed=7, traces_per_domain=4, entities_per_trace=2)

    a_pairs = [(g.domain, g.trace.intent, tuple(g.entities)) for g in a.traces]
    b_pairs = [(g.domain, g.trace.intent, tuple(g.entities)) for g in b.traces]
    assert a_pairs == b_pairs


def test_corpus_covers_all_three_domains() -> None:
    corpus = generate_corpus(seed=0, traces_per_domain=2)
    domains = {g.domain for g in corpus.traces}
    assert domains == {t.name for t in DOMAIN_TEMPLATES}
    assert len(corpus.queries) == len(DOMAIN_TEMPLATES)


def test_corpus_all_entities_returns_sorted_unique() -> None:
    corpus = generate_corpus(seed=0, traces_per_domain=4)
    entities = corpus.all_entities
    assert entities == sorted(entities)
    assert len(entities) == len(set(entities))


def test_run_against_sqlite_produces_metrics(sqlite_registry: StoreRegistry) -> None:
    report = run(sqlite_registry, seed=0, traces_per_domain=4, entities_per_trace=3)

    assert report.name == "synthetic_traces"
    assert report.status in {"pass", "regress"}

    # Always-present metrics regardless of scoring outcome.
    assert report.metrics["trace_count"] == 12.0  # 4 per domain x 3 domains
    assert report.metrics["query_count"] == 3.0
    assert report.metrics["traces_ingested"] == 12.0
    assert report.metrics["entities_upserted"] >= 1.0
    assert "aggregate.weighted_score_mean" in report.metrics


def test_run_emits_per_domain_metrics(sqlite_registry: StoreRegistry) -> None:
    report = run(sqlite_registry, seed=0, traces_per_domain=2, entities_per_trace=3)

    for domain in (t.name for t in DOMAIN_TEMPLATES):
        assert f"{domain}.weighted_score" in report.metrics


def test_default_profile_exists() -> None:
    """Catch a rename in BUILTIN_PROFILES."""
    from trellis.retrieve.evaluate import BUILTIN_PROFILES

    assert DEFAULT_PROFILE_NAME in BUILTIN_PROFILES


def test_regress_threshold_is_reasonable() -> None:
    assert 0.0 <= WEIGHTED_REGRESS_THRESHOLD <= 1.0


def test_corpus_traces_have_outcome_and_steps() -> None:
    """Sanity-check that synthetic traces are well-formed."""
    corpus: GeneratedCorpus = generate_corpus(seed=0, traces_per_domain=2)
    for gt in corpus.traces:
        assert gt.trace.outcome is not None
        assert len(gt.trace.steps) >= 1
        assert gt.trace.context.domain == gt.domain
