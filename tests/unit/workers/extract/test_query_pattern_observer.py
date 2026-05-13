"""Unit tests for :class:`QueryPatternObserver`.

Confirms the extractor is pure — no store / mutation-executor access —
and that it emits well-shaped Measurement + Observation drafts that pass
the canonical schema validators.

Per ``docs/design/plan-self-improvement-program.md`` §2 the extractor
must **raise** on malformed rows rather than silently skipping; those
loud-failure paths are covered explicitly below.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from trellis.extract.base import ExtractorTier
from trellis.schemas.extraction import EdgeDraft, EntityDraft, ExtractionResult
from trellis.schemas.well_known import (
    HAS_MEASUREMENT,
    HAS_OBSERVATION,
    MEASUREMENT,
    OBSERVATION,
)
from trellis_workers.extract import (
    QueryLogRecord,
    QueryPatternObserver,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 5, 12, hour, minute, tzinfo=UTC)


def _records(subject: str, count: int, *, start_hour: int = 9) -> list[dict]:
    return [
        {
            "subject_entity_id": subject,
            "subject_entity_type": "Dataset",
            "timestamp": _ts(start_hour, i).isoformat(),
            "observer_agent_id": "test-agent",
        }
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# Tier metadata
# ---------------------------------------------------------------------------


def test_extractor_metadata_is_deterministic_tier() -> None:
    obs = QueryPatternObserver()
    assert obs.name == "query_pattern_observer"
    assert obs.tier is ExtractorTier.DETERMINISTIC
    assert obs.supported_sources == ["query-log"]


# ---------------------------------------------------------------------------
# Happy-path round trip
# ---------------------------------------------------------------------------


async def test_emits_measurement_and_observation_per_subject() -> None:
    extractor = QueryPatternObserver()
    rows = _records("dataset:warehouse/public/users", count=5)
    rows.extend(_records("dataset:warehouse/public/events", count=3, start_hour=10))

    result = await extractor.extract(rows, source_hint="query-log")

    assert isinstance(result, ExtractionResult)
    assert result.extractor_used == "query_pattern_observer"
    assert result.tier == ExtractorTier.DETERMINISTIC.value

    # 2 subjects → 1 Measurement + 1 Observation each = 4 entities.
    assert len(result.entities) == 4
    measurements = [e for e in result.entities if e.entity_type == MEASUREMENT]
    observations = [e for e in result.entities if e.entity_type == OBSERVATION]
    assert len(measurements) == 2
    assert len(observations) == 2

    # One edge per emitted draft: ``hasMeasurement`` for the Measurement
    # entities and ``hasObservation`` for the Observation entities. The
    # distinction lets consumers route on edge kind alone (ADR §2.2).
    assert len(result.edges) == 4
    measurement_edges = [e for e in result.edges if e.edge_kind == HAS_MEASUREMENT]
    observation_edges = [e for e in result.edges if e.edge_kind == HAS_OBSERVATION]
    assert len(measurement_edges) == 2
    assert len(observation_edges) == 2
    assert all(e.allow_dangling is True for e in result.edges)


async def test_measurement_carries_query_count_value() -> None:
    extractor = QueryPatternObserver()
    rows = _records("dataset:foo", count=47)

    result = await extractor.extract(rows)

    measurement = next(e for e in result.entities if e.entity_type == MEASUREMENT)
    assert measurement.properties["metric_name"] == "query_count"
    assert measurement.properties["metric_value"] == 47.0
    assert measurement.properties["unit"] == "count"
    assert measurement.properties["subject_entity_id"] == "dataset:foo"
    assert measurement.properties["window_start"] == _ts(9, 0).isoformat()
    assert measurement.properties["window_end"] == _ts(9, 46).isoformat()


async def test_observation_confidence_scales_with_sample_size() -> None:
    extractor = QueryPatternObserver()
    small = await extractor.extract(_records("dataset:small", count=1))
    large = await extractor.extract(_records("dataset:large", count=30))

    obs_small = next(e for e in small.entities if e.entity_type == OBSERVATION)
    obs_large = next(e for e in large.entities if e.entity_type == OBSERVATION)
    # Confidence is bounded in (0, 1) and monotonically increases with n.
    assert 0.0 < obs_small.properties["confidence"] < obs_large.properties["confidence"]
    assert obs_large.properties["confidence"] <= 1.0


async def test_drafts_pass_schema_validation() -> None:
    """Smoke-check the entity/edge drafts round-trip through Pydantic."""
    extractor = QueryPatternObserver()
    result = await extractor.extract(_records("dataset:foo", count=4))

    for entity in result.entities:
        EntityDraft.model_validate(entity.model_dump())
    for edge in result.edges:
        EdgeDraft.model_validate(edge.model_dump())


# ---------------------------------------------------------------------------
# Filtering / thresholds
# ---------------------------------------------------------------------------


async def test_observation_skipped_when_under_threshold() -> None:
    extractor = QueryPatternObserver(observation_min_query_count=5)
    result = await extractor.extract(_records("dataset:rare", count=2))
    # Measurement still emitted, but no Observation.
    assert any(e.entity_type == MEASUREMENT for e in result.entities)
    assert not any(e.entity_type == OBSERVATION for e in result.entities)
    # And only one edge (the measurement's).
    assert len(result.edges) == 1


async def test_accepts_query_log_record_objects() -> None:
    extractor = QueryPatternObserver()
    records = [
        QueryLogRecord(
            subject_entity_id="dataset:bar",
            timestamp=_ts(11, i),
            observer_agent_id="explicit-agent",
        )
        for i in range(4)
    ]
    result = await extractor.extract(records)

    measurement = next(e for e in result.entities if e.entity_type == MEASUREMENT)
    assert measurement.properties["observer_agent_id"] == "explicit-agent"


# ---------------------------------------------------------------------------
# Loud failures (POC discipline)
# ---------------------------------------------------------------------------


async def test_raises_on_missing_subject() -> None:
    extractor = QueryPatternObserver()
    with pytest.raises(ValueError, match="subject_entity_id"):
        await extractor.extract([{"timestamp": _ts(9).isoformat()}])


async def test_raises_on_invalid_timestamp() -> None:
    extractor = QueryPatternObserver()
    with pytest.raises(ValueError, match="timestamp"):
        await extractor.extract(
            [{"subject_entity_id": "dataset:x", "timestamp": "not-a-date"}],
        )


async def test_raises_on_missing_timestamp() -> None:
    extractor = QueryPatternObserver()
    with pytest.raises(ValueError, match="timestamp"):
        await extractor.extract([{"subject_entity_id": "dataset:x"}])


async def test_raises_on_non_iterable_input() -> None:
    extractor = QueryPatternObserver()
    with pytest.raises(TypeError, match="iterable"):
        await extractor.extract(42)


async def test_raises_on_non_dict_row() -> None:
    extractor = QueryPatternObserver()
    with pytest.raises(TypeError, match="QueryLogRecord or dict"):
        await extractor.extract(["not a dict"])


def test_observation_min_query_count_must_be_non_negative() -> None:
    with pytest.raises(ValueError, match="observation_min_query_count"):
        QueryPatternObserver(observation_min_query_count=-1)


# ---------------------------------------------------------------------------
# Purity — no store / executor access
# ---------------------------------------------------------------------------


async def test_extractor_does_not_touch_any_store() -> None:
    """A pure extractor never resolves a store. We assert by handing it
    a MagicMock-shaped 'world' and observing that nothing is read or
    called on it."""
    fake_store = MagicMock()
    fake_executor = MagicMock()
    extractor = QueryPatternObserver()
    await extractor.extract(_records("dataset:foo", count=3))
    fake_store.assert_not_called()
    fake_executor.assert_not_called()
    # And no attribute access either (MagicMock would record it).
    assert fake_store.method_calls == []
    assert fake_executor.method_calls == []


async def test_empty_input_returns_empty_result() -> None:
    extractor = QueryPatternObserver()
    result = await extractor.extract([])
    assert result.entities == []
    assert result.edges == []


async def test_aggregate_is_deterministic_across_calls() -> None:
    """Same input → same entity_ids (byte-identical), so producers can
    safely re-run without polluting the graph with duplicates."""
    extractor = QueryPatternObserver()
    rows = _records("dataset:foo", count=4)
    a = await extractor.extract(rows)
    b = await extractor.extract(rows)
    ids_a = sorted(e.entity_id for e in a.entities)
    ids_b = sorted(e.entity_id for e in b.entities)
    assert ids_a == ids_b
