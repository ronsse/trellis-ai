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

``--rejoin-since ISO`` is a different run, not a sweep option: it re-parses the
transcripts modified at or after that instant and rewrites only the pack joins
that differ, to back-fill joins written before a payload key existed. It builds
no judge, distils nothing, writes no memory and leaves the watermark alone.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

import structlog

from trellis.errors import TrellisError
from trellis.logging import configure_stderr_logging
from trellis.stores.registry import StoreRegistry
from trellis.stores.settings_store import load_settings_values
from trellis_workers.session_capture.sweep import (
    CaptureJudgeUnavailableError,
    judge_unavailable_sessions,
    run_rejoin,
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


def _aware_instant(value: str) -> datetime:
    """An ISO-8601 instant that names its UTC offset; anything else is a usage error."""
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        msg = f"not an ISO-8601 instant: {value!r}"
        raise argparse.ArgumentTypeError(msg) from None
    if instant.utcoffset() is None:
        msg = f"{value!r} has no UTC offset; write it as 2026-10-01T00:00:00Z or +00:00"
        raise argparse.ArgumentTypeError(msg)
    return instant


REJOIN_HELP = (
    "Back-fill pack joins instead of sweeping: re-parse every transcript modified "
    "at or after this ISO-8601 instant (with a UTC offset), ignoring the "
    "watermark, and write a CAPTURE_SESSION_PACKS join only where it differs from "
    "the session's latest. No judge, distillation, memory write or watermark "
    "change. A rewritten join is stamped now, never backdated, and the pack "
    "holdout reads that stamp: (1) a rewritten session reads 'unfinished' until a "
    "completed non-dry-run sweep runs at least `trellis analyze holdout "
    "--settle-hours` (default 3) after the rejoin, so run it at least 3 hours "
    "before a nightly sweep and read the holdout only after that sweep; (2) the "
    "main-session rollup re-dates to the rejoin time; (3) the join scan sees the "
    "rewritten join at its new time. Exits 3 on any session error."
)


def _run_rejoin(since: datetime, registry: StoreRegistry, *, dry_run: bool) -> int:
    """Print the rejoin report on stdout; exit 3 if any session raised."""
    rejoin = run_rejoin(since=since, registry=registry, dry_run=dry_run)
    sys.stdout.write(json.dumps(rejoin.to_payload(), indent=2) + "\n")
    if rejoin.sessions_errored:
        sys.stderr.write(
            f"trellis-session-capture: {rejoin.sessions_errored} session(s) "
            "raised during the rejoin and were skipped; see "
            "capture_rejoin_session_failed in the log.\n"
        )
        return EXIT_SESSIONS_ERRORED
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run one capture sweep, or a ``--rejoin-since`` back-fill; return an exit code."""
    parser = argparse.ArgumentParser(prog="trellis-session-capture")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Plan the sweep without writing memories or advancing the watermark; "
            "with --rejoin-since, report the joins it would write and write none."
        ),
    )
    parser.add_argument(
        "--rejoin-since",
        type=_aware_instant,
        metavar="ISO",
        help=REJOIN_HELP,
    )
    args = parser.parse_args(argv)

    # stdout is the report channel; structlog's unconfigured default also
    # writes there, so pin it to stderr before anything can log.
    configure_stderr_logging()

    try:
        # Built here (not left to run_sweep's own default) so this entry
        # point can also read the stored `capture_strict` override below —
        # the registry run_sweep would otherwise build internally and
        # never hand back. Same StoreRegistry.from_config_dir() resolution
        # either way; this just keeps the handle.
        registry = StoreRegistry.from_config_dir()
        if args.rejoin_since is not None:
            return _run_rejoin(args.rejoin_since, registry, dry_run=args.dry_run)
        report = run_sweep(dry_run=args.dry_run, registry=registry)
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
    if report.sessions_skipped_egress_unreadable:
        sys.stderr.write(
            f"trellis-session-capture: {report.sessions_skipped_egress_unreadable} "
            "session(s) kept off the model because a store could not be asked "
            "whether what they were served may leave the host. They stay "
            "un-watermarked for retry; see capture_session_egress_unreadable "
            "in the log.\n"
        )
    if strict_mode(load_settings_values(registry.stores_dir)):
        if unjudged:
            return EXIT_JUDGE_UNAVAILABLE
        if report.sessions_errored or report.sessions_skipped_egress_unreadable:
            return EXIT_SESSIONS_ERRORED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
