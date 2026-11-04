"""Fixtures for advisory-sourced candidates: stored ANTI_PATTERN advisories.

``approach`` builds an APPROACH advisory, which the mapper skips.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tests.precedent_candidates import record_candidate
from trellis.learning.advisory_candidates import map_advisories
from trellis.mutate.commands import CommandStatus
from trellis.schemas.advisory import (
    RETIRED_BY_KEY,
    RETIRED_BY_QUERY_FILTER,
    Advisory,
    AdvisoryCategory,
    AdvisoryEvidence,
    AdvisoryStatus,
)
from trellis.stores.advisory_source import resolve_advisory_path
from trellis.stores.advisory_store import AdvisoryStore

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from trellis.schemas.precedent_record import PrecedentRecord
    from trellis.stores.registry import StoreRegistry

#: A token no other fixture, store row or pack contains.
ADVISORY_SENTINEL = "zqxv-advisory-sentinel"


def make_advisory(
    category: AdvisoryCategory,
    *,
    advisory_id: str = "adv-1",
    entity_id: str | None = None,
    strategy: str | None = None,
    scope: str = "global",
    sample_size: int = 8,
    rate_with: float = 0.25,
    rate_without: float = 0.8,
    effect: float | None = None,
    packs: Sequence[str] = ("pack-1", "pack-2"),
    confidence: float = 0.7,
    evidence_confidence: float | None = 0.6,
    status: AdvisoryStatus = AdvisoryStatus.ACTIVE,
    metadata: dict[str, Any] | None = None,
    message: str = "an advisory message",
) -> Advisory:
    """One stored advisory, defaults shaped like the generator's own."""
    meta = dict(metadata or {})
    if strategy is not None:
        meta["strategy"] = strategy
    return Advisory(
        advisory_id=advisory_id,
        category=category,
        confidence=confidence,
        message=message,
        evidence=AdvisoryEvidence(
            sample_size=sample_size,
            success_rate_with=rate_with,
            success_rate_without=rate_without,
            effect_size=(rate_with - rate_without) if effect is None else effect,
            evidence_confidence=evidence_confidence,
            representative_trace_ids=list(packs),
        ),
        scope=scope,
        entity_id=entity_id,
        metadata=meta,
        status=status,
    )


def anti_pattern(item_id: str = "doc-bad", **kwargs: Any) -> Advisory:
    kwargs.setdefault("advisory_id", f"adv-anti-{item_id}")
    return make_advisory(AdvisoryCategory.ANTI_PATTERN, entity_id=item_id, **kwargs)


def approach(strategy: str = "graph", **kwargs: Any) -> Advisory:
    kwargs.setdefault("advisory_id", f"adv-approach-{strategy}")
    kwargs.setdefault("rate_with", 0.9)
    kwargs.setdefault("rate_without", 0.4)
    return make_advisory(AdvisoryCategory.APPROACH, strategy=strategy, **kwargs)


def retired(advisory: Advisory) -> Advisory:
    """``advisory`` suppressed by a generator rule, the way retirement stores it."""
    return advisory.model_copy(
        update={
            "status": AdvisoryStatus.SUPPRESSED,
            "metadata": {**advisory.metadata, RETIRED_BY_KEY: RETIRED_BY_QUERY_FILTER},
        }
    )


def store_advisories(stores_dir: Path, advisories: Sequence[Advisory]) -> Path:
    """Write ``advisories`` to the deployment's advisory file; return its path."""
    path = resolve_advisory_path(stores_dir)
    AdvisoryStore(path).put_many(list(advisories))
    return path


def record_advisory_candidates(
    registry: StoreRegistry, advisories: Sequence[Advisory]
) -> list[PrecedentRecord]:
    """Map ``advisories`` and record each candidate through the governed executor."""
    records = map_advisories(advisories).records
    assert records, "fixture advisories must map to candidates"
    for record in records:
        assert record_candidate(registry, record).status == CommandStatus.SUCCESS
    return records
