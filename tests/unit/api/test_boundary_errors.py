"""The REST failure boundary — typed errors keep their words (#459).

``unhandled_exception_handler`` answers ``500 {"code": "internal_error",
"message": "internal server error"}`` for *anything* uncaught, and that is
right for an untyped failure: a body that leaks internal types or schema
detail is worse than an opaque one. It was wrong for the typed hierarchy in
:mod:`trellis.errors`, whose messages are written for an operator — a
damaged ``policies.json`` names the file, the specific problem and the
recovery command, and every word of it was discarded on the way out.

Every assertion here fails against the pre-fix source, where the status was
``500`` and the body was the fixed two-field envelope.

The client is built by :func:`~trellis_api.app.create_app` rather than
assembled locally, so "the handler exists" and "the handler is wired" are
the same test. A handler written and never registered is the shape this
repo keeps producing (the Stage 2 gate that was injected but never
supplied).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.errors import (
    BackendNotInstalledError,
    ConfigError,
    StoreError,
    TrellisError,
)
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app
from trellis_api.middleware import CONFIG_ERROR_STATUS

#: A JSON object with no ``"policies"`` key — one of the three shapes #423
#: widened the strict reader to raise on.
DAMAGED_POLICY_FILE = '{"polices": []}'

TRACE_BODY = {"source": "agent", "intent": "probe", "steps": [], "context": {}}


@pytest.fixture
def stores_dir(tmp_path: Path) -> Path:
    path = tmp_path / "stores"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def registry(stores_dir: Path):
    reg = StoreRegistry(stores_dir=stores_dir)
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry) -> TestClient:
    """The real app, so registration is under test alongside the handler.

    ``raise_server_exceptions=False`` so the 500 control case renders its
    envelope instead of re-raising through the test client — the point of
    that case is what a *caller* sees.
    """
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def damaged_policy_file(stores_dir: Path) -> Path:
    path = stores_dir / "policies.json"
    path.write_text(DAMAGED_POLICY_FILE, encoding="utf-8")
    return path


class TestDamagedPolicyFileIsLegibleOverRest:
    def test_status_is_not_a_server_fault(
        self, client: TestClient, damaged_policy_file: Path
    ) -> None:
        """500 says Trellis broke. The operator's file is damaged.

        409 rather than 503 because a damaged config file does not repair
        itself, and rather than 500 because ``GET /api/v1/policies``
        already answers 409 for this very file — one fault, one status.
        """
        resp = client.post("/api/v1/traces", json=TRACE_BODY)

        assert resp.status_code == CONFIG_ERROR_STATUS
        assert resp.status_code == 409

    def test_body_names_the_file_the_problem_and_the_recovery(
        self, client: TestClient, damaged_policy_file: Path
    ) -> None:
        resp = client.post("/api/v1/traces", json=TRACE_BODY)
        body = resp.json()

        assert body["code"] == "config_error"
        assert body["code"] != "internal_error"
        assert str(damaged_policy_file) in body["message"]
        assert 'no "policies" key' in body["message"]
        assert "remove the file to run with no policies" in body["message"]
        # The exception's own ``setting`` hint, carried through rather than
        # re-derived: it is what tells a caller which file to name.
        assert body["setting"] == "policies.json"

    def test_the_envelope_keys_are_unchanged(
        self, client: TestClient, damaged_policy_file: Path
    ) -> None:
        """A client already branching on ``code`` needs no new shape."""
        body = client.post("/api/v1/traces", json=TRACE_BODY).json()

        assert {"code", "message", "request_id"} <= set(body)

    def test_every_governed_write_route_answers_the_same_way(
        self, client: TestClient, damaged_policy_file: Path
    ) -> None:
        """The fault is the deployment's, not one route's.

        Three routes that each build their own executor; the handler is on
        the app, so they cannot drift apart the way the CLI's per-command
        renderings did.
        """
        responses = [
            client.post("/api/v1/traces", json=TRACE_BODY),
            client.post(
                "/api/v1/commands/batch",
                json={
                    "commands": [
                        {"operation": "trace.ingest", "args": {"trace": TRACE_BODY}}
                    ],
                    "strategy": "sequential",
                },
            ),
            client.post(
                "/api/v1/feedback",
                json={"target_id": "trace_1", "rating": 0.9},
            ),
        ]

        for resp in responses:
            assert resp.status_code == CONFIG_ERROR_STATUS, resp.request.url
            assert resp.json()["code"] == "config_error", resp.request.url

    def test_a_healthy_deployment_is_untouched(self, client: TestClient) -> None:
        """No policy file means no policies means an empty, transparent gate.

        The boundary must not turn the shipped default into a failure — the
        whole posture rests on "no file" being indistinguishable from the
        no-gate world.
        """
        resp = client.post("/api/v1/traces", json=TRACE_BODY)

        assert resp.status_code == 200


class TestTheOtherConfigErrorTheSweepFound:
    """A read route, a different raiser, no policy file in sight.

    The gate was not the only ``ConfigError`` reaching the boundary.
    ``build_strategies`` resolves the embedder through
    ``getattr(registry, "embedding_fn", None)``, and ``getattr`` with a
    default does not suppress an exception raised *inside* the property —
    so a configured-but-uninstalled provider answered ``500
    internal_error`` on the pack routes, for a fault whose fix is the one
    ``pip install`` line the exception already carries.

    Registering the base class rather than ``ConfigError`` is what makes
    this work without a second edit, and what makes the ``code`` the
    *subclass's* — ``backend_not_installed`` is strictly more actionable
    than ``config_error`` and a per-class registration would have flattened
    it.
    """

    def test_an_uninstalled_backend_answers_with_its_install_command(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_self: object) -> None:
            raise BackendNotInstalledError(backend_name="openai", extra="llm-openai")

        monkeypatch.setattr(StoreRegistry, "embedding_fn", property(_boom))
        resp = client.post("/api/v1/packs", json={"intent": "anything"})
        body = resp.json()

        assert resp.status_code == CONFIG_ERROR_STATUS
        assert body["code"] == "backend_not_installed"
        assert 'uv pip install -e ".[llm-openai]"' in body["message"]
        assert body["setting"] == "backend.openai"


class TestTheCatchAllKeepsWhatItShouldHave:
    def test_an_untyped_exception_is_still_an_opaque_500(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``RuntimeError`` leaking a message would be #206's defect.

        The narrowing is deliberate: the typed family was *written* for an
        operator, an arbitrary exception was not, and the sparse body stays
        the right answer for the second.
        """

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "internal detail that must not ship"
            raise RuntimeError(msg)

        monkeypatch.setattr("trellis_api.routes.ingest.build_curate_executor", _boom)
        resp = client.post("/api/v1/traces", json=TRACE_BODY)

        assert resp.status_code == 500
        assert resp.json()["code"] == "internal_error"
        assert "internal detail" not in resp.text

    def test_a_non_config_trellis_error_keeps_500_but_gains_a_body(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Legibility and status are separate decisions, and only one moved.

        A ``StoreError`` from a backend that fell over really is a server
        fault, so 500 is honest — what was not honest was answering
        ``internal_error`` for an exception carrying a stable code and a
        written message.
        """

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "sqlite database is locked"
            raise StoreError(msg, store="document")

        monkeypatch.setattr("trellis_api.routes.ingest.build_curate_executor", _boom)
        resp = client.post("/api/v1/traces", json=TRACE_BODY)
        body = resp.json()

        assert resp.status_code == 500
        assert body["code"] == "store_error"
        assert body["message"] == "sqlite database is locked"
        assert body["store"] == "document"

    def test_the_message_is_leak_guarded(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#206's guard is what makes handling the whole family safe.

        A driver-raised ``StoreError`` routinely echoes the DSN it failed
        to reach. Widening the boundary without the sanitizer would have
        widened the leak surface with it.
        """

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "could not connect to postgresql://trellis:hunter2@db:5432/trellis"
            raise StoreError(msg)

        monkeypatch.setattr("trellis_api.routes.ingest.build_curate_executor", _boom)
        resp = client.post("/api/v1/traces", json=TRACE_BODY)

        assert resp.status_code == 500
        assert "hunter2" not in resp.text
        assert "suppressed" in resp.json()["message"]


class TestHandlerRegistration:
    def test_create_app_registers_the_typed_handler(self) -> None:
        """The half that cannot be observed from a response body.

        Starlette resolves a handler by walking the exception's MRO, so
        registering the *base* class is what makes a subclass added later
        legible without an edit here — pinning the base rather than
        ``ConfigError`` is the assertion that keeps that true.
        """
        handlers = create_app().exception_handlers

        assert TrellisError in handlers
        assert ConfigError not in handlers
        assert Exception in handlers
