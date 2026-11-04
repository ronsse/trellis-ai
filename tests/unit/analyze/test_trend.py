"""The weekly trend report: composed readers, build splits, and the sealed outcome.

The series is built from the readers that already own each figure, windowed to
one ISO week each. These tests seed a real SQLite event log across three weeks
and three builds (``_trend_seed``) and assert the per-week value of every
metric family, the build boundaries, the partial-week flag, the session-less
follow-up count and a precedent-served zero and nonzero. They also pin what the
module must never do: read the holdout's outcome, or read events around the
join that drops withheld packs.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.integration._live_server import repo_src_pythonpath
from tests.unit.analyze import _trend_seed as seed_mod
from trellis.analyze import trend as trend_module
from trellis.analyze.trend import (
    OMITTED_METRICS,
    PROVENANCE_EDGE_KINDS,
    STATUS_OK,
    STATUS_TRUNCATED,
    TREND_MAX_WEEKS,
    TrendError,
    TrendReport,
    parse_until,
    render_markdown,
    summarize_trend,
    week_bounds,
)
from trellis.learning.judged_outcomes import summarize_judged_outcomes
from trellis.ops.retrieval_adoption import summarize_retrieval_adoption
from trellis.ops.write_health import summarize_serve_attribution, summarize_write_health
from trellis.retrieve.pack_value import summarize_pack_value
from trellis.schemas import well_known
from trellis.stores.base.event_log import resolve_scan_window
from trellis.stores.registry import StoreRegistry
from trellis.stores.sqlite.event_log import SQLiteEventLog

UNTIL = seed_mod.UNTIL


@pytest.fixture
def log(tmp_path: Path) -> SQLiteEventLog:
    event_log = SQLiteEventLog(tmp_path / "events.db")
    seed_mod.seed(event_log)
    return event_log


@pytest.fixture
def report(log: SQLiteEventLog) -> TrendReport:
    return summarize_trend(log, weeks=3, until=UNTIL)


def _week(report: TrendReport, label: str) -> Any:
    return next(week for week in report.weeks if week.week == label)


class TestWeekBounds:
    def test_three_iso_weeks_oldest_first_with_partial_last(
        self, report: TrendReport
    ) -> None:
        assert [week.week for week in report.weeks] == [
            "2026-W43",
            "2026-W44",
            "2026-W45",
        ]
        assert [week.partial for week in report.weeks] == [False, False, True]
        assert report.weeks[0].start == seed_mod.W43
        assert report.weeks[1].start == seed_mod.W44
        assert report.weeks[2].start == seed_mod.W45
        assert report.weeks[2].end == UNTIL
        assert report.window.weeks == 3
        assert report.window.start == seed_mod.W43
        assert report.window.end == UNTIL

    def test_a_window_ending_on_a_monday_closes_the_previous_week(self) -> None:
        rows = week_bounds(2, seed_mod.W45)

        assert [label for label, *_ in rows] == ["2026-W43", "2026-W44"]
        assert [partial for *_, partial in rows] == [False, False]
        assert rows[-1][2] == seed_mod.W45

    def test_an_event_on_a_week_boundary_counts_once_in_the_later_week(
        self, tmp_path: Path
    ) -> None:
        # The event log's ``until`` is inclusive, so a week read up to the
        # next week's ``start`` would count a Monday-00:00 event twice.
        log = SQLiteEventLog(tmp_path / "events.db")
        seed_mod.pack(log, "p-edge", seed_mod.W44, None, [("doc:edge", 10)])
        seed_mod.feedback(log, "p-edge", seed_mod.W44, helpful=("doc:edge",))
        seed_mod.pack(log, "p-at-until", UNTIL, None, [("doc:late", 10)])

        report = summarize_trend(log, weeks=3, until=UNTIL)

        assert [week.packs for week in report.weeks] == [0, 1, 0]
        assert [week.serve_attribution.packs for week in report.weeks] == [0, 1, 0]

    def test_the_iso_year_boundary_labels_by_iso_year(self) -> None:
        rows = week_bounds(2, datetime(2026, 1, 3, tzinfo=UTC))

        assert [label for label, *_ in rows] == ["2025-W52", "2026-W01"]

    @pytest.mark.parametrize("weeks", [0, -1, TREND_MAX_WEEKS + 1])
    def test_weeks_out_of_bounds_are_refused(
        self, log: SQLiteEventLog, weeks: int
    ) -> None:
        with pytest.raises(TrendError, match="weeks must be between"):
            summarize_trend(log, weeks=weeks, until=UNTIL)

    @pytest.mark.parametrize("weeks", [1, TREND_MAX_WEEKS])
    def test_weeks_bounds_are_inclusive(self, log: SQLiteEventLog, weeks: int) -> None:
        assert len(summarize_trend(log, weeks=weeks, until=UNTIL).weeks) == weeks

    def test_until_parses_a_date_a_naive_time_and_an_aware_time(self) -> None:
        assert parse_until("2026-11-04") == datetime(2026, 11, 4, tzinfo=UTC)
        assert parse_until("2026-11-04T12:00:00") == UNTIL
        assert parse_until("2026-11-04T14:00:00+02:00") == UNTIL
        assert parse_until(None) is None
        with pytest.raises(TrendError, match="until must be"):
            parse_until("last tuesday")


class TestMetricFamilies:
    def test_first_week(self, report: TrendReport) -> None:
        week = _week(report, "2026-W43")

        assert week.packs == 2
        assert week.commits == ["aaaaaaa"]
        # One attributed pack is below the reader's floor: refused, not zero.
        assert week.pack_value.packs == 2
        assert week.pack_value.attributed_packs == 1
        assert week.pack_value.suppressed is True
        assert week.pack_value.useful_token_fraction is None
        assert week.serve_attribution.packs == 2
        assert week.serve_attribution.injected_coverage == 1.0
        assert week.serve_attribution.pack_targeted_feedback == 1
        assert week.serve_attribution.pack_attribution_rate == 1.0
        assert week.judged_outcomes.judged == 1
        assert week.judged_outcomes.cited_helpful == 1
        assert week.judged_outcomes.cited_share == 1.0
        # 3 accepted capture writes + 1 rejected, plus one accepted feedback
        # call that is a write surface but not a capture one.
        assert week.write_health.attempts == 5
        assert week.write_health.rejected == 1
        assert week.write_health.rejection_rate == 0.2
        assert week.write_health.capture_attempts == 4
        assert week.write_health.capture_rejected == 1
        assert week.write_health.capture_rejection_rate == 0.25
        # Three sessions is below the adoption reader's floor: counts, no rate.
        assert week.retrieval_adoption.sessions == 3
        assert week.retrieval_adoption.sessions_with_packs == 2
        assert week.retrieval_adoption.adoption is None
        assert week.retrieval_adoption.state == "insufficient_data"
        assert week.follow_up.sessions == 1
        assert week.follow_up.sessions_with_follow_up == 1
        assert week.follow_up.follow_up_rate == 1.0
        assert week.follow_up.packs_without_session == 0
        assert week.follow_up.retrieval_phase == {"mid_task": 1, "pre_work": 1}
        assert week.precedents.packs_with_precedent == 1
        assert week.precedents.precedent_items_served == 1
        assert week.precedents.precedent_items_helpful == 0
        assert week.precedents.helpful_share == 0.0

    def test_second_week_spans_a_build_change(self, report: TrendReport) -> None:
        week = _week(report, "2026-W44")

        assert week.packs == 6
        assert week.commits == ["aaaaaaa", "bbbbbbb"]
        assert week.pack_value.attributed_packs == 6
        assert week.pack_value.suppressed is False
        assert week.pack_value.injected_tokens == 1100
        assert week.pack_value.useful_token_fraction == pytest.approx(
            600 / 1100, abs=1e-3
        )
        assert week.pack_value.unhelpful_token_fraction == pytest.approx(
            500 / 1100, abs=1e-3
        )
        assert week.serve_attribution.packs == 6
        assert week.serve_attribution.pack_attribution_rate == 1.0
        # A judged memory nothing served: judged, not cited.
        assert week.judged_outcomes.judged == 1
        assert week.judged_outcomes.never_served == 1
        assert week.judged_outcomes.cited_share == 0.0
        # No writes at all: no rate, not a rate of zero.
        assert week.write_health.attempts == 0
        assert week.write_health.rejection_rate is None
        assert week.write_health.capture_rejection_rate is None
        assert week.retrieval_adoption.sessions == 10
        assert week.retrieval_adoption.sessions_with_packs == 4
        assert week.retrieval_adoption.adoption == pytest.approx(0.4)
        assert week.retrieval_adoption.state == "warn"
        assert week.follow_up.sessions == 5
        assert week.follow_up.sessions_with_follow_up == 0
        assert week.follow_up.follow_up_rate == 0.0
        # The session-less pack is counted on its own, never dropped.
        assert week.follow_up.packs_without_session == 1
        assert week.follow_up.retrieval_phase == {"pre_work": 5, "unset": 1}
        assert week.precedents.precedent_items_served == 1
        assert week.precedents.precedent_items_helpful == 1
        assert week.precedents.helpful_share == 1.0

    def test_partial_week_is_marked_and_a_precedent_zero_is_a_zero(
        self, report: TrendReport
    ) -> None:
        week = _week(report, "2026-W45")

        assert week.partial is True
        assert week.packs == 1
        assert week.commits == [None]
        assert week.follow_up.sessions == 1
        assert week.follow_up.sessions_with_follow_up == 0
        assert week.follow_up.retrieval_phase == {"unset": 1}
        # Packs were served and none carried a precedent: 0, and the share
        # has nothing to divide, so it alone is null.
        assert week.precedents.packs == 1
        assert week.precedents.packs_with_precedent == 0
        assert week.precedents.precedent_items_served == 0
        assert week.precedents.precedent_items_helpful == 0
        assert week.precedents.helpful_share is None
        assert week.judged_outcomes.judged == 0
        assert week.judged_outcomes.cited_share is None
        assert week.retrieval_adoption.sessions == 0

    def test_an_empty_log_is_a_full_series_of_nothing(self, tmp_path: Path) -> None:
        empty = SQLiteEventLog(tmp_path / "empty.db")

        result = summarize_trend(empty, weeks=4, until=UNTIL)

        assert len(result.weeks) == 4
        assert result.builds == []
        assert result.status == STATUS_OK
        assert all(week.packs == 0 and not week.truncated for week in result.weeks)
        assert all(week.follow_up.follow_up_rate is None for week in result.weeks)

    def test_loop_health_is_omitted_with_a_reason(self, report: TrendReport) -> None:
        assert [(o.metric, o.reason) for o in report.omitted] == list(OMITTED_METRICS)
        assert report.omitted[0].metric == "loop_health"
        assert "window" in report.omitted[0].reason


class TestBuilds:
    def test_build_boundaries_across_the_window(self, report: TrendReport) -> None:
        by_commit = {build.commit: build for build in report.builds}

        assert [build.commit for build in report.builds] == ["aaaaaaa", "bbbbbbb", None]
        old, new, bare = (by_commit[c] for c in ("aaaaaaa", "bbbbbbb", None))
        assert old.version == "0.9.1.dev100+gaaaaaaa"
        assert (old.first_seen.date(), old.last_seen.date()) == (
            seed_mod.W43.date() + timedelta(days=1),
            seed_mod.W44.date(),
        )
        assert old.first_seen_week == "2026-W43"
        assert old.packs == 4
        assert new.first_seen_week == "2026-W44"
        assert new.packs == 4
        # A build that stamped no commit stays unidentified: never guessed.
        assert bare.version == "0.9.2"
        assert bare.commit is None
        assert bare.first_seen_week == "2026-W45"
        assert bare.packs == 1

    def test_builds_sum_to_the_served_packs(self, report: TrendReport) -> None:
        assert sum(build.packs for build in report.builds) == sum(
            week.packs for week in report.weeks
        )


class TestHoldout:
    def test_a_withheld_pack_and_its_grade_move_nothing(self, tmp_path: Path) -> None:
        plain_log = SQLiteEventLog(tmp_path / "plain.db")
        seed_mod.seed(plain_log)
        held_log = SQLiteEventLog(tmp_path / "held.db")
        seed_mod.seed(held_log, with_holdout=True)

        without = summarize_trend(plain_log, weeks=3, until=UNTIL)
        with_held = summarize_trend(held_log, weeks=3, until=UNTIL)

        assert with_held.weeks == without.weeks
        assert with_held.builds == without.builds
        assert all("hhhhhhh" not in str(week.commits) for week in with_held.weeks)
        assert all(build.commit != "hhhhhhh" for build in with_held.builds)

    def test_the_seeded_holdout_row_is_really_in_the_log(self, tmp_path: Path) -> None:
        """Guard the guard: the comparison above is vacuous if nothing was seeded."""
        held_log = SQLiteEventLog(tmp_path / "held.db")
        seed_mod.seed(held_log, with_holdout=True)

        rows = held_log.get_events(entity_id="held1", limit=10)

        assert [row.event_type.value for row in rows].count("pack.assembled") == 1
        assert any(row.payload.get("holdout") is True for row in rows)


class TestTruncation:
    def test_a_capped_week_is_marked_and_the_report_says_so(
        self, log: SQLiteEventLog
    ) -> None:
        # W44 holds 10 captured sessions and no other event type holds more
        # than 6 in any week, so a cap of 7 bites W44 and only W44.
        result = summarize_trend(log, weeks=3, until=UNTIL, limit=7)

        assert [week.truncated for week in result.weeks] == [False, True, False]
        assert result.weeks[1].truncation_notes
        assert result.status == STATUS_TRUNCATED

    def test_an_uncapped_run_is_ok(self, report: TrendReport) -> None:
        assert report.status == STATUS_OK
        assert not any(week.truncated for week in report.weeks)


class TestGraphShape:
    def test_shares_use_the_provenance_deny_list(
        self, log: SQLiteEventLog, tmp_path: Path
    ) -> None:
        graph = StoreRegistry(stores_dir=tmp_path / "stores").knowledge.graph_store
        for index in range(1, 7):
            graph.upsert_node(f"n{index}", "Concept", {"name": f"n{index}"})
        graph.upsert_edge("n1", "n2", well_known.DEPENDS_ON)
        graph.upsert_edge("n2", "n3", well_known.PART_OF)
        graph.upsert_edge("n1", "n3", well_known.WAS_DERIVED_FROM)
        graph.upsert_edge("n4", "n5", well_known.WAS_GENERATED_BY)

        result = summarize_trend(log, graph, weeks=1, until=UNTIL)

        shape = result.graph_shape
        assert shape is not None
        assert (shape.nodes, shape.edges) == (6, 4)
        assert shape.semantic_edges == 2
        assert shape.semantic_edge_share == 0.5
        assert shape.isolated_nodes == 1
        assert shape.isolated_node_share == pytest.approx(1 / 6, abs=1e-3)
        assert shape.provenance_kinds == sorted(PROVENANCE_EDGE_KINDS)
        assert shape.status == STATUS_OK

    def test_the_deny_list_names_only_canonical_provenance_kinds(self) -> None:
        assert PROVENANCE_EDGE_KINDS <= well_known.CANONICAL_EDGE_KINDS
        assert well_known.DEPENDS_ON not in PROVENANCE_EDGE_KINDS
        assert well_known.PART_OF not in PROVENANCE_EDGE_KINDS
        assert well_known.WAS_GENERATED_BY in PROVENANCE_EDGE_KINDS

    def test_no_graph_store_omits_the_row_and_says_so(
        self, log: SQLiteEventLog
    ) -> None:
        result = summarize_trend(log, weeks=1, until=UNTIL)

        assert result.graph_shape is None
        assert any("graph shape omitted" in note for note in result.notes)


class TestMarkdown:
    def test_frontmatter_header_tables_and_build_boundaries(
        self, report: TrendReport
    ) -> None:
        text = render_markdown(report)

        lines = text.splitlines()
        assert lines[0] == "---"
        assert lines[1].startswith("title: ")
        assert lines[2] == "---"
        assert "sealed" in text
        assert "leading indicators only" in text
        for heading in (
            "## Retrieval adoption and follow-up",
            "## Pack value",
            "## Attribution and grading coverage",
            "## Judged outcomes",
            "## Write rejection",
            "## Precedents served",
            "## Builds",
            "## Notes",
        ):
            assert heading in text
        assert "| --- |" in text
        assert "| 2026-W45 (partial) |" in text
        assert "unidentified build" in text
        assert "aaaaaaa" in text
        assert "loop_health" in text

    def test_no_html_hosts_or_links(self, report: TrendReport) -> None:
        text = render_markdown(report)

        assert "<" not in text
        assert "http" not in text
        assert "](" not in text

    def test_a_truncated_week_is_named_in_the_table(self, log: SQLiteEventLog) -> None:
        text = render_markdown(summarize_trend(log, weeks=3, until=UNTIL, limit=7))

        assert "| 2026-W44 (truncated) |" in text


class TestReaderWindows:
    """The windowed readers keep their trailing-days behaviour by default.

    Each reader gained optional ``since`` / ``until`` so one calendar week can
    be read; with neither given the window is the one ``days`` always named.
    """

    @staticmethod
    def _seed_recent_and_old(log: SQLiteEventLog) -> datetime:
        now = datetime.now(tz=UTC)
        for label, at in (
            ("new", now - timedelta(days=2)),
            ("old", now - timedelta(days=10)),
        ):
            seed_mod.pack(log, f"p-{label}", at, None, [(f"doc:{label}", 10)])
            seed_mod.feedback(log, f"p-{label}", at, helpful=(f"doc:{label}",))
            seed_mod.capture(log, f"cs-{label}", at, pack_ids=(f"p-{label}",))
            seed_mod.rejected_write(log, at)
            seed_mod.judged(log, f"doc:{label}", at - timedelta(hours=1))
        return now

    @staticmethod
    def _counts(log: SQLiteEventLog, **window: Any) -> dict[str, int]:
        days = window.pop("days", 7)
        return {
            "value": summarize_pack_value(log, days=days, **window).packs,
            "serve": summarize_serve_attribution(log, days=days, **window).packs,
            "write": summarize_write_health(log, days=days, **window).boundary_rejected,
            "adoption": summarize_retrieval_adoption(log, days=days, **window).sessions,
            "judged": summarize_judged_outcomes(log, days=days, **window).totals.judged,
        }

    def test_default_window_is_the_trailing_days(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        now = self._seed_recent_and_old(log)

        trailing = self._counts(log, days=7)
        wide = self._counts(log, days=30)
        old_only = self._counts(
            log, since=now - timedelta(days=14), until=now - timedelta(days=5)
        )
        explicit = self._counts(log, since=now - timedelta(days=7))

        assert trailing == dict.fromkeys(trailing, 1)
        assert wide == dict.fromkeys(wide, 2)
        assert old_only == dict.fromkeys(old_only, 1)
        assert explicit == trailing

    def test_resolve_scan_window(self) -> None:
        now = datetime(2026, 11, 4, tzinfo=UTC)

        assert resolve_scan_window(7, now=now) == (now - timedelta(days=7), None)
        assert resolve_scan_window(7, until=now) == (now - timedelta(days=7), now)
        since = datetime(2026, 10, 1, tzinfo=UTC)
        assert resolve_scan_window(7, since=since, until=now) == (since, now)
        assert resolve_scan_window(7, since=since) == (since, None)


class TestSealedOutcome:
    """The trend page shows leading indicators; the holdout's outcome stays sealed."""

    SOURCE = Path(trend_module.__file__)

    def _tree(self) -> ast.Module:
        return ast.parse(self.SOURCE.read_text(encoding="utf-8"))

    def test_imports_nothing_from_the_holdout_analysis(self) -> None:
        modules = []
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
                modules.extend(f"{node.module}.{alias.name}" for alias in node.names)

        assert modules, "the scan found no imports; it is not looking"
        assert [m for m in modules if m.startswith("trellis.analyze.holdout")] == []

    def test_does_not_name_the_outcome_or_its_covariates(self) -> None:
        names: set[str] = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.add(node.value)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
        names = {n for n in names if len(n) < 40}

        assert "pack_ids" not in names, "scan control: the scan sees attribute names"
        assert {"outcome", "outcomes", "arm", "arms"} & names == set()
        assert {"turns", "tokens_per_task", "commits_per_task", "prs"} & names == set()

    def test_every_event_read_goes_through_a_reader_that_drops_the_holdout(
        self,
    ) -> None:
        called = {
            node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            for node in ast.walk(self._tree())
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name | ast.Attribute)
        }

        assert "summarize_trend" not in called, "scan control"
        assert {"scan_events", "get_events", "count"} & called == set()
        assert "join_pack_events_with_coverage" in called

    def test_importing_it_does_not_load_the_holdout_analysis(self) -> None:
        probe = (
            "import sys, trellis.analyze.trend; "
            "print(sorted(m for m in sys.modules "
            "if m.startswith('trellis.analyze.holdout')))"
        )
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONPATH": repo_src_pythonpath(), "PATH": ""},
        )

        assert done.stdout.strip() == "[]"
