"""``trellis-session-capture --rejoin-since``: rewrite only the stale pack joins.

The back-fill for joins written before a payload key (``retrieval_timing``,
``covariates``) existed. It parses transcripts the sweep already watermarked,
so the contract is as much about what it must *not* do as what it does: no
judge, no distillation, no memory write, no sweep event, no watermark change,
and a write only where the join differs from the session's latest.

Fully synthetic fixtures; real SQLite stores under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog

from tests.structlog_isolation import clear_cached_logger_proxies
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_workers.session_capture import __main__ as capture_main
from trellis_workers.session_capture import capture, sweep
from trellis_workers.session_capture.capture import rejoin_sessions, run_capture

from .conftest import (
    PACK_IDS,
    FakeLLMClient,
    candidates_json,
    delegating_session,
    write_transcript,
)
from .test_capture import _pack_session, _registry

A, B, C = PACK_IDS[:3]

#: Far enough back that every fixture file is at or after it.
LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)

#: The keys a join written before the timing and covariates existed lacks.
_LATER_KEYS = ("outcome", "retrieval_timing", "covariates")


@pytest.fixture(autouse=True)
def _isolate_structlog():
    """Undo the structlog reconfiguration ``main()`` performs (see test_main)."""
    yield
    clear_cached_logger_proxies()
    structlog.reset_defaults()


def _events(registry: MagicMock, event_type: EventType) -> list:
    return registry.operational.event_log.get_events(
        event_type=event_type, order="asc", limit=1000
    )


def _joins_of(registry: MagicMock, session_id: str) -> list[dict]:
    return [
        dict(event.payload)
        for event in _events(registry, EventType.CAPTURE_SESSION_PACKS)
        if event.entity_id == session_id
    ]


def _event_types(registry: MagicMock) -> set[EventType]:
    return {
        event.event_type
        for event in registry.operational.event_log.get_events(limit=1000)
    }


def _set_mtime(path: Path, epoch: float) -> None:
    os.utime(path, (epoch, epoch))


def _sweep(registry: MagicMock, root: Path, watermark: Path) -> None:
    """A normal sweep, which writes the current joins and the watermark."""
    run_capture(
        registry,
        transcripts_root=root,
        watermark_path=watermark,
        llm_client=FakeLLMClient([candidates_json()]),
        sample_denominator=10_000,
    )


def _make_stale(registry: MagicMock, session_id: str) -> None:
    """Append a join as an older build wrote it: no outcome, timing or covariates.

    Appended rather than edited because the event log is append-only, and the
    rejoin compares against the *latest* join, which this becomes.
    """
    latest = _joins_of(registry, session_id)[-1]
    for key in _LATER_KEYS:
        latest.pop(key)
    registry.operational.event_log.emit(
        EventType.CAPTURE_SESSION_PACKS,
        source="test",
        entity_id=session_id,
        entity_type="capture_session",
        payload=latest,
    )


def _make_count_stale(registry: MagicMock, session_id: str) -> None:
    """Append a join as the build before ``memory_tool_calls`` wrote it.

    Every other key, the outcome's own included, is the current one: only the
    count is missing, so the join differs from a new parse by that key alone.
    """
    latest = _joins_of(registry, session_id)[-1]
    latest["outcome"] = {
        key: value
        for key, value in latest["outcome"].items()
        if key != "memory_tool_calls"
    }
    registry.operational.event_log.emit(
        EventType.CAPTURE_SESSION_PACKS,
        source="test",
        entity_id=session_id,
        entity_type="capture_session",
        payload=latest,
    )


def _fixture(tmp_path: Path) -> tuple[MagicMock, Path, Path]:
    """Three pack-serving sessions, swept, two of them then made stale.

    ``sess-stale-1`` and ``sess-stale-2`` carry a legacy latest join,
    ``sess-current`` carries a current one, and ``sess-fresh`` was written
    after the sweep so the watermark has never seen it.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    registry = _registry(tmp_path)
    root = tmp_path / "projects"
    for name, packs in (
        ("sess-stale-1", (("get_context", A),)),
        ("sess-stale-2", (("search", B), ("get_context", C))),
        ("sess-current", (("get_context", A),)),
    ):
        _pack_session(root / "proj" / f"{name}.jsonl", name, *packs)
    watermark = tmp_path / "wm.json"
    _sweep(registry, root, watermark)
    _make_stale(registry, "sess-stale-1")
    _make_stale(registry, "sess-stale-2")
    _pack_session(root / "proj" / "sess-fresh.jsonl", "sess-fresh", ("get_context", B))
    return registry, root, watermark


def _rejoin(registry: MagicMock, root: Path, **kwargs) -> capture.RejoinReport:
    kwargs.setdefault("since", LONG_AGO)
    return rejoin_sessions(registry, transcripts_root=root, **kwargs)


class TestRewritesOnlyStaleJoins:
    def test_a_stale_join_gets_exactly_one_new_join_with_the_later_keys(
        self, tmp_path: Path
    ) -> None:
        registry, root, _ = _fixture(tmp_path)
        before = _joins_of(registry, "sess-stale-1")
        assert all(key not in before[-1] for key in _LATER_KEYS)

        report = _rejoin(registry, root)

        after = _joins_of(registry, "sess-stale-1")
        assert len(after) == len(before) + 1
        assert all(key in after[-1] for key in _LATER_KEYS)
        assert after[-1]["pack_ids"] == before[-1]["pack_ids"]
        assert report.pack_joins_recorded == 3  # two stale, one never joined
        assert len(_joins_of(registry, "sess-stale-2")) == 3

    def test_a_join_that_already_matches_is_not_written_again(
        self, tmp_path: Path
    ) -> None:
        registry, root, _ = _fixture(tmp_path)
        before = _joins_of(registry, "sess-current")

        report = _rejoin(registry, root)

        assert _joins_of(registry, "sess-current") == before
        assert report.pack_joins_unchanged == 1

    def test_a_second_rejoin_writes_nothing(self, tmp_path: Path) -> None:
        registry, root, _ = _fixture(tmp_path)
        _rejoin(registry, root)
        joins = len(_events(registry, EventType.CAPTURE_SESSION_PACKS))

        second = _rejoin(registry, root)

        assert len(_events(registry, EventType.CAPTURE_SESSION_PACKS)) == joins
        assert second.pack_joins_recorded == 0
        assert second.pack_joins_unchanged == second.sessions_scanned == 4

    def test_a_transcript_older_than_since_is_not_read(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        registry, root, _ = _fixture(tmp_path)
        old = root / "proj" / "sess-stale-1.jsonl"
        _set_mtime(old, 1_000_000_000)
        for path in root.glob("proj/*.jsonl"):
            if path != old:
                _set_mtime(path, 1_800_000_000)
        parsed: list[str] = []
        real_parse = capture.parse_session
        monkeypatch.setattr(
            capture,
            "parse_session",
            lambda path: parsed.append(path.stem) or real_parse(path),
        )

        report = _rejoin(
            registry, root, since=datetime.fromtimestamp(1_700_000_000, tz=UTC)
        )

        assert "sess-stale-1" not in parsed
        assert sorted(parsed) == ["sess-current", "sess-fresh", "sess-stale-2"]
        assert report.sessions_scanned == 3
        assert len(_joins_of(registry, "sess-stale-1")) == 2  # still the stale one

    def test_the_cutoff_is_inclusive(self, tmp_path: Path) -> None:
        registry, root, _ = _fixture(tmp_path)
        for path in root.glob("proj/*.jsonl"):
            _set_mtime(path, 1_800_000_000)

        report = _rejoin(
            registry, root, since=datetime.fromtimestamp(1_800_000_000, tz=UTC)
        )

        assert report.sessions_scanned == 4

    def test_selection_ignores_the_watermark(self, tmp_path: Path) -> None:
        registry, root, watermark = _fixture(tmp_path)
        # The sweep has watermarked the stale sessions, so a sweep would skip them.
        assert watermark.exists()
        before = _sweep_skips(registry, root, watermark)
        assert before >= 3

        report = _rejoin(registry, root)

        assert report.pack_joins_recorded >= 2


def _sweep_skips(registry: MagicMock, root: Path, watermark: Path) -> int:
    """How many transcripts the sweep itself would skip as unchanged."""
    return run_capture(
        registry,
        transcripts_root=root,
        watermark_path=watermark,
        llm_client=FakeLLMClient([candidates_json()]),
        sample_denominator=10_000,
        dry_run=True,
    ).sessions_skipped_watermark


class TestDoesNothingElse:
    def test_no_judge_no_distil_no_memory_no_sweep_event_no_watermark(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        registry, root, watermark = _fixture(tmp_path)
        before_bytes = watermark.read_bytes()
        before_docs = registry.knowledge.document_store.count()
        before_types = _event_types(registry)
        before_sweeps = len(_events(registry, EventType.CAPTURE_SWEEP_COMPLETED))

        def refuse(*_a, **_kw):
            msg = "a rejoin must not run this"
            raise AssertionError(msg)

        monkeypatch.setenv(sweep.ENV_ROOT, str(root))
        monkeypatch.setenv(sweep.ENV_WATERMARK, str(watermark))
        monkeypatch.setattr(sweep, "build_judge_client", refuse)
        spies = {
            name: MagicMock(side_effect=refuse)
            for name in ("distill_session", "_capture_session", "_write_records")
        }
        monkeypatch.setattr(
            capture.distill, "distill_session", spies["distill_session"]
        )
        monkeypatch.setattr(capture, "_capture_session", spies["_capture_session"])
        monkeypatch.setattr(capture, "_write_records", spies["_write_records"])
        monkeypatch.setattr(capture, "_emit_sweep_completed", refuse)
        monkeypatch.setattr(capture, "_emit_training_pairs", refuse)
        monkeypatch.setattr(
            StoreRegistry, "from_config_dir", staticmethod(lambda: registry)
        )

        report = sweep.run_rejoin(since=LONG_AGO)

        assert report.sessions_errored == 0
        assert report.pack_joins_recorded == 3
        assert all(not spy.called for spy in spies.values())
        assert watermark.read_bytes() == before_bytes
        assert registry.knowledge.document_store.count() == before_docs
        # The fixture's own sweep emitted one; the rejoin adds none.
        assert before_sweeps == 1
        assert len(_events(registry, EventType.CAPTURE_SWEEP_COMPLETED)) == 1
        assert _event_types(registry) == before_types

    def test_a_missing_watermark_file_is_not_created(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        root = tmp_path / "projects"
        _pack_session(root / "proj" / "sess-a.jsonl", "sess-a", ("get_context", A))
        before = {p.name for p in tmp_path.rglob("*")}

        report = _rejoin(registry, root)

        assert report.pack_joins_recorded == 1
        assert {p.name for p in tmp_path.rglob("*")} - before == set()
        assert not (tmp_path / "wm.json").exists()


class TestDryRun:
    def test_writes_nothing_and_reports_the_live_counts(self, tmp_path: Path) -> None:
        live_registry, live_root, _ = _fixture(tmp_path / "live")
        dry_registry, dry_root, _ = _fixture(tmp_path / "dry")
        dry_before = len(_events(dry_registry, EventType.CAPTURE_SESSION_PACKS))

        dry = _rejoin(dry_registry, dry_root, dry_run=True)
        live = _rejoin(live_registry, live_root)

        assert len(_events(dry_registry, EventType.CAPTURE_SESSION_PACKS)) == dry_before
        assert dry.dry_run is True
        counts = ("sessions_scanned", "pack_joins_recorded", "pack_joins_unchanged")
        assert [getattr(dry, key) for key in counts] == [
            getattr(live, key) for key in counts
        ]
        assert dry.pack_joins_recorded == 3


def _count_fixture(tmp_path: Path) -> tuple[MagicMock, Path]:
    """Four swept sessions whose latest joins differ from a new parse by
    ``memory_tool_calls`` alone, in three of them; the fourth is current.

    ``sess-two`` made two Trellis calls and one other call, ``sess-none``
    made one other call and no Trellis call, ``sess-silent`` made no call at
    all, and ``sess-current`` made one Trellis call and keeps its join.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    registry = _registry(tmp_path)
    root = tmp_path / "projects"
    _pack_session(
        root / "proj" / "sess-two.jsonl",
        "sess-two",
        ("get_context", A),
        ("get_items", B),
        error=True,
    )
    _pack_session(root / "proj" / "sess-none.jsonl", "sess-none", error=True)
    _pack_session(root / "proj" / "sess-silent.jsonl", "sess-silent")
    _pack_session(
        root / "proj" / "sess-current.jsonl", "sess-current", ("get_context", C)
    )
    _sweep(registry, root, tmp_path / "wm.json")
    for name in ("sess-two", "sess-none", "sess-silent"):
        _make_count_stale(registry, name)
    return registry, root


class TestBackfillsTheMemoryToolCount:
    """A join whose outcome predates ``memory_tool_calls`` is rewritten with it.

    Selection is unchanged: the rejoin compares the whole payload with the
    session's latest, so a join missing one key of its outcome differs from
    every new parse and is written again, from the transcript as it stands.
    """

    def test_the_old_join_is_absent_the_new_one_is_filled_from_the_transcript(
        self, tmp_path: Path
    ) -> None:
        registry, root = _count_fixture(tmp_path)
        for name in ("sess-two", "sess-none", "sess-silent"):
            old = _joins_of(registry, name)[-1]["outcome"]
            assert "memory_tool_calls" not in old
            assert old.get("memory_tool_calls") is None
            assert len(_joins_of(registry, name)) == 2

        _rejoin(registry, root)

        for name, count in (("sess-two", 2), ("sess-none", 0), ("sess-silent", 0)):
            joins = _joins_of(registry, name)
            assert len(joins) == 3
            assert "memory_tool_calls" not in joins[1]["outcome"]
            assert joins[2]["outcome"]["memory_tool_calls"] == count
            assert type(joins[2]["outcome"]["memory_tool_calls"]) is int
            # The rewrite changes nothing else about the join.
            rest = {
                k: v for k, v in joins[2]["outcome"].items() if k != "memory_tool_calls"
            }
            assert rest == joins[1]["outcome"]
            assert {k: v for k, v in joins[2].items() if k != "outcome"} == {
                k: v for k, v in joins[1].items() if k != "outcome"
            }

    def test_only_the_joins_lacking_the_key_are_selected(self, tmp_path: Path) -> None:
        registry, root = _count_fixture(tmp_path)
        current_before = _joins_of(registry, "sess-current")

        report = _rejoin(registry, root)

        assert _joins_of(registry, "sess-current") == current_before
        assert report.sessions_scanned == 4
        assert report.pack_joins_recorded == 3
        assert report.pack_joins_unchanged == 1

    def test_a_second_rejoin_writes_nothing(self, tmp_path: Path) -> None:
        registry, root = _count_fixture(tmp_path)
        _rejoin(registry, root)
        joins = len(_events(registry, EventType.CAPTURE_SESSION_PACKS))

        second = _rejoin(registry, root)

        assert len(_events(registry, EventType.CAPTURE_SESSION_PACKS)) == joins
        assert second.pack_joins_recorded == 0
        assert second.pack_joins_unchanged == second.sessions_scanned == 4

    def test_the_dry_run_selects_what_the_live_run_writes(self, tmp_path: Path) -> None:
        live_registry, live_root = _count_fixture(tmp_path / "live")
        dry_registry, dry_root = _count_fixture(tmp_path / "dry")
        before = _events(dry_registry, EventType.CAPTURE_SESSION_PACKS)

        dry = _rejoin(dry_registry, dry_root, dry_run=True)
        live = _rejoin(live_registry, live_root)

        assert _events(dry_registry, EventType.CAPTURE_SESSION_PACKS) == before
        assert dry.dry_run is True
        assert {**dry.to_payload(), "dry_run": False, "transcripts_root": ""} == {
            **live.to_payload(),
            "transcripts_root": "",
        }
        assert dry.pack_joins_recorded == 3

    def test_the_cutoff_still_decides_which_transcripts_are_read(
        self, tmp_path: Path
    ) -> None:
        registry, root = _count_fixture(tmp_path)
        old = root / "proj" / "sess-two.jsonl"
        _set_mtime(old, 1_000_000_000)
        for path in root.glob("proj/*.jsonl"):
            if path != old:
                _set_mtime(path, 1_800_000_000)

        report = _rejoin(
            registry, root, since=datetime.fromtimestamp(1_700_000_000, tz=UTC)
        )

        assert report.pack_joins_recorded == 2
        assert len(_joins_of(registry, "sess-two")) == 2
        assert len(_joins_of(registry, "sess-none")) == 3


class TestAsTheSweepParses:
    def test_a_rejoin_writes_the_join_the_sweep_would(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Parent/sub-agent resolution and source_system are the sweep's own."""
        monkeypatch.setenv(sweep.ENV_SOURCE_SYSTEM, "other-source")

        def build(name: str) -> tuple[MagicMock, Path]:
            (tmp_path / name).mkdir()
            registry = _registry(tmp_path / name)
            root = tmp_path / name / "projects"
            parent, child = delegating_session("sess-parent-0001")
            write_transcript(root / "proj" / "sess-parent-0001.jsonl", parent)
            write_transcript(
                root / "proj" / "sess-parent-0001" / "subagents" / "agent-c1.jsonl",
                child,
            )
            return registry, root

        swept, swept_root = build("swept")
        run_capture(
            swept,
            transcripts_root=swept_root,
            watermark_path=tmp_path / "swept" / "wm.json",
            llm_client=FakeLLMClient([candidates_json()]),
            source_system="other-source",
            sample_denominator=10_000,
        )
        rejoined, rejoined_root = build("rejoined")
        monkeypatch.setenv(sweep.ENV_ROOT, str(rejoined_root))

        report = sweep.run_rejoin(since=LONG_AGO, registry=rejoined)

        assert report.pack_joins_recorded == 2
        for session_id in ("sess-parent-0001", "agent-c1"):
            assert _joins_of(rejoined, session_id) == _joins_of(swept, session_id)
        child = _joins_of(rejoined, "agent-c1")[0]
        assert child["parent_session_id"] == "sess-parent-0001"
        assert child["source_system"] == "other-source"


class TestCounts:
    def test_every_scanned_session_is_accounted_for(self, tmp_path: Path) -> None:
        registry, root, _ = _fixture(tmp_path)
        write_transcript(root / "proj" / "sess-blank.jsonl", [])
        _pack_session(
            root / "-tmp-scratch" / "sess-tmp.jsonl", "sess-tmp", ("get_context", A)
        )

        report = _rejoin(registry, root)

        assert report.sessions_skipped_ephemeral == 1
        assert report.sessions_skipped_empty == 1
        assert report.sessions_scanned == 5
        assert (
            report.pack_joins_recorded
            + report.pack_joins_unchanged
            + report.sessions_skipped_empty
            + report.sessions_errored
            == report.sessions_scanned
        )
        assert _joins_of(registry, "sess-tmp") == []
        assert _joins_of(registry, "sess-blank") == []

    def test_a_transcript_with_undecodable_bytes_is_counted_and_the_rest_rejoin(
        self, tmp_path: Path
    ) -> None:
        registry, root, _ = _fixture(tmp_path)
        (root / "proj" / "sess-broken.jsonl").write_bytes(b"\xff\xfe\x00 not utf-8\n")

        report = _rejoin(registry, root)

        assert report.sessions_errored == 1
        assert report.sessions_scanned == 5
        assert report.pack_joins_recorded == 3
        assert all(
            key in _joins_of(registry, "sess-stale-1")[-1] for key in _LATER_KEYS
        )


class TestEntryPoint:
    @pytest.fixture
    def env(self, tmp_path: Path, monkeypatch):
        registry, root, watermark = _fixture(tmp_path)
        monkeypatch.setenv(sweep.ENV_ROOT, str(root))
        monkeypatch.setenv(sweep.ENV_WATERMARK, str(watermark))
        monkeypatch.setattr(
            StoreRegistry, "from_config_dir", staticmethod(lambda: registry)
        )
        monkeypatch.setattr(
            sweep,
            "build_judge_client",
            MagicMock(side_effect=AssertionError("no judge for a rejoin")),
        )
        return registry, root

    def test_prints_the_rejoin_payload_and_exits_zero(self, env, capsys) -> None:
        code = capture_main.main(["--rejoin-since", "2000-01-01T00:00:00Z"])

        assert code == capture_main.EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "rejoin"
        assert payload["since"] == "2000-01-01T00:00:00+00:00"
        assert payload["pack_joins_recorded"] == 3
        assert payload["pack_joins_unchanged"] == 1
        assert payload["sessions_scanned"] == 4
        assert payload["sessions_errored"] == 0
        assert payload["dry_run"] is False
        for key in ("sessions_skipped_empty", "sessions_skipped_ephemeral"):
            assert key in payload

    def test_the_report_names_the_source_system_it_wrote_under(
        self, env, capsys, monkeypatch
    ) -> None:
        # The source comes from the caller's shell, so a dry run must show it
        # before a live run rewrites every join under the wrong one.
        monkeypatch.setenv(sweep.ENV_SOURCE_SYSTEM, "other-source")

        code = capture_main.main(
            ["--rejoin-since", "2000-01-01T00:00:00Z", "--dry-run"]
        )

        assert code == capture_main.EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["source_system"] == "other-source"
        assert payload["transcripts_root"] == str(env[1])

    def test_dry_run_flag_reaches_the_rejoin(self, env, capsys) -> None:
        registry, _ = env
        before = len(_events(registry, EventType.CAPTURE_SESSION_PACKS))

        code = capture_main.main(
            ["--rejoin-since", "2000-01-01T00:00:00+00:00", "--dry-run"]
        )

        assert code == capture_main.EXIT_OK
        assert len(_events(registry, EventType.CAPTURE_SESSION_PACKS)) == before
        assert '"pack_joins_recorded": 3' in capsys.readouterr().out

    def test_a_session_error_exits_three_even_with_strict_off(
        self, env, capsys, monkeypatch
    ) -> None:
        _, root = env
        monkeypatch.setenv(sweep.ENV_STRICT, "0")
        (root / "proj" / "sess-broken.jsonl").write_bytes(b"\xff\xfe\x00 bad\n")

        code = capture_main.main(["--rejoin-since", "2000-01-01T00:00:00Z"])

        assert code == capture_main.EXIT_SESSIONS_ERRORED == 3
        assert '"sessions_errored": 1' in capsys.readouterr().out

    @pytest.mark.parametrize(
        "value", ["2026-10-01T00:00:00", "2026-10-01", "yesterday", ""]
    )
    def test_a_naive_or_unparseable_instant_is_a_usage_error(
        self, value: str, capsys
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            capture_main.main(["--rejoin-since", value])

        assert exc.value.code == 2
        assert "--rejoin-since" in capsys.readouterr().err

    def test_help_states_the_holdout_side_effects(self, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            capture_main.main(["--help"])

        assert exc.value.code == 0
        flat = " ".join(capsys.readouterr().out.split())
        assert "unfinished" in flat
        assert "re-dates" in flat
        assert "never backdated" in flat
