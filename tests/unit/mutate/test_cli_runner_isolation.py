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
    def test_the_runner_resets_the_global_config_it_was_built_to_reset(
        self, tmp_path, monkeypatch, cli_runner: IsolatedCliRunner
    ) -> None:
        """Pins the containment itself, which nothing else did.

        This replaces an assertion that had become unfalsifiable::

            assert error is None or "closed file" in error

        It accepted either outcome by design, because pre-#377 the bare
        runner raised under click 8.5.0 and did not under 8.3.2. Once #377
        removed the raise entirely, *both* disjuncts were satisfied by the
        same value and the test could no longer fail — a detector wired to a
        constant, which is the defect class this repo keeps producing.

        The containment was left genuinely unenforced by that: deleting the
        ``finally: reset_structlog_global_state()`` from
        :meth:`IsolatedCliRunner.invoke` kept the whole suite green. What is
        asserted instead is the contract that survives #377 — after an
        isolated invocation, structlog's *global* config no longer carries
        Trellis's configured factory, so the level it memoised cannot leak
        into the next test's ``capture_logs``.

        The bare-runner hazard itself is covered behaviourally by
        ``tests/unit/test_logging.py::TestBareCliRunner``, which asserts the
        stronger property (the line reaches the stream the process actually
        has) rather than merely that nothing raised.
        """
        import structlog

        from trellis.logging import _STDERR_PROXY
        from trellis_cli.main import app

        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        (tmp_path / "data" / "stores").mkdir(parents=True)

        cli_runner.invoke(app, ["policy", "list", "--format", "json"])

        factory = structlog.get_config()["logger_factory"]
        assert getattr(factory, "_file", None) is not _STDERR_PROXY, (
            "IsolatedCliRunner left Trellis's logger factory installed in "
            "structlog's global config — the invoke-time reset is not running"
        )
