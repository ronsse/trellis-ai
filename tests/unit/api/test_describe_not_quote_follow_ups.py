"""REST/SDK follow-ups to PR #829's ``sanitize_error_message`` sweep.

Three distinct gaps the original sweep left open (tracked under the
same #829 follow-up list), each with its own planted marker so a test
is a true negative only while the real fix holds:

1. **Pre-validation 422s quote pydantic's own ``str(exc)``, which embeds
   ``input_value=<the caller's value>``** -- ``ingest.py``'s
   ``ingest_trace``/``ingest_evidence`` and ``observations.py``'s
   ``record_observation``/``record_measurement`` all build their 422
   ``detail`` from ``model_validate``'s raised exception directly. The
   fix is :func:`describe_validation_error`, which keeps ``loc``/``type``/
   ``msg`` and drops ``input_value`` -- not a post-hoc
   ``sanitize_error_message`` wrap, which would leave an
   innocuous-looking value (not secret-shaped, no `=`/`:` pair, under the
   long-token floor) sitting in the response untouched. Each case here
   plants such a value, so a "wrap instead of describe" fix fails it.

2. **``_LearningCandidatesUnavailableError`` built ``f"...: {exc}"``** --
   ``admin.py``'s ``_load_learning_candidates`` interpolated the caught
   ``OSError``/``ValueError`` directly. For an ``OSError`` built from a
   single positional string (no ``strerror``/``errno``/``filename``),
   ``str(exc)`` *is* that string, while :func:`describe_os_error` falls
   back to the bare type name -- a clean discriminator.

3. **``CommandResult.message`` reaches REST callers raw** through
   ``/commands/batch`` (``mutations.py`` -> ``_results.py::command_response``)
   and, per item, through ``/ingest/bulk``'s three ``BulkItemResult``
   constructions (``ingest.py``), neither of which routed through
   ``sanitize_error_message`` before this fix.

See ``tests/unit/api/test_sanitize_error_message_wraps.py`` (#829) for the
sites this file does not re-cover.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.mutate import Command, CommandResult, CommandStatus, Operation
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

#: A value that fails every `sanitize_error_message` deny-list pattern
#: (no secret-keyword=value pair, no SQL shape, no email/credential URL,
#: under the 40-char long-opaque-token floor) -- so a fix that merely
#: wraps the existing message in `sanitize_error_message` without
#: switching to `describe_validation_error` would leave this marker in
#: the response untouched, and the test would catch that.
_VALIDATION_MARKER = "nightshade-4471"

#: Single positional argument -> `OSError.strerror`/`.errno`/`.filename`
#: are all `None`, so `str(exc)` returns exactly this text while
#: `describe_os_error` has no parts to assemble and falls back to the
#: bare type name.
_OS_ERROR_MARKER = "osmarker-glint-93af"

#: Shaped like `sanitize_error_message`'s unquoted `key=value` deny-list
#: entry, matching the convention in test_sanitize_error_message_wraps.py.
_SECRET = "api_key=s3cr3t-leak-9f8e7d6c5b4a"  # noqa: S105 — test placeholder, not a real credential

#: A clean REJECTED message -- no deny-list shape at all. A fix that
#: wraps `command_response`/`BulkItemResult` in a constant suppression
#: marker regardless of status would still make `_SECRET` disappear, so
#: asserting only its absence cannot tell "sanitized" from "replaced
#: outright"; this value pins the real text must still arrive.
_CLEAN_REJECTED_MESSAGE = "Node not found: d1"

#: 48 characters -- long enough to trip `sanitize_error_message`'s
#: 40-char long-opaque-token rule, same as the gate's probe. A real
#: entity name, not secret-shaped, so gating the wrap by status is what
#: keeps it readable on a SUCCESS result.
_LONG_NAME = "int_customer_orders_aggregated_by_month_v2_daily"

#: A sha256 hex digest -- 64 characters, also past the long-token floor.
#: `evidence.ingest:<sha256>` is the in-tree idempotency-key convention
#: (src/trellis/mutate/evidence_ingest.py); a DUPLICATE message built
#: from one must survive, not be replaced by the suppression marker.
_DIGEST_KEY = hashlib.sha256(b"evidence.ingest:probe-idempotency-key").hexdigest()


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "TRELLIS_API_KEY",
        "TRELLIS_AUTH_MODE",
        "TRELLIS_LEARNING_ARTIFACTS_DIR",
    ):
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
    return TestClient(create_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Item 1 -- pre-validation 422s quote pydantic's str(exc)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "body", "corrupt_field"),
    [
        (
            "/api/v1/traces",
            {"source": "agent", "intent": "probe", "steps": [], "context": {}},
            "intent",
        ),
        (
            "/api/v1/evidence",
            {"evidence_type": "snippet", "content": "x", "source_origin": "manual"},
            "source_origin",
        ),
        (
            "/api/v1/observations",
            {
                "subject_entity_id": "ds-1",
                "subject_entity_type": "Dataset",
                "observer_agent_id": "agent-1",
                "content": "hello",
                "confidence": 0.5,
            },
            "subject_entity_id",
        ),
        (
            "/api/v1/measurements",
            {
                "subject_entity_id": "ds-1",
                "subject_entity_type": "Dataset",
                "metric_name": "null_rate",
                "metric_value": 0.03,
                "observer_agent_id": "agent-1",
            },
            "subject_entity_id",
        ),
        (
            "/api/v1/packs/sectioned",
            {"intent": "probe", "sections": [{"name": "s1"}]},
            "schema_version",
        ),
    ],
    ids=[
        "ingest.py:trace",
        "ingest.py:evidence",
        "observations.py:observation",
        "observations.py:measurement",
        "retrieve.py:sectioned_pack",
    ],
)
def test_validation_422_never_quotes_the_rejected_value(
    client: TestClient, path: str, body: dict[str, Any], corrupt_field: str
) -> None:
    corrupted = dict(body)
    if path == "/api/v1/packs/sectioned":
        # The bad field lives on a per-section dict, not the request's
        # own top level -- `req.sections` stays a valid `list[dict]` so
        # FastAPI's own request validation passes and only
        # `SectionRequest(**s)` inside the route catches the corruption.
        section = {
            **corrupted["sections"][0],
            corrupt_field: {"leak": _VALIDATION_MARKER},
        }
        corrupted["sections"] = [section]
    else:
        # A dict where a str was required: pydantic's own str(exc) embeds
        # `input_value={'leak': 'nightshade-4471'}` for this failure; the
        # error TYPE ("string_type") never varies with the value, only the
        # dropped input_value clause does.
        corrupted[corrupt_field] = {"leak": _VALIDATION_MARKER}

    resp = client.post(path, json=corrupted)

    assert resp.status_code == 422, resp.text
    assert _VALIDATION_MARKER not in resp.text
    detail = resp.json()["detail"]
    # The field location and error type survive -- this is "describe",
    # not "suppress the whole body".
    assert corrupt_field in detail
    assert "string_type" in detail


# ---------------------------------------------------------------------------
# Item 2 -- _LearningCandidatesUnavailableError builds f"...: {exc}"
# ---------------------------------------------------------------------------


def test_unreadable_candidates_file_never_quotes_the_os_error_text(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Matches test_review_routes.py's default artifacts dir: stores_dir is
    # tmp_path/"stores", so the resolver's fallback is tmp_path/"learning".
    artifacts = tmp_path / "learning"
    artifacts.mkdir()
    candidates_path = artifacts / "intent_learning_candidates.json"
    candidates_path.write_text("{}", encoding="utf-8")

    real_read_text = Path.read_text

    def _read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == candidates_path:
            raise OSError(_OS_ERROR_MARKER)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)

    get_resp = client.get("/api/v1/learning/candidates")
    post_resp = client.post(
        "/api/v1/learning/promotions",
        json={"decisions": [{"candidate_id": "x", "approved": True}]},
    )

    assert _OS_ERROR_MARKER not in get_resp.text
    assert _OS_ERROR_MARKER not in post_resp.text
    get_data = get_resp.json()
    assert get_data["code"] == "learning_candidates_unreadable"
    assert "OSError" in get_data["hint"]
    assert post_resp.status_code == 409
    assert post_resp.json()["detail"]["code"] == "learning_candidates_unreadable"


# ---------------------------------------------------------------------------
# Item 3 -- CommandResult.message reaches REST callers raw
# ---------------------------------------------------------------------------


class _StubBatchExecutor:
    """A ``build_curate_executor(...)`` replacement for ``/commands/batch``:
    ``execute_batch`` ignores the batch's command content and returns
    *results* in order, one per command. A single ``CommandResult``
    (not a list) repeats for every command, matching the original
    one-result-fits-all stub."""

    def __init__(self, results: CommandResult | list[CommandResult]) -> None:
        self._results = results if isinstance(results, list) else [results]

    def execute_batch(self, batch: object) -> list[CommandResult]:
        commands = getattr(batch, "commands", [])
        n = len(commands) or 1
        return [self._results[i % len(self._results)] for i in range(n)]


class _StubBulkExecutor:
    """A ``build_curate_executor(...)`` replacement for ``/ingest/bulk``:
    each ``execute`` call (entity, then edge, then alias) returns the
    next *results* entry in order, cycling if called more times than
    *results* has entries -- regardless of the command passed in."""

    def __init__(self, results: CommandResult | list[CommandResult]) -> None:
        self._results = results if isinstance(results, list) else [results]
        self._calls = 0

    def execute(self, _cmd: Command) -> CommandResult:
        result = self._results[self._calls % len(self._results)]
        self._calls += 1
        return result


def _rejected(operation: Operation, message: str = _SECRET) -> CommandResult:
    return CommandResult(
        command_id="cmd-stub",
        status=CommandStatus.REJECTED,
        operation=operation,
        message=message,
    )


def _success(operation: Operation, message: str) -> CommandResult:
    return CommandResult(
        command_id="cmd-stub-success",
        status=CommandStatus.SUCCESS,
        operation=operation,
        created_id="node-stub",
        message=message,
    )


def _duplicate(operation: Operation, message: str) -> CommandResult:
    return CommandResult(
        command_id="cmd-stub-duplicate",
        status=CommandStatus.DUPLICATE,
        operation=operation,
        message=message,
    )


def test_commands_batch_sanitizes_per_item_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two REJECTED commands: the first carries the deny-list secret (must
    # be sanitized), the second a clean message with no deny-list shape
    # at all (must arrive verbatim). A fix that replaces every message
    # with a constant marker regardless of content passes the first
    # assertion but fails the second.
    monkeypatch.setattr(
        "trellis_api.routes.mutations.build_curate_executor",
        lambda _registry: _StubBatchExecutor(
            [
                _rejected(Operation.ENTITY_CREATE),
                _rejected(Operation.ENTITY_CREATE, message=_CLEAN_REJECTED_MESSAGE),
            ]
        ),
    )

    resp = client.post(
        "/api/v1/commands/batch",
        json={
            "commands": [
                {
                    "operation": "entity.create",
                    "args": {"entity_type": "Dataset", "name": "d1"},
                },
                {
                    "operation": "entity.create",
                    "args": {"entity_type": "Dataset", "name": "d2"},
                },
            ]
        },
    )

    assert resp.status_code == 200, resp.text
    assert _SECRET not in resp.text
    results = resp.json()["results"]
    assert len(results) == 2
    assert _SECRET not in results[0]["message"]
    assert results[1]["message"] == _CLEAN_REJECTED_MESSAGE


def test_commands_batch_success_message_with_a_long_name_is_verbatim(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = f"Entity created: {_LONG_NAME}"
    monkeypatch.setattr(
        "trellis_api.routes.mutations.build_curate_executor",
        lambda _registry: _StubBatchExecutor(
            _success(Operation.ENTITY_CREATE, message)
        ),
    )

    resp = client.post(
        "/api/v1/commands/batch",
        json={
            "commands": [
                {
                    "operation": "entity.create",
                    "args": {"entity_type": "Dataset", "name": _LONG_NAME},
                }
            ]
        },
    )

    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == 1
    assert results[0]["message"] == message


def test_commands_batch_duplicate_message_with_a_digest_key_is_verbatim(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = f"Duplicate command (persisted): {_DIGEST_KEY}"
    monkeypatch.setattr(
        "trellis_api.routes.mutations.build_curate_executor",
        lambda _registry: _StubBatchExecutor(
            _duplicate(Operation.ENTITY_CREATE, message)
        ),
    )

    resp = client.post(
        "/api/v1/commands/batch",
        json={
            "commands": [
                {
                    "operation": "entity.create",
                    "args": {"entity_type": "Dataset", "name": "d1"},
                }
            ]
        },
    )

    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == 1
    assert results[0]["message"] == message


def test_ingest_bulk_sanitizes_every_item_result_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Entity keeps the deny-list secret (must be sanitized); edge and
    # alias get the clean message (must arrive verbatim). A fix that
    # replaces every message with a constant marker passes the secret
    # check but fails the clean-message checks.
    monkeypatch.setattr(
        "trellis_api.routes.ingest.build_curate_executor",
        lambda _registry: _StubBulkExecutor(
            [
                _rejected(Operation.ENTITY_CREATE),
                _rejected(Operation.LINK_CREATE, message=_CLEAN_REJECTED_MESSAGE),
                _rejected(Operation.ALIAS_UPSERT, message=_CLEAN_REJECTED_MESSAGE),
            ]
        ),
    )

    resp = client.post(
        "/api/v1/ingest/bulk",
        json={
            "entities": [{"entity_type": "Dataset", "name": "d1"}],
            "edges": [{"source_id": "d1", "target_id": "d2"}],
            "aliases": [
                {
                    "entity_id": "d1",
                    "source_system": "test",
                    "raw_id": "raw-1",
                }
            ],
        },
    )

    assert resp.status_code == 200, resp.text
    assert _SECRET not in resp.text
    body = resp.json()
    entity_messages = [r["message"] for r in body["entities"]["results"]]
    edge_messages = [r["message"] for r in body["edges"]["results"]]
    alias_messages = [r["message"] for r in body["aliases"]["results"]]
    assert entity_messages == [r for r in entity_messages if _SECRET not in r]
    assert edge_messages == [_CLEAN_REJECTED_MESSAGE]
    assert alias_messages == [_CLEAN_REJECTED_MESSAGE]
    assert len(entity_messages) == 1


def test_ingest_bulk_success_message_with_a_long_name_is_verbatim(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = f"Entity created: {_LONG_NAME}"
    monkeypatch.setattr(
        "trellis_api.routes.ingest.build_curate_executor",
        lambda _registry: _StubBulkExecutor(_success(Operation.ENTITY_CREATE, message)),
    )

    resp = client.post(
        "/api/v1/ingest/bulk",
        json={"entities": [{"entity_type": "Dataset", "name": _LONG_NAME}]},
    )

    assert resp.status_code == 200, resp.text
    results = resp.json()["entities"]["results"]
    assert len(results) == 1
    assert results[0]["message"] == message
