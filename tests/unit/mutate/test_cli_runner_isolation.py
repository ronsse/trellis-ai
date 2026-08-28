"""Regression test for the structlog leak a bare ``CliRunner`` causes.

A Trellis CLI invocation calls ``configure_stderr_logging()``, which pins
the *then-current* ``sys.stderr`` into structlog's global logger factory.
Under ``CliRunner`` that stream is a temporary buffer Click closes when
``invoke`` returns, so every later log call in the process raises
``ValueError: I/O operation on closed file``.

This is not a mutate-layer concern, but it is pinned here because this is
where it was introduced and where it cost 109 test failures across 23
directories in CI. The test asserts the *isolation* holds, not that a
particular directory is safe.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner as BareCliRunner

from tests.structlog_isolation import IsolatedCliRunner, reset_structlog_global_state
from trellis.mutate.commands import Command, Operation
from trellis.mutate.policy_gate import DefaultPolicyGate
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope


def _denying_gate() -> DefaultPolicyGate:
    """A gate whose ``check`` takes the logging deny path."""
    return DefaultPolicyGate(
        policies=[
            Policy(
                policy_type=PolicyType.MUTATION,
                scope=PolicyScope(level="global"),
                rules=[PolicyRule(operation="*", action="deny")],
                enforcement=Enforcement.ENFORCE,
            )
        ]
    )


def _cmd() -> Command:
    return Command(
        operation=Operation.ENTITY_CREATE,
        args={"entity_type": "service", "name": "auth"},
    )


@pytest.fixture
def _restore_structlog():
    """Undo whatever the bare-runner test does to global structlog state."""
    yield
    reset_structlog_global_state()


class TestIsolatedCliRunner:
    def test_logging_works_after_an_isolated_invocation(
        self, tmp_path, monkeypatch, cli_runner: IsolatedCliRunner
    ) -> None:
        """The whole point: a log call after ``invoke`` must not raise."""
        from trellis_cli.main import app

        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        (tmp_path / "data" / "stores").mkdir(parents=True)

        cli_runner.invoke(app, ["policy", "list", "--format", "json"])

        # Takes the deny path, which calls logger.warning.
        allowed, _msg, _warnings = _denying_gate().check(_cmd())
        assert allowed is False

    @pytest.mark.usefixtures("_restore_structlog")
    def test_bare_runner_leak_is_contained_to_itself(
        self, tmp_path, monkeypatch
    ) -> None:
        """Documents the hazard, and proves the cleanup contains it.

        Whether the bare runner actually raises depends on the installed
        ``click`` / ``structlog`` — 8.5.0 / 26.1.0 close the buffer and do,
        8.3.2 / 25.5.0 do not, which is exactly why this passed locally and
        failed in CI. The assertion accepts either, because what must never
        regress is the isolated path above. The ``_restore_structlog``
        fixture is what stops this test poisoning the ones after it.
        """
        from trellis_cli.main import app

        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        (tmp_path / "data" / "stores").mkdir(parents=True)

        BareCliRunner().invoke(app, ["policy", "list", "--format", "json"])

        error: str | None = None
        try:
            _denying_gate().check(_cmd())
        except ValueError as exc:
            error = str(exc)

        assert error is None or "closed file" in error
