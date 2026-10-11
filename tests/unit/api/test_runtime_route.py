"""Tests for ``GET /api/v1/runtime`` — the admin Runtime panel.

The route is a read-only snapshot of this process's *effective*
configuration, sourced from the same resolvers the real enforcement
consults (``resolve_auth_mode``, ``resolve_ops_detail``,
``resolve_metrics_public``, ``get_bind_info``) rather than re-deriving
anything, so the panel cannot drift from how the server actually
behaves. The one hard requirement this file exists to prove is
negative: the response must never carry a secret, however the fields
are sourced — a DSN, a credentialed URI, an API key, or a bearer
token — even when the environment or the ``llm:`` config is seeded
with values shaped exactly like one.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.llm.routing import LLMConsumer
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import admin

#: A value shaped exactly like each secret kind the panel must never leak,
#: planted in either the environment or the ``llm:`` config block.
_FAKE_DSN = "postgresql://trellis:sUp3rSecr3t@db.internal:5432/trellis"
_FAKE_API_KEY = "sk-live-RuntimeRouteTestSecretValue000111"
_FAKE_BEARER = "Bearer rt-test-shared-secret-abc123xyz"


@pytest.fixture
def registry(tmp_path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    """Mount only ``admin.router`` — no auth dependency, no lifespan.

    Scope gating on this route is structural (same router, same
    ``admin_auth`` dependency list as every other admin route in
    ``create_app()``) and is already proven generically by
    ``tests/unit/api/test_auth.py::test_mutate_key_denied_on_admin``,
    which probes that dependency through ``/api/v1/stats``. Testing it
    again here per-route would duplicate that coverage without proving
    anything route-specific.
    """

    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
    with TestClient(app) as c:
        yield c


def test_reports_the_expected_fields(client, registry) -> None:
    resp = client.get("/api/v1/runtime")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["auth_mode"] == "off"
    assert body["auth_mode_permissive"] is True
    assert isinstance(body["ops_detail"], str)
    assert isinstance(body["metrics_public"], bool)
    assert isinstance(body["bind_host"], str)
    assert isinstance(body["bind_port"], int)
    assert body["llm_routes_error"] is None


@pytest.mark.parametrize(
    ("env_value", "expect_permissive"),
    [
        ("required", False),
        ("off", True),
        ("optional", True),
    ],
)
def test_auth_mode_permissive_matches_the_real_grant(
    client, registry, monkeypatch, env_value, expect_permissive
) -> None:
    """``auth_mode_permissive`` must track ``resolve_auth_mode`` exactly.

    ``required`` is the one mode where an unauthenticated caller does
    *not* get every scope (``trellis_api/auth.py``); the other two both
    grant ``ALL_SCOPES`` with no credential. The banner this field
    drives is wrong in either direction if it disagrees with that.
    """
    monkeypatch.setenv("TRELLIS_AUTH_MODE", env_value)
    monkeypatch.setenv("TRELLIS_API_KEY", "placeholder-so-required-mode-is-valid")
    body = client.get("/api/v1/runtime").json()
    assert body["auth_mode"] == env_value
    assert body["auth_mode_permissive"] is expect_permissive


def test_stores_are_named_by_real_backend_class(client, registry) -> None:
    body = client.get("/api/v1/runtime").json()
    stores = body["stores"]
    assert set(stores) == {
        "graph_store",
        "vector_store",
        "document_store",
        "blob_store",
        "event_log",
        "trace_store",
    }
    # The fixture registry is the sqlite/local default end to end.
    assert stores["graph_store"] == type(registry.knowledge.graph_store).__name__
    assert stores["vector_store"] == type(registry.knowledge.vector_store).__name__
    assert stores["document_store"] == type(registry.knowledge.document_store).__name__
    assert stores["blob_store"] == type(registry.knowledge.blob_store).__name__
    assert stores["event_log"] == type(registry.operational.event_log).__name__
    assert stores["trace_store"] == type(registry.operational.trace_store).__name__


def test_embedder_is_none_when_unconfigured(client, registry) -> None:
    body = client.get("/api/v1/runtime").json()
    assert body["embedder"] is None


def test_llm_routes_cover_every_consumer_and_are_unrouted_by_default(
    client, registry
) -> None:
    body = client.get("/api/v1/runtime").json()
    assert set(body["llm_routes"]) == {c.value for c in LLMConsumer}
    assert all(tier is None for tier in body["llm_routes"].values())
    assert body["llm_routes_error"] is None


def test_a_malformed_route_is_reported_as_an_error_not_a_500(tmp_path) -> None:
    """An ``llm.routes`` entry naming an undefined tier degrades to a
    field, the same contract :meth:`StoreRegistry.llm_route` gives
    ``trellis admin llm-routes`` — the panel must not 500 on a
    misconfigured route it exists to surface.
    """
    reg = StoreRegistry(
        stores_dir=tmp_path / "stores",
        llm_config={
            "provider": "openai",
            "api_key_env": "OPENAI_API_KEY",
            "routes": {"session_capture": "undefined-tier"},
        },
    )
    app_module._registry = reg
    try:

        @asynccontextmanager
        async def noop_lifespan(app):
            yield

        app = FastAPI(lifespan=noop_lifespan)
        app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
        with TestClient(app) as c:
            resp = c.get("/api/v1/runtime")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["llm_routes"] == {}
        assert body["llm_routes_error"] is not None
        assert "session_capture" in body["llm_routes_error"]
    finally:
        reg.close()
        app_module._registry = None


def test_learning_artifacts_dir_matches_the_resolver(client, registry) -> None:
    from trellis.learning.artifacts import resolve_learning_artifacts_dir

    body = client.get("/api/v1/runtime").json()
    expected = resolve_learning_artifacts_dir(registry.stores_dir)
    assert body["learning_artifacts_dir"] == (str(expected) if expected else None)


def test_host_jobs_dir_echoes_the_env_var_honestly(
    client, registry, monkeypatch
) -> None:
    monkeypatch.delenv("TRELLIS_HOST_JOBS_DIR", raising=False)
    assert client.get("/api/v1/runtime").json()["host_jobs_dir"] is None

    monkeypatch.setenv("TRELLIS_HOST_JOBS_DIR", "/srv/trellis/host-jobs")
    body = client.get("/api/v1/runtime").json()
    assert body["host_jobs_dir"] == "/srv/trellis/host-jobs"


class TestNoSecretLeakage:
    """Seed every secret-shaped value the panel's sources can carry and
    assert none of them survive into the response.

    This is the test the task required explicitly: the panel reports
    backend *class names*, a *tier name*, and plain *paths* — never a
    DSN, a credentialed URI, a key, or a token. Proving that by reading
    the handler is not enough, because a future field added without
    this test in mind could re-introduce exactly this leak; the guard
    has to run the real handler against seeded secrets.
    """

    @pytest.fixture
    def seeded_registry(self, tmp_path, monkeypatch):
        """A registry plus environment carrying every secret shape."""
        monkeypatch.setenv("TRELLIS_KNOWLEDGE_PG_DSN", _FAKE_DSN)
        monkeypatch.setenv("TRELLIS_OPERATIONAL_PG_DSN", _FAKE_DSN)
        monkeypatch.setenv("TRELLIS_API_KEY", _FAKE_BEARER)
        monkeypatch.setenv("OPENAI_API_KEY", _FAKE_API_KEY)
        monkeypatch.setenv("TRELLIS_HOST_JOBS_DIR", "/srv/trellis/host-jobs")

        reg = StoreRegistry(
            stores_dir=tmp_path / "stores",
            llm_config={
                "provider": "openai",
                "api_key": _FAKE_API_KEY,
                "api_key_env": "OPENAI_API_KEY",
                "base_url": f"https://api.openai.com/v1?key={_FAKE_API_KEY}",
                # An empty tier body aliases the parent block (routing.py's
                # own convention) so this resolves to a real route — tier
                # name "default" — with a ``block`` that holds the fake key
                # above. The route handler reports only ``.tier``, never
                # ``.block``; that is the property this fixture exists to
                # exercise under a seeded secret.
                "tiers": {"default": {}},
                "routes": {"session_capture": "default"},
            },
        )
        app_module._registry = reg
        yield reg
        reg.close()
        app_module._registry = None

    @pytest.fixture
    def seeded_client(self, seeded_registry):
        @asynccontextmanager
        async def noop_lifespan(app):
            yield

        app = FastAPI(lifespan=noop_lifespan)
        app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
        with TestClient(app) as c:
            yield c

    def test_no_seeded_secret_appears_in_the_response_body(
        self, seeded_client, seeded_registry
    ) -> None:
        resp = seeded_client.get("/api/v1/runtime")
        assert resp.status_code == 200, resp.text
        raw = resp.text

        for secret in (_FAKE_DSN, _FAKE_API_KEY, _FAKE_BEARER):
            assert secret not in raw, f"leaked secret-shaped value: {secret!r}"

        # And the narrower literal a DSN/URI leak would actually show up
        # as, in case something re-encoded or partially redacted it.
        assert "sUp3rSecr3t" not in raw
        assert "postgresql://" not in raw

        body = json.loads(raw)
        assert body["llm_routes"] == {
            consumer.value: None for consumer in LLMConsumer
        } | {"session_capture": "default"}
