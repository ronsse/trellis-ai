"""Readers of ``PACK_ASSEMBLED`` analyse the served arm only.

A withheld pack's row carries an empty ``injected_items`` (its would-be
items sit under ``holdout_items``), so no reader can count one of them
as served. The readers that aggregate over packs and the feedback that
names them go further and drop holdout packs, with that feedback,
before aggregating: an empty pack the agent received because of a coin
flip is not evidence about retrieval, and counting it would put every
holdout pack in the "without" arm of each advisory and dilute every
pack-level rate. With the flag off no row is a holdout row, so every
output is unchanged.

Each case below appends real holdout packs built through
:class:`~trellis.retrieve.pack_builder.PackBuilder`, plus feedback that
names their would-be items, and requires every reader's output to stay
exactly what it was — then appends the same packs served, and requires
every output to move, so the equality is not vacuous.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import record_feedback
from trellis.learning.pack_observations import (
    build_learning_observations_from_event_log,
    join_pack_events_with_coverage,
)
from trellis.ops.write_health import summarize_serve_attribution
from trellis.retrieve.advisory_generator import AdvisoryGenerator
from trellis.retrieve.effectiveness import (
    analyze_advisory_effectiveness,
    analyze_effectiveness,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.pack_sections import analyze_pack_sections
from trellis.retrieve.strategies import SearchStrategy
from trellis.retrieve.telemetry import analyze_pack_telemetry
from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence
from trellis.schemas.pack import PackBudget, PackItem, SectionRequest
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.sqlite.event_log import SQLiteEventLog

_ITEM_SPECS = [
    ("doc-alpha", 0.91, "reference", "Rotate the signing key before the audit window."),
    ("doc-bravo", 0.74, "domain_knowledge", "The ingest worker retries with backoff."),
    ("doc-charlie", 0.52, "reference", "Schema migrations run one table at a time."),
    ("doc-delta", 0.33, "domain_knowledge", "Cache entries expire after idle minutes."),
]

_SECTIONS = [
    SectionRequest(name="reference", retrieval_affinities=["reference"], max_items=2),
    SectionRequest(
        name="domain", retrieval_affinities=["domain_knowledge"], max_items=1
    ),
]


def _strategy() -> SearchStrategy:
    strategy = MagicMock(spec=SearchStrategy)
    strategy.name = "keyword"
    strategy.search.return_value = [
        PackItem(
            item_id=item_id,
            item_type="document",
            excerpt=body,
            relevance_score=score,
            metadata={"content_tags": {"retrieval_affinity": [affinity]}},
        )
        for item_id, score, affinity, body in _ITEM_SPECS
    ]
    return strategy


@pytest.fixture
def event_log(tmp_path: Path) -> Iterator[SQLiteEventLog]:
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


@pytest.fixture
def advisory_store(tmp_path: Path) -> AdvisoryStore:
    store = AdvisoryStore(tmp_path / "adv.json")
    for advisory_id, confidence, category in [
        ("adv-entity", 0.82, AdvisoryCategory.ENTITY),
        ("adv-approach", 0.61, AdvisoryCategory.APPROACH),
    ]:
        store.put(
            Advisory(
                advisory_id=advisory_id,
                category=category,
                confidence=confidence,
                message=f"Synthetic advisory {advisory_id}",
                evidence=AdvisoryEvidence(
                    sample_size=9,
                    success_rate_with=0.7,
                    success_rate_without=0.4,
                    effect_size=0.3,
                ),
                scope="global",
            )
        )
    return store


class _Corpus:
    """Builds packs through the real builder and grades them."""

    def __init__(
        self, event_log: SQLiteEventLog, advisory_store: AdvisoryStore, tmp_path: Path
    ) -> None:
        self.event_log = event_log
        self.advisory_store = advisory_store
        self.feedback_dir = tmp_path / "feedback"
        self._runs = 0

    def _builder(self, rate: float) -> PackBuilder:
        return PackBuilder(
            strategies=[_strategy()],
            event_log=self.event_log,
            advisory_store=self.advisory_store,
            holdout_rate=rate,
        )

    def _grade(
        self,
        pack_id: str,
        rating: float,
        helpful: Sequence[str],
        unhelpful: Sequence[str],
    ) -> None:
        self._runs += 1
        feedback = PackFeedback.from_agent_signal(
            run_id=f"run-{self._runs}",
            rating=rating,
            helpful_item_ids=helpful,
            unhelpful_item_ids=unhelpful,
            pack_id=pack_id,
        )
        record_feedback(
            feedback,
            log_dir=self.feedback_dir,
            event_log=self.event_log,
            pack_id=pack_id,
        )

    def flat(
        self,
        rate: float,
        *,
        max_items: int,
        rating: float,
        helpful: Sequence[str],
        unhelpful: Sequence[str] = (),
    ) -> str:
        pack = self._builder(rate).build(
            "rotate keys", budget=PackBudget(max_items=max_items, max_tokens=4000)
        )
        self._grade(pack.pack_id, rating, helpful, unhelpful)
        return pack.pack_id

    def sectioned(self, rate: float, *, rating: float, helpful: Sequence[str]) -> str:
        pack = self._builder(rate).build_sectioned("rotate keys", sections=_SECTIONS)
        self._grade(pack.pack_id, rating, helpful, [])
        return pack.pack_id

    def seed(self) -> None:
        """Served history, varied in budget, rating and citations."""
        self.flat(
            0.0,
            max_items=3,
            rating=0.9,
            helpful=["doc-alpha"],
            unhelpful=["doc-charlie"],
        )
        self.flat(0.0, max_items=2, rating=0.2, helpful=[], unhelpful=["doc-bravo"])
        self.flat(0.0, max_items=3, rating=0.7, helpful=["doc-bravo", "doc-charlie"])
        self.sectioned(0.0, rating=0.8, helpful=["doc-bravo"])

    def append(self, rate: float) -> list[str]:
        """Two packs graded as if their would-be items had been seen."""
        return [
            self.flat(
                rate,
                max_items=3,
                rating=0.95,
                helpful=["doc-alpha", "doc-bravo"],
                unhelpful=["doc-charlie"],
            ),
            self.sectioned(rate, rating=0.1, helpful=["doc-charlie"]),
        ]


def _readers(corpus: _Corpus, tmp_path: Path, tag: str) -> dict[str, Any]:
    """Every pack-level reader's output, minus the raw-read coverage."""
    generated_into = AdvisoryStore(tmp_path / f"generated-{tag}.json")
    generator = AdvisoryGenerator(
        corpus.event_log, generated_into, min_sample_size=2, min_effect_size=0.0
    ).generate(days=30)
    return {
        "observations": build_learning_observations_from_event_log(
            corpus.event_log, days=30
        ),
        "effectiveness": analyze_effectiveness(
            corpus.event_log, days=30, min_appearances=1
        ).model_dump(),
        "advisory_effectiveness": analyze_advisory_effectiveness(
            corpus.event_log, corpus.advisory_store, days=30, min_presentations=1
        ).model_dump(),
        "generator": generator.model_dump(exclude={"coverage"}),
        "generated": sorted(
            (
                a.model_dump(mode="json", exclude={"created_at", "updated_at"})
                for a in generated_into.list(include_suppressed=True)
            ),
            key=lambda a: a["advisory_id"],
        ),
        "telemetry": analyze_pack_telemetry(corpus.event_log, days=7).model_dump(
            exclude={"scan"}
        ),
        "sections": analyze_pack_sections(corpus.event_log, days=30).model_dump(
            exclude={"scan"}
        ),
        "serve_attribution": summarize_serve_attribution(
            corpus.event_log, days=7
        ).model_dump(exclude={"scan"}),
    }


@pytest.fixture
def corpus(
    event_log: SQLiteEventLog, advisory_store: AdvisoryStore, tmp_path: Path
) -> _Corpus:
    return _Corpus(event_log, advisory_store, tmp_path)


class TestNoServeIsCounted:
    def test_the_promotion_and_effectiveness_joins_see_no_serve(
        self, corpus: _Corpus
    ) -> None:
        corpus.flat(
            1.0,
            max_items=3,
            rating=0.95,
            helpful=["doc-alpha", "doc-bravo"],
            unhelpful=["doc-charlie"],
        )
        # The shared join drops the feedback too, not only the pack it names.
        since = datetime.now(tz=UTC) - timedelta(days=1)
        feedback, packs, pack_count, _ = join_pack_events_with_coverage(
            corpus.event_log, since=since, limit=100
        )
        assert (feedback, packs, pack_count) == ([], {}, 0)
        assert build_learning_observations_from_event_log(corpus.event_log) == []
        held = analyze_effectiveness(corpus.event_log, min_appearances=1)
        assert (held.total_packs, held.total_feedback, held.item_scores) == (0, 0, [])

        # The same pack, served and graded the same way, is counted.
        corpus.flat(
            0.0,
            max_items=3,
            rating=0.95,
            helpful=["doc-alpha", "doc-bravo"],
            unhelpful=["doc-charlie"],
        )
        observations = build_learning_observations_from_event_log(corpus.event_log)
        assert [[i["item_id"] for i in o["items"]] for o in observations] == [
            ["doc-alpha", "doc-bravo", "doc-charlie"]
        ]
        served = analyze_effectiveness(corpus.event_log, min_appearances=1)
        assert served.total_packs == 1
        assert {r["item_id"]: r["appearances"] for r in served.item_scores} == {
            "doc-alpha": 1,
            "doc-bravo": 1,
            "doc-charlie": 1,
        }


class TestEveryReaderIgnoresTheHoldoutArm:
    def test_holdout_packs_and_their_feedback_change_no_output(
        self, corpus: _Corpus, tmp_path: Path
    ) -> None:
        corpus.seed()
        before = _readers(corpus, tmp_path, "before")
        corpus.append(1.0)
        after_holdout = _readers(corpus, tmp_path, "holdout")
        corpus.append(0.0)
        after_served = _readers(corpus, tmp_path, "served")

        # The seeded history is visible to every reader ...
        assert len(before["observations"]) == 4
        assert before["effectiveness"]["total_packs"] == 4
        assert before["advisory_effectiveness"]["total_feedback"] == 4
        assert before["generator"]["total_packs"] == 4
        assert before["telemetry"]["total_packs"] == 4
        assert before["sections"]["total_sectioned_packs"] == 1
        assert before["serve_attribution"]["packs"] == 4

        # ... the holdout arm moves none of them ...
        for reader, output in before.items():
            assert after_holdout[reader] == output, reader

        # ... and the same packs served move every one.
        for reader, output in after_holdout.items():
            if reader == "generated":
                continue  # what is mined depends on thresholds, not only counts
            assert after_served[reader] != output, reader
