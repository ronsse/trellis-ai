"""Black-box smoke tests for every REST route family against live uvicorn.

These tests prove that ``uvicorn`` boots, the lifespan validates a
production-shape config against Neon + AuraDB, and every public route
family returns a load-bearing response. They sit on top of the
``tests/unit/api/`` ``TestClient`` suites, which run the FastAPI app
in-process and skip the lifespan, real HTTP, and connection pooling.

The tests share a single uvicorn process per test (see the
``live_api_server`` fixture) and only import ``httpx`` from the
``trellis`` graph — never any ``trellis_api`` route symbol — so the
black-box contract stays honest.

Skipped when ``TRELLIS_TEST_NEO4J_URI`` or ``TRELLIS_TEST_PG_DSN``
isn't set. Run with::

    set -a && source .env && set +a
    pytest tests/integration/api/ -v
"""

from __future__ import annotations

import httpx  # noqa: TC002 — used at runtime in test fixtures' type hints

# ── Unversioned: deployment plumbing + version handshake ──────────────


def test_version_handshake(client: httpx.Client) -> None:
    """``/api/version`` reports the running API version."""
    resp = client.get("/api/version")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body.get("api_version"), str)
    assert body["api_version"]


def test_healthz_probe(client: httpx.Client) -> None:
    """``/healthz`` is the k8s liveness probe — always cheap, always 200.

    Probe routes are deliberately unversioned (no ``/api/v1`` prefix);
    they're deployment plumbing, not versioned API surface.
    """
    resp = client.get("/healthz")
    assert resp.status_code == 200


def test_readyz_probe(client: httpx.Client) -> None:
    """``/readyz`` is the k8s readiness probe (unversioned)."""
    resp = client.get("/readyz")
    assert resp.status_code == 200


# ── Admin: health + stats over the live registry ──────────────────────


def test_health_against_live_registry(client: httpx.Client) -> None:
    """``/api/v1/health`` exposes the per-store health checks."""
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["api"] is True


def test_stats_returns_zeros_on_empty_registry(client: httpx.Client) -> None:
    """``/api/v1/stats`` round-trips counts from each plane.

    The fixture wipes live state before yielding, so every count
    starts at 0. Anything > 0 here means the wipe regressed — failing
    the assertion is the right call.
    """
    resp = client.get("/api/v1/stats")
    assert resp.status_code == 200
    body = resp.json()
    for field in ("traces", "documents", "nodes", "edges", "events"):
        assert body[field] == 0, f"stats.{field} should be 0 after wipe, got {body}"


# ── Ingest: the bulk path lands rows on the live planes ───────────────


def test_bulk_ingest_lands_on_live_backends(client: httpx.Client) -> None:
    """``POST /api/v1/ingest/bulk`` writes through to Neo4j + Postgres."""
    resp = client.post(
        "/api/v1/ingest/bulk",
        json={
            "entities": [
                {
                    "entity_type": "service",
                    "name": "smoke-svc-a",
                    "entity_id": "smoke:svc-a",
                    "properties": {"team": "platform"},
                },
                {
                    "entity_type": "service",
                    "name": "smoke-svc-b",
                    "entity_id": "smoke:svc-b",
                    "properties": {"team": "platform"},
                },
            ],
            "edges": [
                {
                    "source_id": "smoke:svc-a",
                    "target_id": "smoke:svc-b",
                    "edge_kind": "depends_on",
                },
            ],
            "requested_by": "live-smoke",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["entities"]["succeeded"] == 2
    assert body["edges"]["succeeded"] == 1


# ── Retrieve: graph reads land on Neo4j ───────────────────────────────


def test_get_entity_returns_neo4j_row(client: httpx.Client) -> None:
    """After bulk ingest, ``GET /entities/{id}`` returns the stored entity."""
    seed = client.post(
        "/api/v1/ingest/bulk",
        json={
            "entities": [
                {
                    "entity_type": "service",
                    "name": "smoke-getentity",
                    "entity_id": "smoke:getentity",
                    "properties": {"team": "retrieve"},
                },
            ],
            "requested_by": "live-smoke",
        },
    )
    assert seed.status_code == 200, seed.text

    resp = client.get("/api/v1/entities/smoke:getentity")
    assert resp.status_code == 200, resp.text
    # Field naming: routes/retrieve.py wraps the graph row, the entity_id
    # we passed in must be reachable somewhere in the body.
    assert "smoke:getentity" in resp.text


def test_search_against_live_registry(client: httpx.Client) -> None:
    """``GET /api/v1/search`` returns a search response (empty pre-seed is fine)."""
    resp = client.get("/api/v1/search", params={"q": "anything"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "count" in body


def test_assemble_pack_returns_pack_id(client: httpx.Client) -> None:
    """``POST /api/v1/packs`` round-trips through PackBuilder + EventLog."""
    resp = client.post(
        "/api/v1/packs",
        json={
            "intent": "live-smoke pack",
            "max_items": 5,
            "max_tokens": 1000,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pack_id"]
    assert body["intent"] == "live-smoke pack"
    assert isinstance(body["count"], int)


# ── Curate: feedback writes through the mutation pipeline + EventLog ──


def test_record_feedback_against_live_registry(client: httpx.Client) -> None:
    """``POST /api/v1/feedback`` returns a CommandResponse with the executed op."""
    # Need a target_id that exists for the FEEDBACK_RECORD command — seed one.
    seed = client.post(
        "/api/v1/ingest/bulk",
        json={
            "entities": [
                {
                    "entity_type": "service",
                    "name": "smoke-feedback-target",
                    "entity_id": "smoke:feedback-target",
                    "properties": {},
                },
            ],
            "requested_by": "live-smoke",
        },
    )
    assert seed.status_code == 200, seed.text

    resp = client.post(
        "/api/v1/feedback",
        json={
            "target_id": "smoke:feedback-target",
            "rating": 1.0,
            "comment": "live-smoke",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation"] == "feedback.record"
    assert body["command_id"]


# ── Analytics: effectiveness + advisories read from the live EventLog ─


def test_effectiveness_returns_report(client: httpx.Client) -> None:
    """``GET /api/v1/effectiveness`` returns a report shape (empty corpus is fine)."""
    resp = client.get("/api/v1/effectiveness")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    # The report's exact shape depends on data, but a fresh registry must
    # at least be parseable without 500ing.


def test_generate_advisories_against_live_event_log(client: httpx.Client) -> None:
    """``POST /api/v1/advisories/generate`` runs against the Postgres EventLog."""
    resp = client.post("/api/v1/advisories/generate")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"


def test_list_advisories(client: httpx.Client) -> None:
    """``GET /api/v1/advisories`` returns a list (possibly empty)."""
    resp = client.get("/api/v1/advisories")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body.get("count"), int)
    assert isinstance(body.get("advisories"), list)
