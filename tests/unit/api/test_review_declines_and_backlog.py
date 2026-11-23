"""A reviewer can decline a learning candidate, and loop health sizes the queue.

Before this, the Review queue's learning section only ever recorded an
approval: ``prepare_learning_promotions`` drops every ``approved=False``
row, so a reviewer who said "no" left nothing behind and the agreement
metric could only read ``None`` or ``1.0``. These tests pin the decline
route, the "declined on" stamp the listing carries, the promote event's
accepted ids, and the per-kind backlog ``GET /loops`` reports.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.unit.api import test_review_routes as _routes
from trellis.stores.base.event_log import EventType

# Shared fixtures and seeders from the neighbouring Review-route suite.
_clean_auth_env = _routes._clean_auth_env
client = _routes.client
registry = _routes.registry
_learning_candidate = _routes._learning_candidate
_seed_candidate_target = _routes._seed_candidate_target
_seed_code_proposal = _routes._seed_code_proposal
_seed_proposal = _routes._seed_proposal
_seed_well_known_candidate = _routes._seed_well_known_candidate
_write_learning_candidates = _routes._write_learning_candidates


def _decisions(registry, action):
    return registry.operational.event_log.get_events(
        event_type=EventType.REVIEW_DECISION_RECORDED,
        payload_filters={"surface": "learning_promotion", "action": action},
        limit=50,
    )


class TestDeclineRoute:
    def test_decline_emits_a_review_decision_with_the_candidate(
        self, client, registry, tmp_path, monkeypatch
    ):
        cand = _learning_candidate()
        _write_learning_candidates(tmp_path, monkeypatch, [cand])
        resp = client.post(
            f"/api/v1/learning/candidates/{cand['candidate_id']}/decline",
            json={"reason": "duplicates an existing precedent"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["candidate_id"] == cand["candidate_id"]
        assert body["status"] == "declined"
        assert body["reason"] == "duplicates an existing precedent"
        events = _decisions(registry, "decline")
        assert len(events) == 1
        payload = events[0].payload
        assert payload["candidate_ids"] == [cand["candidate_id"]]
        assert payload["reason"] == "duplicates an existing precedent"
        # Stamped with the caller identity like every other review decision.
        assert "key_id" in payload
        assert "key_name" in payload
        assert events[0].entity_id == cand["candidate_id"]

    def test_decline_without_a_body_uses_the_default_reason(
        self, client, registry, tmp_path, monkeypatch
    ):
        cand = _learning_candidate()
        _write_learning_candidates(tmp_path, monkeypatch, [cand])
        resp = client.post(
            f"/api/v1/learning/candidates/{cand['candidate_id']}/decline"
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["reason"] == "declined_by_reviewer"

    def test_decline_of_an_unlisted_candidate_is_404_and_records_nothing(
        self, client, registry, tmp_path, monkeypatch
    ):
        _write_learning_candidates(tmp_path, monkeypatch, [_learning_candidate()])
        resp = client.post("/api/v1/learning/candidates/no-such-id/decline")
        assert resp.status_code == 404
        assert _decisions(registry, "decline") == []

    def test_an_overlong_reason_is_rejected(
        self, client, registry, tmp_path, monkeypatch
    ):
        cand = _learning_candidate()
        _write_learning_candidates(tmp_path, monkeypatch, [cand])
        resp = client.post(
            f"/api/v1/learning/candidates/{cand['candidate_id']}/decline",
            json={"reason": "x" * 2001},
        )
        assert resp.status_code == 422
        assert _decisions(registry, "decline") == []

    def test_decline_without_an_artifact_is_409(self, client, registry):
        resp = client.post("/api/v1/learning/candidates/any/decline")
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "learning_artifacts_dir_missing"
        assert _decisions(registry, "decline") == []


class TestDeclinedOnListing:
    def test_declined_candidate_stays_listed_with_its_decline_time(
        self, client, registry, tmp_path, monkeypatch
    ):
        declined = _learning_candidate("source_analysis:declined")
        other = _learning_candidate("source_analysis:other")
        _write_learning_candidates(tmp_path, monkeypatch, [declined, other])
        client.post(f"/api/v1/learning/candidates/{declined['candidate_id']}/decline")
        data = client.get("/api/v1/learning/candidates").json()
        assert data["status"] == "ok"
        by_id = {c["candidate_id"]: c for c in data["candidates"]}
        # Never hidden: both candidates are still served.
        assert set(by_id) == {declined["candidate_id"], other["candidate_id"]}
        stamp = by_id[declined["candidate_id"]]["last_declined_at"]
        assert stamp is not None
        assert datetime.fromisoformat(stamp) <= datetime.now(UTC) + timedelta(seconds=5)
        assert by_id[other["candidate_id"]]["last_declined_at"] is None


class TestPromoteEventNamesOnlyAcceptedIds:
    def test_a_refused_approval_is_not_in_candidate_ids(
        self, client, registry, tmp_path, monkeypatch
    ):
        _seed_candidate_target(registry)
        good = _learning_candidate("source_analysis:good")
        report_only = _learning_candidate(
            "source_analysis:report", recommendation_type="investigate_noise"
        )
        _write_learning_candidates(tmp_path, monkeypatch, [good, report_only])
        client.post(
            "/api/v1/learning/promotions",
            json={
                "decisions": [
                    {"candidate_id": good["candidate_id"], "approved": True},
                    {"candidate_id": report_only["candidate_id"], "approved": True},
                ]
            },
        )
        (event,) = _decisions(registry, "promote")
        assert event.payload["candidate_ids"] == [good["candidate_id"]]
        assert event.payload["refused_candidate_ids"] == [report_only["candidate_id"]]


def _backlog(client):
    data = client.get("/api/v1/loops").json()
    return {row["kind"]: row for row in data["review_backlog"]}


class TestReviewBacklog:
    def test_every_review_kind_is_reported(self, client, registry):
        rows = _backlog(client)
        assert set(rows) == {
            "tuner_proposal",
            "learning_candidate",
            "schema_evolution",
            "code_proposal",
        }

    def test_only_reviewer_resolvable_kinds_are_actionable(self, client, registry):
        rows = _backlog(client)
        assert {k: r["actionable"] for k, r in rows.items()} == {
            "tuner_proposal": True,
            "learning_candidate": True,
            "schema_evolution": False,
            "code_proposal": False,
        }
        # Each row states the window it counted over.
        assert rows["schema_evolution"]["scan_limit"] == 5000
        assert rows["code_proposal"]["scan_limit"] == 5000

    def test_backlog_route_sizes_only_actionable_kinds_without_event_scans(
        self, client, registry, monkeypatch
    ):
        _seed_proposal(registry)
        _seed_well_known_candidate(registry, "entity_type:one")
        log = registry.operational.event_log
        real = log.get_events
        asked: list[object] = []

        def spy(*args, **kwargs):
            asked.append(kwargs.get("event_type"))
            return real(*args, **kwargs)

        monkeypatch.setattr(log, "get_events", spy)
        resp = client.get("/api/v1/review/backlog")
        assert resp.status_code == 200, resp.text
        rows = {r["kind"]: r for r in resp.json()["review_backlog"]}
        assert set(rows) == {"tuner_proposal", "learning_candidate"}
        assert all(r["actionable"] for r in rows.values())
        assert rows["tuner_proposal"]["pending_count"] == 1
        assert EventType.WELL_KNOWN_CANDIDATE not in asked
        assert EventType.PROPOSAL_DRAFTED not in asked

    def test_empty_event_kinds_read_zero_with_no_age(self, client, registry):
        rows = _backlog(client)
        for kind in ("tuner_proposal", "schema_evolution", "code_proposal"):
            assert rows[kind]["status"] == "ok"
            assert rows[kind]["pending_count"] == 0
            assert rows[kind]["oldest_pending_at"] is None
            assert rows[kind]["oldest_pending_age_seconds"] is None

    def test_counts_and_oldest_age_per_kind(
        self, client, registry, tmp_path, monkeypatch
    ):
        _seed_proposal(registry)
        _seed_well_known_candidate(registry, "entity_type:one")
        _seed_well_known_candidate(registry, "entity_type:two")
        _seed_well_known_candidate(registry, "entity_type:one")
        _seed_code_proposal(registry, "prop-1")
        promotable = _learning_candidate("source_analysis:a")
        decided = _learning_candidate("source_analysis:b")
        report_only = _learning_candidate(
            "source_analysis:c", recommendation_type="investigate_noise"
        )
        _write_learning_candidates(
            tmp_path, monkeypatch, [promotable, decided, report_only]
        )
        client.post(f"/api/v1/learning/candidates/{decided['candidate_id']}/decline")

        rows = _backlog(client)
        assert rows["tuner_proposal"]["pending_count"] == 1
        assert rows["schema_evolution"]["pending_count"] == 2
        assert rows["code_proposal"]["pending_count"] == 1
        # Promotable and undecided only: the declined one has a decision and
        # the report-only one has no decision to make.
        assert rows["learning_candidate"]["status"] == "ok"
        assert rows["learning_candidate"]["pending_count"] == 1
        # The artifact's own generation time: candidates carry no per-item one.
        assert rows["learning_candidate"]["oldest_pending_at"].startswith(
            "2026-06-01T00:00:00"
        )
        for kind in ("tuner_proposal", "schema_evolution", "code_proposal"):
            assert rows[kind]["oldest_pending_at"] is not None
            assert rows[kind]["oldest_pending_age_seconds"] >= 0

    def test_unwritten_learning_artifact_is_null_not_zero(self, client, registry):
        row = _backlog(client)["learning_candidate"]
        assert row["status"] == "not_written"
        assert row["reason"] == "learning_artifacts_dir_missing"
        assert row["pending_count"] is None
        assert row["oldest_pending_at"] is None

    def test_unreadable_learning_artifact_is_null_not_zero(
        self, client, registry, tmp_path, monkeypatch
    ):
        artifacts = _write_learning_candidates(tmp_path, monkeypatch, [])
        (artifacts / "intent_learning_candidates.json").write_text(
            "{not json", encoding="utf-8"
        )
        row = _backlog(client)["learning_candidate"]
        assert row["status"] == "unreadable"
        assert row["reason"] == "learning_candidates_unreadable"
        assert row["pending_count"] is None

    def test_unreadable_sources_are_null_not_zero(self, registry, monkeypatch):
        # Called directly: the tuner loop row above it reads the same store
        # and is outside this change.
        from trellis.ops.loop_health import summarize_review_backlog

        msg = "store offline"

        def boom(**_kwargs):
            raise RuntimeError(msg)

        monkeypatch.setattr(
            registry.operational.tuner_state_store, "list_proposals", boom
        )
        monkeypatch.setattr(registry.operational.event_log, "get_events", boom)
        rows = {
            r.kind: r
            for r in summarize_review_backlog(
                registry.operational.event_log,
                registry.operational.tuner_state_store,
                learning_candidates=[_learning_candidate()],
                learning_generated_at=None,
            )
        }
        for kind in ("tuner_proposal", "schema_evolution", "code_proposal"):
            assert rows[kind].status == "unreadable", kind
            assert rows[kind].pending_count is None, kind
            assert rows[kind].oldest_pending_at is None, kind
        # The learning row needs the decision events to tell pending from
        # decided, so an unreadable EventLog makes it unreadable too.
        assert rows["learning_candidate"].status == "unreadable"
        assert rows["learning_candidate"].pending_count is None


def _decline_events(event_log, *candidate_ids):
    for candidate_id in candidate_ids:
        event_log.emit(
            EventType.REVIEW_DECISION_RECORDED,
            source="trellis_api.review",
            payload={
                "surface": "learning_promotion",
                "action": "decline",
                "candidate_ids": [candidate_id],
            },
        )


def _learning_row(registry, ids, *, decision_limit):
    from trellis.ops.loop_health import summarize_review_backlog

    rows = summarize_review_backlog(
        registry.operational.event_log,
        registry.operational.tuner_state_store,
        learning_candidates=[_learning_candidate(i) for i in ids],
        learning_generated_at=datetime(2026, 6, 1, tzinfo=UTC),
        actionable_only=True,
        decision_limit=decision_limit,
    )
    return {r.kind: r for r in rows}["learning_candidate"]


class TestLearningBacklogDecisionCap:
    """Past the decision read's cap the oldest decisions drop out, so a
    decided candidate reads as pending: the row must say it is a floor."""

    def test_a_capped_decision_read_marks_the_row_truncated(self, registry):
        ids = ["source_analysis:a", "source_analysis:b", "source_analysis:c"]
        _decline_events(registry.operational.event_log, *ids)
        row = _learning_row(registry, ids, decision_limit=2)
        assert row.status == "ok"
        # One decided candidate fell out of the read and counts as pending.
        assert row.pending_count == 1
        assert row.truncated is True
        assert row.scan_limit == 2

    def test_exactly_the_limit_of_decisions_is_not_truncated(self, registry):
        ids = ["source_analysis:a", "source_analysis:b"]
        _decline_events(registry.operational.event_log, *ids)
        row = _learning_row(registry, ids, decision_limit=2)
        assert row.pending_count == 0
        assert row.truncated is False
        assert row.scan_limit == 2

    def test_the_row_states_the_default_decision_limit(
        self, client, registry, tmp_path, monkeypatch
    ):
        from trellis.learning.review_decisions import DEFAULT_DECISION_SCAN_LIMIT

        _write_learning_candidates(tmp_path, monkeypatch, [_learning_candidate()])
        row = _backlog(client)["learning_candidate"]
        assert row["status"] == "ok"
        assert row["scan_limit"] == DEFAULT_DECISION_SCAN_LIMIT
        assert row["truncated"] is False


# ---------------------------------------------------------------------------
# The Review view: a Decline control and the "declined on" stamp
# ---------------------------------------------------------------------------

from tests.unit.api.test_ui_review_truth import (  # noqa: E402
    needs_node,
    render_review_learning,
)


def _listing(*candidates):
    return {
        "status": "ok",
        "artifacts_dir": "/data/learning",
        "generated_at_utc": "2026-06-01T00:00:00.000Z",
        "candidate_count": len(candidates),
        "candidates": list(candidates),
    }


@needs_node
def test_a_declined_candidate_renders_its_decline_date_and_stays(tmp_path):
    declined = {
        **_learning_candidate("source_analysis:declined"),
        "promotable": True,
        "last_declined_at": "2026-06-02T09:30:00+00:00",
    }
    fresh = {
        **_learning_candidate("source_analysis:fresh"),
        "promotable": True,
        "last_declined_at": None,
    }
    body = render_review_learning(_listing(declined, fresh), tmp_path)["body"]
    assert 'data-candidate="source_analysis:declined"' in body
    assert body.count("declined on ") == 1
    assert "2026-06-02" in body
    # Both promotable cards keep Approve and get Decline.
    assert body.count('class="learn-approve"') == 2
    assert body.count('data-action="declineLearningCandidate"') == 2


@needs_node
def test_a_report_only_candidate_gets_no_decline_control(tmp_path):
    report_only = {
        **_learning_candidate(
            "source_analysis:report", recommendation_type="investigate_noise"
        ),
        "promotable": False,
        "last_declined_at": None,
    }
    body = render_review_learning(_listing(report_only), tmp_path)["body"]
    assert 'data-action="declineLearningCandidate"' not in body


def test_decline_handler_is_registered_for_data_action():
    from tests.unit.api.test_review_nav_badge import _page

    page = _page()
    registry_block = page[page.index("const ID_ACTIONS = {") :]
    registry_block = registry_block[: registry_block.index("};")]
    assert "declineLearningCandidate" in registry_block
    assert "`/learning/candidates/${encodeURIComponent(cid)}/decline`" in page
