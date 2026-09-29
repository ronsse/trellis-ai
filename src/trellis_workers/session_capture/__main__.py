"""CLI entry for the capture sweep — ``python -m trellis_workers.session_capture``.

Also installed as the ``trellis-session-capture`` console script. Both run
:func:`~trellis_workers.session_capture.sweep.run_sweep`, which is where the
actual work (and every ``TRELLIS_CAPTURE_*`` env var) lives; this module is
argparse, the stdout report, and the exit code.

Exit codes follow the sweep's fail-closed contract: no judge at all is always
a failure (nothing ran, nothing will be retried), and a judge that goes away
*mid*-sweep is a failure under the default strict mode — see
:func:`~trellis_workers.session_capture.sweep.strict_mode` for the
``TRELLIS_CAPTURE_STRICT=0`` opt-out. A session that raised mid-sweep is the
same kind of partial failure and follows the same strict rule, under its own
exit code. A typed Trellis error that stops the sweep, such as a refused
``config.yaml``, exits ``5`` with its message on stderr in place of a
traceback; ``trellis worker capture-sessions`` also exits ``5`` on a refused
config.
"""

from __future__ import annotations

import argparse
import json
import sys

import structlog

from trellis.errors import TrellisError
from trellis.logging import configure_stderr_logging
from trellis_workers.session_capture.sweep import (
    CaptureJudgeUnavailableError,
    judge_unavailable_sessions,
    run_sweep,
    strict_mode,
)

logger = structlog.get_logger(__name__)

#: Exit codes. Non-zero means "this sweep did not finish every session it
#: saw" — the systemd unit surfaces that instead of logging a clean success.
EXIT_OK = 0
EXIT_JUDGE_UNAVAILABLE = 1
#: Not 2: argparse exits 2 on a usage error.
EXIT_SESSIONS_ERRORED = 3
#: A typed Trellis error stopped the sweep before it could report, such as a
#: refused ``config.yaml`` (a literal ``${VAR}``, unparseable YAML) or a store
#: backend the registry does not know. 5, not 1: 1 already means the judge was
#: unavailable, and 5 is what ``trellis worker capture-sessions`` exits for
#: those two faults through the CLI's #459 boundary
#: (``trellis_cli.exit_codes.exit_code_for`` maps ConfigError and StoreError
#: to 5). Kept local: nothing under trellis_workers imports trellis_cli.
EXIT_TRELLIS_ERROR = 5


def main(argv: list[str] | None = None) -> int:
    """Run one capture sweep; return a process exit code."""
    parser = argparse.ArgumentParser(prog="trellis-session-capture")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan the sweep without writing memories or advancing the watermark.",
    )
    args = parser.parse_args(argv)

    # stdout is the report channel; structlog's unconfigured default also
    # writes there, so pin it to stderr before anything can log.
    configure_stderr_logging()

    try:
        report = run_sweep(dry_run=args.dry_run)
    except CaptureJudgeUnavailableError as exc:
        # A misconfigured `llm:` block is an operator error, not a crash: the
        # message already names the fix, and a stack trace would bury it.
        logger.error("capture_judge_unavailable", error=str(exc))  # noqa: TRY400
        sys.stderr.write(f"trellis-session-capture: {exc}\n")
        return EXIT_JUDGE_UNAVAILABLE
    except TrellisError as exc:
        # The same operator-error shape for the typed family (#459's boundary,
        # for this entry): a refused config's message names the file and the
        # fix. Nothing broader: an untyped exception keeps its traceback.
        logger.error(  # noqa: TRY400
            "capture_sweep_refused", error=str(exc), error_type=type(exc).__name__
        )
        sys.stderr.write(f"trellis-session-capture: {exc}\n")
        return EXIT_TRELLIS_ERROR

    payload = report.to_payload()
    unjudged = judge_unavailable_sessions(report)
    payload["sessions_judge_unavailable"] = unjudged
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    if unjudged:
        sys.stderr.write(
            f"trellis-session-capture: {unjudged} session(s) left unjudged — "
            f"the judge was unreachable. They stay un-watermarked for retry.\n"
        )
    if report.sessions_errored:
        sys.stderr.write(
            f"trellis-session-capture: {report.sessions_errored} session(s) "
            "raised mid-sweep. They stay un-watermarked for retry; see "
            "capture_session_failed in the log.\n"
        )
    if strict_mode():
        if unjudged:
            return EXIT_JUDGE_UNAVAILABLE
        if report.sessions_errored:
            return EXIT_SESSIONS_ERRORED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
