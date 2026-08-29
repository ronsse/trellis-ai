"""Tests for AdvisoryStore."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from trellis.errors import DegradedStoreWriteError
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
        over the file nobody has read yet.
        """
        path = tmp_path / "a.json"
        keeper = _advisory(scope="keeper")
        AdvisoryStore(path).put_many([keeper])
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["advisories"].append({"advisory_id": "broken", "category": "nonsense"})
        path.write_text(json.dumps(raw), encoding="utf-8")
        before = path.read_text(encoding="utf-8")

        store = AdvisoryStore(path)
        assert store.is_degraded is True
        # The good row loaded, so suppress/restore/remove have a real target.
        assert store.get(keeper.advisory_id) is not None

        for call in (
            lambda: store.put(_advisory()),
            lambda: store.put_many([_advisory()]),
            lambda: store.suppress(keeper.advisory_id),
            lambda: store.remove(keeper.advisory_id),
            store.clear,
        ):
            with pytest.raises(DegradedStoreWriteError):
                call()

        # restore() needs a suppressed row, which suppress() could not make.
        suppressed = keeper.model_copy(update={"status": AdvisoryStatus.SUPPRESSED})
        store._advisories[suppressed.advisory_id] = suppressed
        with pytest.raises(DegradedStoreWriteError):
            store.restore(suppressed.advisory_id)

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
