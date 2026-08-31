"""Tests for AdvisoryStore."""

from __future__ import annotations

import json
import os
import shlex
import stat
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from trellis.errors import DegradedStoreWriteError, StaleStoreWriteError
from trellis.schemas.advisory import (
    Advisory,
    AdvisoryCategory,
    AdvisoryEvidence,
    AdvisoryStatus,
)
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


class TestCorruptFileIsPreservedNotOverwritten:
    """#393 — a file this store could not read is not a file it may replace.

    ``_save`` serialises ``self._advisories.values()`` over the whole file.
    Degrading an unreadable load to an empty set therefore armed the *next*
    write to delete it, and since stable ids (#394) to silently un-suppress
    everything with it. The read stays lenient — that is #382's call and it
    is right — and the write refuses.

    Every assertion here fails against the pre-#393 store: it degraded to
    an empty dict with no record, so ``is_degraded`` did not exist, ``put``
    returned happily, and the file on disk was gone.
    """

    def test_absent_file_is_not_degradation(self, tmp_path: Path) -> None:
        """ "No file" and "unreadable file" are different states.

        A greenfield deployment must keep writing normally; conflating the
        two would refuse every write on every fresh install.
        """
        store = AdvisoryStore(tmp_path / "never-written.json")
        assert store.is_degraded is False
        assert store.degradation is None
        store.put(_advisory())  # must not raise
        assert len(store.list()) == 1

    def test_clean_file_is_not_degradation(self, tmp_path: Path) -> None:
        path = tmp_path / "a.json"
        AdvisoryStore(path).put(_advisory())
        reloaded = AdvisoryStore(path)
        assert reloaded.is_degraded is False
        assert reloaded.degradation is None

    def test_malformed_json_leaves_the_file_untouched(self, tmp_path: Path) -> None:
        """The load-bearing acceptance test: the bytes survive the next write."""
        path = tmp_path / "a.json"
        AdvisoryStore(path).put_many([_advisory() for _ in range(3)])
        original = path.read_text(encoding="utf-8")
        # The shape a killed or disk-full write leaves behind.
        path.write_text(original[: len(original) // 2], encoding="utf-8")
        corrupted = path.read_text(encoding="utf-8")

        store = AdvisoryStore(path)
        assert store.is_degraded is True
        assert store.degradation is not None
        assert store.degradation.reason == "malformed_json"

        with pytest.raises(DegradedStoreWriteError):
            store.put(_advisory())

        assert path.read_text(encoding="utf-8") == corrupted

    @pytest.mark.parametrize(
        ("content", "reason"),
        [
            ("not json at all", "malformed_json"),
            ("", "malformed_json"),
            ('["a", "list"]', "malformed_envelope"),
            ('{"advisories": "not a list"}', "malformed_envelope"),
            # A JSON object with no ``advisories`` key at all. ``_save``
            # always emits it, so these are not files this store wrote.
            ("{}", "malformed_envelope"),
            ('{"advisorees": [{"advisory_id": "a"}]}', "malformed_envelope"),
            ('{"version": 2, "items": []}', "malformed_envelope"),
        ],
    )
    def test_every_unreadable_shape_degrades(
        self, tmp_path: Path, content: str, reason: str
    ) -> None:
        path = tmp_path / "a.json"
        path.write_text(content, encoding="utf-8")
        store = AdvisoryStore(path)
        assert store.degradation is not None
        assert store.degradation.reason == reason

    @pytest.mark.parametrize(
        "content",
        ["{}", '{"advisorees": [{"advisory_id": "a"}]}', '{"version": 2}'],
    )
    def test_a_missing_advisories_key_is_not_an_empty_store(
        self, tmp_path: Path, content: str
    ) -> None:
        """The hole that left #393 intact for a whole class of file.

        Reading the key with a ``[]`` default made "no ``advisories`` key"
        and "an empty ``advisories`` list" the same state, so a hand-edit, a
        renamed field or the wrong file at this path loaded as a *clean*
        empty store — and the next nightly write replaced it. Nothing about
        that path was degraded, so nothing refused it.
        """
        path = tmp_path / "a.json"
        path.write_text(content, encoding="utf-8")
        before = path.read_text(encoding="utf-8")

        store = AdvisoryStore(path)

        assert store.is_degraded is True
        with pytest.raises(DegradedStoreWriteError):
            store.put(_advisory())
        assert path.read_text(encoding="utf-8") == before

    def test_an_empty_advisories_list_is_a_clean_empty_store(
        self, tmp_path: Path
    ) -> None:
        """The negative control: what ``_save`` writes must still load clean."""
        path = tmp_path / "a.json"
        path.write_text('{"advisories": []}', encoding="utf-8")
        store = AdvisoryStore(path)
        assert store.is_degraded is False
        store.put(_advisory())
        assert len(store.list()) == 1

    def test_an_unexpected_load_failure_degrades_rather_than_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The outer catch is the unconditional half of #382's promise.

        Every *known* corruption shape is handled by a narrow branch inside
        ``_load_rows``, so nothing else exercises the broad outer handler —
        and narrowing it would pass CI while breaking the guarantee that
        constructing a store never raises, whatever shape the corruption
        takes. Retrieval depends on that.
        """
        path = tmp_path / "a.json"
        AdvisoryStore(path).put(_advisory())

        def _boom(_self: AdvisoryStore) -> None:
            msg = "something nobody enumerated"
            raise RuntimeError(msg)

        monkeypatch.setattr(AdvisoryStore, "_load_rows", _boom)

        store = AdvisoryStore(path)  # must not raise

        assert store.degradation is not None
        assert store.degradation.reason == "load_failed"
        assert "RuntimeError" in store.degradation.detail
        with pytest.raises(DegradedStoreWriteError):
            store.put(_advisory())

    def test_a_whole_file_failure_reports_an_unknown_row_count(
        self, tmp_path: Path
    ) -> None:
        """ "0 could not be read" reads as "nothing was lost".

        On a whole-file failure the count is unknowable — 51 rows may be
        sitting in the file unread. Rendering that as ``0`` tells an
        operator at 03:00 the opposite of the truth.
        """
        path = tmp_path / "a.json"
        path.write_text("{ broken", encoding="utf-8")
        store = AdvisoryStore(path)

        assert store.degradation is not None
        assert store.degradation.rows_skipped is None
        assert store.degradation.rows_skipped_display == "unknown"
        assert store.degradation.to_dict()["rows_skipped_display"] == "unknown"

        with pytest.raises(DegradedStoreWriteError) as excinfo:
            store.put(_advisory())
        assert "unknown could not be read" in str(excinfo.value)
        assert "0 could not be read" not in str(excinfo.value)

    def test_a_per_row_failure_reports_the_count_it_knows(self, tmp_path: Path) -> None:
        """The other half: a countable loss must not read as "unknown"."""
        path = tmp_path / "a.json"
        AdvisoryStore(path).put_many([_advisory(scope=f"s{i}") for i in range(3)])
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["advisories"][1] = {"advisory_id": "x", "renamed": 1}
        path.write_text(json.dumps(raw), encoding="utf-8")

        store = AdvisoryStore(path)
        assert store.degradation is not None
        assert store.degradation.rows_skipped == 1
        assert store.degradation.rows_skipped_display == "1"

    def test_many_bad_rows_are_summarised_not_dumped(self, tmp_path: Path) -> None:
        """A cron line has to stay readable — but say how much it elided."""
        path = tmp_path / "a.json"
        rows = [{"advisory_id": f"bad{i}", "renamed": i} for i in range(7)]
        path.write_text(json.dumps({"advisories": rows}), encoding="utf-8")

        store = AdvisoryStore(path)

        assert store.degradation is not None
        assert store.degradation.rows_skipped == 7
        assert "(+4 more)" in store.degradation.detail

    def test_the_error_carries_a_stable_machine_readable_code(
        self, tmp_path: Path
    ) -> None:
        """A new public contract, set after ``super().__init__`` — easy to drop."""
        path = tmp_path / "a.json"
        path.write_text("{ broken", encoding="utf-8")
        store = AdvisoryStore(path)

        with pytest.raises(DegradedStoreWriteError) as excinfo:
            store.put(_advisory())

        assert excinfo.value.code == "DEGRADED_STORE_WRITE"
        assert excinfo.value.path == str(path)
        assert excinfo.value.recovery == f"mv {path} {path}.corrupt"

    def test_a_refused_write_does_not_mutate_the_store_either(
        self, tmp_path: Path
    ) -> None:
        """Refusing after mutating is #393's own symptom, surviving in-process.

        The file being intact is only half the promise — a caller that
        catches the error and keeps serving packs from the same store must
        not have silently lost rows, and ``restore()`` must not have
        un-suppressed one in memory. Both were true before the refusal
        moved ahead of the mutation.
        """
        path = tmp_path / "a.json"
        seed = AdvisoryStore(path)
        keeper = seed.put(_advisory(scope="keeper"))
        dropped = seed.put(_advisory(scope="dropped"))
        seed.suppress(dropped.advisory_id, reason="fitness")
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["advisories"].append({"advisory_id": "broken", "renamed": 1})
        path.write_text(json.dumps(raw), encoding="utf-8")

        store = AdvisoryStore(path)
        assert store.is_degraded is True
        before_ids = {a.advisory_id for a in store.list(include_suppressed=True)}
        assert len(before_ids) == 2

        for call in (
            lambda: store.put(_advisory()),
            lambda: store.put_many([_advisory()]),
            lambda: store.suppress(keeper.advisory_id),
            lambda: store.restore(dropped.advisory_id),
            lambda: store.remove(keeper.advisory_id),
            store.clear,
        ):
            with pytest.raises(DegradedStoreWriteError):
                call()

        after = store.list(include_suppressed=True)
        assert {a.advisory_id for a in after} == before_ids
        still_suppressed = store.get(dropped.advisory_id)
        assert still_suppressed is not None
        assert still_suppressed.status == AdvisoryStatus.SUPPRESSED
        assert store.get(keeper.advisory_id) is not None

    def test_binary_garbage_degrades_rather_than_raising(self, tmp_path: Path) -> None:
        """#382's promise is unconditional: constructing a store never raises.

        ``read_text`` raises ``UnicodeDecodeError`` — a ``ValueError``, not
        an ``OSError`` — on a partially-binary file, which is precisely
        what a torn write produces.
        """
        path = tmp_path / "a.json"
        path.write_bytes(b'{"advisories": [\xff\xfe\x00binary')
        store = AdvisoryStore(path)
        assert store.degradation is not None
        assert store.degradation.reason == "unreadable_file"
        assert store.list() == []

    def test_every_write_path_refuses(self, tmp_path: Path) -> None:
        """put / put_many / suppress / restore / remove / clear, all of them.

        A partial exemption is a hole: ``clear`` in particular reads like
        the recovery path and would happily write ``{"advisories": []}``
        over the file nobody has read yet. Covered black-box — the file is
        seeded with a suppressed row so ``restore`` has a real target,
        rather than reaching into ``store._advisories``.
        """
        path = tmp_path / "a.json"
        seed = AdvisoryStore(path)
        keeper = seed.put(_advisory(scope="keeper"))
        suppressed = seed.put(_advisory(scope="suppressed"))
        seed.suppress(suppressed.advisory_id, reason="fitness")
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["advisories"].append({"advisory_id": "broken", "category": "nonsense"})
        path.write_text(json.dumps(raw), encoding="utf-8")
        before = path.read_text(encoding="utf-8")

        store = AdvisoryStore(path)
        assert store.is_degraded is True
        assert store.get(keeper.advisory_id) is not None
        assert store.get(suppressed.advisory_id) is not None

        for call in (
            lambda: store.put(_advisory()),
            lambda: store.put_many([_advisory()]),
            lambda: store.suppress(keeper.advisory_id),
            lambda: store.restore(suppressed.advisory_id),
            lambda: store.remove(keeper.advisory_id),
            store.clear,
        ):
            with pytest.raises(DegradedStoreWriteError):
                call()

        assert path.read_text(encoding="utf-8") == before

    def test_one_bad_row_costs_one_row_not_the_file(self, tmp_path: Path) -> None:
        """Per-row validation — a renamed field must not blank the corpus.

        The blast radius the issue names: the pre-#393 ``except`` wrapped
        the whole loop, so one unparseable entry discarded every valid row
        and the next write then overwrote them.
        """
        path = tmp_path / "a.json"
        AdvisoryStore(path).put_many([_advisory(scope=f"s{i}") for i in range(3)])
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["advisories"][1] = {"advisory_id": "x", "renamed_field": 1}
        path.write_text(json.dumps(raw), encoding="utf-8")

        store = AdvisoryStore(path)
        assert len(store.list()) == 2
        assert store.degradation is not None
        assert store.degradation.reason == "invalid_rows"
        assert store.degradation.rows_loaded == 2
        assert store.degradation.rows_skipped == 1

    def test_a_partial_load_still_refuses_to_write(self, tmp_path: Path) -> None:
        """The two halves are a pair.

        Per-row leniency is only safe because the write refuses. Allowing a
        write here would rewrite the file with the two rows that parsed and
        drop the third — the same data loss at a narrower granularity.
        """
        path = tmp_path / "a.json"
        AdvisoryStore(path).put_many([_advisory(scope=f"s{i}") for i in range(3)])
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["advisories"][1] = {"advisory_id": "x", "renamed_field": 1}
        path.write_text(json.dumps(raw), encoding="utf-8")
        before = path.read_text(encoding="utf-8")

        store = AdvisoryStore(path)
        with pytest.raises(DegradedStoreWriteError):
            store.put(_advisory())
        assert path.read_text(encoding="utf-8") == before

    def test_reads_still_serve_what_parsed(self, tmp_path: Path) -> None:
        """#382 survives: a corrupt file must not take retrieval down."""
        path = tmp_path / "a.json"
        AdvisoryStore(path).put_many([_advisory(scope=f"s{i}") for i in range(3)])
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["advisories"][0] = {"advisory_id": "x", "renamed_field": 1}
        path.write_text(json.dumps(raw), encoding="utf-8")

        store = AdvisoryStore(path)
        assert {a.scope for a in store.list()} == {"s1", "s2"}
        assert store.list(scope="s1")
        assert store.get(next(iter(store.list())).advisory_id) is not None

    def test_the_refusal_names_the_recovery_command(self, tmp_path: Path) -> None:
        """An operator meets this in a cron log; a diagnosis is not enough."""
        path = tmp_path / "a.json"
        path.write_text("{ broken", encoding="utf-8")
        store = AdvisoryStore(path)

        with pytest.raises(DegradedStoreWriteError) as excinfo:
            store.put(_advisory())

        assert excinfo.value.recovery == f"mv {path} {path}.corrupt"
        assert str(path) in str(excinfo.value)
        assert f"mv {path}" in str(excinfo.value)
        assert store.degradation is not None
        assert store.degradation.to_dict()["recovery"] == excinfo.value.recovery

    def test_degradation_is_logged_where_the_cli_can_see_it(
        self, tmp_path: Path
    ) -> None:
        """``error``, not ``info``.

        ``trellis_cli.main._root`` pins ``TRELLIS_LOG_LEVEL=WARNING`` unless
        ``--verbose`` is passed, so an ``info`` line here is filtered out of
        the surface that runs this nightly — the same no-op as a
        ``logger.debug`` under an INFO filter.

        The level is pinned because ``capture_logs`` swaps the processor
        chain but leaves ``wrapper_class`` alone: under pytest the bound
        logger records ``debug`` too, so every other assertion in this test
        would pass just as well against an invisible line.
        """
        path = tmp_path / "a.json"
        path.write_text("{ broken", encoding="utf-8")

        with capture_logs() as logs:
            AdvisoryStore(path)

        lines = [e for e in logs if e["event"] == "advisory_load_degraded"]
        assert len(lines) == 1
        assert lines[0]["log_level"] == "error"
        assert lines[0]["path"] == str(path)
        assert lines[0]["reason"] == "malformed_json"
        assert lines[0]["recovery"] == f"mv {path} {path}.corrupt"

    def test_a_clean_load_says_nothing_alarming(self, tmp_path: Path) -> None:
        """A warning on every load would train the reader to skip it."""
        path = tmp_path / "a.json"
        AdvisoryStore(path).put(_advisory())
        with capture_logs() as logs:
            AdvisoryStore(path)
        assert not [e for e in logs if e["event"] == "advisory_load_degraded"]


class TestSaveIsAtomic:
    """The half-written file the rest of this module survives has one author.

    ``write_text`` truncates the destination and *then* writes, so a crash,
    a full disk or a killed cron between the two produces it. This store is
    the file's only writer.
    """

    def test_no_temp_files_are_left_behind(self, tmp_path: Path) -> None:
        path = tmp_path / "a.json"
        store = AdvisoryStore(path)
        store.put_many([_advisory() for _ in range(3)])
        store.put(_advisory())
        assert [p.name for p in tmp_path.iterdir()] == ["a.json"]

    def test_a_failed_write_leaves_the_destination_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The property atomicity buys, stated as behaviour.

        ``write_text`` truncates and *then* writes, so a failure between
        the two is how ``advisories.json`` becomes the corrupt file the
        rest of this module has to survive. Here the failure is injected at
        ``fsync``; the destination must be byte-identical and no temp file
        may be left behind.
        """
        path = tmp_path / "a.json"
        store = AdvisoryStore(path)
        store.put(_advisory(scope="original"))
        before = path.read_text(encoding="utf-8")

        def _boom(_fd: int) -> None:
            msg = "No space left on device"
            raise OSError(msg)

        # Patched on the shared helper (#413 lifted it into
        # ``trellis.core.atomic_write``), which is where the write now
        # happens. The injected failure and the property are unchanged.
        monkeypatch.setattr("trellis.core.atomic_write.os.fsync", _boom)

        with pytest.raises(OSError, match="No space left"):
            store.put(_advisory(scope="doomed"))

        assert path.read_text(encoding="utf-8") == before
        assert [p.name for p in tmp_path.iterdir()] == ["a.json"]

        # And memory rolled back with it. This is the case the degraded-path
        # refusal cannot cover — the store loaded cleanly, so nothing refuses
        # ahead of the mutation, and without the rollback the object would go
        # on serving an advisory that is not on disk and never will be.
        assert {a.scope for a in store.list()} == {"original"}
        assert store.get(_advisory(scope="doomed").advisory_id) is None

    def test_an_existing_file_keeps_its_mode(self, tmp_path: Path) -> None:
        """``mkstemp`` creates 0600; inheriting it would narrow a live file.

        The reference deployment bind-mounts the data directory into
        containers, so a silently-narrowed advisories.json is a reader that
        stops reading.
        """
        path = tmp_path / "a.json"
        store = AdvisoryStore(path)
        store.put(_advisory())
        path.chmod(0o640)

        store.put(_advisory())

        assert stat.S_IMODE(path.stat().st_mode) == 0o640

    def test_a_fresh_file_is_group_and_world_readable(self, tmp_path: Path) -> None:
        path = tmp_path / "a.json"
        AdvisoryStore(path).put(_advisory())
        assert stat.S_IMODE(path.stat().st_mode) == 0o644


class TestLiveFileShapeRoundTrips:
    """Rows written before #383/#394 must not read as invalid rows.

    This is the failure mode per-row validation could plausibly introduce
    on the reference deployment, whose 51 live rows carry neither
    ``fitness_scored_at`` nor ``evidence.evidence_confidence`` — both added
    after those rows were written. Reading them as invalid would degrade the
    live store and refuse every write, turning a fix into an outage.

    The fixture reproduces the live file's exact key set (verified against a
    copy of ``~/.trellis/data/advisories.json``, 51 rows, 29 distinct
    messages, confidence 0.231-0.600) rather than shipping its content.
    """

    @staticmethod
    def _pre_394_row(index: int) -> dict[str, object]:
        return {
            "schema_version": "0.1.0",
            "created_at": "2026-08-08T03:30:04.134918Z",
            "updated_at": "2026-08-08T03:30:04.134920Z",
            "advisory_id": f"01KZFPQBQ6TR1VJW5CNXGCEBV{index:02d}",
            "category": "approach",
            "confidence": 0.231 + (index % 10) * 0.04,
            "message": f"Packs using strategy {index % 29} succeeded 60% ...",
            "evidence": {
                "schema_version": "0.1.0",
                "sample_size": 5,
                "success_rate_with": 0.6,
                "success_rate_without": 0.0,
                "effect_size": 0.6,
                "representative_trace_ids": [],
            },
            "scope": "global",
            "entity_id": None,
            "metadata": {"strategy": "semantic"},
            "status": "active",
            "suppressed_at": None,
            "suppression_reason": None,
        }

    def test_fifty_one_pre_394_rows_load_clean(self, tmp_path: Path) -> None:
        path = tmp_path / "advisories.json"
        rows = [self._pre_394_row(i) for i in range(51)]
        path.write_text(json.dumps({"advisories": rows}), encoding="utf-8")

        store = AdvisoryStore(path)

        assert store.is_degraded is False, (
            "pre-#394 rows must not read as invalid rows — that would "
            "degrade the live store and refuse every write"
        )
        assert len(store.list(include_suppressed=True)) == 51
        assert all(a.fitness_scored_at is None for a in store.list())
        assert all(a.evidence.evidence_confidence is None for a in store.list())

    def test_the_round_trip_is_lossless(self, tmp_path: Path) -> None:
        """Load, write, reload: the same rows, and writes are permitted."""
        path = tmp_path / "advisories.json"
        rows = [self._pre_394_row(i) for i in range(51)]
        path.write_text(json.dumps({"advisories": rows}), encoding="utf-8")

        store = AdvisoryStore(path)
        store.put_many(store.list())  # a real write against a real load

        reloaded = AdvisoryStore(path)
        assert reloaded.is_degraded is False
        assert {a.advisory_id for a in reloaded.list()} == {
            str(r["advisory_id"]) for r in rows
        }


class TestDegradationIsNotTheOnlyStaleView:
    """The laundering primitive is wider than a degraded load (#438).

    A whole-file rewrite from *any* in-memory view that is no longer the
    file produces #393's end state. These are the routes that reach it with
    nothing degraded — closed on ``PolicyStore`` by #423 and ported here,
    to the store that actually has a file on the reference deployment.
    """

    def test_a_second_writer_is_not_silently_overwritten(self, tmp_path: Path) -> None:
        """Three processes write this file, and none of them held a lock.

        The nightly ``trellis worker curate`` cron, the host ``trellis
        analyze`` advisory commands, and the containerised ``POST
        /api/v1/advisories/generate`` all write one bind-mounted
        ``advisories.json``. A store that loaded ``[A]`` must not rewrite
        the file as ``[A, C]`` after another made it ``[A, B]``: since
        advisory ids are stable (#394) that deletes ``B`` from disk, from
        every future pack, and — the half no read notices — takes ``B``'s
        suppression with it, on a call that succeeds.
        """
        path = tmp_path / "advisories.json"
        mine = AdvisoryStore(path)
        mine.put(_advisory(scope="a"))

        # Another process appends B between my load and my next write.
        theirs = AdvisoryStore(path)
        b = theirs.put(_advisory(scope="b"))
        theirs.suppress(b.advisory_id, reason="fitness")
        after_theirs = path.read_bytes()

        with pytest.raises(StaleStoreWriteError) as excinfo:
            mine.put(_advisory(scope="c"))

        assert path.read_bytes() == after_theirs
        # Transient and retryable, so it must not claim the file is damaged.
        assert excinfo.value.code == "STALE_STORE_WRITE"
        assert excinfo.value.path == str(path)
        assert mine.is_degraded is False
        # B's suppression survived, which is what the refusal is protecting.
        survivor = AdvisoryStore(path).get(b.advisory_id)
        assert survivor is not None
        assert survivor.status == AdvisoryStatus.SUPPRESSED

    def test_a_file_created_after_construction_is_not_wiped(
        self, tmp_path: Path
    ) -> None:
        """A store built against an absent path never read the file.

        Not degradation — an absent advisory file is the normal state of a
        deployment that has never run generate-advisories (#393) — so
        nothing about the *load* would ever refuse this write.
        """
        path = tmp_path / "advisories.json"
        store = AdvisoryStore(path)  # absent: not degraded, writes permitted

        AdvisoryStore(path).put(_advisory(scope="declared"))
        landed = path.read_bytes()

        with pytest.raises(StaleStoreWriteError):
            store.put(_advisory(scope="mine"))

        assert path.read_bytes() == landed

    def test_damage_arriving_after_a_clean_load_is_not_overwritten(
        self, tmp_path: Path
    ) -> None:
        """The refusal must not be keyed on load-time state alone.

        A store that read a healthy file is not degraded and never will be,
        so before this guard it would whole-file-rewrite a file an operator
        had since broken (or hand-edited), destroying the evidence and
        resuming from its own stale snapshot. The module docstring's
        promise that "the corrupt bytes stay on disk, where an operator can
        look at them" was false on exactly this path.
        """
        path = tmp_path / "advisories.json"
        store = AdvisoryStore(path)
        store.put(_advisory(scope="a"))
        assert store.is_degraded is False

        path.write_text('{"advisorees": [{"advisory_id": "x"}]}', encoding="utf-8")
        damaged = path.read_bytes()

        with pytest.raises(StaleStoreWriteError):
            store.put(_advisory(scope="b"))

        assert path.read_bytes() == damaged

    def test_the_same_store_may_write_repeatedly(self, tmp_path: Path) -> None:
        """The guard must not fire on a store's own previous write.

        ``os.replace`` changes the inode every time, so without refreshing
        the fingerprint after a successful save the second write on any
        store would refuse — which would break the nightly cycle, which
        writes once per generated advisory and once per fitness adjustment.
        """
        path = tmp_path / "advisories.json"
        store = AdvisoryStore(path)
        first = store.put(_advisory(scope="a"))
        store.put_many([_advisory(scope="b"), _advisory(scope="c")])
        store.suppress(first.advisory_id, reason="fitness")
        store.restore(first.advisory_id)
        store.remove(first.advisory_id)

        assert len(store.list()) == 2
        assert len(AdvisoryStore(path).list()) == 2

    def test_a_symlinked_path_writes_repeatedly(self, tmp_path: Path) -> None:
        """The fingerprint reads through the link, as the write does.

        ``atomic_write_text`` deliberately follows a symlink and replaces
        the *target*, and ``Path.stat`` follows it too — so the two agree
        and a store may write through a link more than once. A fingerprint
        taken on the link itself (``lstat``) would never change and the
        guard would never fire; one that disagreed with the write would
        fire on every second write. A symlink is a plausible answer to
        ``resolve_advisory_path``'s canonical-path advice, so the shape is
        reachable.
        """
        real = tmp_path / "real.json"
        real.write_text('{"advisories": []}', encoding="utf-8")
        link = tmp_path / "advisories.json"
        link.symlink_to(real)

        store = AdvisoryStore(link)
        store.put(_advisory(scope="a"))
        store.put(_advisory(scope="b"))

        assert link.is_symlink()
        assert len(json.loads(real.read_text(encoding="utf-8"))["advisories"]) == 2

    def test_the_fingerprint_catches_a_same_size_same_mtime_replacement(
        self, tmp_path: Path
    ) -> None:
        """``st_ino`` is the load-bearing field, and only it catches this.

        Every write lands through ``os.replace`` from a fresh temp file, so
        a completed write by any process changes the inode even when size
        and mtime collide. The collision is not exotic: a filesystem with
        coarse mtime granularity reaches it for any two same-size writes
        inside one tick, and this is a small JSON document that three
        processes rewrite. Dropping ``st_ino`` from the tuple must fail
        here and nowhere else.
        """
        path = tmp_path / "advisories.json"
        path.write_text('{"advisories": []}', encoding="utf-8")
        before = path.stat()
        store = AdvisoryStore(path)

        replacement = tmp_path / "other.json"
        replacement.write_text('{"advisories":[] }', encoding="utf-8")
        assert replacement.stat().st_size == before.st_size
        replacement.replace(path)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

        after = path.stat()
        assert (after.st_mtime_ns, after.st_size) == (
            before.st_mtime_ns,
            before.st_size,
        ), "the collision this test exists for was not constructed"
        assert after.st_ino != before.st_ino

        with pytest.raises(StaleStoreWriteError):
            store.put(_advisory())

    def test_a_degraded_load_still_refuses_with_its_own_error(
        self, tmp_path: Path
    ) -> None:
        """#414's guard is not weakened by the new one.

        The two are ordered ``degraded`` then ``stale`` on every write
        path, and they are not interchangeable: a degraded store needs an
        operator to look at the file (``DEGRADED_STORE_WRITE``, carrying
        the ``mv``), a stale one needs nothing but a retry. A store whose
        file broke *after* construction is both — its load degraded and its
        fingerprint moved — and it must report the one that needs a human.
        """
        path = tmp_path / "advisories.json"
        AdvisoryStore(path).put(_advisory())
        path.write_text("{ broken", encoding="utf-8")

        store = AdvisoryStore(path)
        assert store.is_degraded is True

        with pytest.raises(DegradedStoreWriteError) as excinfo:
            store.put(_advisory())
        assert excinfo.value.code == "DEGRADED_STORE_WRITE"
        assert excinfo.value.recovery.startswith("mv ")

    @pytest.mark.parametrize(
        "write", ["put", "put_many", "suppress", "restore", "remove", "clear"]
    )
    def test_a_stale_write_never_enters_the_write_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write: str
    ) -> None:
        """Every write path's own guard, pinned against ``_save``'s masking.

        ``_save`` calls ``refuse_if_stale`` too and ``_save_or_roll_back``
        undoes the mutation, so deleting the guard from any one of these
        methods leaves the exception and the final ``list()`` identical —
        the same masking #423 measured on ``PolicyStore``, where dropping
        the guard from ``_save`` killed **zero** tests. Stubbing ``_save``
        is what makes each call site's own guard observable: with the guard
        removed the stub records a call and the assertion fires.

        The difference it protects is real. ``suppress``, ``restore`` and
        ``remove`` all return *before* reaching a write when the id is
        unknown to the in-memory view, so ``_save``'s guard cannot cover
        them at all — see the wrong-answer test below. And a store outlives
        the refused call in every caller that catches and keeps serving.
        """
        path = tmp_path / "advisories.json"
        mine = AdvisoryStore(path)
        seeded = mine.put(_advisory(scope="seeded"))

        AdvisoryStore(path).put(_advisory(scope="theirs"))  # another process

        entered: list[str] = []
        monkeypatch.setattr(
            mine,
            "_save",
            lambda: entered.append("save"),  # type: ignore[method-assign]
        )
        attempts = {
            "put": lambda: mine.put(_advisory(scope="mine")),
            "put_many": lambda: mine.put_many([_advisory(scope="mine")]),
            "suppress": lambda: mine.suppress(seeded.advisory_id, reason="x"),
            "restore": lambda: mine.restore(seeded.advisory_id),
            "remove": lambda: mine.remove(seeded.advisory_id),
            "clear": mine.clear,
        }

        with pytest.raises(StaleStoreWriteError):
            attempts[write]()

        assert entered == [], "the write path was entered on a stale store"

    def test_save_refuses_a_stale_write_on_its_own(self, tmp_path: Path) -> None:
        """The unconditional backstop, pinned separately from its callers.

        Every public write refuses first, which masks this completely:
        removing ``refuse_if_stale()`` from ``_save`` alone leaves every
        other test in this class green. It is what protects a future write
        path added without the leading guard — the claim
        ``refuse_if_degraded``'s docstring already makes ("not calling it
        is never a way to avoid one") is a claim about ``_save``, and so
        needs its own test.
        """
        path = tmp_path / "advisories.json"
        store = AdvisoryStore(path)
        store.put(_advisory(scope="a"))
        AdvisoryStore(path).put(_advisory(scope="b"))

        with pytest.raises(StaleStoreWriteError):
            store._save()

    @pytest.mark.parametrize("write", ["suppress", "restore", "remove"])
    def test_a_stale_lookup_does_not_report_not_found(
        self, tmp_path: Path, write: str
    ) -> None:
        """These three answer from the in-memory view and never reach a write.

        ``remove`` returns ``False`` and ``suppress`` / ``restore`` return
        ``None`` for an id this store has not loaded — which for a row
        another process added since is "no such advisory" about a row that
        is right there in the file. A wrong answer, not merely an unhelpful
        one, and ``_save``'s guards are structurally too late to prevent
        it: the call returns before reaching a write at all.
        """
        path = tmp_path / "advisories.json"
        mine = AdvisoryStore(path)
        mine.put(_advisory(scope="mine"))

        theirs = AdvisoryStore(path).put(_advisory(scope="theirs"))

        calls = {
            "suppress": lambda: mine.suppress(theirs.advisory_id, reason="x"),
            "restore": lambda: mine.restore(theirs.advisory_id),
            "remove": lambda: mine.remove(theirs.advisory_id),
        }
        with pytest.raises(StaleStoreWriteError):
            calls[write]()

    def test_a_stale_idempotent_suppress_is_not_reported_as_applied(
        self, tmp_path: Path
    ) -> None:
        """The other wrong answer ``suppress`` can give from a stale view.

        Suppressing an already-suppressed advisory is a documented no-op
        that returns the existing row. From a stale view that row is read
        from a superseded file: another process may have restored it, so
        the call would report the suppression as already applied against a
        file whose row is ACTIVE — and write nothing.
        """
        path = tmp_path / "advisories.json"
        mine = AdvisoryStore(path)
        adv = mine.put(_advisory(scope="a"))
        mine.suppress(adv.advisory_id, reason="fitness")

        theirs = AdvisoryStore(path)
        theirs.restore(adv.advisory_id)

        with pytest.raises(StaleStoreWriteError):
            mine.suppress(adv.advisory_id, reason="fitness")

        current = AdvisoryStore(path).get(adv.advisory_id)
        assert current is not None
        assert current.status == AdvisoryStatus.ACTIVE

    @pytest.mark.skipif(
        os.geteuid() == 0, reason="root traverses any directory regardless of mode"
    )
    def test_an_unsearchable_parent_degrades_rather_than_reading_as_absent(
        self, tmp_path: Path
    ) -> None:
        """``Path.exists()`` swallows ``OSError``, which was the hazard.

        An unreadable file presenting as *absent* is the worst of the
        available answers here: absent means "a deployment that has never
        generated an advisory", which is neither degraded nor stale, and so
        is the one state this store will freely write over.
        """
        parent = tmp_path / "locked"
        parent.mkdir()
        path = parent / "advisories.json"
        path.write_text('{"advisories": []}', encoding="utf-8")
        parent.chmod(0o000)
        try:
            store = AdvisoryStore(path)
        finally:
            parent.chmod(0o755)

        assert store.is_degraded is True
        assert store.degradation is not None
        assert store.degradation.reason == "unreadable_file"


class TestTheRecoveryCommandRuns:
    """#427: the ``mv`` is the entire justification for refusing the write.

    #414 argued it — "an operator at 03:00 needs the fix, not a diagnosis",
    "the one string that must survive rendering byte-for-byte" — and then
    built it with bare interpolation, so it survives Rich markup and does
    not survive the shell.
    """

    def test_the_recovery_command_survives_a_path_with_a_space(
        self, tmp_path: Path
    ) -> None:
        """A data dir containing a space word-splits into four operands.

        ``/tmp/my staging dir/`` , ``~/Library/Application Support/…``, any
        Windows-ish mount. The assertion goes at the real property — that
        the string *parses as a shell command with the two operands it
        should have* — rather than at the symptom, because the obvious
        ``f"mv {path} {path}.corrupt" in command`` check passes happily
        against a command that cannot run.
        """
        data_dir = tmp_path / "my staging dir"
        data_dir.mkdir()
        path = data_dir / "advisories.json"
        path.write_text("{ broken", encoding="utf-8")
        store = AdvisoryStore(path)

        assert store.degradation is not None
        command = store.degradation.recovery

        assert shlex.split(command) == ["mv", str(path), f"{path}.corrupt"]

    def test_the_refused_write_carries_the_runnable_command(
        self, tmp_path: Path
    ) -> None:
        """Through the exception too, which is where an operator meets it."""
        data_dir = tmp_path / "my [staging] dir"
        data_dir.mkdir()
        path = data_dir / "advisories.json"
        path.write_text("{ broken", encoding="utf-8")
        store = AdvisoryStore(path)

        with pytest.raises(DegradedStoreWriteError) as excinfo:
            store.put(_advisory())

        assert excinfo.value.recovery is not None
        assert shlex.split(excinfo.value.recovery) == [
            "mv",
            str(path),
            f"{path}.corrupt",
        ]

    def test_an_ordinary_path_is_left_alone(self, tmp_path: Path) -> None:
        """``shlex.quote`` must not start quoting every ordinary path.

        The command is pasted by hand and read by humans; gratuitous quotes
        on the common case would be a regression in the thing this string
        exists for.
        """
        path = tmp_path / "advisories.json"
        path.write_text("{ broken", encoding="utf-8")
        store = AdvisoryStore(path)

        assert store.degradation is not None
        assert store.degradation.recovery == f"mv {path} {path}.corrupt"
