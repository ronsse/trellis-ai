"""Tests for :mod:`trellis.core.write_provenance` — the event stamp."""

from __future__ import annotations

import json

import pytest

from trellis.core.write_config import ENV_VAR_BY_FIELD, WriteBehaviourConfig
from trellis.core.write_provenance import (
    WRITE_PROVENANCE_KEY,
    build_write_provenance,
    get_write_provenance,
    stamp_metadata,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VAR_BY_FIELD.values():
        monkeypatch.delenv(name, raising=False)
    get_write_provenance.cache_clear()


class TestStampShape:
    def test_carries_build_identity_and_flags(self, pin_source_tree) -> None:
        """Pinned fresh: the key set is the *healthy* shape, not ambient.

        Left to the real install, this would assert on the git state of
        whichever tree the test run happens to be installed from.
        """
        pin_source_tree(commit="abc1234", head="abc1234" + "0" * 33)
        stamp = build_write_provenance()
        assert set(stamp) == {
            "version",
            "version_source",
            "commit",
            "dirty",
            "env_flags",
            "env_flags_digest",
        }
        assert stamp["env_flags"] == WriteBehaviourConfig().as_dict()

    def test_reflects_the_configuration_it_was_given(self) -> None:
        config = WriteBehaviourConfig(classify_on_ingest=True, embed_on_ingest=True)
        stamp = build_write_provenance(config)
        assert stamp["env_flags"]["classify_on_ingest"] is True
        assert stamp["env_flags"]["embed_on_ingest"] is True

    def test_is_json_serializable(self) -> None:
        json.dumps(build_write_provenance())

    def test_stamp_nests_at_most_one_level(self) -> None:
        """``_copy_stamp`` copies one level of dicts; pin that that's enough.

        A future field nesting deeper would silently reintroduce shared
        mutable state between the memo and every stamped event.
        """
        for value in build_write_provenance().values():
            if isinstance(value, dict):
                assert not any(isinstance(v, (dict, list)) for v in value.values())
            else:
                assert not isinstance(value, list)


class TestFlagsDigest:
    def test_stable_for_the_same_flags(self) -> None:
        first = build_write_provenance(WriteBehaviourConfig(embed_on_ingest=True))
        second = build_write_provenance(WriteBehaviourConfig(embed_on_ingest=True))
        assert first["env_flags_digest"] == second["env_flags_digest"]

    def test_differs_when_any_flag_differs(self) -> None:
        base = build_write_provenance(WriteBehaviourConfig())
        changed = build_write_provenance(WriteBehaviourConfig(trace_extraction=True))
        assert base["env_flags_digest"] != changed["env_flags_digest"]


class TestProcessCache:
    def test_resolved_once_per_process(self) -> None:
        """The event hot path must not rebuild the stamp per event."""
        assert get_write_provenance() is get_write_provenance()

    def test_reset_re_reads_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert get_write_provenance()["env_flags"]["embed_on_ingest"] is False
        monkeypatch.setenv(ENV_VAR_BY_FIELD["embed_on_ingest"], "1")
        # Still the snapshot taken at first use — deliberate.
        assert get_write_provenance()["env_flags"]["embed_on_ingest"] is False
        get_write_provenance.cache_clear()
        assert get_write_provenance()["env_flags"]["embed_on_ingest"] is True


class TestStampMetadata:
    def test_adds_the_stamp_to_empty_metadata(self) -> None:
        assert stamp_metadata(None)[WRITE_PROVENANCE_KEY] == get_write_provenance()

    def test_preserves_caller_supplied_keys(self) -> None:
        stamped = stamp_metadata({"agent": "claude"})
        assert stamped["agent"] == "claude"
        assert WRITE_PROVENANCE_KEY in stamped

    def test_does_not_mutate_the_callers_dict(self) -> None:
        original: dict[str, object] = {"agent": "claude"}
        stamp_metadata(original)
        assert original == {"agent": "claude"}

    def test_caller_supplied_stamp_wins(self) -> None:
        """Replay tools re-emit on behalf of a *different* build."""
        historical = {"version": "0.1.0", "version_source": "dist-metadata"}
        stamped = stamp_metadata({WRITE_PROVENANCE_KEY: historical})
        assert stamped[WRITE_PROVENANCE_KEY] == historical

    def test_stamp_copy_is_not_shared_with_the_cache(self) -> None:
        """A caller mutating an event's metadata must not poison the memo.

        Asserted on the *nested* ``env_flags`` dict as well as the top
        level: a shallow ``dict()`` copy passes the top-level check while
        still handing every stamped event the memo's own flag map.
        """
        first = stamp_metadata(None)
        second = stamp_metadata(None)
        first[WRITE_PROVENANCE_KEY]["version"] = "tampered"
        first[WRITE_PROVENANCE_KEY]["env_flags"]["classify_on_ingest"] = "tampered"

        assert get_write_provenance()["version"] != "tampered"
        assert get_write_provenance()["env_flags"]["classify_on_ingest"] is False
        assert second[WRITE_PROVENANCE_KEY]["env_flags"]["classify_on_ingest"] is False


class TestStalenessKeys:
    """#348 — an editable install whose tree has moved on says so."""

    FRESH_SHA = "abc1234" + "0" * 33
    LIVE_SHA = "def5678" + "1" * 33

    def test_healthy_stamp_is_byte_identical_across_every_non_stale_state(
        self, pin_source_tree
    ) -> None:
        """The load-bearing property: a healthy deployment pays nothing.

        The stamp rides ``Event.metadata`` on every emitted event. A fresh
        editable install, an install the probe could not read, and a
        container image must each produce exactly the bytes they produced
        before this probe existed.
        """
        pin_source_tree(commit="abc1234", head=self.FRESH_SHA)
        fresh = json.dumps(build_write_provenance())
        pin_source_tree(commit="abc1234", head=None)
        unresolved = json.dumps(build_write_provenance())
        pin_source_tree(commit="abc1234", head=self.LIVE_SHA, tree=None)
        not_editable = json.dumps(build_write_provenance())
        pin_source_tree(commit=None, head=self.LIVE_SHA, source="fallback-version")
        container = json.dumps(build_write_provenance())

        assert fresh == unresolved == not_editable
        assert "stamp_stale" not in fresh
        # The container differs only in the build fields it already had.
        assert "stamp_stale" not in container

    def test_stale_install_adds_exactly_two_keys(self, pin_source_tree) -> None:
        pin_source_tree(commit="abc1234", head=self.LIVE_SHA)
        stamp = build_write_provenance()
        assert set(stamp) == {
            "version",
            "version_source",
            "commit",
            "dirty",
            "env_flags",
            "env_flags_digest",
            "stamp_stale",
            "source_tree_commit",
        }
        assert stamp["stamp_stale"] is True
        assert stamp["source_tree_commit"] == self.LIVE_SHA

    def test_the_stamped_commit_is_never_overwritten(self, pin_source_tree) -> None:
        """The row must still record what the metadata said.

        Silently substituting the live sha would destroy the very fact an
        analyst bucketing rows by build is reading.
        """
        pin_source_tree(commit="abc1234", head=self.LIVE_SHA)
        stamp = build_write_provenance()
        assert stamp["commit"] == "abc1234"
        assert stamp["version"] == "0.9.1.dev1+gabc1234"

    def test_install_time_dirty_is_a_different_field(self, pin_source_tree) -> None:
        """``dirty`` speaks for install time and must not move with this."""
        pin_source_tree(commit="abc1234", head=self.LIVE_SHA)
        assert build_write_provenance()["dirty"] is False

    def test_a_stale_stamp_still_nests_at_most_one_level(self, pin_source_tree) -> None:
        """``_copy_stamp`` copies one level of dicts; the new keys are flat."""
        pin_source_tree(commit="abc1234", head=self.LIVE_SHA)
        for value in build_write_provenance().values():
            if isinstance(value, dict):
                assert not any(isinstance(v, (dict, list)) for v in value.values())
            else:
                assert not isinstance(value, list)

    def test_a_stale_stamp_reaches_the_event_hot_path(self, pin_source_tree) -> None:
        pin_source_tree(commit="abc1234", head=self.LIVE_SHA)
        stamped = stamp_metadata(None)[WRITE_PROVENANCE_KEY]
        assert stamped["stamp_stale"] is True
        assert stamped["source_tree_commit"] == self.LIVE_SHA

    def test_a_raising_probe_cannot_fail_a_write(
        self, pin_source_tree, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole stamp must survive the probe blowing up.

        ``stamp_metadata`` runs inside ``EventLog.emit``, so an exception
        escaping the probe does not lose one verdict — it fails every
        write for the life of the process. The bytes must also be the
        *healthy* ones: a failed probe reports nothing, exactly as a
        fresh one does.
        """
        from trellis.core import version as version_mod

        pin_source_tree(commit="abc1234", head=self.FRESH_SHA)
        healthy = json.dumps(build_write_provenance())

        encoding = "utf-8"
        reason = "corrupt direct_url.json"

        def _boom() -> str | None:
            raise UnicodeDecodeError(encoding, b"\xff", 0, 1, reason)

        monkeypatch.setattr(version_mod, "_editable_source_tree", _boom)
        version_mod.resolve_stamp_staleness.cache_clear()
        get_write_provenance.cache_clear()

        assert json.dumps(build_write_provenance()) == healthy
        assert "stamp_stale" not in stamp_metadata(None)[WRITE_PROVENANCE_KEY]
