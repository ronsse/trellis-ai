"""The CLI failure boundary — typed errors render, exit, and stay parseable (#459).

Before this boundary existed a damaged ``policies.json`` reached the
operator as a Typer rich traceback, exit ``1``, and nothing at all on
stdout — ``--format json`` included. Every assertion here fails against
that source: the exit code was ``1`` (``EXIT_INTERNAL``, "unexpected; file
a bug"), stdout was empty, and no JSON envelope existed to parse.

The damaged-file cases go through :func:`trellis.mutate.build_policy_gate`
for real rather than raising a hand-made ``ConfigError``: the claim under
test is that the message the *policy loader* writes survives to the
operator, and a synthesised exception would pass while the real path
regressed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis.core.error_sanitize import SUPPRESSED_MARKER
from trellis.errors import (
    ApprovalRequiredError,
    BackendNotInstalledError,
    ConfigError,
    DegradedStoreWriteError,
    IdempotencyError,
    NotFoundError,
    PolicyViolationError,
    StaleStoreWriteError,
    StoreError,
    TrellisError,
    ValidationError,
)
from trellis_cli.exit_codes import (
    EXIT_IDEMPOTENCY,
    EXIT_INTERNAL,
    EXIT_POLICY,
    EXIT_STORE,
    EXIT_VALIDATION,
    exit_code_for,
)
from trellis_cli.main import _requested_format, app

runner = CliRunner()

#: A JSON object with no ``"policies"`` key. One of the three shapes #423
#: widened the strict reader to raise on, and the one an operator reaches
#: by a single-character hand-edit.
DAMAGED_POLICY_FILE = '{"polices": []}'


@pytest.fixture
def damaged_policy_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a store directory whose policy file will not load."""
    data_dir = tmp_path / "data"
    stores = data_dir / "stores"
    stores.mkdir(parents=True)
    path = stores / "policies.json"
    path.write_text(DAMAGED_POLICY_FILE, encoding="utf-8")
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    return path


class TestDamagedPolicyFileIsLegibleOnTheCli:
    """The operator-facing half of #425, on the surface most people meet it."""

    def test_text_names_the_file_the_problem_and_the_recovery(
        self, damaged_policy_file: Path
    ) -> None:
        result = runner.invoke(app, ["curate", "feedback", "trace_1", "0.9"])

        assert result.exit_code == EXIT_STORE
        # The file: an operator cannot fix what they cannot find.
        assert str(damaged_policy_file) in result.output
        # The specific problem, in the loader's own words.
        assert 'no "policies" key' in result.output
        # The recovery, likewise — not a second vocabulary for it.
        assert "remove the file to run with no policies" in result.output
        # And the exception's own stable code, so the reader can tell a
        # config fault from a validation one without parsing prose.
        assert "CONFIG_ERROR" in result.output

    def test_exit_code_is_not_the_unhandled_traceback_default(
        self, damaged_policy_file: Path
    ) -> None:
        """``1`` means "unexpected; file a bug" (``docs/design/adr-cli-exit-codes.md``).

        A malformed file the operator owns is neither unexpected nor a bug,
        and a wrapper branching on ``1`` sends them to the issue tracker.
        """
        result = runner.invoke(app, ["curate", "feedback", "trace_1", "0.9"])

        assert result.exit_code == EXIT_STORE
        assert result.exit_code != EXIT_INTERNAL

    def test_json_surface_emits_a_parseable_envelope(
        self, damaged_policy_file: Path
    ) -> None:
        """#403's rule: the machine surface must not be the one that breaks.

        Pre-fix this path wrote *nothing* to stdout, so a caller doing the
        documented thing got a ``JSONDecodeError`` where it expected a
        structured error it could act on.
        """
        result = runner.invoke(
            app, ["curate", "feedback", "trace_1", "0.9", "--format", "json"]
        )

        payload = json.loads(result.stdout)
        assert payload["status"] == "error"
        assert payload["error_type"] == "ConfigError"
        assert payload["error_code"] == "CONFIG_ERROR"
        assert payload["setting"] == "policies.json"
        assert str(damaged_policy_file) in payload["message"]

    def test_exit_code_does_not_depend_on_format(
        self, damaged_policy_file: Path
    ) -> None:
        """The #437 rule, on the boundary that renders every command's failure."""
        text = runner.invoke(app, ["curate", "feedback", "trace_1", "0.9"])
        machine = runner.invoke(
            app, ["curate", "feedback", "trace_1", "0.9", "--format", "json"]
        )

        assert text.exit_code == machine.exit_code == EXIT_STORE

    def test_equals_form_of_the_format_option_is_honoured(
        self, damaged_policy_file: Path
    ) -> None:
        """``--format=json`` is the same request as ``--format json``.

        The boundary reads the raw argv tail rather than a parsed option,
        so both spellings have to be handled explicitly — and a reader that
        only understood the split form would silently hand Rich prose to
        half its machine callers.
        """
        result = runner.invoke(
            app, ["curate", "feedback", "trace_1", "0.9", "--format=json"]
        )

        assert json.loads(result.stdout)["error_code"] == "CONFIG_ERROR"

    def test_another_command_group_gets_the_same_treatment(
        self, damaged_policy_file: Path
    ) -> None:
        """The boundary is on the root group, not on one command's module.

        ``ingest trace`` reaches the gate through a different sub-app than
        ``curate feedback``; both propagate to the same handler, which is
        the property that makes this one edit cover the CLI.
        """
        trace = tmp_trace_file()
        result = runner.invoke(app, ["ingest", "trace", str(trace)])

        assert result.exit_code == EXIT_STORE
        assert "CONFIG_ERROR" in result.output


def tmp_trace_file() -> Path:
    """A minimal valid trace on disk, for the ``ingest trace`` path."""
    import tempfile

    path = Path(tempfile.mkdtemp()) / "trace.json"
    path.write_text(
        json.dumps({"source": "agent", "intent": "probe", "steps": [], "context": {}}),
        encoding="utf-8",
    )
    return path


class TestTheBoundaryCoversTheWholeTypedFamily:
    """The CLI half of the design claim, which nothing else here pinned.

    The fix registers on ``TrellisError`` rather than ``ConfigError``
    precisely so a subclass added later is covered without a roster edit.
    ``TestHandlerRegistration`` pins that for REST. On the CLI every case
    above raises a ``ConfigError``, so narrowing ``except TrellisError`` to
    ``except ConfigError`` left the whole file green — a mutation that
    deletes the design claim and passes. These are that mutant's tests.
    """

    @pytest.fixture
    def store_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> pytest.MonkeyPatch:
        """A ``StoreError`` — not a ``ConfigError`` — from a real command."""
        data_dir = tmp_path / "data"
        (data_dir / "stores").mkdir(parents=True)
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "sqlite database is locked"
            raise StoreError(msg, store="document")

        monkeypatch.setattr("trellis_cli.curate._get_registry", _boom)
        return monkeypatch

    def test_a_non_config_trellis_error_renders_and_exits(
        self, store_error: pytest.MonkeyPatch
    ) -> None:
        result = runner.invoke(app, ["curate", "feedback", "trace_1", "0.9"])

        assert result.exit_code == EXIT_STORE
        assert "STORE_ERROR" in result.output
        assert "sqlite database is locked" in result.output

    def test_its_json_envelope_carries_the_subclass_context(
        self, store_error: pytest.MonkeyPatch
    ) -> None:
        result = runner.invoke(
            app, ["curate", "feedback", "trace_1", "0.9", "--format", "json"]
        )
        payload = json.loads(result.stdout)

        assert payload["error_type"] == "StoreError"
        assert payload["error_code"] == "STORE_ERROR"
        assert payload["store"] == "document"


class TestEveryMachineFormatGetsTheEnvelope:
    """``jsonl`` is in ``MACHINE_FORMATS`` and nothing exercised it.

    ``_requested_format`` was unit-tested with ``jsonl``, which proves the
    *reader* returns the string — not that ``_render_boundary_failure``
    treats it as machine output. Dropping ``"jsonl"`` from
    ``MACHINE_FORMATS`` left the file green while handing a ``jsonl``
    caller Rich prose on stdout, which is the #403 defect this boundary
    exists to prevent.
    """

    def test_jsonl_is_parseable_too(self, damaged_policy_file: Path) -> None:
        result = runner.invoke(
            app, ["curate", "feedback", "trace_1", "0.9", "--format", "jsonl"]
        )

        assert result.exit_code == EXIT_STORE
        payload = json.loads(result.stdout)
        assert payload["error_code"] == "CONFIG_ERROR"


class TestTheMachineEnvelopeIsLeakGuarded:
    """#206's guard, on the surface the PR claimed it covered.

    The API test pins the message. The CLI had no leak test at all, so
    replacing ``sanitized_error_payload`` with a hand-built dict shipping
    ``exc.message`` raw kept every assertion green — on the one surface
    whose output is routinely captured into CI logs and review bundles.
    """

    @pytest.fixture
    def clean_deployment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> pytest.MonkeyPatch:
        data_dir = tmp_path / "data"
        (data_dir / "stores").mkdir(parents=True)
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
        return monkeypatch

    def test_a_dsn_in_the_message_does_not_ship(
        self, clean_deployment: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "could not connect to postgresql://trellis:hunter2@db:5432/trellis"
            raise StoreError(msg)

        clean_deployment.setattr("trellis_cli.curate._get_registry", _boom)
        result = runner.invoke(
            app, ["curate", "feedback", "trace_1", "0.9", "--format", "json"]
        )
        payload = json.loads(result.stdout)

        assert result.exit_code == EXIT_STORE
        assert "hunter2" not in result.stdout
        assert "suppressed" in payload["message"]

    def test_the_context_fields_are_guarded_too(
        self, clean_deployment: pytest.MonkeyPatch
    ) -> None:
        """A guard the message passes and a sibling key defeats is not a guard.

        ``path`` and ``recovery`` are built from a resolved filesystem
        path, so they are exception content, not caller-authored context —
        which is what ``sanitized_error_payload``'s own contract requires
        of the fields handed to it. Shipping them raw beside a suppressed
        message put the identical text back into the same envelope.
        """
        leaky = "/srv/deploy/token=s3cr3tvalue/policies.json"

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "refused"
            raise DegradedStoreWriteError(
                msg,
                store="policy",
                path=leaky,
                recovery=f"mv {leaky} {leaky}.bak",
            )

        clean_deployment.setattr("trellis_cli.curate._get_registry", _boom)
        result = runner.invoke(
            app, ["curate", "feedback", "trace_1", "0.9", "--format", "json"]
        )

        assert "s3cr3tvalue" not in result.stdout
        assert json.loads(result.stdout)["path"] == SUPPRESSED_MARKER

    def test_a_clean_path_still_reaches_the_operator(
        self, damaged_policy_file: Path
    ) -> None:
        """The guard must not cost the legibility this whole change buys.

        A resolved path is exactly the shape the sanitizer was written to
        pass — ``/`` and ``.`` break its token heuristic — so the ordinary
        case is unchanged and the file is still named.
        """
        result = runner.invoke(
            app, ["curate", "feedback", "trace_1", "0.9", "--format", "json"]
        )
        payload = json.loads(result.stdout)

        assert payload["setting"] == "policies.json"
        assert str(damaged_policy_file) in payload["message"]


class TestUntypedFailuresAreNotSwallowed:
    """The boundary catches the typed family and nothing else."""

    def test_a_plain_exception_still_reaches_the_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``RuntimeError`` here really is "unexpected; file a bug".

        Folding it into the same actionable envelope would tell an operator
        to go fix their configuration for what is a Trellis defect — the
        boundary is a translator for errors that were *written* for an
        operator, not a blanket.
        """
        data_dir = tmp_path / "data"
        (data_dir / "stores").mkdir(parents=True)
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "not a Trellis error"
            raise RuntimeError(msg)

        monkeypatch.setattr("trellis_cli.curate._get_registry", _boom)
        result = runner.invoke(app, ["curate", "feedback", "trace_1", "0.9"])

        assert isinstance(result.exception, RuntimeError)
        assert result.exit_code == EXIT_INTERNAL


class TestRequestedFormat:
    """The argv reader, in isolation — it is the one guess the boundary makes."""

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            ([], "text"),
            (["curate", "feedback", "t", "0.9"], "text"),
            (["curate", "feedback", "--format", "json"], "json"),
            (["curate", "feedback", "--format=json"], "json"),
            (["retrieve", "pack", "--format", "jsonl"], "jsonl"),
            # Last wins, as click resolves a non-``multiple`` option.
            (["x", "--format", "text", "--format", "json"], "json"),
            (["x", "--format=json", "--format=text"], "text"),
            # A trailing ``--format`` with no value parses as no request:
            # click will reject it, and guessing ``json`` would put an
            # envelope on stdout for a usage error.
            (["x", "--format"], "text"),
        ],
    )
    def test_reads_the_option(self, args: list[str], expected: str) -> None:
        assert _requested_format(args) == expected


class TestExitCodeMapping:
    """``exit_codes.py``'s prose map, now executable and pinned to it."""

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (ValidationError("bad"), EXIT_VALIDATION),
            (PolicyViolationError("denied", policy_id="p1"), EXIT_POLICY),
            (IdempotencyError(idempotency_key="k"), EXIT_IDEMPOTENCY),
            (StoreError("down"), EXIT_STORE),
            (NotFoundError(entity_type="trace", entity_id="t1"), EXIT_STORE),
            (DegradedStoreWriteError("refused"), EXIT_STORE),
            (StaleStoreWriteError("refused"), EXIT_STORE),
            (ConfigError("bad file"), EXIT_STORE),
            (BackendNotInstalledError(backend_name="arcadedb"), EXIT_STORE),
            # No documented code of its own, and no reason to invent one.
            (ApprovalRequiredError("wait", approval_id="a1"), EXIT_INTERNAL),
            (TrellisError("generic"), EXIT_INTERNAL),
            (RuntimeError("untyped"), EXIT_INTERNAL),
        ],
    )
    def test_maps_the_hierarchy(self, exc: BaseException, expected: int) -> None:
        assert exit_code_for(exc) == expected

    def test_subclasses_resolve_before_their_base(self) -> None:
        """``NotFoundError`` is a ``StoreError``; ``PolicyViolationError`` is not.

        The ordering inside :func:`exit_code_for` is load-bearing — a
        ``MutationError`` check placed above ``PolicyViolationError`` would
        silently collapse three codes into one, and every case above would
        still pass if the map were merely *a* map rather than *the* map.
        """
        assert exit_code_for(PolicyViolationError("d", policy_id="p")) == EXIT_POLICY
        assert exit_code_for(IdempotencyError(idempotency_key="k")) == EXIT_IDEMPOTENCY
        assert (
            exit_code_for(NotFoundError(entity_type="t", entity_id="i")) == EXIT_STORE
        )
