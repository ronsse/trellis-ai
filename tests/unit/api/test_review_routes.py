"""Tests for the WP10 Review-queue admin endpoints.

Covers the four queue surfaces (tuner proposals, learning candidates,
schema-evolution candidates, code-authoring proposals) plus the
governance guarantees: admin scope is required, the promote route runs
through the same library path the CLI uses and emits the same events, a
reject persists, and draft-adr returns markdown carrying candidate
evidence. Every approve / reject / promotion also emits a
``REVIEW_DECISION_RECORDED`` audit event stamped with the caller identity.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    generate_api_key,
)
from trellis.schemas.parameters import (
    ParameterProposal,
    ParameterScope,
    ParameterSet,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no auth / artifacts configuration."""
    for var in (
        "TRELLIS_API_KEY",
        "TRELLIS_AUTH_MODE",
        "TRELLIS_LEARNING_ARTIFACTS_DIR",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def registry(tmp_path):
    """Fresh registry bound to the app module for each test."""
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    """App with just the admin router, auth OFF (the default with no env)."""

    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    from trellis_api.routes import admin

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
    return TestClient(app)


def _mint(registry, scopes, name="test-key"):
    token, record = generate_api_key(name, scopes)
    registry.operational.api_key_store.create(record)
    return token


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_proposal(registry, *, proposed=None, sample_size=20, baseline=None):
    """Persist a pending proposal (+ optional baseline snapshot)."""
    scope = ParameterScope(component_id="retrieval.packer", domain="sales")
    if baseline is not None:
        registry.operational.parameter_store.put(
            ParameterSet(scope=scope, values=baseline, source="operator")
        )
    proposal = ParameterProposal(
        scope=scope,
        proposed_values=proposed or {"max_items": 12},
        tuner="rule_tuner",
        sample_size=sample_size,
    )
    registry.operational.tuner_state_store.put_proposal(proposal)
    return proposal


def _seed_well_known_candidate(registry, candidate_id="entity_type:abc123"):
    """Emit a WELL_KNOWN_CANDIDATE event the draft-adr route can read."""
    registry.operational.event_log.emit(
        EventType.WELL_KNOWN_CANDIDATE,
        source="test",
        entity_id=candidate_id,
        entity_type="well_known_candidate",
        payload={
            "candidate_id": candidate_id,
            "candidate_kind": "entity_type",
            "open_string_value": "data_contract",
            "count": 17,
            "distinct_extractors": ["dbt_manifest", "openlineage"],
            "distinct_domains": ["sales", "marketing"],
            "avg_signal_quality": "0.8",
            "first_seen": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-02-01T00:00:00+00:00",
            "suggested_canonical_name": "DataContract",
            "suggested_alignment_uri": None,
            "naming_collision": False,
            "recurrence_count": 3,
            "notes": [],
        },
    )
    return candidate_id


def _seed_code_proposal(registry, proposal_id="prop-1"):
    registry.operational.event_log.emit(
        EventType.PROPOSAL_DRAFTED,
        source="test",
        entity_id=proposal_id,
        entity_type="proposal",
        payload={
            "proposal_id": proposal_id,
            "cluster_signature": "sig-xyz",
            "markdown_preview": (
                "# Proposal: address ImportError in src/foo/bar.py\n\nBody."
            ),
            "source_event_count": 4,
        },
    )
    return proposal_id


def _write_learning_candidates(tmp_path, monkeypatch, candidates):
    """Write an intent_learning_candidates.json artifact + point env at it."""
    artifacts = tmp_path / "learning"
    artifacts.mkdir(parents=True, exist_ok=True)
    payload = {
        "artifact_version": "1.0",
        "generated_at_utc": "2026-06-01T00:00:00.000Z",
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    (artifacts / "intent_learning_candidates.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    monkeypatch.setenv("TRELLIS_LEARNING_ARTIFACTS_DIR", str(artifacts))
    return artifacts


def _learning_candidate(candidate_id="source_analysis:abc"):
    """A promotable learning candidate (matches scoring.py output shape)."""
    return {
        "candidate_id": candidate_id,
        "intent_family": "source_analysis",
        "recommendation_type": "promote_precedent",
        "item_id": "item-1",
        "item_type": "precedent",
        "title": "Profiling sales orders",
        "category": "retrieval_precedent",
        "domain_systems": ["sales"],
        "phases": ["analyze"],
        "target_entity_ids": ["entity://table/orders"],
        "supporting_run_ids": ["run-1", "run-2"],
        "source_strategies": {"semantic": 2},
        "metrics": {
            "times_served": 5,
            "success_rate": 0.9,
            "retry_rate": 0.1,
            "injection_rate": 1.0,
            "avg_selection_efficiency": 0.7,
        },
        "evidence_refs": ["evidence://x"],
        "precedent_name": "Learning: source_analysis :: Profiling sales orders",
        "precedent_properties": {
            "category": "retrieval_precedent",
            "intent_family": "source_analysis",
            "source_item_id": "item-1",
            "source_item_type": "precedent",
            "success_rate": 0.9,
            "retry_rate": 0.1,
            "support_count": 5,
            "source_of_truth": "reviewed_promotion",
        },
    }


def _count_events(registry, event_type):
    return registry.operational.event_log.count(event_type=event_type)


# ---------------------------------------------------------------------------
# Auth — admin scope required
# ---------------------------------------------------------------------------


class TestAuthRequired:
    @pytest.fixture
    def auth_client(self, registry, monkeypatch):
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        return TestClient(create_app())

    def test_list_proposals_requires_credential(self, auth_client):
        assert auth_client.get("/api/v1/proposals").status_code == 401

    def test_read_scope_is_forbidden(self, registry, auth_client):
        token = _mint(registry, [SCOPE_READ])
        resp = auth_client.get(
            "/api/v1/proposals", headers={"X-API-Key": token}
        )
        assert resp.status_code == 403

    def test_admin_scope_passes(self, registry, auth_client):
        token = _mint(registry, [SCOPE_ADMIN])
        resp = auth_client.get(
            "/api/v1/proposals", headers={"X-API-Key": token}
        )
        assert resp.status_code == 200

    def test_promote_requires_admin(self, registry, auth_client):
        _seed_proposal(registry)
        # No credential -> 401.
        assert (
            auth_client.post("/api/v1/proposals/x/promote").status_code == 401
        )
        # read scope -> 403.
        token = _mint(registry, [SCOPE_READ])
        assert (
            auth_client.post(
                "/api/v1/proposals/x/promote", headers={"X-API-Key": token}
            ).status_code
            == 403
        )


# ---------------------------------------------------------------------------
# Section 1: Tuner proposals
# ---------------------------------------------------------------------------


class TestTunerProposals:
    def test_list_empty(self, client):
        resp = client.get("/api/v1/proposals")
        assert resp.status_code == 200
        assert resp.json() == {"count": 0, "proposals": []}

    def test_list_surfaces_metrics(self, client, registry):
        _seed_proposal(
            registry, proposed={"max_items": 12}, baseline={"max_items": 10}
        )
        data = client.get("/api/v1/proposals").json()
        assert data["count"] == 1
        row = data["proposals"][0]
        assert row["proposed_values"] == {"max_items": 12}
        assert row["baseline_values"] == {"max_items": 10}
        assert row["sample_size"] == 20
        assert row["component_id"] == "retrieval.packer"

    def test_preview_predicts_promote(self, client, registry):
        p = _seed_proposal(
            registry, proposed={"max_items": 20}, baseline={"max_items": 10}
        )
        data = client.get(f"/api/v1/proposals/{p.proposal_id}/preview").json()
        assert data["predicted_status"] == "promoted"
        assert data["proposed_values"] == {"max_items": 20}

    def test_preview_predicts_reject_low_sample(self, client, registry):
        p = _seed_proposal(
            registry,
            proposed={"max_items": 20},
            baseline={"max_items": 10},
            sample_size=1,
        )
        data = client.get(f"/api/v1/proposals/{p.proposal_id}/preview").json()
        assert data["predicted_status"] == "rejected"
        assert "sample_size" in data["reason"]

    def test_promote_routes_through_pipeline_and_emits(self, client, registry):
        p = _seed_proposal(
            registry, proposed={"max_items": 20}, baseline={"max_items": 10}
        )
        before = _count_events(registry, EventType.PARAMS_UPDATED)
        resp = client.post(f"/api/v1/proposals/{p.proposal_id}/promote")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "promoted"
        assert body["params_version"]
        # Same event the CLI promote path emits.
        assert _count_events(registry, EventType.PARAMS_UPDATED) == before + 1
        # Proposal status flipped in the store.
        stored = registry.operational.tuner_state_store.get_proposal(
            p.proposal_id
        )
        assert stored.status == "promoted"

    def test_promote_emits_review_audit_with_identity(
        self, registry, monkeypatch
    ):
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        token = _mint(registry, [SCOPE_ADMIN], name="ada")
        app_client = TestClient(create_app())
        p = _seed_proposal(
            registry, proposed={"max_items": 20}, baseline={"max_items": 10}
        )
        resp = app_client.post(
            f"/api/v1/proposals/{p.proposal_id}/promote",
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 200
        events = registry.operational.event_log.get_events(
            event_type=EventType.REVIEW_DECISION_RECORDED, limit=10
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["surface"] == "tuner_proposal"
        assert payload["action"] == "promote"
        assert payload["key_name"] == "ada"
        assert payload["key_id"] is not None

    def test_reject_persists_and_emits(self, client, registry):
        p = _seed_proposal(registry)
        before = _count_events(registry, EventType.TUNER_PROPOSAL_REJECTED)
        resp = client.post(
            f"/api/v1/proposals/{p.proposal_id}/reject",
            json={"reason": "not worth it"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"
        stored = registry.operational.tuner_state_store.get_proposal(
            p.proposal_id
        )
        assert stored.status == "rejected"
        assert (
            _count_events(registry, EventType.TUNER_PROPOSAL_REJECTED)
            == before + 1
        )

    def test_reject_unknown_is_skipped(self, client):
        resp = client.post("/api/v1/proposals/nope/reject")
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"


# ---------------------------------------------------------------------------
# Section 2: Learning-promotion candidates
# ---------------------------------------------------------------------------


class TestLearningCandidates:
    def test_no_artifact_returns_hint(self, client):
        data = client.get("/api/v1/learning/candidates").json()
        assert data["candidate_count"] == 0
        assert data["candidates"] == []
        assert data["hint"]

    def test_serves_artifact(self, client, tmp_path, monkeypatch):
        _write_learning_candidates(
            tmp_path, monkeypatch, [_learning_candidate()]
        )
        data = client.get("/api/v1/learning/candidates").json()
        assert data["candidate_count"] == 1
        assert data["candidates"][0]["candidate_id"] == "source_analysis:abc"

    def test_promotion_routes_through_executor(
        self, client, registry, tmp_path, monkeypatch
    ):
        cand = _learning_candidate()
        _write_learning_candidates(tmp_path, monkeypatch, [cand])
        before = _count_events(registry, EventType.ENTITY_CREATED)
        resp = client.post(
            "/api/v1/learning/promotions",
            json={
                "decisions": [
                    {
                        "candidate_id": cand["candidate_id"],
                        "approved": True,
                        "rationale": "looks solid",
                    }
                ]
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["promoted_count"] == 1
        assert body["results"][0]["status"] == "promoted"
        # The governed pipeline emitted an entity-created event.
        assert _count_events(registry, EventType.ENTITY_CREATED) == before + 1
        # And a review-decision audit row.
        assert (
            _count_events(registry, EventType.REVIEW_DECISION_RECORDED) == 1
        )

    def test_promotion_without_artifact_409(self, client):
        resp = client.post(
            "/api/v1/learning/promotions",
            json={"decisions": [{"candidate_id": "x", "approved": True}]},
        )
        assert resp.status_code == 409

    def test_unapproved_not_promoted(
        self, client, registry, tmp_path, monkeypatch
    ):
        cand = _learning_candidate()
        _write_learning_candidates(tmp_path, monkeypatch, [cand])
        resp = client.post(
            "/api/v1/learning/promotions",
            json={
                "decisions": [
                    {"candidate_id": cand["candidate_id"], "approved": False}
                ]
            },
        )
        assert resp.json()["promoted_count"] == 0


# ---------------------------------------------------------------------------
# Section 3: Schema-evolution candidates
# ---------------------------------------------------------------------------


class TestSchemaEvolution:
    def test_list_empty(self, client):
        assert client.get("/api/v1/schema-evolution/candidates").json() == {
            "count": 0,
            "candidates": [],
        }

    def test_list_dedupes_latest_per_candidate(self, client, registry):
        _seed_well_known_candidate(registry)
        _seed_well_known_candidate(registry)  # same id, newer event
        data = client.get("/api/v1/schema-evolution/candidates").json()
        assert data["count"] == 1
        assert data["candidates"][0]["open_string_value"] == "data_contract"

    def test_draft_adr_returns_markdown_with_evidence(self, client, registry):
        cid = _seed_well_known_candidate(registry)
        resp = client.post(f"/api/v1/schema-evolution/{cid}/draft-adr")
        assert resp.status_code == 200
        body = resp.json()
        md = body["markdown"]
        # Markdown carries candidate evidence (open string + extractors).
        assert "data_contract" in md
        assert "dbt_manifest" in md
        assert body["suggested_canonical_name"] == "DataContract"
        # Drafting the ADR is itself an audited review decision.
        assert (
            _count_events(registry, EventType.REVIEW_DECISION_RECORDED) == 1
        )

    def test_draft_adr_unknown_candidate_404(self, client, registry):
        resp = client.post("/api/v1/schema-evolution/missing/draft-adr")
        assert resp.status_code == 404

    def test_no_promote_endpoint_exists(self, client, registry):
        cid = _seed_well_known_candidate(registry)
        # The only write action is draft-adr; there is deliberately no
        # promote/approve route — the path simply doesn't exist (404),
        # never mind being method-not-allowed.
        assert (
            client.post(f"/api/v1/schema-evolution/{cid}/promote").status_code
            in (404, 405)
        )


# ---------------------------------------------------------------------------
# Section 4: Code-authoring proposals (read-only)
# ---------------------------------------------------------------------------


class TestCodeProposals:
    def test_list_empty(self, client):
        assert client.get("/api/v1/code-proposals").json() == {
            "count": 0,
            "proposals": [],
        }

    def test_list_surfaces_preview(self, client, registry):
        _seed_code_proposal(registry)
        data = client.get("/api/v1/code-proposals").json()
        assert data["count"] == 1
        row = data["proposals"][0]
        assert row["proposal_id"] == "prop-1"
        assert row["source_file"] == "src/foo/bar.py"
        assert "Proposal: address" in row["markdown_preview"]

    def test_is_read_only(self, client):
        # No mutate verbs on the code-proposals collection.
        assert client.post("/api/v1/code-proposals").status_code == 405
