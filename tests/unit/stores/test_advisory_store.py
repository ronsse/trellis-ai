"""Tests for AdvisoryStore."""

from __future__ import annotations

from pathlib import Path

from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence
from trellis.stores.advisory_store import AdvisoryStore


def _evidence(**overrides: object) -> AdvisoryEvidence:
    defaults = {
        "sample_size": 10,
        "success_rate_with": 0.8,
        "success_rate_without": 0.4,
        "effect_size": 0.4,
    }
    return AdvisoryEvidence(**{**defaults, **overrides})  # type: ignore[arg-type]


def _advisory(
    *,
    category: AdvisoryCategory = AdvisoryCategory.ENTITY,
    confidence: float = 0.7,
    scope: str = "global",
    **kwargs: object,
) -> Advisory:
    return Advisory(
        category=category,
        confidence=confidence,
        message=f"Test advisory ({category.value})",
        evidence=_evidence(),
        scope=scope,
        **kwargs,  # type: ignore[arg-type]
    )


class TestAdvisoryStore:
    def test_empty_store(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "advisories.json")
        assert store.list() == []

    def test_put_and_get(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "advisories.json")
        adv = _advisory()
        store.put(adv)
        assert store.get(adv.advisory_id) is not None
        assert store.get(adv.advisory_id).confidence == 0.7  # type: ignore[union-attr]

    def test_put_many(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "advisories.json")
        advs = [_advisory(confidence=0.5 + i * 0.1) for i in range(3)]
        count = store.put_many(advs)
        assert count == 3
        assert len(store.list()) == 3

    def test_remove(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "advisories.json")
        adv = _advisory()
        store.put(adv)
        assert store.remove(adv.advisory_id) is True
        assert store.get(adv.advisory_id) is None

    def test_remove_nonexistent(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "advisories.json")
        assert store.remove("nonexistent") is False

    def test_clear(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "advisories.json")
        store.put_many([_advisory() for _ in range(3)])
        cleared = store.clear()
        assert cleared == 3
        assert store.list() == []

    def test_filter_by_scope(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "advisories.json")
        store.put(_advisory(scope="platform"))
        store.put(_advisory(scope="data"))
        store.put(_advisory(scope="platform"))

        platform = store.list(scope="platform")
        assert len(platform) == 2
        data = store.list(scope="data")
        assert len(data) == 1

    def test_filter_by_min_confidence(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "advisories.json")
        store.put(_advisory(confidence=0.3))
        store.put(_advisory(confidence=0.6))
        store.put(_advisory(confidence=0.9))

        high = store.list(min_confidence=0.5)
        assert len(high) == 2
        assert all(a.confidence >= 0.5 for a in high)

    def test_list_ordered_by_confidence_desc(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "advisories.json")
        store.put(_advisory(confidence=0.3))
        store.put(_advisory(confidence=0.9))
        store.put(_advisory(confidence=0.6))

        result = store.list()
        confidences = [a.confidence for a in result]
        assert confidences == sorted(confidences, reverse=True)

    def test_persistence_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "advisories.json"
        store1 = AdvisoryStore(path)
        adv = _advisory(scope="test-persist")
        store1.put(adv)

        # Create a new store from the same file
        store2 = AdvisoryStore(path)
        loaded = store2.get(adv.advisory_id)
        assert loaded is not None
        assert loaded.scope == "test-persist"
        assert loaded.confidence == adv.confidence

    def test_put_replaces_existing(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "advisories.json")
        adv = _advisory(confidence=0.5)
        store.put(adv)
        updated = adv.model_copy(update={"confidence": 0.9})
        store.put(updated)
        assert len(store.list()) == 1
        assert store.get(adv.advisory_id).confidence == 0.9  # type: ignore[union-attr]
