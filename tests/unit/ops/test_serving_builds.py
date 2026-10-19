"""``analyze health`` groups the window's packs by the build that assembled them.

Each test seeds ``PACK_ASSEMBLED`` rows into a real SQLite event log with a
stamp the test chose, because the writer's own stamp is one build by
construction: only a seeded stamp can stand in for a second process.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trellis.core.pack_holdout import HOLDOUT_KEY
from trellis.core.write_provenance import WRITE_PROVENANCE_KEY
from trellis.ops.serving_builds import (
    NO_PROVENANCE_LABEL,
    build_label,
    mixed_builds_reason,
    summarize_serving_builds,
)
from trellis.ops.write_health import (
    summarize_backend_health,
    summarize_serve_attribution,
)
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

NEW = {"version": "0.9.1.dev200+gbbbbbbb", "commit": "bbbbbbb"}
OLD = {"version": "0.9.1.dev100+gaaaaaaa", "commit": "aaaaaaa"}


def _now() -> datetime:
    return datetime.now(tz=UTC).replace(microsecond=0)


def _pack(
    event_log: SQLiteEventLog,
    stamp: Any,
    *,
    hours_ago: float,
    payload: dict[str, Any] | None = None,
    entity_id: str | None = None,
) -> None:
    """Append a pack row; ``stamp=None`` leaves the metadata without one."""
    metadata = {} if stamp is None else {WRITE_PROVENANCE_KEY: stamp}
    event_log.append(
        Event(
            event_type=EventType.PACK_ASSEMBLED,
            source="pack_builder",
            entity_id=entity_id,
            occurred_at=_now() - timedelta(hours=hours_ago),
            # Carries items by default, so a status assertion is about builds
            # and not about the injected-items coverage warning.
            payload=payload
            if payload is not None
            else {"injected_items": [{"item_id": "i"}]},
            metadata=metadata,
        )
    )


class TestSingleBuild:
    def test_one_build_is_listed_and_does_not_flag(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        for hours_ago in (5, 3, 1):
            _pack(event_log, NEW, hours_ago=hours_ago)

        report = summarize_backend_health(event_log, days=1)

        builds = report.serve.serving_builds
        assert builds.mixed_builds is False
        assert builds.packs_from_other_builds == 0
        assert builds.packs == 3
        assert [(g.version, g.commit, g.packs) for g in builds.groups] == [
            (NEW["version"], NEW["commit"], 3)
        ]
        assert builds.latest_build == f"{NEW['version']} @ {NEW['commit']}"
        assert not any("mixed builds" in reason for reason in report.reasons)
        assert report.status == "ok"

    def test_an_empty_window_has_no_groups_and_no_flag(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")

        builds = summarize_serve_attribution(event_log, days=1).serving_builds

        assert builds.groups == []
        assert builds.packs == 0
        assert builds.mixed_builds is False
        assert builds.latest_build is None


class TestTwoBuilds:
    def test_a_clean_redeploy_lists_both_builds_and_does_not_flag(
        self, tmp_path: Path
    ) -> None:
        # The old build's last pack precedes the new build's first: one build
        # replaced the other, so nothing is serving from the old one.
        event_log = SQLiteEventLog(tmp_path / "events.db")
        for hours_ago in (20, 10, 6):
            _pack(event_log, OLD, hours_ago=hours_ago)
        for hours_ago in (4, 1):
            _pack(event_log, NEW, hours_ago=hours_ago)

        report = summarize_backend_health(event_log, days=1)

        builds = report.serve.serving_builds
        assert builds.mixed_builds is False
        assert builds.packs_from_other_builds == 0
        assert builds.packs == 5
        # Newest last_seen first, and the latest is the new build.
        new, old = builds.groups
        assert (new.commit, new.packs) == (NEW["commit"], 2)
        assert (old.commit, old.packs) == (OLD["commit"], 3)
        assert old.last_seen - old.first_seen == timedelta(hours=14)
        assert new.last_seen - new.first_seen == timedelta(hours=3)
        assert new.first_seen > old.last_seen
        assert builds.latest_build == new.label
        assert not any("mixed builds" in reason for reason in report.reasons)
        assert report.status == "ok"

    def test_a_stale_session_serving_after_the_new_build_started_is_flagged(
        self, tmp_path: Path
    ) -> None:
        # The old build keeps assembling packs after the new build's first.
        event_log = SQLiteEventLog(tmp_path / "events.db")
        for hours_ago in (20, 10, 3, 2):
            _pack(event_log, OLD, hours_ago=hours_ago)
        for hours_ago in (4, 1):
            _pack(event_log, NEW, hours_ago=hours_ago)

        report = summarize_backend_health(event_log, days=1)

        builds = report.serve.serving_builds
        assert builds.mixed_builds is True
        assert builds.packs == 6
        # Only the old build's packs at or after the new build's first count:
        # the two before it are history, not concurrency.
        assert builds.packs_from_other_builds == 2
        assert builds.latest_build == f"{NEW['version']} @ {NEW['commit']}"
        assert report.status == "warn"
        (reason,) = [r for r in report.reasons if "mixed builds" in r]
        assert "2 of 6 pack(s)" in reason
        assert NEW["commit"] in reason
        assert "after it began" in reason

    def test_the_newest_build_is_the_one_that_started_last_not_the_busiest(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        for hours_ago in (20, 19, 18, 17, 16):
            _pack(event_log, OLD, hours_ago=hours_ago)
        _pack(event_log, NEW, hours_ago=1)

        builds = summarize_serve_attribution(event_log, days=1).serving_builds

        assert builds.latest_build == f"{NEW['version']} @ {NEW['commit']}"
        assert builds.mixed_builds is False
        assert builds.packs_from_other_builds == 0

    def test_the_newest_build_is_the_latest_first_pack_not_the_last_pack(
        self, tmp_path: Path
    ) -> None:
        # The old build serves before and after the new one started, and its
        # pack is the last in the window. Picking the build by last pack would
        # call the old build the newest, and the warning would name it.
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _pack(event_log, OLD, hours_ago=5)
        _pack(event_log, NEW, hours_ago=3)
        _pack(event_log, NEW, hours_ago=2)
        _pack(event_log, OLD, hours_ago=1)

        report = summarize_backend_health(event_log, days=1)

        builds = report.serve.serving_builds
        old = next(g for g in builds.groups if g.commit == OLD["commit"])
        new = next(g for g in builds.groups if g.commit == NEW["commit"])
        assert old.last_seen > new.last_seen
        assert builds.latest_build == new.label
        assert builds.groups[0].commit == NEW["commit"]
        assert builds.mixed_builds is True
        # Only the old build's pack at or after the new build's first counts.
        assert builds.packs_from_other_builds == 1
        (reason,) = [r for r in report.reasons if "mixed builds" in r]
        assert f"newest build ({new.label})" in reason
        assert "1 of 4 pack(s)" in reason

    def test_a_pack_at_the_instant_the_latest_build_began_counts_as_concurrent(
        self,
    ) -> None:
        began = _now() - timedelta(hours=2)

        def row(stamp: dict[str, str], when: datetime) -> Event:
            return Event(
                event_type=EventType.PACK_ASSEMBLED,
                source="pack_builder",
                occurred_at=when,
                metadata={WRITE_PROVENANCE_KEY: stamp},
            )

        builds = summarize_serving_builds(
            [row(OLD, began), row(NEW, began), row(NEW, began + timedelta(hours=1))]
        )

        assert builds.mixed_builds is True
        assert builds.packs_from_other_builds == 1

    def test_a_pack_outside_the_window_does_not_make_a_second_group(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _pack(event_log, OLD, hours_ago=24 * 10)
        _pack(event_log, NEW, hours_ago=1)

        builds = summarize_serve_attribution(event_log, days=1).serving_builds

        assert builds.mixed_builds is False
        assert [g.commit for g in builds.groups] == [NEW["commit"]]

    def test_same_commit_under_two_versions_is_two_builds(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _pack(event_log, {"version": "1.0", "commit": "abc"}, hours_ago=3)
        _pack(event_log, {"version": "1.1", "commit": "abc"}, hours_ago=2)
        _pack(event_log, {"version": "1.0", "commit": "abc"}, hours_ago=1)

        builds = summarize_serve_attribution(event_log, days=1).serving_builds

        assert sorted(g.version for g in builds.groups if g.version) == ["1.0", "1.1"]
        assert builds.mixed_builds is True
        assert builds.packs_from_other_builds == 1


class TestUnidentifiedBuilds:
    def test_an_interleaved_provenance_less_group_is_counted_and_flagged(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _pack(event_log, NEW, hours_ago=4)
        _pack(event_log, None, hours_ago=3)
        _pack(event_log, NEW, hours_ago=2)
        _pack(event_log, NEW, hours_ago=1)

        report = summarize_backend_health(event_log, days=1)

        builds = report.serve.serving_builds
        by_label = {g.label: g for g in builds.groups}
        assert set(by_label) == {
            f"{NEW['version']} @ {NEW['commit']}",
            NO_PROVENANCE_LABEL,
        }
        unstamped = by_label[NO_PROVENANCE_LABEL]
        assert (unstamped.version, unstamped.commit, unstamped.packs) == (None, None, 1)
        assert builds.packs == 4
        assert builds.mixed_builds is True
        # The unstamped group started last, so it is the newest build, and the
        # stamped build's two later packs are the concurrent ones.
        assert builds.latest_build == NO_PROVENANCE_LABEL
        assert builds.packs_from_other_builds == 2
        assert any("mixed builds" in reason for reason in report.reasons)

    def test_a_provenance_less_group_that_ended_first_is_listed_not_flagged(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _pack(event_log, None, hours_ago=4)
        _pack(event_log, None, hours_ago=3)
        _pack(event_log, NEW, hours_ago=2)
        _pack(event_log, NEW, hours_ago=1)

        report = summarize_backend_health(event_log, days=1)

        builds = report.serve.serving_builds
        # Never dropped, and never read as a second build serving at once.
        assert {g.label: g.packs for g in builds.groups} == {
            NO_PROVENANCE_LABEL: 2,
            f"{NEW['version']} @ {NEW['commit']}": 2,
        }
        assert builds.mixed_builds is False
        assert builds.packs_from_other_builds == 0
        assert not any("mixed builds" in reason for reason in report.reasons)

    def test_only_unstamped_packs_are_one_group_not_a_mix(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _pack(event_log, None, hours_ago=2)
        _pack(event_log, None, hours_ago=1)

        builds = summarize_serve_attribution(event_log, days=1).serving_builds

        assert [(g.label, g.packs) for g in builds.groups] == [(NO_PROVENANCE_LABEL, 2)]
        assert builds.mixed_builds is False

    def test_a_null_commit_is_not_folded_into_a_known_build(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        fallback_stamp = {"version": "fallback-version", "commit": None}
        _pack(event_log, fallback_stamp, hours_ago=3)
        _pack(event_log, NEW, hours_ago=2)
        _pack(event_log, fallback_stamp, hours_ago=1)

        builds = summarize_serve_attribution(event_log, days=1).serving_builds

        assert builds.mixed_builds is True
        assert builds.packs_from_other_builds == 1
        fallback = next(g for g in builds.groups if g.version == "fallback-version")
        assert fallback.commit is None
        assert fallback.label == "fallback-version @ commit unknown"
        assert fallback.packs == 2

    def test_a_malformed_stamp_reads_as_no_provenance_and_never_raises(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _pack(event_log, "not-a-mapping", hours_ago=4)
        _pack(event_log, ["list"], hours_ago=3)
        _pack(event_log, {"version": 7, "commit": ""}, hours_ago=2)
        _pack(event_log, {"commit": "   "}, hours_ago=1)

        builds = summarize_serve_attribution(event_log, days=1).serving_builds

        assert [(g.label, g.packs) for g in builds.groups] == [(NO_PROVENANCE_LABEL, 4)]

    def test_two_keys_that_share_a_label_are_two_builds_not_one(self) -> None:
        # (None, "x") and ("unknown version", "x") both print as
        # "unknown version @ x"; they are different builds all the same.
        assert build_label(None, "x") == build_label("unknown version", "x")
        base = _now() - timedelta(hours=3)

        def row(version: str | None, when: datetime) -> Event:
            return Event(
                event_type=EventType.PACK_ASSEMBLED,
                source="pack_builder",
                occurred_at=when,
                metadata={WRITE_PROVENANCE_KEY: {"version": version, "commit": "x"}},
            )

        builds = summarize_serving_builds(
            [
                row(None, base),
                row("unknown version", base + timedelta(hours=1)),
                row(None, base + timedelta(hours=2)),
            ]
        )

        assert len(builds.groups) == 2
        assert {(g.version, g.packs) for g in builds.groups} == {
            (None, 2),
            ("unknown version", 1),
        }
        assert builds.mixed_builds is True
        assert builds.packs_from_other_builds == 1

    def test_label_names_whichever_half_is_unknown(self) -> None:
        assert build_label(None, None) == NO_PROVENANCE_LABEL
        assert build_label("1.0", None) == "1.0 @ commit unknown"
        assert build_label(None, "abc") == "unknown version @ abc"
        assert build_label("1.0", "abc") == "1.0 @ abc"


class TestStaleStamp:
    def test_a_group_whose_stamp_the_probe_caught_says_so(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _pack(event_log, {**OLD, "stamp_stale": True}, hours_ago=2)
        _pack(event_log, NEW, hours_ago=1)

        builds = summarize_serve_attribution(event_log, days=1).serving_builds

        stale = {g.commit: g.stamp_stale for g in builds.groups}
        assert stale == {OLD["commit"]: True, NEW["commit"]: False}


class TestHoldout:
    def test_a_held_out_pack_is_not_counted_so_groups_sum_to_serve_packs(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _pack(event_log, NEW, hours_ago=3, entity_id="served-1")
        _pack(event_log, NEW, hours_ago=2, entity_id="served-2")
        _pack(
            event_log,
            OLD,
            hours_ago=1,
            entity_id="withheld-1",
            payload={HOLDOUT_KEY: True},
        )

        serve = summarize_serve_attribution(event_log, days=1)

        assert serve.packs == 2
        assert serve.serving_builds.packs == serve.packs
        assert [g.commit for g in serve.serving_builds.groups] == [NEW["commit"]]
        assert serve.serving_builds.mixed_builds is False


class TestTimestamps:
    def test_naive_and_aware_rows_in_one_window_are_compared_as_utc(self) -> None:
        # SQLite hands back naive timestamps and Postgres aware ones, so a
        # window can mix them; a zone other than UTC must also land on UTC.
        base = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
        plus_five = timezone(timedelta(hours=5))

        def row(stamp: dict[str, str], when: datetime) -> Event:
            return Event(
                event_type=EventType.PACK_ASSEMBLED,
                source="pack_builder",
                occurred_at=when,
                metadata={WRITE_PROVENANCE_KEY: stamp},
            )

        builds = summarize_serving_builds(
            [
                row(OLD, base),
                row(NEW, (base + timedelta(hours=1)).replace(tzinfo=None)),
                row(OLD, (base + timedelta(hours=2)).astimezone(plus_five)),
                row(NEW, base + timedelta(hours=3)),
            ]
        )

        assert builds.mixed_builds is True
        assert builds.packs_from_other_builds == 1
        new, old = builds.groups
        assert new.commit == NEW["commit"]
        assert new.first_seen == base + timedelta(hours=1)
        assert old.last_seen == base + timedelta(hours=2)
        for group in builds.groups:
            assert group.first_seen.utcoffset() == timedelta(0)
            assert group.last_seen.utcoffset() == timedelta(0)
        reason = mixed_builds_reason(builds, days=1)
        assert reason is not None
        assert "2026-03-01 13:00 UTC" in reason


class TestSerialisation:
    def test_the_section_round_trips_through_json(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _pack(event_log, OLD, hours_ago=3)
        _pack(event_log, NEW, hours_ago=2)
        _pack(event_log, OLD, hours_ago=1.5)
        _pack(event_log, NEW, hours_ago=1)

        report = summarize_backend_health(event_log, days=1)
        dumped = json.loads(json.dumps(report.model_dump(mode="json")))

        section = dumped["serve"]["serving_builds"]
        assert section["mixed_builds"] is True
        assert section["packs_from_other_builds"] == 1
        assert [g["commit"] for g in section["groups"]] == [
            NEW["commit"],
            OLD["commit"],
        ]
        assert set(section["groups"][0]) >= {
            "version",
            "commit",
            "label",
            "packs",
            "first_seen",
            "last_seen",
        }

    def test_summarize_accepts_any_iterable_of_rows(self) -> None:
        assert summarize_serving_builds(iter(())).groups == []
