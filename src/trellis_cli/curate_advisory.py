"""``trellis curate advisory restore`` — undo an advisory suppression (#968).

``AdvisoryStore.restore`` was the only way back from a suppression, and it had
no CLI or REST route, so "reversible" meant "reversible from Python". The
case that needs it: a QUERY advisory the generator retired under the
document-frequency rule comes back only when the generator emits its word
again, which it will not once the word's evidence no longer clears
``min_sample_size`` / ``min_effect_size`` / the comparison-arm floor.

**Not routed through ``MutationExecutor``, on purpose.** No advisory write is:
the generator, the fitness loop and the nightly cron all write
``advisories.json`` through :class:`~trellis.stores.advisory_store.AdvisoryStore`'s
own guarded path (degraded and stale refusals), and
``tests/unit/test_governed_write_rule.py`` polices ``put`` / ``delete`` /
``upsert`` on the document and vector planes only, so this command grows no
roster. Governing the advisory file is a separate decision that would move
every one of those writers with it. What this command does mirror is how
suppression is audited: the store write is the source of truth and the
:attr:`~trellis.stores.base.event_log.EventType.ADVISORY_RESTORED` event is a
fail-soft side effect, emitted after the write so it never names a row the
store does not hold.

Exit codes follow :mod:`trellis_cli.exit_codes` and are computed before the
format branch, so ``--format`` cannot change them
(``tests/unit/test_format_exit_parity_rule.py``):

* :data:`EXIT_OK` (0) — restored, previewed, or nothing to do (an already-active
  advisory, or a rule with no suppressed rows).
* :data:`EXIT_VALIDATION` (2) — a bad argument combination, an unknown rule, or
  an unknown advisory id.
* :data:`EXIT_STORE` (5) — the advisory file is degraded or another process
  wrote it since this command read it; nothing was written.
"""

from __future__ import annotations

from typing import Any

import structlog
import typer
from rich.markup import escape

from trellis.errors import StoreWriteRefusedError
from trellis.schemas.advisory import (
    RETIRED_RULE_KEY,
    RETIRED_RULES,
    Advisory,
    AdvisoryStatus,
)
from trellis.stores.advisory_source import resolve_advisory_path
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventType
from trellis_cli.analyze import (
    _exit_if_advisory_store_degraded,
    _exit_on_refused_advisory_write,
)
from trellis_cli.exit_codes import EXIT_OK, EXIT_VALIDATION
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import _get_registry, get_event_log

advisory_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect and undo advisory suppression.",
)
console = build_console()
logger = structlog.get_logger(__name__)

#: The ``source`` on the audit event, as ``cli:analyze`` / ``cli:classify`` are
#: for their commands' events.
_EVENT_SOURCE = "cli:curate"
#: How much of an advisory's message a text row shows.
_MESSAGE_WIDTH = 72


def _refuse_usage(message: str, output_format: str) -> None:
    """Render an argument refusal on the caller's surface, then exit non-zero.

    The exit sits below the format branch, so both formats leave alike.
    """
    if output_format == "json":
        emit_json({"status": "invalid", "message": message})
    else:
        console.print(f"[red]✗ {escape(message)}[/red]", soft_wrap=True)
    raise typer.Exit(code=EXIT_VALIDATION)


def _validate_selection(
    advisory_id: str | None, rule: str | None, *, select_all: bool, output_format: str
) -> None:
    """Refuse every combination that does not name exactly one selection."""
    if advisory_id is not None and select_all:
        _refuse_usage(
            "pass either an advisory id or --rule with --all, not both", output_format
        )
    if advisory_id is None and not select_all:
        _refuse_usage(
            "name an advisory id, or use --rule <rule> --all to restore every "
            "advisory a rule retired",
            output_format,
        )
    if select_all and rule is None:
        _refuse_usage(
            "--all requires --rule: restoring every suppressed advisory would "
            "also undo the fitness loop's suppressions. Rules: "
            + ", ".join(RETIRED_RULES),
            output_format,
        )
    if rule is not None and not select_all:
        _refuse_usage("--rule selects a set; add --all to restore it", output_format)
    if rule is not None and rule not in RETIRED_RULES:
        _refuse_usage(
            f"unknown rule {rule!r}; rules: " + ", ".join(RETIRED_RULES), output_format
        )


def _row(before: Advisory) -> dict[str, Any]:
    """The JSON shape of one advisory in this command's report.

    Built from the row as it was *before* the restore, in a dry run and a real
    one alike, so the two read the same and the operator sees what was undone.
    """
    return {
        "advisory_id": before.advisory_id,
        "category": before.category.value,
        "scope": before.scope,
        "message": before.message,
        "previous_suppression_reason": before.suppression_reason,
        "previous_retired_rule": before.metadata.get(RETIRED_RULE_KEY),
    }


def _emit_restored_event(before: Advisory, reason: str | None) -> bool:
    """Audit one restore, fail-soft. ``True`` when the event landed.

    The store write already landed and is the source of truth; the event is an
    audit side effect that must not turn a restored row into a failed command.
    The failure is not silent, though: the report counts it, because the
    advisory-fitness time series counts ``ADVISORY_RESTORED`` rows, and one the
    operator expected would otherwise be missing without a trace.
    """
    payload: dict[str, Any] = {
        "advisory_id": before.advisory_id,
        "restored_via": "cli",
        "previous_suppression_reason": before.suppression_reason,
        "previous_retired_rule": before.metadata.get(RETIRED_RULE_KEY),
    }
    if reason:
        payload["reason"] = reason
    try:
        get_event_log().emit(
            EventType.ADVISORY_RESTORED,
            source=_EVENT_SOURCE,
            entity_id=before.advisory_id,
            entity_type="advisory",
            payload=payload,
        )
    # GRACEFUL-DEGRADATION: the restore already landed in the store; the event
    # is an audit side-effect and must not fail the command (the report counts
    # the miss).
    except Exception:
        logger.exception(
            "advisory_restore_event_emit_failed", advisory_id=before.advisory_id
        )
        return False
    return True


def _select(
    store: AdvisoryStore, advisory_id: str | None, rule: str | None
) -> tuple[list[Advisory], str | None]:
    """The suppressed rows to restore, and a note when the answer is not rows.

    A single id that is unknown answers ``("not_found")``; one that is already
    active answers ``("noop")``. ``--rule --all`` matches the suppressed rows
    whose ``retired_rule`` is *rule*, so a row the fitness loop suppressed (no
    ``retired_rule``) is never swept up by a bulk restore.
    """
    if advisory_id is not None:
        advisory = store.get(advisory_id)
        if advisory is None:
            return [], "not_found"
        if advisory.status != AdvisoryStatus.SUPPRESSED:
            return [], "noop"
        return [advisory], None
    matched = [
        a
        for a in store.list(include_suppressed=True)
        if a.status == AdvisoryStatus.SUPPRESSED
        and a.metadata.get(RETIRED_RULE_KEY) == rule
    ]
    return matched, None


def _render_text(report: dict[str, Any], rows: list[Advisory]) -> None:
    status = report["status"]
    count = report["count"]
    if status == "not_found":
        console.print(
            f"[red]✗ No advisory {escape(str(report['advisory_id']))}[/red]",
            soft_wrap=True,
        )
        return
    if status == "noop":
        console.print(
            f"  Advisory {escape(str(report['advisory_id']))} is already active; "
            "nothing to restore.",
            soft_wrap=True,
        )
        return
    verb = "Would restore" if status == "dry_run" else "Restored"
    scope = f" retired by {escape(str(report['rule']))}" if report["rule"] else ""
    colour = "yellow" if status == "dry_run" else "green"
    console.print(
        f"[{colour}]{verb} {count} advisor{'y' if count == 1 else 'ies'}"
        f"{scope}[/{colour}]",
        soft_wrap=True,
    )
    for advisory in rows:
        message = advisory.message
        if len(message) > _MESSAGE_WIDTH:
            message = message[: _MESSAGE_WIDTH - 1] + "…"
        category = escape(f"[{advisory.category.value}]")
        console.print(
            f"  {escape(advisory.advisory_id)}  {category}  {escape(message)}",
            soft_wrap=True,
        )
    if report["audit_events_failed"]:
        console.print(
            f"  [yellow]Warning:[/yellow] {report['audit_events_failed']} "
            "ADVISORY_RESTORED event(s) were not recorded; the restores landed.",
            soft_wrap=True,
        )
    if status == "dry_run":
        console.print("  (dry run: nothing written)")


@advisory_app.command("restore")
def restore_advisory(
    advisory_id: str | None = typer.Argument(
        None, help="Advisory id to restore (any suppression, whoever made it)."
    ),
    rule: str | None = typer.Option(
        None,
        "--rule",
        help="With --all: the retired_rule to restore ("
        + ", ".join(RETIRED_RULES)
        + ").",
    ),
    select_all: bool = typer.Option(
        False,
        "--all",
        help="Restore every suppressed advisory that --rule retired. Needs --rule.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List what would be restored; write nothing."
    ),
    reason: str | None = typer.Option(
        None, "--reason", help="Audit-trail justification, recorded on each event."
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Restore a suppressed advisory to active, clearing its retirement marker.

    ``trellis curate advisory restore <id>`` restores one row, however it was
    suppressed. ``--rule document_frequency --all`` restores every row that
    rule retired, in one file write; ``--all`` without ``--rule`` is refused so
    a bulk restore never reverses the fitness loop's evidence-based
    suppressions. Each restored row emits an ``ADVISORY_RESTORED`` event.

    A restore is not a pardon: the generator retires the row again on its next
    run if the rule that retired it still excludes the word (a stop-list word
    stays retired; a frequency retirement holds only while
    ``--max-keyword-df`` is set and the word's share stays over it), and the
    fitness loop scores it again as it does any active row.

    Refuses, exit 5, while the advisory file is degraded or another process has
    rewritten it since this command read it; nothing is written in either case.
    """
    _validate_selection(
        advisory_id, rule, select_all=select_all, output_format=output_format
    )

    stores_dir = _get_registry().stores_dir
    assert stores_dir is not None  # from_config_dir always sets it
    store = AdvisoryStore(resolve_advisory_path(stores_dir))
    # Before any lookup: a degraded store's "not found" may be a row that
    # failed to parse, and a dry run on it would preview a set missing rows.
    _exit_if_advisory_store_degraded(store, output_format)

    audit_failed = 0
    try:
        targets, note = _select(store, advisory_id, rule)
        if dry_run:
            store.refuse_if_stale()
            restored = targets
        else:
            restored_rows = store.restore_many([a.advisory_id for a in targets])
            landed = {a.advisory_id for a in restored_rows}
            restored = [a for a in targets if a.advisory_id in landed]
            audit_failed = sum(
                1 for before in restored if not _emit_restored_event(before, reason)
            )
    except StoreWriteRefusedError as exc:
        _exit_on_refused_advisory_write(exc, output_format)

    status = note or ("dry_run" if dry_run else "restored")
    exit_code = EXIT_VALIDATION if status == "not_found" else EXIT_OK
    report: dict[str, Any] = {
        "status": status,
        "dry_run": dry_run,
        "advisory_id": advisory_id,
        "rule": rule,
        "count": len(restored),
        "audit_events_failed": audit_failed,
        "advisories": [_row(a) for a in restored],
    }
    if output_format == "json":
        emit_json(report)
    else:
        _render_text(report, restored)

    # Below the format branch, so both formats exit alike
    # (tests/unit/test_format_exit_parity_rule.py).
    if exit_code != EXIT_OK:
        raise typer.Exit(code=exit_code)
