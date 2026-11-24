"""``trellis analyze unanswered-intents``: requests whose packs keep failing.

Packs are emitted as ``PACK_ASSEMBLED`` rows in the shape the builder writes
(``intent``, ``domain``, ``intent_family``, ``injected_items``). Feedback of the
full family goes through ``record_feedback``; the governed family goes through
the CLI's ``feedback.record`` command; a row carrying only a ``rating`` (no
``success``) is emitted raw, because only such a row reaches the threshold
comparison. Every intent below is invented.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.analyze.unanswered_intents import (
    NO_DOMAIN_KEY,
    NO_INTENT_KEY,
    intent_key,
    summarize_unanswered_intents,
)
from trellis.core.pack_holdout import HOLDOUT_KEY
from trellis.feedback.models import SUCCESS_RATING_THRESHOLD, PackFeedback
from trellis.feedback.recording import record_feedback
from trellis.learning.intent_families import classify_intent_family
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()

INTENT_A = "How do I rotate the widget frobnicator?"
INTENT_A_VARIANT = "  how do I   ROTATE the widget frobnicator  "
INTENT_B = "summarize the teapot calibration notes"
INTENT_HELD = "zebra quartz holdout request"


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()
    return StoreRegistry(stores_dir=stores_dir)


def _pack(
    registry: StoreRegistry,
    pack_id: str,
    intent: str | None,
    item_ids: list[str],
    *,
    domain: str | None = "testing",
    holdout: bool = False,
) -> None:
    payload: dict[str, Any] = {
        "domain": domain,
        "intent_family": "general_context",
        "injected_items": [
            {"item_id": item_id, "item_type": "document", "rank": rank}
            for rank, item_id in enumerate(item_ids)
        ],
    }
    if intent is not None:
        payload["intent"] = intent
    if holdout:
        payload[HOLDOUT_KEY] = True
    registry.operational.event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=pack_id,
        entity_type="pack",
        payload=payload,
    )


def _grade(
    registry: StoreRegistry,
    tmp_path: Path,
    pack_id: str,
    rating: float,
    *,
    unhelpful: tuple[str, ...] = (),
) -> None:
    feedback = PackFeedback.from_agent_signal(
        run_id=f"run-{pack_id}",
        rating=rating,
        unhelpful_item_ids=unhelpful,
        pack_id=pack_id,
    )
    record_feedback(
        feedback,
        log_dir=tmp_path / "feedback",
        event_log=registry.operational.event_log,
        pack_id=pack_id,
    )


def _grade_governed(registry: StoreRegistry, pack_id: str, rating: float) -> None:
    executor = build_curate_executor(registry, clearance="restricted")
    result = executor.execute(
        Command(
            operation=Operation.FEEDBACK_RECORD,
            args={"target_id": pack_id, "rating": rating, "pack_id": pack_id},
            target_id=pack_id,
            target_type="pack",
            requested_by="cli:curate-feedback",
        )
    )
    assert result.status == CommandStatus.SUCCESS, result.message


def _grade_rating_only(registry: StoreRegistry, pack_id: str, rating: float) -> None:
    registry.operational.event_log.emit(
        EventType.FEEDBACK_RECORDED,
        source="test",
        entity_id=pack_id,
        entity_type="pack",
        payload={"pack_id": pack_id, "rating": rating},
    )


def _seed(registry: StoreRegistry, tmp_path: Path) -> None:
    # Intent A, three spellings: two unanswered (one empty, one with an
    # unhelpful item), one answered.
    _pack(registry, "a1", INTENT_A, [])
    _grade(registry, tmp_path, "a1", 0.1)
    _pack(registry, "a2", INTENT_A_VARIANT, ["doc-x", "doc-y"])
    _grade_governed(registry, "a2", 0.2)
    _grade(registry, tmp_path, "a2", 0.0, unhelpful=("doc-x",))
    _pack(registry, "a3", INTENT_A.upper(), ["doc-z"], domain="other")
    _grade(registry, tmp_path, "a3", 0.9)
    # Ungraded pack of intent A: not unanswered.
    _pack(registry, "a4", INTENT_A, ["doc-z"])
    # Intent B: one unanswered pack only (a one-off).
    _pack(registry, "b1", INTENT_B, ["doc-q"])
    _grade_governed(registry, "b1", 0.1)
    # No intent: two unanswered packs that served items nobody named.
    _pack(registry, "n1", None, ["doc-n"])
    _grade(registry, tmp_path, "n1", 0.2)
    _pack(registry, "n2", "   ", ["doc-n"])
    _grade_rating_only(registry, "n2", 0.3)
    # Held out: dropped with its feedback.
    _pack(registry, "h1", INTENT_HELD, [], holdout=True)
    _grade(registry, tmp_path, "h1", 0.0)
    _pack(registry, "h2", INTENT_HELD, [], holdout=True)
    _grade(registry, tmp_path, "h2", 0.0)
    # Feedback naming a pack the scan never saw.
    _grade(registry, tmp_path, "ghost", 0.0)


def _report(registry: StoreRegistry, **kwargs: Any) -> Any:
    return summarize_unanswered_intents(
        registry.operational.event_log, days=30, **kwargs
    )


def _groups(report: Any) -> dict[str, Any]:
    return {group.key: group for group in report.groups}


class TestIntentKey:
    def test_case_whitespace_and_punctuation_merge(self) -> None:
        assert intent_key(INTENT_A) == intent_key(INTENT_A_VARIANT)
        assert intent_key(INTENT_A) == intent_key(INTENT_A.upper())

    @pytest.mark.parametrize("raw", [None, "", "   ", "?!", 7])
    def test_nothing_recoverable_is_no_intent(self, raw: Any) -> None:
        assert intent_key(raw) == NO_INTENT_KEY


class TestReport:
    def test_variants_group_as_one_line(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        a = _groups(_report(registry))[intent_key(INTENT_A)]
        assert (a.packs, a.graded, a.unanswered, a.answered) == (4, 3, 2, 1)
        assert a.domains == ["other", "testing"]
        assert set(a.example_pack_ids) == {"a1", "a2"}

    def test_unanswered_split_by_what_was_served(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        groups = _groups(_report(registry))
        a = groups[intent_key(INTENT_A)]
        assert (a.unanswered_empty, a.unanswered_named_unhelpful) == (1, 1)
        assert a.unanswered_not_named_unhelpful == 0
        none = groups[NO_INTENT_KEY]
        assert none.unanswered == 2
        assert none.unanswered_not_named_unhelpful == 2

    def test_no_intent_is_grouped_never_dropped(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        report = _report(registry)
        assert _groups(report)[NO_INTENT_KEY].label == NO_INTENT_KEY
        assert report.limits.packs_without_intent == 2

    def test_held_out_pack_dropped_and_counted(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        report = _report(registry, min_unanswered=1)
        assert intent_key(INTENT_HELD) not in _groups(report)
        assert report.limits.holdout_packs_dropped == 2
        assert report.limits.holdout_feedback_dropped == 2

    def test_ungraded_is_not_unanswered(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        report = _report(registry)
        assert report.limits.ungraded_packs == 1
        assert report.totals.ungraded == 1
        assert report.totals.unanswered == 5

    def test_feedback_outside_scan_counted(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        assert _report(registry).limits.feedback_outside_scan == 1

    def test_latest_verdict_wins_and_conflicts_counted(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _pack(registry, "c1", INTENT_B, ["doc-q"])
        _grade(registry, tmp_path, "c1", 0.1)
        _grade(registry, tmp_path, "c1", 0.9)
        report = _report(registry, min_unanswered=0)
        b = _groups(report)[intent_key(INTENT_B)]
        assert (b.unanswered, b.answered) == (0, 1)
        assert report.limits.packs_with_multiple_verdicts == 1
        assert report.limits.packs_with_conflicting_verdicts == 1

    def test_both_families_are_read(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _pack(registry, "f1", INTENT_B, ["doc-q"])
        _grade(registry, tmp_path, "f1", 0.1)
        _pack(registry, "f2", INTENT_B, ["doc-q"])
        _grade_governed(registry, "f2", 0.1)
        b = _groups(_report(registry))[intent_key(INTENT_B)]
        assert (b.graded, b.unanswered) == (2, 2)

    def test_threshold_boundary_is_answered(self, registry: StoreRegistry) -> None:
        _pack(registry, "t1", INTENT_B, ["doc-q"])
        _grade_rating_only(registry, "t1", SUCCESS_RATING_THRESHOLD)
        _pack(registry, "t2", INTENT_B, ["doc-q"])
        _grade_rating_only(registry, "t2", SUCCESS_RATING_THRESHOLD - 0.01)
        b = _groups(_report(registry, min_unanswered=0))[intent_key(INTENT_B)]
        assert (b.graded, b.unanswered, b.answered) == (2, 1, 1)
        assert b.example_pack_ids == ["t2"]

    def test_min_unanswered_hides_and_counts(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        report = _report(registry)
        assert set(_groups(report)) == {intent_key(INTENT_A), NO_INTENT_KEY}
        assert report.limits.groups_hidden_below_min == 1
        shown = _report(registry, min_unanswered=1)
        assert intent_key(INTENT_B) in _groups(shown)

    def test_sorted_by_unanswered_then_recency(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        report = _report(registry)
        # Both groups hold two unanswered packs; the no-intent packs are newer.
        assert [g.key for g in report.groups] == [
            NO_INTENT_KEY,
            intent_key(INTENT_A),
        ]

    def test_top_caps_and_counts(self, registry: StoreRegistry, tmp_path: Path) -> None:
        _seed(registry, tmp_path)
        report = _report(registry, top=1)
        assert len(report.groups) == 1
        assert report.limits.groups_beyond_top == 1

    def test_redaction_leaves_no_intent_text(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        report = _report(registry, redact_intents=True, min_unanswered=1)
        dumped = report.model_dump_json().casefold()
        for word in ("frobnicator", "teapot", "zebra"):
            assert word not in dumped
        assert report.redacted is True
        labels = {g.label for g in report.groups}
        assert NO_INTENT_KEY in labels
        again = _report(registry, redact_intents=True, min_unanswered=1)
        assert labels == {g.label for g in again.groups}


# Two wordings of one bugfix need and one review need, all in one domain.
FIX_A = "fix the flaky frobnicator test"
FIX_B = "repair a crash in the teapot loader"
REVIEW_A = "review the widget gasket change"
FAMILY_CONFIG = """\
retrieval:
  intent_families:
    fallback: misc
    rules:
      - id: x.teapot
        family: teapots
        keywords: [teapot]
"""


def _seed_families(registry: StoreRegistry, tmp_path: Path) -> None:
    for pack_id, intent, domain in (
        ("g1", FIX_A, "testing"),
        ("g2", FIX_B, "testing"),
        ("g3", REVIEW_A, "testing"),
        ("g4", REVIEW_A, None),
        ("g5", None, None),
    ):
        _pack(registry, pack_id, intent, ["doc-g"], domain=domain)
        _grade(registry, tmp_path, pack_id, 0.1)


class TestGroupBy:
    def test_default_dump_has_no_new_keys(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        moment = datetime.now(tz=UTC)
        default = _report(registry, min_unanswered=1, now=moment).model_dump()
        assert "group_by" not in default
        for group in default["groups"]:
            assert "distinct_intents" not in group
        explicit = _report(registry, min_unanswered=1, group_by="intent", now=moment)
        assert explicit.model_dump() == default

    def test_domain_merges_differently_worded_requests(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed_families(registry, tmp_path)
        report = _report(registry, group_by="domain", min_unanswered=1)
        testing = _groups(report)["testing"]
        assert (testing.packs, testing.unanswered) == (3, 3)
        assert testing.label == "testing"
        assert testing.distinct_intents == 3
        dumped = report.model_dump()
        assert dumped["group_by"] == "domain"

    def test_no_domain_bucket_counted_never_dropped(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed_families(registry, tmp_path)
        report = _report(registry, group_by="domain", min_unanswered=1)
        bucket = _groups(report)[NO_DOMAIN_KEY]
        assert (bucket.packs, bucket.unanswered, bucket.label) == (
            2,
            2,
            NO_DOMAIN_KEY,
        )
        # One review request and one pack with no intent text.
        assert bucket.distinct_intents == 2
        assert report.totals.packs == 5

    def test_family_groups_by_rule_set_not_payload_field(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed_families(registry, tmp_path)
        fix = classify_intent_family(FIX_A).family
        review = classify_intent_family(REVIEW_A).family
        assert classify_intent_family(FIX_B).family == fix != review
        report = _report(registry, group_by="family", min_unanswered=1)
        groups = _groups(report)
        assert (groups[fix].packs, groups[fix].distinct_intents) == (2, 2)
        assert (groups[review].packs, groups[review].distinct_intents) == (2, 1)
        assert "general_context" not in groups
        assert groups[fix].label == fix

    def test_labels_carry_no_intent_text(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed_families(registry, tmp_path)
        for mode in ("domain", "family"):
            for redact in (False, True):
                report = _report(
                    registry, group_by=mode, min_unanswered=1, redact_intents=redact
                )
                dumped = report.model_dump_json().casefold()
                for word in ("frobnicator", "teapot", "gasket"):
                    assert word not in dumped, (mode, redact)
                assert {g.key for g in report.groups} == {
                    g.label for g in report.groups
                }

    def test_redaction_leaves_domain_key(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed_families(registry, tmp_path)
        report = _report(
            registry, group_by="domain", min_unanswered=1, redact_intents=True
        )
        assert "testing" in _groups(report)

    def test_unknown_group_by_is_refused(self, registry: StoreRegistry) -> None:
        with pytest.raises(ValueError, match="group_by"):
            _report(registry, group_by="session")


class TestCli:
    def test_json_shape(self, registry: StoreRegistry, tmp_path: Path) -> None:
        _seed(registry, tmp_path)
        result = runner.invoke(
            app,
            ["analyze", "unanswered-intents", "--format", "json", "--no-meta-trace"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert set(data) == {
            "window_days",
            "since",
            "min_unanswered",
            "top",
            "redacted",
            "groups",
            "totals",
            "limits",
            "scan",
        }
        assert data["totals"]["unanswered"] == 5

    def test_text_names_each_group(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        result = runner.invoke(
            app, ["analyze", "unanswered-intents", "--no-meta-trace"]
        )
        assert result.exit_code == 0, result.output
        rendered = plain(result.stdout)
        assert "frobnicator" in rendered
        assert NO_INTENT_KEY in rendered
        assert "1 group(s) below --min-unanswered 2 hidden" in rendered

    @pytest.mark.parametrize("fmt", ["text", "json"])
    def test_redact_flag_leaves_no_intent_text(
        self, registry: StoreRegistry, tmp_path: Path, fmt: str
    ) -> None:
        _seed(registry, tmp_path)
        result = runner.invoke(
            app,
            [
                "analyze",
                "unanswered-intents",
                "--redact-intents",
                "--min-unanswered",
                "1",
                "--format",
                fmt,
                "--no-meta-trace",
            ],
        )
        assert result.exit_code == 0, result.output
        out = result.stdout.casefold()
        for word in ("frobnicator", "teapot", "zebra", "rotate"):
            assert word not in out

    def test_exit_code_does_not_depend_on_format(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        text = runner.invoke(app, ["analyze", "unanswered-intents", "--no-meta-trace"])
        machine = runner.invoke(
            app,
            ["analyze", "unanswered-intents", "--format", "json", "--no-meta-trace"],
        )
        assert text.exit_code == machine.exit_code == 0

    def test_no_meta_trace_run_writes_nothing(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)

        def _footprint() -> tuple[int, int, int]:
            return (
                len(registry.operational.event_log.get_events(limit=100_000)),
                registry.knowledge.document_store.count(),
                len(registry.operational.trace_store.query(limit=100_000)),
            )

        before = _footprint()
        _reset_registry()
        for fmt in ("text", "json"):
            result = runner.invoke(
                app,
                ["analyze", "unanswered-intents", "--format", fmt, "--no-meta-trace"],
            )
            assert result.exit_code == 0, result.output
        assert _footprint() == before

    def test_group_by_domain_json(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed_families(registry, tmp_path)
        result = runner.invoke(
            app,
            [
                "analyze",
                "unanswered-intents",
                "--group-by",
                "domain",
                "--min-unanswered",
                "1",
                "--format",
                "json",
                "--no-meta-trace",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["group_by"] == "domain"
        groups = {g["key"]: g for g in data["groups"]}
        assert set(groups) == {"testing", NO_DOMAIN_KEY}
        assert groups["testing"]["distinct_intents"] == 3

    def test_group_by_family_reads_configured_rules(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed_families(registry, tmp_path)
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "config" / "config.yaml").write_text(
            FAMILY_CONFIG, encoding="utf-8"
        )
        _reset_registry()
        for fmt in ("text", "json"):
            result = runner.invoke(
                app,
                [
                    "analyze",
                    "unanswered-intents",
                    "--group-by",
                    "family",
                    "--min-unanswered",
                    "1",
                    "--format",
                    fmt,
                    "--no-meta-trace",
                ],
            )
            assert result.exit_code == 0, result.output
            out = plain(result.stdout)
            assert "frobnicator" not in out.casefold()
            assert "gasket" not in out.casefold()
            if fmt == "json":
                data = json.loads(out.strip())
                keys = {g["key"]: g["packs"] for g in data["groups"]}
                # Only the configured rule matched; the shipped bugfix and
                # review rules were replaced, not consulted.
                assert keys == {"misc": 4, "teapots": 1}
                assert data["totals"]["packs"] == 5
            else:
                assert "grouped by family" in out
                assert "distinct intent(s)" in out

    @pytest.mark.parametrize("fmt", ["text", "json"])
    def test_invalid_group_by_exits_2(
        self, registry: StoreRegistry, tmp_path: Path, fmt: str
    ) -> None:
        _seed(registry, tmp_path)
        result = runner.invoke(
            app,
            [
                "analyze",
                "unanswered-intents",
                "--group-by",
                "session",
                "--format",
                fmt,
                "--no-meta-trace",
            ],
        )
        assert result.exit_code == 2, result.output
        if fmt == "json":
            assert json.loads(result.stdout.strip())["status"] == "error"
