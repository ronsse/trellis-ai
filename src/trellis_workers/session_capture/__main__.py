"""CLI entry for the capture sweep — ``python -m trellis_workers.session_capture``.

Also installed as the ``trellis-session-capture`` console script. Both run
:func:`~trellis_workers.session_capture.sweep.run_sweep`, which is where the
actual work (and every ``TRELLIS_CAPTURE_*`` env var) lives; this module is
argparse, the stdout report, and the exit code.

Exit codes follow the sweep's fail-closed contract: no judge at all is always
a failure (nothing ran, nothing will be retried), and a judge that goes away
*mid*-sweep is a failure under the default strict mode — see
:func:`~trellis_workers.session_capture.sweep.strict_mode` for the
``TRELLIS_CAPTURE_STRICT=0`` opt-out.
"""

from __future__ import annotations

import argparse
import json
import sys

import structlog

from trellis.logging import configure_stderr_logging
from trellis_workers.session_capture.sweep import (
    CaptureJudgeUnavailableError,
    judge_unavailable_sessions,
    run_sweep,
    strict_mode,
)

logger = structlog.get_logger(__name__)

#: Exit codes. Non-zero means "this sweep did not judge everything it saw" —
#: the systemd unit surfaces that instead of logging a clean success.
EXIT_OK = 0
EXIT_JUDGE_UNAVAILABLE = 1


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

    payload = report.to_payload()
    unjudged = judge_unavailable_sessions(report)
    payload["sessions_judge_unavailable"] = unjudged
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    if unjudged:
        sys.stderr.write(
            f"trellis-session-capture: {unjudged} session(s) left unjudged — "
            f"the judge was unreachable. They stay un-watermarked for retry.\n"
        )
        if strict_mode():
            return EXIT_JUDGE_UNAVAILABLE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
