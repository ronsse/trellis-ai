"""The 8 REST sites that wrap a message in ``sanitize_error_message``
before it leaves the process (PR #829 gate).

Each of these builds an ``HTTPException`` detail or a response field
directly from a ``CommandResult.message`` or a caught exception's own
``.message`` -- bypassing ``trellis_error_handler``, the middleware that
already sanitizes an *uncaught* ``TrellisError`` (see
``tests/unit/api/test_boundary_errors.py``). Each site wraps that value
in :func:`sanitize_error_message` itself, one call per site:

* ``trellis_api/routes/curate.py:43`` -- ``_execute_command``, 400.
* ``trellis_api/routes/ingest.py:68`` -- ``ingest_trace`` REJECTED, 400.
* ``trellis_api/routes/ingest.py:74`` -- ``ingest_trace`` FAILED, 409.
* ``trellis_api/routes/ingest.py:114`` -- ``ingest_evidence``, 400.
* ``trellis_api/routes/observations.py:94`` -- ``record_observation``, 400.
* ``trellis_api/routes/observations.py:155`` -- ``record_measurement``, 400.
* ``trellis_api/routes/admin.py:1033`` -- ``list_learning_candidates``,
  200 body ``hint``.
* ``trellis_api/routes/admin.py:1093`` -- ``promote_learning_candidates``,
  409 body ``message``.

Each case plants a secret-shaped synthetic string -- never a real
site's own text -- in the exact object the route reads (a monkeypatched
executor's ``CommandResult.message``, or a monkeypatched
``_load_learning_candidates`` raising ``_LearningCandidatesUnavailableError``
with that string as its ``.message``) and asserts the string is gone from
the response, replaced by ``SUPPRESSED_MARKER``. A case fails against the
pre-wrap source (the site's ``sanitize_error_message(...)`` call reverted
to its bare argument) exactly as mutant M5 (``curate.py:43``) does.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.core.error_sanitize import SUPPRESSED_MARKER
from trellis.mutate import Command, CommandResult, CommandStatus, Operation
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app
from trellis_api.routes.admin import _LearningCandidatesUnavailableError

#: Shaped like `sanitize_error_message`'s unquoted ``key[=:]value``
#: deny-list entry -- planted in place of each site's real message/
#: exception text, so a case is a true negative only while the site's
#: own wrap is intact.
_SECRET = "api_key=s3cr3t-leak-9f8e7d6c5b4a"  # noqa: S105 — test placeholder, not a real credential

TRACE_BODY = {"source": "agent", "intent": "probe", "steps": [], "context": {}}

EVIDENCE_BODY = {"evidence_type": "snippet", "content": "x", "source_origin": "manual"}

OBSERVATION_BODY = {
    "subject_entity_id": "ds-1",
    "subject_entity_type": "Dataset",
    "observer_agent_id": "agent-1",
    "content": "hello",
    "confidence": 0.5,
}

MEASUREMENT_BODY = {
    "subject_entity_id": "ds-1",
    "subject_entity_type": "Dataset",
    "metric_name": "null_rate",
    "metric_value": 0.03,
    "observer_agent_id": "agent-1",
}


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with auth off, matching ``test_review_routes.py``."""
    for var in ("TRELLIS_API_KEY", "TRELLIS_AUTH_MODE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry: StoreRegistry) -> TestClient:
    """The real app -- every router, every dependency, wired as in prod."""
    return TestClient(create_app(), raise_server_exceptions=False)


class _StubExecutor:
    """A ``build_curate_executor(...)`` replacement whose ``execute``
    ignores the command and returns a fixed, pre-built ``CommandResult``."""

    def __init__(self, result: CommandResult) -> None:
        self._result = result

    def execute(self, _cmd: Command) -> CommandResult:
        return self._result


def _stub_build_executor(result: CommandResult) -> Callable[[object], _StubExecutor]:
    return lambda _registry: _StubExecutor(result)


def _rejected(operation: Operation) -> CommandResult:
    return CommandResult(
        command_id="cmd-stub",
        status=CommandStatus.REJECTED,
        operation=operation,
        message=_SECRET,
    )


def _failed(operation: Operation) -> CommandResult:
    return CommandResult(
        command_id="cmd-stub",
        status=CommandStatus.FAILED,
        operation=operation,
        message=_SECRET,
    )


def _unavailable(code: str) -> _LearningCandidatesUnavailableError:
    return _LearningCandidatesUnavailableError(
        _SECRET, code=code, path=None, artifacts_dir=None
    )


@dataclass(frozen=True)
class _Site:
    """One of the 8 ``sanitize_error_message`` wrap sites."""

    patch_target: str
    patch_value: object
    issue: Callable[[TestClient], object]
    expected_status: int
    extract_message: Callable[[dict], str]


_SITES: dict[str, _Site] = {
    "curate.py:43": _Site(
        patch_target="trellis_api.routes.curate.build_curate_executor",
        patch_value=_stub_build_executor(_rejected(Operation.PRECEDENT_PROMOTE)),
        issue=lambda client: client.post(
            "/api/v1/precedents",
            json={"trace_id": "t1", "title": "t", "description": "d"},
        ),
        expected_status=400,
        extract_message=lambda body: body["detail"],
    ),
    "ingest.py:68": _Site(
        patch_target="trellis_api.routes.ingest.build_curate_executor",
        patch_value=_stub_build_executor(_rejected(Operation.TRACE_INGEST)),
        issue=lambda client: client.post("/api/v1/traces", json=TRACE_BODY),
        expected_status=400,
        extract_message=lambda body: body["detail"],
    ),
    "ingest.py:74": _Site(
        patch_target="trellis_api.routes.ingest.build_curate_executor",
        patch_value=_stub_build_executor(_failed(Operation.TRACE_INGEST)),
        issue=lambda client: client.post("/api/v1/traces", json=TRACE_BODY),
        expected_status=409,
        extract_message=lambda body: body["detail"],
    ),
    "ingest.py:114": _Site(
        patch_target="trellis_api.routes.ingest.build_curate_executor",
        patch_value=_stub_build_executor(_rejected(Operation.EVIDENCE_INGEST)),
        issue=lambda client: client.post("/api/v1/evidence", json=EVIDENCE_BODY),
        expected_status=400,
        extract_message=lambda body: body["detail"],
    ),
    "observations.py:94": _Site(
        patch_target="trellis_api.routes.observations.build_curate_executor",
        patch_value=_stub_build_executor(_rejected(Operation.OBSERVATION_RECORD)),
        issue=lambda client: client.post("/api/v1/observations", json=OBSERVATION_BODY),
        expected_status=400,
        extract_message=lambda body: body["detail"],
    ),
    "observations.py:155": _Site(
        patch_target="trellis_api.routes.observations.build_curate_executor",
        patch_value=_stub_build_executor(_rejected(Operation.MEASUREMENT_RECORD)),
        issue=lambda client: client.post("/api/v1/measurements", json=MEASUREMENT_BODY),
        expected_status=400,
        extract_message=lambda body: body["detail"],
    ),
    "admin.py:1033": _Site(
        patch_target="trellis_api.routes.admin._load_learning_candidates",
        patch_value=lambda: (_ for _ in ()).throw(
            _unavailable("learning_artifacts_dir_missing")
        ),
        issue=lambda client: client.get("/api/v1/learning/candidates"),
        expected_status=200,
        extract_message=lambda body: body["hint"],
    ),
    "admin.py:1093": _Site(
        patch_target="trellis_api.routes.admin._load_learning_candidates",
        patch_value=lambda: (_ for _ in ()).throw(
            _unavailable("learning_artifacts_dir_missing")
        ),
        issue=lambda client: client.post(
            "/api/v1/learning/promotions",
            json={"decisions": [{"candidate_id": "x", "approved": True}]},
        ),
        expected_status=409,
        extract_message=lambda body: body["detail"]["message"],
    ),
}


@pytest.mark.parametrize("site_name", sorted(_SITES), ids=sorted(_SITES))
def test_secret_shaped_message_is_suppressed(
    site_name: str, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _SITES[site_name]
    monkeypatch.setattr(site.patch_target, site.patch_value)

    resp = site.issue(client)

    assert resp.status_code == site.expected_status, resp.text
    body = resp.json()
    message = site.extract_message(body)
    assert _SECRET not in message
    assert message == SUPPRESSED_MARKER
    # Belt-and-suspenders: the secret must not leak through any sibling
    # field in the same response body either.
    assert _SECRET not in resp.text
