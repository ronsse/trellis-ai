"""``trellis curate review-ready`` — list, approve, refuse, and exit alike.

Every id below is synthetic. The ready set is built so the order the command
returns differs from the digest's, which is what makes the ordering assertion
mean something: by helpful count alone the list would run ``e, b, a, g``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import assert_coloured, force_colour, plain
from trellis.learning import (
    LEARNING_ARTIFACTS_DIR_ENV,
    LEARNING_CANDIDATES_FILENAME,
    write_learning_review_artifacts,
)
from trellis.learning.review_ready import REVIEW_EXCERPT_MAX_CHARS
from trellis.learning.scoring import precedent_entity_id_for_candidate
from trellis.stores.base.event_log import EventType
from trellis_cli import curate as curate_cli
from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.main import app

runner = CliRunner()

TARGET = "entity://review-target"


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


def _candidate(
    candidate_id: str,
    *,
    item_id: str,
    family: str = "fam_a",
    recommendation_type: str = "promote_guidance",
    helpful: int = 1,
    unhelpful: int = 0,
    sessions: int | None = 1,
    sessionless: int | None = 0,
    targets: tuple[str, ...] = (),
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "appearances": 4,
        "helpful_count": helpful,
        "unhelpful_count": unhelpful,
    }
    if sessions is not None:
        evidence["distinct_helpful_sessions"] = sessions
    if sessionless is not None:
        evidence["sessionless_citations"] = sessionless
    return {
        "candidate_id": candidate_id,
        "intent_family": family,
        "recommendation_type": recommendation_type,
        "item_id": item_id,
        "item_type": "document",
        "title": f"title {candidate_id}",
        "precedent_name": f"Learning: {family} :: {candidate_id}",
        "precedent_properties": {
            "category": "retrieval_guidance",
            "intent_family": family,
            "source_item_id": item_id,
            "source_of_truth": "reviewed_promotion",
        },
        "target_entity_ids": list(targets),
        "supporting_run_ids": ["run-1"],
        "phases": [],
        "domain_systems": [],
        "metrics": {"times_served": 4, "success_rate": 1.0},
        "citation_evidence": evidence,
    }


def _write(tmp_path: Path, *candidates: dict[str, Any]) -> Path:
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"candidates": list(candidates)}), encoding="utf-8")
    return path


def _review(
    path: Path | None, *extra: str, fmt: str = "json"
) -> tuple[int, dict[str, Any] | None, str]:
    args = ["curate", "review-ready", "--format", fmt, *extra]
    if path is not None:
        args += ["--candidates", str(path)]
    result = runner.invoke(app, args)
    data = json.loads(result.stdout.strip()) if fmt == "json" else None
    return result.exit_code, data, result.stdout


def _seed(
    *, docs: dict[str, tuple[str, dict]] | None = None, nodes: tuple[str, ...] = ()
) -> None:
    registry = curate_cli._get_registry()
    try:
        for doc_id, (content, metadata) in (docs or {}).items():
            registry.knowledge.document_store.put(doc_id, content, metadata)
        for node_id in nodes:
            registry.knowledge.graph_store.upsert_node(node_id, "entity", {})
    finally:
        registry.close()


def _promote_events() -> list[Any]:
    registry = curate_cli._get_registry()
    try:
        return [
            e
            for e in registry.operational.event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED, limit=500
            )
            if e.payload.get("operation") == "precedent.promote"
            and e.payload.get("status") == "success"
        ]
    finally:
        registry.close()


def _store_counts() -> tuple[int, int, int]:
    registry = curate_cli._get_registry()
    try:
        graph = registry.knowledge.graph_store
        events = registry.operational.event_log.get_events(limit=100_000)
        return len(events), graph.count_nodes(), graph.count_edges()
    finally:
        registry.close()


def _node(entity_id: str) -> dict[str, Any] | None:
    registry = curate_cli._get_registry()
    try:
        return registry.knowledge.graph_store.get_node(entity_id)
    finally:
        registry.close()


def _entity(candidate_id: str) -> str:
    return precedent_entity_id_for_candidate(candidate_id)


def _ready_set(tmp_path: Path) -> Path:
    """a(2), g(1) clean; b(5) contradicted; e(9) promoted by a test; plus non-ready."""
    return _write(
        tmp_path,
        _candidate(
            "fam_a:a",
            item_id="doc:a",
            helpful=2,
            sessions=2,
            sessionless=1,
            targets=(TARGET, "entity://other"),
        ),
        _candidate("fam_a:g", item_id="doc:g", helpful=1),
        _candidate("fam_a:b", item_id="doc:b", helpful=5),
        _candidate(
            "fam_b:b",
            item_id="doc:b",
            family="fam_b",
            recommendation_type="investigate_noise",
            helpful=0,
            unhelpful=3,
        ),
        _candidate("fam_a:e", item_id="doc:e", helpful=9),
        # Not ready: an unhelpful citation, no helpful one, the wider type.
        _candidate("fam_a:u", item_id="doc:u", helpful=4, unhelpful=1),
        _candidate("fam_a:z", item_id="doc:z", helpful=0),
        _candidate(
            "fam_a:p",
            item_id="doc:p",
            recommendation_type="promote_precedent",
            helpful=3,
        ),
    )


class TestListing:
    def test_lists_exactly_the_digest_ready_set_clean_first_flagged_last(
        self, tmp_path: Path
    ) -> None:
        path = _ready_set(tmp_path)
        # Promote e first, so it is the already-promoted row.
        code, data, _ = _review(path, "--approve", "fam_a:e")
        assert code == EXIT_OK, data

        code, data, _ = _review(path)
        assert code == EXIT_OK
        assert data is not None
        assert data["status"] == "ok"
        ids = [c["candidate_id"] for c in data["candidates"]]
        assert ids == ["fam_a:a", "fam_a:g", "fam_a:b", "fam_a:e"]
        # The count the digest announces is the count this lists.
        report = {"candidates": json.loads(path.read_text())["candidates"]}
        write_learning_review_artifacts(report=report, output_dir=tmp_path / "out")
        assert data["ready_count"] == report["promotion_ready"]["count"] == 4

    def test_each_row_carries_the_evidence_and_the_flags(self, tmp_path: Path) -> None:
        path = _ready_set(tmp_path)
        _seed(docs={"doc:a": ("Check the retry budget first.", {})})
        _, data, _ = _review(path)
        assert data is not None
        rows = {c["candidate_id"]: c for c in data["candidates"]}
        a = rows["fam_a:a"]
        assert a["intent_family"] == "fam_a"
        assert a["item_id"] == "doc:a"
        assert a["source_excerpt"] == "Check the retry budget first."
        assert a["source_excerpt_status"] == "included"
        assert (a["helpful_count"], a["unhelpful_count"]) == (2, 0)
        assert (a["distinct_helpful_sessions"], a["sessionless_helpful_count"]) == (
            2,
            1,
        )
        assert a["target_entity_count"] == 2
        assert a["cross_family_noise"] is False
        assert a["already_promoted"] is False
        b = rows["fam_a:b"]
        assert b["cross_family_noise"] is True
        assert b["noise_intent_families"] == ["fam_b"]

    def test_excerpt_is_short_and_a_withheld_source_is_not_quoted(
        self, tmp_path: Path
    ) -> None:
        path = _ready_set(tmp_path)
        _seed(
            docs={
                "doc:a": ("Sentence one is here. " * 80, {}),
                "doc:b": (
                    "Text that must not be copied.",
                    {"content_tags": {"signal_quality": "noise"}},
                ),
            }
        )
        _, data, _ = _review(path)
        assert data is not None
        rows = {c["candidate_id"]: c for c in data["candidates"]}
        assert 0 < len(rows["fam_a:a"]["source_excerpt"]) <= REVIEW_EXCERPT_MAX_CHARS
        assert rows["fam_a:b"]["source_excerpt"] is None
        assert rows["fam_a:b"]["source_excerpt_status"] == "source_withheld"
        assert rows["fam_a:g"]["source_excerpt_status"] == "document_not_found"

    def test_absent_session_fields_read_as_absent_not_zero(
        self, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path,
            _candidate("fam_a:old", item_id="doc:o", sessions=None, sessionless=None),
        )
        _, data, _ = _review(path)
        assert data is not None
        row = data["candidates"][0]
        assert row["distinct_helpful_sessions"] is None
        assert row["sessionless_helpful_count"] is None

    def test_listing_is_read_only_and_limit_caps_the_list_not_the_count(
        self, tmp_path: Path
    ) -> None:
        path = _ready_set(tmp_path)
        before = _store_counts()
        code, data, _ = _review(path, "--limit", "1")
        assert code == EXIT_OK
        assert _review(path, "--limit", "1", fmt="text")[0] == EXIT_OK
        assert _store_counts() == before
        assert data is not None
        assert data["ready_count"] == 4
        assert data["shown_count"] == 1
        assert [c["candidate_id"] for c in data["candidates"]] == ["fam_a:e"]
        assert _promote_events() == []
        assert _node(_entity("fam_a:a")) is None

    def test_text_listing_shows_flags_and_markup_survives_colour(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(
            tmp_path,
            _candidate("fam_[/x]:c[b]", item_id="doc:[red]x[/red]", helpful=2),
            _candidate(
                "fam_[/x]:n",
                item_id="doc:[red]x[/red]",
                family="fam_n[/n]",
                recommendation_type="investigate_noise",
                helpful=0,
                unhelpful=2,
            ),
        )
        _seed(docs={"doc:[red]x[/red]": ("Body with [bold]brackets[/bold].", {})})
        force_colour(monkeypatch, curate_cli)
        code, _, out = _review(path, fmt="text")
        assert code == EXIT_OK
        rendered = assert_coloured(out)
        assert "fam_[/x]:c[b]" in rendered
        assert "doc:[red]x[/red]" in rendered
        assert "Body with [bold]brackets[/bold]." in rendered
        assert "contradicted" in rendered
        assert "fam_n[/n]" in rendered

    def test_empty_ready_set_says_so_in_both_formats(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _candidate("fam_a:z", item_id="doc:z", helpful=0))
        code, data, _ = _review(path)
        assert code == EXIT_OK
        assert data is not None
        assert data["candidates"] == []
        code, _, out = _review(path, fmt="text")
        assert code == EXIT_OK
        assert "No promotion-ready candidates" in plain(out)

    def test_default_path_is_the_learning_artifacts_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        (artifacts / LEARNING_CANDIDATES_FILENAME).write_text(
            json.dumps({"candidates": [_candidate("fam_a:d", item_id="doc:d")]}),
            encoding="utf-8",
        )
        monkeypatch.setenv(LEARNING_ARTIFACTS_DIR_ENV, str(artifacts))
        code, data, _ = _review(None)
        assert code == EXIT_OK
        assert data is not None
        assert data["ready_count"] == 1

    def test_unreadable_candidates_file_is_a_usage_error(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            ["curate", "review-ready", "--candidates", str(tmp_path / "nope.json")],
        )
        assert result.exit_code == EXIT_VALIDATION


class TestApprove:
    def test_approval_writes_precedent_excerpt_edges_and_one_promote_event(
        self, tmp_path: Path
    ) -> None:
        path = _ready_set(tmp_path)
        _seed(
            docs={"doc:a": ("Check the retry budget first.", {})},
            nodes=(TARGET, "entity://other"),
        )
        code, data, _ = _review(
            path, "--approve", "fam_a:a", "--rationale", "reviewed by hand"
        )
        assert code == EXIT_OK, data
        assert data is not None
        assert data["status"] == "ok"
        assert (data["approved_count"], data["promoted_count"]) == (1, 1)
        (outcome,) = data["results"]
        assert outcome["status"] == "promoted"
        assert outcome["candidate_id"] == "fam_a:a"

        entity_id = _entity("fam_a:a")
        node = _node(entity_id)
        assert node is not None
        assert node["node_type"] == "precedent"
        assert "Check the retry budget first." in node["properties"]["description"]
        assert node["properties"]["source_excerpt_status"] == "included"
        assert node["properties"]["approved_rationale"] == "reviewed by hand"

        registry = curate_cli._get_registry()
        try:
            edges = registry.knowledge.graph_store.get_edges(
                entity_id, direction="outgoing", edge_type="precedent_applies_to"
            )
        finally:
            registry.close()
        assert {e["target_id"] for e in edges} == {TARGET, "entity://other"}

        events = _promote_events()
        assert [e.entity_id for e in events] == [entity_id]

    def test_the_governed_requester_is_review_ready(self, tmp_path: Path) -> None:
        path = _ready_set(tmp_path)
        _review(path, "--approve", "fam_a:g")
        registry = curate_cli._get_registry()
        try:
            events = registry.operational.event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED, limit=500
            )
        finally:
            registry.close()
        requesters = {
            e.payload.get("requested_by")
            for e in events
            if e.payload.get("operation")
            in {"entity.create", "link.create", "precedent.promote"}
        }
        assert requesters == {"cli:review-ready"}

    def test_a_dangling_target_leaves_the_approval_incomplete_not_ok(
        self, tmp_path: Path
    ) -> None:
        path = _ready_set(tmp_path)  # fam_a:a targets nodes that do not exist
        code, data, _ = _review(path, "--approve", "fam_a:a")
        assert data is not None
        assert data["results"][0]["status"] == "promoted_incomplete"
        assert data["status"] == "failed"
        assert code == EXIT_STORE
        assert data["promoted_count"] == 0

    def test_not_ready_ids_are_refused_and_write_nothing(self, tmp_path: Path) -> None:
        path = _ready_set(tmp_path)
        code, data, _ = _review(
            path,
            "--approve",
            "fam_a:u",  # unhelpful citation
            "--approve",
            "fam_a:z",  # no helpful citation
            "--approve",
            "fam_a:p",  # promote_precedent, not promote_guidance
            "--approve",
            "fam_x:none",  # unknown
        )
        assert code == EXIT_VALIDATION
        assert data is not None
        assert data["status"] == "refused"
        assert [r["status"] for r in data["results"]] == ["refused_not_ready"] * 4
        assert data["promoted_count"] == 0
        assert _promote_events() == []
        for cid in ("fam_a:u", "fam_a:z", "fam_a:p"):
            assert _node(_entity(cid)) is None

    def test_an_already_promoted_id_is_refused_and_promotes_once(
        self, tmp_path: Path
    ) -> None:
        path = _ready_set(tmp_path)
        assert _review(path, "--approve", "fam_a:g")[0] == EXIT_OK
        code, data, _ = _review(path, "--approve", "fam_a:g")
        assert code == EXIT_VALIDATION
        assert data is not None
        assert data["results"][0]["status"] == "refused_already_promoted"
        assert len(_promote_events()) == 1

    def test_a_precedent_node_alone_counts_as_already_promoted(
        self, tmp_path: Path
    ) -> None:
        """No promote event, but the node is there: the event scan is capped."""
        path = _ready_set(tmp_path)
        registry = curate_cli._get_registry()
        try:
            registry.knowledge.graph_store.upsert_node(
                _entity("fam_a:g"), "precedent", {}
            )
        finally:
            registry.close()
        code, data, _ = _review(path, "--approve", "fam_a:g")
        assert code == EXIT_VALIDATION
        assert data is not None
        assert data["results"][0]["status"] == "refused_already_promoted"
        assert _promote_events() == []

    def test_a_contradicted_id_is_refused_unless_forced(self, tmp_path: Path) -> None:
        path = _ready_set(tmp_path)
        code, data, _ = _review(path, "--approve", "fam_a:b")
        assert code == EXIT_VALIDATION
        assert data is not None
        refusal = data["results"][0]
        assert refusal["status"] == "refused_contradicted"
        assert refusal["noise_intent_families"] == ["fam_b"]
        assert _node(_entity("fam_a:b")) is None

        code, data, _ = _review(path, "--approve", "fam_a:b", "--force-contradicted")
        assert code == EXIT_OK, data
        assert data is not None
        assert data["results"][0]["status"] == "promoted"
        assert _node(_entity("fam_a:b")) is not None

    def test_force_contradicted_does_not_lift_the_other_refusals(
        self, tmp_path: Path
    ) -> None:
        path = _ready_set(tmp_path)
        assert _review(path, "--approve", "fam_a:b", "--force-contradicted")[0] == 0
        code, data, _ = _review(
            path, "--approve", "fam_a:b", "--approve", "fam_a:u", "--force-contradicted"
        )
        assert code == EXIT_VALIDATION
        assert data is not None
        assert [r["status"] for r in data["results"]] == [
            "refused_already_promoted",
            "refused_not_ready",
        ]

    def test_mixed_ids_land_independently_and_the_run_still_fails(
        self, tmp_path: Path
    ) -> None:
        path = _ready_set(tmp_path)
        code, data, _ = _review(
            path, "--approve", "fam_a:g", "--approve", "fam_a:z", "--approve", "fam_a:g"
        )
        assert code == EXIT_VALIDATION
        assert data is not None
        # A repeated id is one outcome, not two promotions.
        assert [(r["candidate_id"], r["status"]) for r in data["results"]] == [
            ("fam_a:g", "promoted"),
            ("fam_a:z", "refused_not_ready"),
        ]
        assert data["promoted_count"] == 1
        assert len(_promote_events()) == 1

    def test_text_approval_names_each_outcome_and_survives_markup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(tmp_path, _candidate("fam_[/x]:c[b]", item_id="doc:c"))
        force_colour(monkeypatch, curate_cli)
        code, _, out = _review(
            path, "--approve", "fam_[/x]:c[b]", "--approve", "fam_[/y]:gone", fmt="text"
        )
        assert code == EXIT_VALIDATION
        rendered = assert_coloured(out)
        assert "promoted fam_[/x]:c[b]" in rendered
        assert "refused_not_ready fam_[/y]:gone" in rendered


def _fake_submit(outcome: dict[str, Any]) -> Any:
    def submit(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return dict(outcome)

    return submit


#: scenario -> (approve args, submit override or None, expected exit, expected status)
_SCENARIOS: dict[str, tuple[list[str], dict[str, Any] | None, int, str]] = {
    "landed": (["--approve", "fam_a:g"], None, EXIT_OK, "ok"),
    "refused": (["--approve", "fam_a:z"], None, EXIT_VALIDATION, "refused"),
    "contradicted": (["--approve", "fam_a:b"], None, EXIT_VALIDATION, "refused"),
    "entity_failed": (
        ["--approve", "fam_a:g"],
        {"status": "entity_failed", "message": "boom"},
        EXIT_STORE,
        "failed",
    ),
    "refused_state": (
        ["--approve", "fam_a:g"],
        {"status": "refused_demoted", "message": "demoted"},
        EXIT_VALIDATION,
        "refused",
    ),
    "event_not_recorded": (
        ["--approve", "fam_a:g"],
        {
            "status": "promoted",
            "node_id": "n",
            "edges": [],
            "precedent_event_status": "rejected",
        },
        EXIT_STORE,
        "failed",
    ),
}


class TestFormatParity:
    @pytest.mark.parametrize("fmt", ["json", "text"])
    @pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
    def test_exit_code_and_status_do_not_depend_on_format(
        self,
        scenario: str,
        fmt: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        args, override, expected_exit, expected_status = _SCENARIOS[scenario]
        if override is not None:
            monkeypatch.setattr(
                curate_cli, "submit_learning_promotion", _fake_submit(override)
            )
        code, data, out = _review(_ready_set(tmp_path), *args, fmt=fmt)
        assert code == expected_exit, out
        if fmt == "json":
            assert data is not None
            assert data["status"] == expected_status
        else:
            # The human surface states the verdict; it does not only exit.
            assert f"Verdict: {expected_status} (exit {expected_exit})" in plain(out)

    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_listing_exits_zero_and_prints_no_verdict(
        self, fmt: str, tmp_path: Path
    ) -> None:
        # Nothing was promoted, so there is no outcome to give a verdict on.
        code, data, out = _review(_ready_set(tmp_path), fmt=fmt)
        assert code == EXIT_OK, out
        if data is not None:
            assert data["status"] == "ok"
        else:
            assert "Verdict:" not in plain(out)


#: scenario -> (submit override or None, id to approve). Each is approved
#: through ``review-ready --approve`` and through a ``promote-learning``
#: decisions file; the two must reach the same verdict.
_SHARED_VERDICT_SCENARIOS: dict[str, tuple[dict[str, Any] | None, str]] = {
    "landed": (
        {
            "status": "promoted",
            "node_id": "n",
            "edges": [],
            "precedent_event_status": "success",
        },
        "fam_a:g",
    ),
    "failed": ({"status": "entity_failed", "message": "boom"}, "fam_a:g"),
    "refused_state": ({"status": "refused_demoted", "message": "d"}, "fam_a:g"),
    "event_not_recorded": (
        {
            "status": "promoted",
            "node_id": "n",
            "edges": [],
            "precedent_event_status": "rejected",
        },
        "fam_a:g",
    ),
    "unknown_id": (None, "fam_a:nope"),
}


class TestSameVerdictAsPromoteLearning:
    @pytest.mark.parametrize("scenario", sorted(_SHARED_VERDICT_SCENARIOS))
    def test_review_approve_prints_the_verdict_promote_learning_prints(
        self,
        scenario: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        override, approved_id = _SHARED_VERDICT_SCENARIOS[scenario]
        if override is not None:
            monkeypatch.setattr(
                curate_cli, "submit_learning_promotion", _fake_submit(override)
            )
        path = _ready_set(tmp_path)
        decisions = tmp_path / "decisions.json"
        decisions.write_text(
            json.dumps(
                {"decisions": [{"candidate_id": approved_id, "approved": True}]}
            ),
            encoding="utf-8",
        )

        review_code, _, review_out = _review(path, "--approve", approved_id, fmt="text")
        promote = runner.invoke(
            app,
            [
                "curate",
                "promote-learning",
                "--candidates",
                str(path),
                "--decisions",
                str(decisions),
                "--format",
                "text",
            ],
        )

        assert review_code == promote.exit_code
        review_verdict = [
            line
            for line in plain(review_out).splitlines()
            if line.startswith("Verdict:")
        ]
        promote_verdict = [
            line
            for line in plain(promote.stdout).splitlines()
            if line.startswith("Verdict:")
        ]
        assert len(review_verdict) == 1
        assert review_verdict == promote_verdict
