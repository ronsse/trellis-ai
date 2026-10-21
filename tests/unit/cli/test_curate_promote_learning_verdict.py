"""``trellis curate promote-learning`` derives its verdict from its outcomes (#930).

The per-candidate statuses were always right. The command-level ``status`` and
the exit code ignored them, so a script reading either took a refused or
half-written promotion as done. Both now come from one reading of the
outcomes, shared with ``review-ready --approve``: ``0`` / ``ok`` when every
approved row landed, ``2`` / ``refused`` when any was refused and none
failed, ``5`` / ``failed`` when a write did not land, in either format.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.learning.scoring import (
    precedent_entity_id_for_candidate,
    prepare_learning_promotions,
)
from trellis_cli import curate as curate_cli
from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.main import app

runner = CliRunner()

#: A target that is never seeded, so the ``precedent_applies_to`` edge to it is
#: rejected after the entity is written: a half-landed promotion.
DANGLING = "entity://not-in-the-graph"


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


def _candidate(
    candidate_id: str,
    *,
    recommendation_type: str = "promote_guidance",
    targets: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "intent_family": "fam",
        "recommendation_type": recommendation_type,
        "item_id": f"doc:{candidate_id}",
        "item_type": "document",
        "title": f"title {candidate_id}",
        "precedent_name": f"Learning: {candidate_id}",
        "precedent_properties": {"source_of_truth": "reviewed_promotion"},
        "target_entity_ids": list(targets),
        "supporting_run_ids": ["run-1"],
        "phases": [],
        "domain_systems": [],
    }


def _seed_precedent(candidate_id: str, lifecycle: Any) -> None:
    """Seed the candidate's precedent node carrying ``lifecycle`` as written."""
    registry = curate_cli._get_registry()
    try:
        registry.knowledge.graph_store.upsert_node(
            precedent_entity_id_for_candidate(candidate_id),
            "precedent",
            {"lifecycle": lifecycle},
        )
    finally:
        registry.close()


def _demote(candidate_id: str) -> None:
    """Seed a demoted precedent, so re-approving the candidate is refused."""
    _seed_precedent(candidate_id, {"state": "demoted"})


def _promote(
    tmp_path: Path,
    candidates: list[dict[str, Any]],
    decisions: list[tuple[str, bool]],
    *,
    fmt: str,
    dry_run: bool = False,
) -> tuple[int, dict[str, Any] | None, str]:
    candidates_path = tmp_path / "candidates.json"
    decisions_path = tmp_path / "decisions.json"
    candidates_path.write_text(json.dumps({"candidates": candidates}), encoding="utf-8")
    decisions_path.write_text(
        json.dumps(
            {
                "decisions": [
                    {"candidate_id": cid, "approved": approved, "rationale": "r"}
                    for cid, approved in decisions
                ]
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "curate",
            "promote-learning",
            "--candidates",
            str(candidates_path),
            "--decisions",
            str(decisions_path),
            *(["--dry-run"] if dry_run else []),
            "--format",
            fmt,
        ],
    )
    data = json.loads(result.stdout.strip()) if fmt == "json" else None
    return result.exit_code, data, plain(result.stdout)


def _store_counts() -> tuple[int, ...]:
    """Row counts of every store a promotion or its meta-trace could write."""
    registry = curate_cli._get_registry()
    try:
        graph = registry.knowledge.graph_store
        events = registry.operational.event_log.get_events(limit=100_000)
        return (
            len(events),
            graph.count_nodes(),
            graph.count_edges(),
            registry.knowledge.document_store.count(),
            registry.knowledge.vector_store.count(),
            registry.operational.trace_store.count(),
        )
    finally:
        registry.close()


# scenario -> (candidates, decisions, demoted ids, exit, status, promoted_count)
_OK = _candidate("ok")
_OK2 = _candidate("ok2")
_REFUSED = _candidate("refused")  # seeded demoted
_INCOMPLETE = _candidate("incomplete", targets=(DANGLING,))
_NOISE = _candidate("noise", recommendation_type="investigate_noise")

_SCENARIOS: dict[str, tuple[Any, ...]] = {
    "all_landed": ([_OK, _OK2], [("ok", True), ("ok2", True)], [], EXIT_OK, "ok", 2),
    "one_refused_rest_landed": (
        [_OK, _REFUSED],
        [("ok", True), ("refused", True)],
        ["refused"],
        EXIT_VALIDATION,
        "refused",
        1,
    ),
    "only_refused": (
        [_REFUSED],
        [("refused", True)],
        ["refused"],
        EXIT_VALIDATION,
        "refused",
        0,
    ),
    "approved_id_names_no_candidate": (
        [_OK],
        [("ghost", True)],
        [],
        EXIT_VALIDATION,
        "refused",
        0,
    ),
    "approved_candidate_not_promotable": (
        [_NOISE],
        [("noise", True)],
        [],
        EXIT_VALIDATION,
        "refused",
        0,
    ),
    "half_landed_is_a_failure": (
        [_INCOMPLETE],
        [("incomplete", True)],
        [],
        EXIT_STORE,
        "failed",
        0,
    ),
    "failed_beside_landed": (
        [_OK, _INCOMPLETE],
        [("ok", True), ("incomplete", True)],
        [],
        EXIT_STORE,
        "failed",
        1,
    ),
    # Both orders: the verdict must not depend on which came first.
    "refused_then_failed": (
        [_REFUSED, _INCOMPLETE],
        [("refused", True), ("incomplete", True)],
        ["refused"],
        EXIT_STORE,
        "failed",
        0,
    ),
    "failed_then_refused": (
        [_INCOMPLETE, _REFUSED],
        [("incomplete", True), ("refused", True)],
        ["refused"],
        EXIT_STORE,
        "failed",
        0,
    ),
    "refused_failed_and_landed": (
        [_OK, _REFUSED, _INCOMPLETE],
        [("ok", True), ("refused", True), ("incomplete", True)],
        ["refused"],
        EXIT_STORE,
        "failed",
        1,
    ),
    # Not asking to promote a row is not a failure to promote it.
    "unapproved_row_beside_landed": (
        [_OK, _REFUSED],
        [("ok", True), ("refused", False)],
        ["refused"],
        EXIT_OK,
        "ok",
        1,
    ),
}


class TestVerdictFollowsOutcomes:
    @pytest.mark.parametrize("fmt", ["json", "text"])
    @pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
    def test_exit_code_and_status_do_not_depend_on_format(
        self, scenario: str, fmt: str, tmp_path: Path
    ) -> None:
        candidates, decisions, demoted, exit_code, status, promoted = _SCENARIOS[
            scenario
        ]
        for candidate_id in demoted:
            _demote(candidate_id)

        code, data, out = _promote(tmp_path, candidates, decisions, fmt=fmt)

        assert code == exit_code, out
        if fmt == "json":
            assert data is not None
            assert data["status"] == status
            assert data["promoted_count"] == promoted
        else:
            # The human surface states the verdict; it does not only exit.
            assert f"Verdict: {status} (exit {exit_code})" in out

    def test_half_landed_promotion_is_labelled_incomplete(self, tmp_path: Path) -> None:
        code, data, _ = _promote(
            tmp_path, [_INCOMPLETE], [("incomplete", True)], fmt="json"
        )
        assert data is not None
        assert code == EXIT_STORE
        assert data["results"][0]["status"] == "promoted_incomplete"
        assert data["promoted_count"] == 0

    def test_a_refused_row_keeps_its_own_status(self, tmp_path: Path) -> None:
        _demote("refused")
        _, data, _ = _promote(
            tmp_path, [_OK, _REFUSED], [("ok", True), ("refused", True)], fmt="json"
        )
        assert data is not None
        assert [(r["candidate_id"], r["status"]) for r in data["results"]] == [
            ("ok", "promoted"),
            ("refused", "refused_demoted"),
        ]

    def test_an_unreadyable_approved_row_is_reported_not_dropped(
        self, tmp_path: Path
    ) -> None:
        # No row is ready, so nothing reaches the executor; the refusals still
        # have to appear, because the verdict reads them.
        _, data, _ = _promote(
            tmp_path, [_NOISE], [("noise", True), ("ghost", True)], fmt="json"
        )
        assert data is not None
        assert data["approved_count"] == 2
        assert data["ready_count"] == 0
        assert [(r["candidate_id"], r["status"]) for r in data["results"]] == [
            ("noise", "skipped_non_promotable"),
            ("ghost", "missing_candidate"),
        ]

    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_an_empty_run_is_ok_in_both_formats(self, fmt: str, tmp_path: Path) -> None:
        for decisions in ([("ok", False)], []):
            code, data, out = _promote(tmp_path, [_OK], decisions, fmt=fmt)
            assert code == EXIT_OK, out
            if fmt == "json":
                assert data is not None
                assert data["status"] == "ok"
                assert data["results"] == []
            else:
                assert "No approved promotions found" in out

    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_a_write_the_executor_reports_failed_is_a_failure(
        self, fmt: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            curate_cli,
            "submit_learning_promotion",
            lambda *_a, **_k: {"status": "entity_failed", "message": "boom"},
        )
        code, data, out = _promote(tmp_path, [_OK], [("ok", True)], fmt=fmt)
        assert code == EXIT_STORE, out
        if data is not None:
            assert data["status"] == "failed"
            assert data["results"][0]["status"] == "entity_failed"


class TestDryRunPreview:
    """``--dry-run`` previews the verdict as ``would_*`` and always exits ``0``.

    The preview is read-only and says what a real run would reach if every
    ready write landed. It is never an exit code: a script that gates on a
    dry run's exit must not be told the run failed when nothing ran.
    """

    @pytest.mark.parametrize("fmt", ["json", "text"])
    @pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
    def test_a_dry_run_always_exits_zero_whatever_it_previews(
        self, scenario: str, fmt: str, tmp_path: Path
    ) -> None:
        candidates, decisions, demoted, _, _, _ = _SCENARIOS[scenario]
        for candidate_id in demoted:
            _demote(candidate_id)
        before = _store_counts()

        code, data, out = _promote(
            tmp_path, candidates, decisions, fmt=fmt, dry_run=True
        )

        assert code == EXIT_OK, out
        assert _store_counts() == before
        if data is not None:
            # The command's own status is the dry run's, never the preview's.
            assert data["status"] == "ok"
            assert data["dry_run"] is True
            assert "promoted_count" not in data
        else:
            assert not any(line.startswith("Verdict:") for line in out.splitlines())
            assert "this dry run exits 0" in out

    @pytest.mark.parametrize("fmt", ["json", "text"])
    @pytest.mark.parametrize(
        ("scenario", "would_exit", "would_status"),
        [
            ("all_landed", EXIT_OK, "ok"),
            ("one_refused_rest_landed", EXIT_VALIDATION, "refused"),
            ("only_refused", EXIT_VALIDATION, "refused"),
            ("approved_id_names_no_candidate", EXIT_VALIDATION, "refused"),
            ("approved_candidate_not_promotable", EXIT_VALIDATION, "refused"),
            ("unapproved_row_beside_landed", EXIT_OK, "ok"),
            # A dangling edge target is rejected by the graph during the write,
            # so no read can foresee it: the preview stops at what it can see.
            ("half_landed_is_a_failure", EXIT_OK, "ok"),
        ],
    )
    def test_the_preview_names_the_verdict_a_real_run_reaches(
        self,
        scenario: str,
        would_exit: int,
        would_status: str,
        fmt: str,
        tmp_path: Path,
    ) -> None:
        candidates, decisions, demoted, real_exit, real_status, _ = _SCENARIOS[scenario]
        for candidate_id in demoted:
            _demote(candidate_id)

        _, data, out = _promote(tmp_path, candidates, decisions, fmt=fmt, dry_run=True)

        if data is not None:
            assert (data["would_verdict"], data["would_exit_code"]) == (
                would_status,
                would_exit,
            )
        else:
            assert (
                f"Would-be verdict: {would_status} (a real run would exit "
                f"{would_exit}; this dry run exits 0)" in out
            )
        if scenario != "half_landed_is_a_failure":
            # Where the preview can see everything, it equals the real run.
            assert (would_exit, would_status) == (real_exit, real_status)

    def test_each_row_carries_the_status_a_real_run_would_give_it(
        self, tmp_path: Path
    ) -> None:
        _demote("refused")
        _, data, _ = _promote(
            tmp_path,
            [_OK, _REFUSED, _NOISE],
            [("ok", True), ("refused", True), ("noise", True), ("ghost", True)],
            fmt="json",
            dry_run=True,
        )
        assert data is not None
        assert [
            (r["candidate_id"], r["status"], r["would_status"]) for r in data["results"]
        ] == [
            ("ok", "ready", "promoted"),
            ("refused", "ready", "refused_demoted"),
            ("noise", "skipped_non_promotable", "skipped_non_promotable"),
            ("ghost", "missing_candidate", "missing_candidate"),
        ]

    def test_text_names_the_row_a_real_run_would_refuse(self, tmp_path: Path) -> None:
        _demote("refused")
        _, _, out = _promote(
            tmp_path,
            [_OK, _REFUSED],
            [("ok", True), ("refused", True)],
            fmt="text",
            dry_run=True,
        )
        assert "- ok: ready (would be promoted)" in out
        assert "- refused: ready (would be refused_demoted)" in out

    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_a_dry_run_with_nothing_approved_would_be_ok(
        self, fmt: str, tmp_path: Path
    ) -> None:
        code, data, out = _promote(
            tmp_path, [_OK], [("ok", False)], fmt=fmt, dry_run=True
        )
        assert code == EXIT_OK
        if data is not None:
            assert (data["would_verdict"], data["would_exit_code"]) == ("ok", EXIT_OK)
        else:
            assert "Would-be verdict: ok" in out


_BLANK_ID_ROWS: list[tuple[str, bool]] = [
    ("ok", True),
    ("", True),
    ("   ", True),
    ("ok2", False),
    ("ghost", True),
]


class TestApprovedRowsWithoutAnId:
    """``approved_count`` is what was marked; ``named_count`` is what could run.

    An approved row with a blank ``candidate_id`` never becomes a result, so
    it is neither refused nor promoted. Counting it as submitted overstated
    the run, and ``approved_count`` stays as it was for the readers that
    parse it.
    """

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_a_blank_id_is_approved_but_not_named(
        self, dry_run: bool, tmp_path: Path
    ) -> None:
        _, data, _ = _promote(
            tmp_path,
            [_OK, _OK2],
            _BLANK_ID_ROWS,
            fmt="json",
            dry_run=dry_run,
        )
        assert data is not None
        assert data["approved_count"] == 4
        assert data["named_count"] == 2
        assert [r["candidate_id"] for r in data["results"]] == ["ok", "ghost"]

    def test_the_planner_counts_named_rows_apart_from_approved_ones(self) -> None:
        plan = prepare_learning_promotions(
            candidates_payload={"candidates": [_OK]},
            decisions_payload={
                "decisions": [
                    {"candidate_id": "ok", "approved": True},
                    {"candidate_id": "", "approved": True},
                    {"approved": True},
                    {"candidate_id": "ok", "approved": False},
                ]
            },
        )
        assert (plan["approved_count"], plan["named_count"]) == (3, 1)
        assert len(plan["results"]) == plan["named_count"]

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_text_says_how_many_approved_rows_were_skipped(
        self, dry_run: bool, tmp_path: Path
    ) -> None:
        _, _, out = _promote(
            tmp_path, [_OK], _BLANK_ID_ROWS, fmt="text", dry_run=dry_run
        )
        assert "2 approved row(s) name no candidate_id and were skipped." in out

    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_only_blank_ids_is_still_a_no_op_that_says_what_it_skipped(
        self, fmt: str, tmp_path: Path
    ) -> None:
        code, data, out = _promote(tmp_path, [_OK], [("", True), ("  ", True)], fmt=fmt)
        assert code == EXIT_OK, out
        if data is not None:
            assert (data["approved_count"], data["named_count"]) == (2, 0)
            assert data["results"] == []
        else:
            assert "2 approved row(s) name no candidate_id" in out
            assert "No approved promotions found" not in out

    def test_a_run_with_no_blank_ids_prints_no_note(self, tmp_path: Path) -> None:
        _, _, out = _promote(tmp_path, [_OK], [("ok", True)], fmt="text")
        assert "no candidate_id" not in out


class TestMalformedLifecycleReadsAsCurrent:
    """An empty lifecycle state re-approves, in the preview and in the write.

    The scorer read ``{"state": ""}`` as current and let the write proceed;
    the handler's own reader returned ``""`` and refused it, so the real run
    exited ``5`` after a preview of ``ok``. Both readers now agree.
    """

    @staticmethod
    def _invoke(tmp_path: Path, *, dry_run: bool) -> tuple[int, dict[str, Any]]:
        candidates_path = tmp_path / "candidates.json"
        decisions_path = tmp_path / "decisions.json"
        candidates_path.write_text(json.dumps({"candidates": [_OK]}), encoding="utf-8")
        decisions_path.write_text(
            json.dumps({"decisions": [{"candidate_id": "ok", "approved": True}]}),
            encoding="utf-8",
        )
        args = [
            "curate",
            "promote-learning",
            "--candidates",
            str(candidates_path),
            "--decisions",
            str(decisions_path),
            "--format",
            "json",
        ]
        result = runner.invoke(app, [*args, "--dry-run"] if dry_run else args)
        return result.exit_code, json.loads(result.stdout.strip())

    @pytest.mark.parametrize(
        "lifecycle",
        [{"state": ""}, {"state": None}, {"state": 5}, {}, "demoted"],
        ids=["empty", "null", "non_str", "no_state_key", "non_dict"],
    )
    def test_a_malformed_record_re_approves(
        self, lifecycle: Any, tmp_path: Path
    ) -> None:
        _seed_precedent("ok", lifecycle)

        preview_code, preview = self._invoke(tmp_path, dry_run=True)
        code, data = self._invoke(tmp_path, dry_run=False)

        assert preview_code == EXIT_OK
        assert [r["status"] for r in preview["results"]] == ["ready"]
        assert code == EXIT_OK, data
        assert data["status"] == "ok"
        assert data["promoted_count"] == 1
        assert [r["status"] for r in data["results"]] == ["promoted"]

    def test_a_demoted_record_is_still_refused(self, tmp_path: Path) -> None:
        # The control: only a malformed record reads as current. A real state
        # keeps its refusal, so the fix cannot have failed open.
        _seed_precedent("ok", {"state": "demoted"})

        code, data = self._invoke(tmp_path, dry_run=False)

        assert code == EXIT_VALIDATION, data
        assert data["status"] == "refused"
        assert data["promoted_count"] == 0
        assert [r["status"] for r in data["results"]] == ["refused_demoted"]


class TestPromotionVerdict:
    """The shared helper, read directly: the same table both commands use."""

    @staticmethod
    def _landed() -> dict[str, Any]:
        return {
            "status": "promoted",
            "edges": [{"edge_kind": "k", "status": "success"}],
            "precedent_event_status": "success",
        }

    @pytest.mark.parametrize(
        ("statuses", "expected"),
        [
            ([], (EXIT_OK, "ok")),
            (["landed"], (EXIT_OK, "ok")),
            (["landed", "refused_demoted"], (EXIT_VALIDATION, "refused")),
            (["refused_not_current"], (EXIT_VALIDATION, "refused")),
            (["missing_candidate"], (EXIT_VALIDATION, "refused")),
            (["skipped_non_promotable"], (EXIT_VALIDATION, "refused")),
            (["entity_failed"], (EXIT_STORE, "failed")),
            (["promoted_incomplete"], (EXIT_STORE, "failed")),
            (["refused_demoted", "entity_failed"], (EXIT_STORE, "failed")),
            (["entity_failed", "refused_demoted"], (EXIT_STORE, "failed")),
            # An unknown status is a failure, never an ``ok`` or a refusal.
            (["something_new"], (EXIT_STORE, "failed")),
        ],
    )
    def test_table(self, statuses: list[str], expected: tuple[int, str]) -> None:
        outcomes = [
            self._landed() if status == "landed" else {"status": status}
            for status in statuses
        ]
        assert curate_cli._promotion_verdict(outcomes) == expected

    def test_a_promoted_status_that_did_not_fully_land_is_a_failure(self) -> None:
        # Whatever the label says: the edge or the event did not land.
        edge_down = {
            **self._landed(),
            "edges": [{"edge_kind": "k", "status": "failed"}],
        }
        event_down = {**self._landed(), "precedent_event_status": "rejected"}
        for outcome in (edge_down, event_down):
            assert curate_cli._promotion_verdict([outcome]) == (EXIT_STORE, "failed")
