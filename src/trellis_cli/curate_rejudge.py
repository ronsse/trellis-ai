"""``trellis curate rejudge-withheld``: re-judge pairs withheld from a non-local route.

With reconcile-on-write on and the ``reconcile`` route not declared local,
``save_memory`` does not send a pair with a side labelled above ``internal`` to
the judge (:mod:`trellis.llm.egress`); it stores the new memory as a plain ADD
and emits ``RECONCILE_DEGRADED`` (``reason="egress_withheld"``). This command
revisits those memories once the pair may be sent, through
:func:`trellis.mcp.reconcile_rejudge.rejudge_withheld`; the verdict logic is
reconcile-on-write's own.

Report-only by default, like ``curate prune``: it finds each memory, re-finds
its near-duplicate and screens the pair through the egress gate, then stops. The
judge is not called, so nothing is spent and no text leaves the host; the pairs
``--apply`` would judge are counted as ``would_judge``. Nothing is written until
``--apply``, and nothing is deleted. The exit code is non-zero only for an
operational failure (a store that cannot be read, or a verdict that could not be
written); a pair that is still withheld, or a judge that did not answer, is a
count, never an exit code. It is not scheduled by anything.
"""

from __future__ import annotations

from typing import Any

import structlog
import typer
from rich.markup import escape

from trellis.errors import TrellisError
from trellis.mcp.reconcile_rejudge import RejudgeReport, rejudge_withheld
from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE, exit_code_for
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import _get_registry

console = build_console()
logger = structlog.get_logger(__name__)

_TEXT_ROWS: tuple[tuple[str, str], ...] = (
    ("found", "memories stored under egress_withheld"),
    ("no_longer_pending", "no longer pending (judged, changed or gone)"),
    ("still_withheld", "still withheld (route not local, side labelled): not sent"),
    ("no_candidate", "no older current near-duplicate left"),
    ("would_judge", "would be judged by --apply (report-only: judge not called)"),
    ("judged", "judged"),
    ("applied", "applied"),
    ("judge_failures", "judge failures (left pending)"),
    ("target_protected", "target protected (never stamped)"),
    ("candidate_changed", "changed while judging (left pending)"),
    ("apply_failures", "writes that failed (left pending)"),
    ("deferred", "pending beyond --max-items (not examined)"),
)


def _payload(report: RejudgeReport, *, failed: bool) -> dict[str, Any]:
    return {
        "status": "error" if failed else "ok",
        "dry_run": not report.apply,
        **report.model_dump(mode="json"),
    }


def rejudge_withheld_command(
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Judge the pairs and write the verdicts. Without this the command is "
        "report-only: the judge is not called, the pairs it would judge are "
        "counted, and nothing is written.",
    ),
    max_items: int = typer.Option(
        500, "--max-items", min=1, help="Cap on pending memories examined in one run."
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Re-judge memories stored without a verdict because the route was not local.

    Finds memories ``save_memory`` stored under the ``egress_withheld`` fallback
    and judges each against its near-duplicate with the same judge, verdicts and
    markers as reconcile-on-write. The egress gate is asked again for every
    pair: while the route is still not local and a side is still labelled above
    ``internal``, the pair stays pending, is counted and is not sent.

    A SUPERSEDE stale-marks the older row (never a delete) and an UPDATE links
    an addendum. A NOOP is recorded and the newer row is left current. A
    judged row is not found again. A promoted precedent's row is never stamped.

    Report-only by default: the judge is not called (nothing is spent or sent),
    and the pairs ``--apply`` would judge are counted as ``would_judge``. Pass
    ``--apply`` to judge and write. Exits non-zero only when a store cannot be
    read or a verdict cannot be written.
    """
    failed = False
    report: RejudgeReport | None = None
    error: str | None = None
    code = EXIT_OK
    try:
        report = rejudge_withheld(
            _get_registry(),
            clearance="restricted",  # trusted: operator CLI
            apply=apply,
            max_items=max_items,
        )
        failed = report.apply_failures > 0
        code = EXIT_STORE if failed else EXIT_OK
    except TrellisError as exc:
        failed = True
        error = str(exc)
        code = exit_code_for(exc)

    if output_format == "json":
        if report is None:
            emit_json({"status": "error", "dry_run": not apply, "message": error})
        else:
            emit_json(_payload(report, failed=failed))
    elif report is None:
        console.print(f"[red]{escape(error or '')}[/red]", soft_wrap=True)
    else:
        for field, label in _TEXT_ROWS:
            console.print(f"  {getattr(report, field):>5}  {label}")
        verdicts = ", ".join(f"{n} {k}" for k, n in report.judged_by_verdict.items())
        console.print(f"         judged by verdict: {verdicts}")
        if report.events_truncated:
            console.print(
                "[yellow]The event read hit its cap; older withheld memories "
                "may not be listed.[/yellow]"
            )
        if not apply:
            console.print(
                "[yellow]Report-only: the judge was not called and nothing was "
                "written. Re-run with --apply to judge and write.[/yellow]"
            )

    # Below the format branch: both formats exit alike
    # (tests/unit/test_format_exit_parity_rule.py).
    if code != EXIT_OK:
        raise typer.Exit(code=code)


def register(curate_app: typer.Typer) -> None:
    """Mount ``rejudge-withheld`` onto the ``curate`` Typer app."""
    curate_app.command("rejudge-withheld")(rejudge_withheld_command)


__all__ = ["register", "rejudge_withheld_command"]
