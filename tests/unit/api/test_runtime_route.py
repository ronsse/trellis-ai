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
from structlog.testing import capture_logs

import trellis_api.app as app_module
from trellis.llm.routing import LLMConsumer
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import admin

#: A value shaped exactly like each secret kind the panel must never leak,
#: planted in either the environment or the ``llm:`` config block.
_FAKE_DSN = "postgresql://trellis:sUp3rSecr3t@db.internal:5432/trellis"
_FAKE_API_KEY = "sk-live-RuntimeRouteTestSecretValue000111"
_FAKE_BEARER = "Bearer rt-test-shared-secret-abc123xyz"
#: Shaped like a secret pasted into TRELLIS_EMBEDDING_FN by mistake --
#: has a dot so it passes _import_callable's syntax check and reaches
#: (and fails) the real import, the path that used to echo the raw knob
#: value into both the response and the warning log on failure.
_FAKE_EMBEDDING_FN_SECRET = "sk-live-RuntimeEmbedderSecretValue000111.not_callable"  # noqa: S105


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


def test_embedder_reports_the_env_var_source_when_set(
    client, registry, monkeypatch
) -> None:
    """``embedder`` must derive from ``registry.embedding_fn`` -- the
    resolver PackBuilder, the MCP server and the trace-embed worker all
    actually call -- not from the separate, nothing-else-reads
    ``llm.embedding:`` block ``build_embedder_client()`` builds from.
    ``TRELLIS_EMBEDDING_FN`` outranks ``embeddings.provider``, exactly
    like ``embedding_fn`` itself.
    """
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", "json.dumps")
    body = client.get("/api/v1/runtime").json()
    assert body["embedder"] == "json.dumps (TRELLIS_EMBEDDING_FN)"


def test_embedder_reports_the_config_provider_source_when_set(tmp_path) -> None:
    reg = StoreRegistry(
        stores_dir=tmp_path / "stores",
        embedding_config={"provider": "custom.module.embed"},
    )
    app_module._registry = reg
    try:

        @asynccontextmanager
        async def noop_lifespan(app):
            yield

        app = FastAPI(lifespan=noop_lifespan)
        app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
        with TestClient(app) as c:
            body = c.get("/api/v1/runtime").json()
    finally:
        reg.close()
        app_module._registry = None
    # custom.module.embed cannot import (module does not exist), so
    # embedding_fn raises -- the panel must degrade to "unknown" naming
    # only the KNOB (never the configured value, which is exactly the
    # typo/attacker-controlled text an operator pasted in) and the
    # exception type, never a 500.
    assert body["embedder"] is not None
    assert body["embedder"].startswith("unknown (embeddings.provider failed:")
    assert "custom.module.embed" not in body["embedder"]


def test_embedder_env_wins_over_config_provider_when_both_set(
    tmp_path, monkeypatch
) -> None:
    """Re-gate #867 M21: the two tests above each set exactly one of the
    two knobs, so a mutant that swaps
    :meth:`StoreRegistry.embedding_source_label`'s check order (config
    read before the ``TRELLIS_EMBEDDING_FN`` env lookup, rather than
    after) still passes both -- each test's untouched knob is simply
    absent, so there is nothing for the swapped order to disagree about.
    Setting both at once is the only way to observe which one actually
    wins.
    """
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", "json.dumps")
    reg = StoreRegistry(
        stores_dir=tmp_path / "stores",
        embedding_config={"provider": "custom.module.embed"},
    )
    app_module._registry = reg
    try:

        @asynccontextmanager
        async def noop_lifespan(app):
            yield

        app = FastAPI(lifespan=noop_lifespan)
        app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
        with TestClient(app) as c:
            body = c.get("/api/v1/runtime").json()
    finally:
        reg.close()
        app_module._registry = None
    assert body["embedder"] == "json.dumps (TRELLIS_EMBEDDING_FN)"


def test_embedder_degrades_to_unknown_with_a_reason_on_failure(
    tmp_path, monkeypatch
) -> None:
    """A configured-but-broken embedder must never 500 the panel, and the
    degraded value must still name *which* knob failed -- not just that
    something, somewhere, did.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    reg = StoreRegistry(
        stores_dir=tmp_path / "stores",
        embedding_config={"provider": "openai"},
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
    finally:
        reg.close()
        app_module._registry = None
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["embedder"] is not None
    assert body["embedder"].startswith("unknown (embeddings.provider failed:")
    assert "openai" not in body["embedder"]


def test_embedder_failure_never_echoes_the_knob_value_in_the_response(
    client, registry, monkeypatch
) -> None:
    """B1': a secret pasted into ``TRELLIS_EMBEDDING_FN`` that fails to
    import must not reach the response body -- only the knob NAME
    (``TRELLIS_EMBEDDING_FN``) and the exception type are safe to show;
    the value is operator-supplied text with no safety guarantee.
    """
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", _FAKE_EMBEDDING_FN_SECRET)
    resp = client.get("/api/v1/runtime")
    assert resp.status_code == 200, resp.text
    raw = resp.text
    assert _FAKE_EMBEDDING_FN_SECRET not in raw
    assert "sk-live-RuntimeEmbedderSecretValue000111" not in raw
    body = json.loads(raw)
    assert body["embedder"] == "unknown (TRELLIS_EMBEDDING_FN failed: ConfigError)"


def test_embedder_failure_never_echoes_the_knob_value_in_the_log(
    client, registry, monkeypatch
) -> None:
    """Same defect, the log layer: ``runtime_embedder_probe_failed`` must
    carry ``knob=`` only, never the raw value that failed to import --
    proven against real captured structlog records, not just the
    response.
    """
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", _FAKE_EMBEDDING_FN_SECRET)
    with capture_logs() as logs:
        resp = client.get("/api/v1/runtime")
    assert resp.status_code == 200, resp.text

    failures = [
        log for log in logs if log.get("event") == "runtime_embedder_probe_failed"
    ]
    assert len(failures) == 1, logs
    record = failures[0]
    assert record["knob"] == "TRELLIS_EMBEDDING_FN"
    assert record["error_type"] == "ConfigError"
    assert "source" not in record
    for value in record.values():
        assert _FAKE_EMBEDDING_FN_SECRET not in str(value)
        assert "sk-live-RuntimeEmbedderSecretValue000111" not in str(value)


@pytest.mark.parametrize(
    ("embedding_config", "needle"),
    [
        pytest.param("openai", None, id="str"),
        pytest.param(
            ["sk-live-NonMappingEmbeddingConfigSecret000111"],
            "sk-live-NonMappingEmbeddingConfigSecret000111",
            id="list",
        ),
        pytest.param(True, None, id="bool"),
        pytest.param(5, None, id="int"),
        pytest.param(
            "sk-live-NonMappingEmbeddingConfigSecret000222",
            "sk-live-NonMappingEmbeddingConfigSecret000222",
            id="fake-key-str",
        ),
    ],
)
def test_a_non_mapping_embeddings_config_is_reported_as_an_error_not_a_500(
    tmp_path, embedding_config, needle
) -> None:
    """A truthy non-Mapping ``embeddings:`` block -- a bare string, a
    list, ``True``, or a bare int -- used to reach ``.get("provider")``
    inside :attr:`StoreRegistry.embedding_fn` as an uncaught
    ``AttributeError``. The route's except branch then called
    :meth:`StoreRegistry.embedding_source_knob` to build the degraded
    message, which hit the same ``.get`` call on the same non-Mapping
    value and raised a *second*, uncaught ``AttributeError`` -- an
    operator's ``embeddings: <scalar-or-list>`` typo 500'd the whole
    Runtime panel, auth banner included, instead of degrading to a
    reported field like every other malformed config shape.

    ``embedding_fn``, ``embedding_source_label`` and
    ``embedding_source_knob`` now mapping-guard ``embeddings:`` and treat
    that shape as unconfigured (logging only the key and the value's
    *type*), so the panel reports ``"unknown (embeddings)"`` instead of
    raising, and never echoes the configured value itself -- which, in
    the ``list`` and ``fake-key-str`` cases here, is shaped exactly like
    a pasted credential.
    """
    reg = StoreRegistry(
        stores_dir=tmp_path / "stores", embedding_config=embedding_config
    )
    app_module._registry = reg
    try:

        @asynccontextmanager
        async def noop_lifespan(app):
            yield

        app = FastAPI(lifespan=noop_lifespan)
        app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
        with TestClient(app) as c, capture_logs() as logs:
            resp = c.get("/api/v1/runtime")
    finally:
        reg.close()
        app_module._registry = None

    assert resp.status_code == 200, resp.text
    raw = resp.text
    body = json.loads(raw)
    assert body["embedder"] == "unknown (embeddings)"

    not_mapping_logs = [
        log for log in logs if log.get("event") == "embedding_config_not_mapping"
    ]
    assert len(not_mapping_logs) == 1, logs
    assert not_mapping_logs[0]["key"] == "embeddings"
    assert not_mapping_logs[0]["type"] == type(embedding_config).__name__
    # Pin the level too -- a malformed ``embeddings:``
    # block is an operator-visible misconfiguration, not a debug detail,
    # so it must survive the default ``TRELLIS_LOG_LEVEL=WARNING`` floor.
    assert not_mapping_logs[0]["log_level"] == "warning"

    if needle is not None:
        assert needle not in raw
        for log in logs:
            for value in log.values():
                assert needle not in str(value)


@pytest.mark.parametrize(
    ("provider", "needle"),
    [
        pytest.param(5, None, id="int"),
        pytest.param(
            ["sk-live-NonStrProviderSecret000333"],
            "sk-live-NonStrProviderSecret000333",
            id="list",
        ),
    ],
)
def test_a_non_str_provider_names_the_knob_without_stringifying_the_value(
    tmp_path, provider, needle
) -> None:
    """``embeddings.provider`` set to a non-string
    (an int or a list) makes :attr:`StoreRegistry.embedding_fn` raise
    ``ConfigError`` naming only the value's *type*. The panel's except
    branch then calls :meth:`StoreRegistry.embedding_source_knob` to
    attribute the failure to a knob -- which used to return ``None`` for
    exactly this shape (its truthy check required ``isinstance(provider,
    str)`` before naming anything), degrading the field to the generic
    ``"unknown (ConfigError)"`` instead of ``"unknown (embeddings.provider
    failed: ConfigError)"``. Fixed to mirror ``embedding_fn``'s own
    truthy gate (present and non-empty, not "and is a string"), so the
    knob is named on this path the same as it already is when a *string*
    provider fails to import.
    """
    reg = StoreRegistry(
        stores_dir=tmp_path / "stores", embedding_config={"provider": provider}
    )
    app_module._registry = reg
    try:

        @asynccontextmanager
        async def noop_lifespan(app):
            yield

        app = FastAPI(lifespan=noop_lifespan)
        app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
        with TestClient(app) as c, capture_logs() as logs:
            resp = c.get("/api/v1/runtime")
    finally:
        reg.close()
        app_module._registry = None

    assert resp.status_code == 200, resp.text
    raw = resp.text
    body = json.loads(raw)
    assert body["embedder"] == "unknown (embeddings.provider failed: ConfigError)"

    probe_failed_logs = [
        log for log in logs if log.get("event") == "runtime_embedder_probe_failed"
    ]
    assert len(probe_failed_logs) == 1, logs
    assert probe_failed_logs[0]["knob"] == "embeddings.provider"
    assert probe_failed_logs[0]["error_type"] == "ConfigError"

    if needle is not None:
        assert needle not in raw
        for log in logs:
            for value in log.values():
                assert needle not in str(value)


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


@pytest.mark.parametrize(
    "llm_config",
    [
        pytest.param("sk-live-NonMappingLlmConfigSecret000111", id="str"),
        pytest.param([1, 2, 3], id="list"),
        pytest.param(True, id="bool"),
        pytest.param(42, id="int"),
    ],
)
def test_a_non_mapping_llm_config_is_reported_as_an_error_not_a_500(
    tmp_path, llm_config
) -> None:
    """S1 (re-gate #867): a truthy non-Mapping ``llm:`` block -- a string,
    a list, ``True``, or a bare int -- used to reach ``_section``'s own
    ``.get`` call inside :func:`trellis.llm.routing.validate_llm_routing`
    as an uncaught ``AttributeError``, which is not an ``LLMRoutingError``
    and so was never caught by this route's ``except LLMRoutingError``
    clause: an operator's ``llm: <scalar-or-list>`` typo 500'd the whole
    Runtime panel, auth banner included, instead of degrading to a
    reported field like every other malformed ``llm:`` shape.

    ``validate_llm_routing`` now raises ``LLMRoutingError(setting="llm")``
    for any non-Mapping parent, so this is caught the same way a bad
    ``llm.routes`` entry already was -- proven end to end through the real
    route, not just at the ``routing.py`` unit level.
    """
    reg = StoreRegistry(stores_dir=tmp_path / "stores", llm_config=llm_config)
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
        raw = resp.text
        assert "sk-live-NonMappingLlmConfigSecret000111" not in raw

        body = json.loads(raw)
        assert body["llm_routes"] == {}
        assert body["llm_routes_error"] == "llm is invalid; see the server log."
    finally:
        reg.close()
        app_module._registry = None


def test_a_non_routing_exception_in_llm_routes_is_reported_not_500(
    client, registry, monkeypatch
) -> None:
    """S1 (re-gate #867), the second independent gate: the route's
    ``except LLMRoutingError`` clause only catches what
    ``trellis.llm.routing`` actually raises. ``admin.py``'s ``runtime()``
    also wraps the whole ``llm_routes`` block in a generic ``except
    Exception`` -- mirroring the embedder block's existing
    graceful-degradation pattern -- so a *different* failure (a resolver
    bug, a future shape ``validate_llm_routing`` doesn't anticipate; the
    gate's own probe used a synthetic exception for exactly this reason)
    also degrades to a reported field instead of a 500. Proven
    independently of the ``routing.py`` fix by making
    ``StoreRegistry.llm_route`` itself raise something that is not an
    ``LLMRoutingError``.
    """

    def _boom(consumer):
        msg = "synthetic failure unrelated to llm.routes/llm.tiers shape"
        raise RuntimeError(msg)

    monkeypatch.setattr(registry, "llm_route", _boom)

    with capture_logs() as logs:
        resp = client.get("/api/v1/runtime")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["llm_routes"] == {}
    assert (
        body["llm_routes_error"]
        == "llm.routes/llm.tiers is invalid; see the server log."
    )

    warnings = [log for log in logs if log.get("event") == "runtime_llm_routes_invalid"]
    assert len(warnings) == 1
    assert warnings[0]["setting"] is None
    assert warnings[0]["error_type"] == "RuntimeError"
    for log in logs:
        for value in log.values():
            assert "synthetic failure" not in str(value)


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


class TestNoSecretLeakageInMalformedRoutes:
    """The four route-value shapes the adversarial gate probed for #867.

    ``_validate_route`` (``trellis/llm/routing.py``) used to interpolate
    the raw, malformed ``llm.routes.<consumer>`` value into its raised
    ``LLMRoutingError`` message via ``repr()``, and the runtime route used
    to surface that message verbatim as ``llm_routes_error`` — so a route
    value that is actually a mistyped credential (a mapping holding an
    ``api_key``, a list holding one, the bare key itself, or a short
    password standing in for a tier name) reached the REST body in full.
    Both are fixed now: ``_validate_route`` names only the value's type or
    the defined tier names, and the route reports only ``exc.setting`` (a
    YAML key path) plus fixed text.
    """

    @pytest.fixture
    def client_factory(self, tmp_path):
        def _build(route_value):
            reg = StoreRegistry(
                stores_dir=tmp_path / "stores",
                llm_config={
                    "provider": "openai",
                    "api_key_env": "OPENAI_API_KEY",
                    "routes": {"session_capture": route_value},
                },
            )
            app_module._registry = reg

            @asynccontextmanager
            async def noop_lifespan(app):
                yield

            app = FastAPI(lifespan=noop_lifespan)
            app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
            return reg, TestClient(app)

        return _build

    @pytest.mark.parametrize(
        "route_value",
        [
            pytest.param(
                {"provider": "openai", "api_key": _FAKE_API_KEY},
                id="mapping_with_api_key",
            ),
            pytest.param([_FAKE_API_KEY], id="list"),
            pytest.param(_FAKE_API_KEY, id="bare_key_string"),
            pytest.param("p@ssw0rd-Sh0rt1", id="short_password_as_tier_name"),
        ],
    )
    def test_the_secret_never_reaches_the_response(
        self, client_factory, route_value
    ) -> None:
        reg, client = client_factory(route_value)
        try:
            resp = client.get("/api/v1/runtime")
            assert resp.status_code == 200, resp.text
            raw = resp.text
            assert _FAKE_API_KEY not in raw, f"leaked secret-shaped value in: {raw}"
            assert "p@ssw0rd-Sh0rt1" not in raw

            body = json.loads(raw)
            assert body["llm_routes"] == {}
            assert body["llm_routes_error"] is not None
            assert body["llm_routes_error"].startswith("llm.routes.session_capture")
            assert _FAKE_API_KEY not in body["llm_routes_error"]
            assert "p@ssw0rd-Sh0rt1" not in body["llm_routes_error"]
        finally:
            reg.close()
            app_module._registry = None
