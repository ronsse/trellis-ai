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


class TestAdvisorySuppressionLifecycle:
    """Gap 2.1 — soft suppression is reversible, filter-aware, and audited."""

    def test_new_advisory_is_active(self, tmp_path: Path) -> None:
        from trellis.schemas.advisory import AdvisoryStatus

        store = AdvisoryStore(tmp_path / "a.json")
        adv = store.put(_advisory())
        assert adv.status == AdvisoryStatus.ACTIVE
        assert adv.suppressed_at is None
        assert adv.suppression_reason is None

    def test_suppress_flips_status_and_stamps_metadata(self, tmp_path: Path) -> None:
        from trellis.schemas.advisory import AdvisoryStatus

        store = AdvisoryStore(tmp_path / "a.json")
        adv = store.put(_advisory())

        updated = store.suppress(adv.advisory_id, reason="fitness below threshold")

        assert updated is not None
        assert updated.status == AdvisoryStatus.SUPPRESSED
        assert updated.suppressed_at is not None
        assert updated.suppression_reason == "fitness below threshold"

    def test_suppress_unknown_id_returns_none(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "a.json")
        assert store.suppress("does-not-exist") is None

    def test_suppress_is_idempotent(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "a.json")
        adv = store.put(_advisory())
        first = store.suppress(adv.advisory_id, reason="r1")
        second = store.suppress(adv.advisory_id, reason="r2")

        # Idempotent: second call returns the existing record unchanged.
        assert first is not None
        assert second is not None
        assert second.suppressed_at == first.suppressed_at
        assert second.suppression_reason == "r1"

    def test_list_excludes_suppressed_by_default(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "a.json")
        keep = store.put(_advisory(scope="keep"))
        drop = store.put(_advisory(scope="drop"))
        store.suppress(drop.advisory_id)

        visible = store.list()
        assert [a.advisory_id for a in visible] == [keep.advisory_id]

    def test_list_include_suppressed_shows_all(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "a.json")
        a = store.put(_advisory(scope="s1"))
        b = store.put(_advisory(scope="s2"))
        store.suppress(b.advisory_id)

        all_advisories = store.list(include_suppressed=True)
        ids = {adv.advisory_id for adv in all_advisories}
        assert ids == {a.advisory_id, b.advisory_id}

    def test_get_returns_suppressed_advisory(self, tmp_path: Path) -> None:
        from trellis.schemas.advisory import AdvisoryStatus

        store = AdvisoryStore(tmp_path / "a.json")
        adv = store.put(_advisory())
        store.suppress(adv.advisory_id)

        # get() is status-agnostic so the fitness loop can still evaluate
        # suppressed advisories and so the UI can show suppression history.
        retrieved = store.get(adv.advisory_id)
        assert retrieved is not None
        assert retrieved.status == AdvisoryStatus.SUPPRESSED

    def test_restore_flips_status_and_clears_metadata(self, tmp_path: Path) -> None:
        from trellis.schemas.advisory import AdvisoryStatus

        store = AdvisoryStore(tmp_path / "a.json")
        adv = store.put(_advisory())
        store.suppress(adv.advisory_id, reason="low lift")

        restored = store.restore(adv.advisory_id)
        assert restored is not None
        assert restored.status == AdvisoryStatus.ACTIVE
        assert restored.suppressed_at is None
        assert restored.suppression_reason is None

        # And it's visible to default list() again.
        assert restored.advisory_id in [a.advisory_id for a in store.list()]

    def test_restore_is_idempotent(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "a.json")
        adv = store.put(_advisory())
        # Restoring an already-active advisory is a no-op.
        assert store.restore(adv.advisory_id) is not None
        # Round-trip suppress→restore→restore.
        store.suppress(adv.advisory_id)
        store.restore(adv.advisory_id)
        assert store.restore(adv.advisory_id) is not None

    def test_restore_unknown_id_returns_none(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "a.json")
        assert store.restore("does-not-exist") is None

    def test_suppression_survives_reload(self, tmp_path: Path) -> None:
        from trellis.schemas.advisory import AdvisoryStatus

        path = tmp_path / "a.json"
        store1 = AdvisoryStore(path)
        adv = store1.put(_advisory(scope="persist-suppressed"))
        store1.suppress(adv.advisory_id, reason="persisted reason")

        store2 = AdvisoryStore(path)
        reloaded = store2.get(adv.advisory_id)
        assert reloaded is not None
        assert reloaded.status == AdvisoryStatus.SUPPRESSED
        assert reloaded.suppression_reason == "persisted reason"

    def test_remove_still_hard_deletes(self, tmp_path: Path) -> None:
        """remove() is preserved for admin cleanup — distinct from suppress()."""
        store = AdvisoryStore(tmp_path / "a.json")
        adv = store.put(_advisory())
        assert store.remove(adv.advisory_id) is True
        assert store.get(adv.advisory_id) is None
