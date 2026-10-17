"""Curate commands — promote, link, label, feedback, entity, promote-learning."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NoReturn

import structlog
import typer
from rich.markup import escape
from rich.table import Table

from trellis.errors import ConfigError
from trellis.learning import (
    LEARNING_ARTIFACTS_DIR_ENV,
    LEARNING_CANDIDATES_FILENAME,
    prepare_learning_promotions,
    resolve_learning_artifacts_dir,
    submit_learning_promotion,
)
from trellis.learning.candidate_auto_promote import (
    CANDIDATE_AUTO_PROMOTE_MODE_SETTING,
    CandidateAutoPromoteMode,
    CandidateAutoPromoteReport,
    LearningCandidateAutoPromotePolicy,
    emit_candidate_auto_promote_failure_event,
    report_to_dict,
    resolve_candidate_auto_promote_overrides,
    run_candidate_auto_promotion,
    sanitize_configured_mode,
)
from trellis.learning.precedent_auto_demote import (
    PRECEDENT_AUTO_DEMOTE_MODE_SETTING,
    PrecedentAutoDemoteMode,
    PrecedentAutoDemotePolicy,
    emit_precedent_auto_demote_failure_event,
    resolve_precedent_auto_demote_overrides,
    run_precedent_auto_demotion,
)
from trellis.learning.precedent_auto_demote import (
    report_to_dict as precedent_auto_demote_report_to_dict,
)
from trellis.learning.precedent_auto_demote import (
    sanitize_configured_mode as sanitize_precedent_auto_demote_mode,
)
from trellis.learning.precedent_evidence_gate import MIN_UNHELPFUL_CITATIONS
from trellis.mutate import (
    Command,
    CommandStatus,
    Operation,
    build_curate_executor,
)
from trellis.stores.settings_store import load_settings_values
from trellis_cli.exit_codes import EXIT_VALIDATION, refusal_exit_code
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import _get_registry, get_event_log, get_graph_store

curate_app = typer.Typer(no_args_is_help=True)
console = build_console()
logger = structlog.get_logger(__name__)


def _print_warnings(warnings: list[str]) -> None:
    """Print a command's warnings below its result, one ``Warning:`` line each.

    Every curate write that shows warnings renders them here, so they look
    alike; ``promote-learning`` shows none yet. The ``escape`` is
    load-bearing: a policy's id and condition are free text, and so is the
    error the audit note quotes. ``[/x]`` in any of them raises
    ``MarkupError``, after the write has committed on the success path. The
    id-markup rule does not police a name like ``warning``, so the escape
    lives in this one place.
    """
    for warning in warnings:
        console.print(f"  [yellow]Warning:[/yellow] {escape(warning)}", soft_wrap=True)


def _execute_command(cmd: Command, output_format: str) -> None:
    """Submit a command, display the result, and exit non-zero on refusal or failure.

    A refused or failed command exits with ``refusal_exit_code``, as every
    curate write does. A DUPLICATE exits ``0``.
    """
    result = build_curate_executor(_get_registry()).execute(cmd)

    # ``Enforcement.WARN`` means "allow, but say so", and the only
    # caller-facing channel it has is ``CommandResult.warnings``. The CLI
    # is also the surface where the structlog fallback is unavailable:
    # ``trellis_cli.main`` pins ``TRELLIS_LOG_LEVEL`` to WARNING absent
    # ``-v``, and the gate logs ``policy_warning`` at ``info``.
    #
    # The JSON key is unconditional. An always-present ``[]`` tells a
    # machine consumer that the gate ran and nothing warned; a key that
    # appears only when a policy fires is indistinguishable from a build
    # that predates the field. (The audit *event* keeps its key
    # conditional for a different and still-good reason -- see
    # ``MutationExecutor._emit_event``.)
    if output_format == "json":
        emit_json(
            {
                "status": result.status.value,
                "command_id": result.command_id,
                "operation": result.operation,
                "message": result.message,
                "created_id": result.created_id,
                "warnings": list(result.warnings),
            }
        )
    else:
        if result.status == CommandStatus.SUCCESS:
            console.print(f"[green]\u2713 Command executed[/green]: {result.operation}")
        else:
            console.print(
                f"[red]\u2717 Command {result.status}[/red]: {result.operation}",
                soft_wrap=True,
            )
        console.print(f"  ID: {escape(result.command_id)}")
        console.print(
            f"  Message: {result.message}",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        _print_warnings(result.warnings)

    # Below the format branch, so both formats exit alike
    # (tests/unit/test_format_exit_parity_rule.py) and warnings print first.
    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        raise typer.Exit(code=refusal_exit_code(result))


@curate_app.command()
def promote(
    trace_id: str = typer.Argument(..., help="Trace ID to promote to precedent"),
    title: str = typer.Option(..., help="Precedent title"),
    description: str = typer.Option(..., help="Precedent description"),
    requested_by: str = typer.Option(
        "cli:promote", "--by", help="Audit-trail identifier for the caller."
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Promote a trace to a precedent."""
    cmd = Command(
        operation=Operation.PRECEDENT_PROMOTE,
        args={"trace_id": trace_id, "title": title, "description": description},
        target_id=trace_id,
        target_type="trace",
        requested_by=requested_by,
    )
    _execute_command(cmd, output_format)


@curate_app.command()
def link(
    source_id: str = typer.Argument(..., help="Source entity/node ID"),
    target_id: str = typer.Argument(..., help="Target entity/node ID"),
    edge_kind: str = typer.Option("entity_related_to", "--kind", help="Edge kind"),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Create a link between two entities."""
    cmd = Command(
        operation=Operation.LINK_CREATE,
        args={
            "source_id": source_id,
            "target_id": target_id,
            "edge_kind": edge_kind,
        },
        target_id=source_id,
        target_type="entity",
        requested_by="cli:link",
    )
    result = build_curate_executor(_get_registry()).execute(cmd)

    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        # Handler-raised ValidationError now surfaces as REJECTED (Variant A'
        # of adr-extraction-validation.md §5.5); both error states should
        # exit non-zero so shell pipelines fail loud.
        if output_format == "json":
            emit_json(
                {
                    "status": result.status.value,
                    "command_id": result.command_id,
                    "message": result.message,
                    "warnings": list(result.warnings),
                }
            )
        else:
            console.print(f"[red]{escape(result.message)}[/red]", soft_wrap=True)
            _print_warnings(result.warnings)
        raise typer.Exit(code=refusal_exit_code(result))

    if output_format == "json":
        emit_json(
            {
                "status": "ok",
                "edge_id": result.created_id,
                "source_id": source_id,
                "target_id": target_id,
                "edge_kind": edge_kind,
                "warnings": list(result.warnings),
            }
        )
    else:
        console.print(
            f"[green]\u2713 Link created[/green]: {escape(str(result.created_id))}"
        )
        # ``markup=False`` rather than two escapes: the arrow's own
        # ``--[edge_kind]-->`` brackets are the #492 defect in *literal*
        # text, so Rich ate the edge kind on this line exactly as it ate
        # ``[document]`` from ``retrieve pack``'s item line. Escaping only
        # the two ids would have left the middle of the line still wrong,
        # and nothing here is styled, so there is nothing to lose.
        console.print(
            f"  {source_id} --[{edge_kind}]--> {target_id}",
            markup=False,
            highlight=False,
        )
        _print_warnings(result.warnings)


@curate_app.command()
def label(
    target_id: str = typer.Argument(..., help="Entity ID to label"),
    label_value: str = typer.Argument(..., help="Label to add"),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Add a label to an entity."""
    cmd = Command(
        operation=Operation.LABEL_ADD,
        args={"target_id": target_id, "label": label_value},
        target_id=target_id,
        requested_by="cli:label",
    )
    _execute_command(cmd, output_format)


@curate_app.command("demote-precedent")
def demote_precedent(
    target_id: str = typer.Argument(..., help="Precedent entity ID to demote"),
    reason: str = typer.Option(
        ..., "--reason", help="Audit-trail justification (required, non-empty)"
    ),
    requested_by: str = typer.Option(
        "cli:demote-precedent",
        "--by",
        help="Audit-trail identifier for the caller.",
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Demote a precedent, stamping it ``Lifecycle.state="demoted"``.

    A soft SCD-2 rewrite of the precedent's graph node, not a purge, so
    the pre-demotion version stays readable via ``get_node_history``/
    ``as_of``. The reversal verb is ``trellis curate reinstate-precedent``
    (``precedent.reinstate``) -- ``entity.update`` refuses a write to
    ``properties.lifecycle`` on a precedent node, so it is no longer a
    way to undo this. A demoted precedent stops reaching
    ``list_precedents`` / ``get_lessons`` and is withheld from packs at
    the collect seam, with the withholding recorded in
    ``rejected_items``. Only a precedent promoted from a learning
    candidate has a graph node and can be demoted; a trace- or
    miner-sourced precedent id is refused. Re-demoting an
    already-demoted precedent is an idempotent no-op (exit 0, message
    says so, nothing changes). Re-approving its learning candidate
    instead of reinstating it is refused too -- see
    :func:`trellis.learning.scoring.submit_learning_promotion`.
    """
    cmd = Command(
        operation=Operation.PRECEDENT_DEMOTE,
        args={"target_id": target_id, "reason": reason},
        target_id=target_id,
        target_type="precedent",
        requested_by=requested_by,
    )
    _execute_command(cmd, output_format)


@curate_app.command("reinstate-precedent")
def reinstate_precedent(
    target_id: str = typer.Argument(..., help="Precedent entity ID to reinstate"),
    reason: str = typer.Option(
        ..., "--reason", help="Audit-trail justification (required, non-empty)"
    ),
    requested_by: str = typer.Option(
        "cli:reinstate-precedent",
        "--by",
        help="Audit-trail identifier for the caller.",
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Reinstate a demoted precedent, stamping it ``Lifecycle.state="current"``.

    The reversal verb ``trellis curate demote-precedent`` shipped
    without: a soft SCD-2 rewrite in the other direction, so the demoted
    version stays readable via ``get_node_history``/``as_of`` too, and
    the precedent's vector row is re-synced in the same call so a
    semantic hit stops carrying the demoted snapshot. Only a currently
    ``"demoted"`` precedent may be reinstated -- a target that is
    ``"current"``, ``"archived"``, not found, or not graph-backed is
    refused legibly, naming its actual state. Human-only: the automatic
    demotion policy's own identity is refused outright, since it has no
    legitimate reason to reverse a demotion.
    """
    cmd = Command(
        operation=Operation.PRECEDENT_REINSTATE,
        args={"target_id": target_id, "reason": reason},
        target_id=target_id,
        target_type="precedent",
        requested_by=requested_by,
    )
    _execute_command(cmd, output_format)


@curate_app.command()
def prune(
    reason: str = typer.Option(
        ..., "--reason", help="Audit-trail justification (required, non-empty)"
    ),
    noise_documents: bool = typer.Option(
        False,
        "--noise-documents",
        help="Select documents tagged signal_quality=noise (the demote loop's output).",
    ),
    unconfirmed_mints: bool = typer.Option(
        False,
        "--unconfirmed-mints",
        help="Select unconfirmed extraction mints older than --older-than-days.",
    ),
    lifecycle_state: list[str] = typer.Option(  # noqa: B008 - typer option factory
        [],
        "--lifecycle-state",
        help="Select items in this lifecycle state (repeatable): "
        "draft, deprecated, superseded.",
    ),
    older_than_days: int = typer.Option(
        30,
        "--older-than-days",
        help="Grace period for the age-based criteria. Does not gate "
        "--noise-documents: a noise tag is a verdict, not an age.",
    ),
    max_items: int = typer.Option(
        500, "--max-items", help="Cap on candidates selected in one pass."
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually archive. Without this the command is a DRY RUN that "
        "reports what it would take and writes nothing.",
    ),
    requested_by: str = typer.Option(
        "cli:prune", "--by", help="Audit-trail identifier for the caller."
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Archive low-value derived items via the governed retention pipeline.

    Phase one is ARCHIVAL, not deletion: selected items are stamped
    ``Lifecycle.state="archived"`` and retrieval stops serving them. The
    content stays in the store, so a wrong prune is walked back by
    re-stamping rather than restored from a backup.

    Dry-run by default — pass ``--apply`` to write. Both modes emit a
    RETENTION_PRUNED audit event; the dry run's carries ``dry_run=true``.

    Traces and event-log rows are never candidates, and confirmed entities
    are never candidates regardless of age.
    """
    criteria = {
        "noise_documents": noise_documents,
        "unconfirmed_mints": unconfirmed_mints,
        "lifecycle_states": list(lifecycle_state),
        "older_than_days": older_than_days,
        "max_items": max_items,
    }
    if not (noise_documents or unconfirmed_mints or lifecycle_state):
        message = (
            "No criteria selected — a prune must say what it is pruning. "
            "Pass at least one of --noise-documents / --unconfirmed-mints / "
            "--lifecycle-state."
        )
        if output_format == "json":
            emit_json({"status": "error", "message": message})
        else:
            console.print(f"[red]{message}[/red]", soft_wrap=True)
        raise typer.Exit(code=EXIT_VALIDATION)

    cmd = Command(
        operation=Operation.RETENTION_PRUNE,
        args={"criteria": criteria, "reason": reason, "dry_run": not apply},
        target_type="retention_run",
        requested_by=requested_by,
    )
    result = build_curate_executor(_get_registry()).execute(cmd)

    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        if output_format == "json":
            emit_json(
                {
                    "status": result.status.value,
                    "command_id": result.command_id,
                    "message": result.message,
                    "warnings": list(result.warnings),
                }
            )
        else:
            console.print(f"[red]{escape(result.message)}[/red]", soft_wrap=True)
            _print_warnings(result.warnings)
        raise typer.Exit(code=refusal_exit_code(result))

    if output_format == "json":
        emit_json(
            {
                "status": result.status.value,
                "command_id": result.command_id,
                "dry_run": not apply,
                "message": result.message,
                "warnings": list(result.warnings),
            }
        )
    else:
        console.print(f"[green]✓[/green] {escape(result.message)}")
        if not apply:
            console.print(
                "[yellow]Dry run — nothing was written. Re-run with --apply "
                "to archive.[/yellow]"
            )
        _print_warnings(result.warnings)


@curate_app.command()
def restore(
    item_id: list[str] = typer.Option(  # noqa: B008 - typer option factory
        [],
        "--item-id",
        help="Archived item id to restore (repeatable). Ids ride the "
        "RETENTION_PRUNED audit payload.",
    ),
    reason: str = typer.Option(
        ..., "--reason", help="Audit-trail justification (required, non-empty)"
    ),
    from_file: str = typer.Option(
        "",
        "--from-file",
        help="Read ids from a file, one per line (alternative to --item-id).",
    ),
    requested_by: str = typer.Option(
        "cli:restore", "--by", help="Audit-trail identifier for the caller."
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Return archived items to their pre-archival ``Lifecycle``.

    The governed inverse of ``curate prune``. A restored document goes to
    ``state="current"``; a restored precedent goes to whatever state its
    SCD-2 history shows it held just before that archival (so a demoted
    precedent that was then pruned comes back demoted, not reinstated).
    Ids that are not currently archived are skipped, not treated as
    errors — a corrective batch built from an audit payload will
    legitimately name items already restored. A precedent whose history
    cannot answer what it held before archival is refused individually
    (named, with its code, in ``warnings``) without aborting the rest of
    the batch; the command itself is rejected only when nothing in the
    batch was actually restored.
    """
    ids = list(item_id)
    if from_file:
        try:
            text = Path(from_file).read_text()
        except OSError as exc:
            unreadable = f"Cannot read --from-file: {exc}"
            if output_format == "json":
                emit_json({"status": "error", "message": unreadable})
            else:
                console.print(f"[red]{escape(unreadable)}[/red]", soft_wrap=True)
            raise typer.Exit(code=EXIT_VALIDATION) from exc
        ids.extend(line.strip() for line in text.splitlines() if line.strip())
    if not ids:
        message = "No ids supplied — pass --item-id (repeatable) or --from-file."
        if output_format == "json":
            emit_json({"status": "error", "message": message})
        else:
            console.print(f"[red]{message}[/red]", soft_wrap=True)
        raise typer.Exit(code=EXIT_VALIDATION)

    cmd = Command(
        operation=Operation.RETENTION_RESTORE,
        args={"item_ids": ids, "reason": reason},
        target_type="retention_restore",
        requested_by=requested_by,
    )
    result = build_curate_executor(_get_registry()).execute(cmd)

    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        if output_format == "json":
            emit_json(
                {
                    "status": result.status.value,
                    "command_id": result.command_id,
                    "message": result.message,
                    "warnings": list(result.warnings),
                }
            )
        else:
            console.print(f"[red]{escape(result.message)}[/red]", soft_wrap=True)
            _print_warnings(result.warnings)
        raise typer.Exit(code=refusal_exit_code(result))

    if output_format == "json":
        emit_json(
            {
                "status": result.status.value,
                "command_id": result.command_id,
                "message": result.message,
                "warnings": list(result.warnings),
            }
        )
    else:
        console.print(f"[green]✓[/green] {escape(result.message)}")
        _print_warnings(result.warnings)


@curate_app.command()
def redact(
    target_id: str = typer.Argument(..., help="Entity/node ID to hard-purge"),
    reason: str = typer.Option(
        ..., "--reason", help="Audit-trail justification (required, non-empty)"
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the interactive confirmation (required for scripted use).",
    ),
    requested_by: str = typer.Option(
        "cli:redact", "--by", help="Audit-trail identifier for the caller."
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Redact (hard-purge) a graph entity via the governed pipeline.

    Irreversibly removes ALL versions of the node, every edge touching
    it, its aliases, and its vector entry. The REDACTION_APPLIED audit
    event records the shape of what was removed, never the content.
    Linked documents and observation/measurement nodes are not cascaded;
    their ids ride the audit payload for follow-up.
    """
    if not yes:
        console.print(
            f"[yellow]Irreversibly purge entity {escape(target_id)} — all "
            "versions, edges, aliases, and vector entry?[/yellow]"
        )
        if not typer.confirm("Are you sure?"):
            raise typer.Abort()

    cmd = Command(
        operation=Operation.REDACTION_APPLY,
        args={"target_id": target_id, "reason": reason},
        target_id=target_id,
        target_type="entity",
        requested_by=requested_by,
    )
    result = build_curate_executor(_get_registry()).execute(cmd)

    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        # Destructive command: error states must exit non-zero so shell
        # pipelines fail loud, and the code follows the exit_codes map —
        # a policy refusal is EXIT_POLICY, any other REJECTED (a blank
        # reason, or a target id that names no node) EXIT_VALIDATION, and
        # FAILED on this path is a store outcome such as a concurrent purge
        # or a backend error (EXIT_STORE). ``command_id`` rides the JSON so
        # a failed attempt still joins to its MUTATION_REJECTED audit event.
        if output_format == "json":
            emit_json(
                {
                    "status": result.status.value,
                    "command_id": result.command_id,
                    "message": result.message,
                    "warnings": list(result.warnings),
                }
            )
        else:
            console.print(f"[red]{escape(result.message)}[/red]", soft_wrap=True)
            _print_warnings(result.warnings)
        raise typer.Exit(code=refusal_exit_code(result))

    if output_format == "json":
        emit_json(
            {
                "status": result.status.value,
                "command_id": result.command_id,
                "target_id": target_id,
                "message": result.message,
                "warnings": list(result.warnings),
            }
        )
    else:
        console.print(f"[green]✓[/green] {escape(result.message)}")
        _print_warnings(result.warnings)


@curate_app.command()
def entity(
    entity_type: str = typer.Argument(
        ..., help="Entity type (concept, person, system, etc.)"
    ),
    name: str = typer.Argument(..., help="Entity name"),
    properties: str = typer.Option(
        None,
        "--properties",
        "-p",
        help='JSON properties dict, e.g. \'{"k": "v"}\'',
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Create an entity node in the knowledge graph."""
    props: dict[str, object] = {}
    if properties:
        try:
            props = json.loads(properties)
        except json.JSONDecodeError as exc:
            if output_format == "json":
                emit_json(
                    {
                        "status": "error",
                        "message": f"Invalid JSON for --properties: {exc}",
                    }
                )
            else:
                console.print(
                    f"[red]Invalid JSON for --properties[/red]: {escape(str(exc))}",
                    soft_wrap=True,
                )
            raise typer.Exit(code=EXIT_VALIDATION) from exc

    cmd = Command(
        operation=Operation.ENTITY_CREATE,
        args={
            "entity_type": entity_type,
            "name": name,
            "properties": props,
        },
        target_type="entity",
        requested_by="cli:entity",
    )
    result = build_curate_executor(_get_registry()).execute(cmd)

    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        if output_format == "json":
            emit_json(
                {
                    "status": result.status.value,
                    "command_id": result.command_id,
                    "message": result.message,
                    "warnings": list(result.warnings),
                }
            )
        else:
            console.print(f"[red]{escape(result.message)}[/red]", soft_wrap=True)
            _print_warnings(result.warnings)
        raise typer.Exit(code=refusal_exit_code(result))

    if output_format == "json":
        emit_json(
            {
                "status": "ok",
                "node_id": result.created_id,
                "entity_type": entity_type,
                "name": name,
                "properties": {**props, "name": name},
                "warnings": list(result.warnings),
            }
        )
    else:
        console.print(
            f"[green]\u2713 Entity created[/green]: {escape(str(result.created_id))}"
        )
        console.print(f"  Type: {entity_type}", markup=False, highlight=False)
        console.print(f"  Name: {name}", markup=False, highlight=False)
        if properties:
            console.print(f"  Properties: {props}", markup=False, highlight=False)
        _print_warnings(result.warnings)


@curate_app.command()
def feedback(
    target_id: str = typer.Argument(..., help="Trace or precedent ID"),
    rating: float = typer.Argument(..., help="Rating (0.0 to 1.0)"),
    comment: str = typer.Option(None, help="Optional comment"),
    pack_id: str = typer.Option(
        None,
        "--pack-id",
        help=(
            "Context pack this feedback is about. Without it the event "
            "cannot join to the pack and the learning loop never sees it. "
            "For per-item attribution use the MCP record_feedback tool or "
            "POST /packs/{pack_id}/feedback."
        ),
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Record feedback on a trace or precedent.

    ``--pack-id`` is the join key. This command records a pack-level grade
    only; the per-item ``helpful_item_ids`` / ``unhelpful_item_ids``
    attribution the promote half of the loop consumes is carried by the
    MCP ``record_feedback`` tool and ``POST /packs/{pack_id}/feedback``.

    ``rating`` is held to the MCP tool's inclusive range. Typer parses
    ``nan`` and ``inf`` as floats, and NaN fails every comparison, so the
    check is written as "inside the range" rather than "outside it".
    """
    if not 0.0 <= rating <= 1.0:
        message = f"rating must be between 0.0 and 1.0, got {rating}"
        if output_format == "json":
            emit_json({"status": "error", "message": message})
        else:
            console.print(f"[red]{message}[/red]", soft_wrap=True)
        raise typer.Exit(code=EXIT_VALIDATION)
    args: dict[str, object] = {"target_id": target_id, "rating": rating}
    if comment:
        args["comment"] = comment
    if pack_id:
        args["pack_id"] = pack_id
    cmd = Command(
        operation=Operation.FEEDBACK_RECORD,
        args=args,
        target_id=target_id,
        requested_by="cli:feedback",
    )
    _execute_command(cmd, output_format)


# ---------------------------------------------------------------------------
# promote-learning (H2.3 — operator surface for the promote half)
# ---------------------------------------------------------------------------


@curate_app.command("promote-learning")
def promote_learning(
    candidates: Path = typer.Option(  # noqa: B008 - typer option default
        ...,
        "--candidates",
        help=(
            "Path to ``intent_learning_candidates.json`` produced by "
            "``trellis analyze learning-candidates``."
        ),
    ),
    decisions: Path = typer.Option(  # noqa: B008 - typer option default
        ...,
        "--decisions",
        help=(
            "Path to the filled-in decisions JSON (operator copies the "
            "template emitted by ``learning-candidates`` and sets "
            "``approved: true`` on rows to promote)."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would be promoted without executing any mutations.",
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Promote approved learning candidates into precedent nodes.

    Reads ``--candidates`` + ``--decisions``, runs
    :func:`trellis.learning.prepare_learning_promotions` to build
    entity + edge payloads, then submits each approved promotion
    through the governed mutation pipeline (``ENTITY_CREATE`` + per-
    target ``LINK_CREATE``).

    Use ``--dry-run`` to preview the entity / edge payloads before
    committing — the planner is pure, so dry-run is safe to rerun.
    """
    candidates_payload = json.loads(candidates.read_text(encoding="utf-8"))
    decisions_payload = json.loads(decisions.read_text(encoding="utf-8"))
    plan = prepare_learning_promotions(
        candidates_payload=candidates_payload,
        decisions_payload=decisions_payload,
    )

    ready = [r for r in plan["results"] if r["status"] == "ready"]

    if dry_run:
        if output_format == "json":
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "dry_run": True,
                        "approved_count": plan["approved_count"],
                        "ready_count": len(ready),
                        "results": plan["results"],
                    }
                )
            )
            return
        console.print(
            f"[bold]Dry run[/bold] — {plan['approved_count']} approved, "
            f"{len(ready)} ready to promote"
        )
        for entry in plan["results"]:
            console.print(f"  - {escape(entry['candidate_id'])}: {entry['status']}")
        return

    if not ready:
        if output_format == "json":
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "dry_run": False,
                        "approved_count": plan["approved_count"],
                        "ready_count": 0,
                        "promoted_count": 0,
                        "results": [],
                    }
                )
            )
            return
        console.print(
            "[yellow]No approved promotions found.[/yellow] Edit the "
            "decisions file and set ``approved: true`` on the rows you "
            "want to promote."
        )
        return

    registry = _get_registry()
    executor = build_curate_executor(registry)

    submission_results: list[dict[str, Any]] = []
    for entry in plan["results"]:
        if entry["status"] != "ready":
            submission_results.append({"candidate_id": entry["candidate_id"], **entry})
            continue
        outcome = submit_learning_promotion(
            executor,
            entry["entity_payload"],
            entry["edge_payloads"],
            requested_by="cli:promote-learning",
            graph_store=registry.knowledge.graph_store,
        )
        submission_results.append(
            {
                "candidate_id": entry["candidate_id"],
                "entity_id": entry["entity_id"],
                **outcome,
            }
        )

    promoted_count = sum(1 for r in submission_results if r.get("status") == "promoted")

    if output_format == "json":
        print(
            json.dumps(
                {
                    "status": "ok",
                    "dry_run": False,
                    "approved_count": plan["approved_count"],
                    "ready_count": len(ready),
                    "promoted_count": promoted_count,
                    "results": submission_results,
                }
            )
        )
        return

    console.print(
        f"[bold]Promote Learning[/bold] — {promoted_count}/"
        f"{plan['approved_count']} approved candidates promoted"
    )
    table = Table(title="Promotion Results")
    table.add_column("Candidate ID", style="cyan", max_width=24)
    table.add_column("Status", style="bold")
    table.add_column("Node ID", style="dim", max_width=30)
    table.add_column("Edges")
    for entry in submission_results:
        edges = entry.get("edges") or []
        edge_summary = (
            ", ".join(f"{e['edge_kind']}:{e['status']}" for e in edges)
            if edges
            else "-"
        )
        status_style = "green" if entry.get("status") == "promoted" else "red"
        table.add_row(
            escape(entry["candidate_id"]),
            f"[{status_style}]{entry['status']}[/{status_style}]",
            # ``or`` rather than a ``.get`` default: ``CommandResult.
            # created_id`` is ``str | None`` and a failed submission writes
            # the key with ``None``, so the default never fires and
            # ``escape(None)`` is a ``TypeError``. Rich took ``None`` as an
            # empty cell before the escape went in, so this is a crash the
            # sweep itself would have introduced — and mypy is blind to it
            # because ``entry`` is a ``dict[str, Any]``.
            escape(entry.get("node_id") or "-"),
            escape(edge_summary),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# auto-promote-candidates — report-only evaluation of learning candidates
# ---------------------------------------------------------------------------


def _resolve_holding_a_refused_mode(
    resolve: Callable[..., dict[str, bool | int | str]],
    settings: Mapping[str, bool | int | float | str],
    mode_setting: str,
) -> tuple[dict[str, bool | int | str], ConfigError | None]:
    """Resolve *settings*, holding back a refused stored mode for the caller.

    A stored mode the setting refuses (a hand-edited ``mode: commit``)
    makes *resolve* raise :class:`~trellis.errors.ConfigError`. That
    refusal guards a write, and ``--dry-run`` writes nothing, so the
    caller decides: raise it for a real run, drop it for a dry run. The
    overrides come back with the stored value as ``mode`` either way, so
    the run event still names the mode the operator configured.
    """
    try:
        return resolve(settings=settings), None
    except ConfigError as exc:
        if exc.setting != mode_setting:
            raise
        rest = {k: v for k, v in settings.items() if k != mode_setting}
        overrides = resolve(settings=rest)
        overrides["mode"] = str(settings[mode_setting])
        return overrides, exc


def _raise_held_refusal(*refusals: ConfigError | None, dry_run: bool) -> None:
    """Raise the first held refusal unless this is a ``--dry-run``."""
    if dry_run:
        return
    for refusal in refusals:
        if refusal is not None:
            raise refusal


def _emit_auto_job_validation_failure(
    output_format: str, policy_label: str, error_type: str
) -> None:
    """Render a resolver's value-free ``ValueError``, format-aware.

    Both auto-* report-only commands catch a resolver's ``ValueError``
    (an unrecognized mode, a non-finite env override) the same way:
    never let ``str(exc)`` — which can embed the raw rejected value —
    reach stderr, but a ``--format json`` caller still needs a JSON
    payload carrying ``status`` rather than a plain-text line, so a
    script reading stdout can tell the run failed without scraping
    stderr. *policy_label* names the job whose own configuration
    actually failed, so a sibling policy's bad value is never reported
    as this one's mistake.

    Never exits. The caller raises ``typer.Exit`` itself, below this
    call and outside any format branch, so the exit code stays
    identical in both formats (tests/unit/test_format_exit_parity_rule.py).
    """
    message = f"invalid {policy_label} configuration ({error_type})"
    if output_format == "json":
        emit_json({"status": "error", "error_type": error_type, "message": message})
    else:
        console.print(f"[red]{escape(message)}[/red]", soft_wrap=True)


def _resolve_candidate_auto_promote_overrides_for_cli(
    stores_dir: Path | None,
) -> tuple[dict[str, bool | int | str], ConfigError | None]:
    """Load settings and resolve the six candidate-auto-promote knobs.

    A settings-store read failure degrades to the defaults alone —
    advisory, like every other settings-aware read in this CLI — never
    fails the job over a catalog/settings-file problem.

    Split out from policy construction so a caller can read
    ``overrides["mode"]`` — the true *configured* mode — before anything
    downstream gets a chance to lose or overwrite it: a ``mode="commit"``
    policy raises :class:`~trellis.errors.ConfigError` unconditionally in
    ``__post_init__`` (there is no way to construct one and then force it
    back to report-only and read ``policy.mode`` afterwards), and
    ``--dry-run`` forces the mode passed to construction to
    ``"report_only"`` regardless of what this resolves to. This is the
    only point the true value is ever available.
    """
    try:
        settings = load_settings_values(stores_dir)
    except Exception:
        logger.warning("candidate_auto_promote_settings_read_failed", exc_info=True)
        settings = {}
    return _resolve_holding_a_refused_mode(
        resolve_candidate_auto_promote_overrides,
        settings,
        CANDIDATE_AUTO_PROMOTE_MODE_SETTING,
    )


def _build_candidate_auto_promote_policy(
    overrides: Mapping[str, bool | int | str],
    *,
    force_report_only: bool = False,
    demotion_enabled: bool = False,
) -> LearningCandidateAutoPromotePolicy:
    """Construct the policy from already-resolved *overrides*.

    ``force_report_only`` (``--dry-run``) is applied here, to the mode
    *before* the policy is constructed — never after. A ``mode="commit"``
    policy raises :class:`~trellis.errors.ConfigError` unconditionally in
    ``__post_init__`` unless ``demotion_enabled`` is also ``True`` (there
    is no way to construct one and then force it back to report-only),
    so a stored ``mode="commit"`` would make ``--dry-run`` itself fail
    with exit 5 if the override happened any later than this. The caller
    is expected to have already captured ``overrides["mode"]`` as
    ``configured_mode`` (:func:`_resolve_candidate_auto_promote_overrides_for_cli`)
    before calling this — this function itself reports only the
    (possibly forced) construction mode, never the pre-force value.

    ``demotion_enabled`` is the caller's already-resolved
    ``learning.precedent_auto_demote.enabled`` value (see
    :func:`_resolve_precedent_auto_demote_overrides_for_cli`) — it is not
    one of this policy's own six knobs, since whether the *sibling*
    demotion policy is enabled is what ``mode="commit"`` here is
    conditioned on, not a setting of this policy's own namespace.
    """
    mode = "report_only" if force_report_only else overrides["mode"]
    return LearningCandidateAutoPromotePolicy(
        enabled=bool(overrides["enabled"]),
        mode=mode,  # type: ignore[arg-type]
        demotion_enabled=demotion_enabled,
        min_helpful_count=int(overrides["min_helpful_count"]),
        min_distinct_sessions=int(overrides["min_distinct_sessions"]),
        require_zero_unhelpful=bool(overrides["require_zero_unhelpful"]),
        max_promotions_per_run=int(overrides["max_promotions_per_run"]),
    )


def _raise_bad_candidates_param(msg: str) -> NoReturn:
    """Raise ``BadParameter`` from an inner function (TRY301): the two call
    sites below are validation inside ``auto_promote_candidates``'s own
    ``try`` block, caught by that same function's ``except Exception`` to
    emit a failure event before re-raising — raising directly in the try
    body is exactly the control-flow-as-exception shape TRY301 flags.
    """
    raise typer.BadParameter(msg, param_hint="'--candidates'")


def _load_candidates_file(
    candidates_path: Path | None, stores_dir: Path | None
) -> tuple[Path, list[Any]]:
    """Resolve ``--candidates``'s default path, then read and parse its JSON.

    Split out of ``auto_promote_candidates`` to keep that command's
    branch/statement count under the linter's ceiling
    (``PLR0912``/``PLR0915``) — a mechanical extraction, not a behaviour
    change: every raise here is still inside the caller's own ``try``
    block (unchanged call site), caught by the same ``except Exception``
    that emits a failure event before re-raising.
    """
    if candidates_path is None:
        resolved_dir = resolve_learning_artifacts_dir(stores_dir)
        if resolved_dir is None:
            msg = (
                "no learning-artifacts directory: the store registry "
                f"has no stores_dir and {LEARNING_ARTIFACTS_DIR_ENV} "
                "is unset; pass --candidates"
            )
            _raise_bad_candidates_param(msg)
        candidates_path = resolved_dir / LEARNING_CANDIDATES_FILENAME

    try:
        raw_text = candidates_path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"cannot read candidates file {candidates_path}: {exc}"
        raise typer.BadParameter(msg, param_hint="'--candidates'") from exc
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        msg = f"candidates file {candidates_path} is not valid JSON: {exc}"
        raise typer.BadParameter(msg, param_hint="'--candidates'") from exc
    if not isinstance(payload, dict):
        msg = (
            f"candidates file {candidates_path} must contain a JSON "
            'object with a top-level "candidates" array, got '
            f"{type(payload).__name__}"
        )
        _raise_bad_candidates_param(msg)
    candidates = payload.get("candidates", [])
    if not isinstance(candidates, list):
        candidates = []
    return candidates_path, candidates


@curate_app.command("auto-promote-candidates")
def auto_promote_candidates(
    candidates_path: Path | None = typer.Option(  # noqa: B008 - typer option default
        None,
        "--candidates",
        help=(
            "Path to intent_learning_candidates.json. Defaults to "
            "resolve_learning_artifacts_dir() over the registry's "
            "stores_dir — the same directory the Review queue reads."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Force report_only for this run's effective_mode, applied "
            "before the policy is built. Without --dry-run, a stored or "
            "env-configured learning.candidate_auto_promote.mode of "
            "commit makes every run fail with a configuration error "
            "unless learning.precedent_auto_demote.enabled is also true "
            "(demotion is commit mode's safety rollback); --dry-run "
            "avoids that failure either way by forcing effective_mode to "
            "report_only ahead of the check, "
            "regardless of what mode resolved to. It never changes what "
            "configured_mode reports: a stored commit still reports "
            "configured_mode: commit (effective_mode: report_only) in the "
            "event, the Loop Health row, and this command's own output —"
            " --dry-run hides nothing about what is actually configured, "
            "it only stops that configuration from taking effect."
        ),
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Evaluate learning candidates for automatic promotion — default-off,
    report-only unless an operator enables commit mode.

    Every run — including one that fails before evaluating a single
    candidate — emits at least one ``LEARNING_AUTO_PROMOTE_EVALUATED``
    event and writes nothing else. A completed run's event names each
    candidate's disposition — ``would_promote``, ``already_promoted`` (a
    precedent already exists for it, human-approved or from an earlier
    automatic run), or ``ineligible:<reason>`` — and carries
    ``outcome: "ok"`` / ``"degraded"`` / ``"failed"`` (see
    :func:`~trellis.learning.candidate_auto_promote._run_status`:
    ``"failed"`` means every submission this run attempted was refused,
    ``"degraded"`` means some were, or a candidate was skipped for having
    no usable id). A run that never got that far carries
    ``outcome: "refused"`` (a :class:`~trellis.errors.ConfigError` —
    e.g. a stored or env ``mode="commit"`` without ``--dry-run``, exit 5)
    or ``outcome: "failed"`` (a malformed ``--candidates`` file, or a
    non-finite env override, exit 2 or 1) instead — the same ``"failed"``
    string covers both a completed run with no successful attempt and a
    run that never reached one; the event's other fields disambiguate.
    ``learning.candidate_auto_promote.mode`` can only be set to
    ``commit`` once ``learning.precedent_auto_demote.enabled`` is also
    true — demotion is commit mode's safety rollback. Without that,
    constructing the policy with ``mode="commit"`` raises
    :class:`~trellis.errors.ConfigError`, exit 5 — unless ``--dry-run``
    is passed, since ``--dry-run`` forces ``effective_mode`` to
    ``report_only`` ahead of that construction, so a configured
    ``commit`` never reaches it; ``configured_mode`` in the event and in
    this command's own output still reports the true pre-``--dry-run``
    value. Both policies still ship ``enabled=False`` by default, so
    nothing commits unattended until an operator turns both on.

    This command builds a real governed ``MutationExecutor`` via
    ``build_curate_executor`` on every run, report-only included: the
    executor is what stays fail-closed (Stage 2's policy gate, idempotency,
    audit emission) if ``effective_mode`` is ever wrong, not a missing
    executor. report_only means the policy never issues a
    ``precedent.promote`` command, not that the executor is skipped. See
    the "trellis curate auto-promote-candidates" section in
    docs/agent-guide/operations.md.
    """
    registry = _get_registry()
    event_log = get_event_log()
    configured_mode: CandidateAutoPromoteMode | None = None
    try:
        candidates_path, candidates = _load_candidates_file(
            candidates_path, registry.stores_dir
        )

        overrides, refusal = _resolve_candidate_auto_promote_overrides_for_cli(
            registry.stores_dir
        )
        configured_mode = overrides["mode"]  # type: ignore[assignment]
    except ValueError as exc:
        # A resolver or policy ValueError (an invalid mode, a knob looser
        # than its floor, a non-finite env override) can embed the raw
        # value it rejected — never let str(exc) reach stderr. The clean,
        # value-free message plus a validation exit code is this
        # command's whole response; the failure event records the type.
        emit_candidate_auto_promote_failure_event(
            event_log, error=exc, configured_mode=configured_mode
        )
        _emit_auto_job_validation_failure(
            output_format, "candidate auto-promote", type(exc).__name__
        )
        raise typer.Exit(code=EXIT_VALIDATION) from exc
    except Exception as exc:
        emit_candidate_auto_promote_failure_event(
            event_log, error=exc, configured_mode=configured_mode
        )
        raise

    # Resolved in its own try/except, separate from the block above and
    # below: a bogus TRELLIS_PRECEDENT_AUTO_DEMOTE_MODE (or a hand-edited
    # settings.json refusal) is the *sibling* policy's own configuration
    # problem, never this command's — each failure event and message
    # below names precedent auto-demote, not candidate auto-promote, and
    # a ConfigError (the refused-value case) is left to propagate so the
    # CLI's TrellisError boundary renders and exits exactly as it does
    # for every other ConfigError (gate-879 follow-up #6).
    try:
        demote_overrides, demote_refusal = (
            _resolve_precedent_auto_demote_overrides_for_cli(registry.stores_dir)
        )
    except ConfigError as exc:
        emit_precedent_auto_demote_failure_event(event_log, error=exc)
        # This command's own run also ended without outcomes, so it records
        # its evaluated event too: otherwise loop health keeps showing the
        # last good promote run while every run fails here.
        emit_candidate_auto_promote_failure_event(
            event_log, error=exc, configured_mode=configured_mode
        )
        raise
    except ValueError as exc:
        emit_precedent_auto_demote_failure_event(event_log, error=exc)
        emit_candidate_auto_promote_failure_event(
            event_log, error=exc, configured_mode=configured_mode
        )
        _emit_auto_job_validation_failure(
            output_format, "precedent auto-demote", type(exc).__name__
        )
        raise typer.Exit(code=EXIT_VALIDATION) from exc

    try:
        _raise_held_refusal(refusal, demote_refusal, dry_run=dry_run)
        policy = _build_candidate_auto_promote_policy(
            overrides,
            force_report_only=dry_run,
            demotion_enabled=bool(demote_overrides["enabled"]),
        )

        report = run_candidate_auto_promotion(
            candidates=candidates,
            policy=policy,
            event_log=event_log,
            executor=build_curate_executor(registry),
            graph_store=registry.knowledge.graph_store,
            force_report_only=dry_run,
            configured_mode=configured_mode,
        )
    except ValueError as exc:
        emit_candidate_auto_promote_failure_event(
            event_log, error=exc, configured_mode=configured_mode
        )
        _emit_auto_job_validation_failure(
            output_format, "candidate auto-promote", type(exc).__name__
        )
        raise typer.Exit(code=EXIT_VALIDATION) from exc
    except ConfigError as exc:
        # _raise_held_refusal can surface either policy's own held
        # refusal here — attribute by which setting it names, same
        # convention as the resolution try/except above.
        if exc.setting == PRECEDENT_AUTO_DEMOTE_MODE_SETTING:
            emit_precedent_auto_demote_failure_event(event_log, error=exc)
        emit_candidate_auto_promote_failure_event(
            event_log, error=exc, configured_mode=configured_mode
        )
        raise
    except Exception as exc:
        emit_candidate_auto_promote_failure_event(
            event_log, error=exc, configured_mode=configured_mode
        )
        raise

    if output_format == "json":
        emit_json(report_to_dict(report))
        return
    _print_auto_promote_report(report)


def _print_auto_promote_report(report: CandidateAutoPromoteReport) -> None:
    """Render ``auto_promote_candidates``'s text-format output.

    Split out to keep that command's statement count under the linter's
    ceiling (``PLR0915``); a mechanical extraction, not a behaviour
    change.
    """
    console.print(
        f"[bold]Auto-Promote Candidates[/bold] — mode={report.mode} "
        f"(configured={sanitize_configured_mode(report.configured_mode)})"
    )
    console.print(f"  would_promote: {report.would_promote_count}")
    console.print(f"  already_promoted: {report.already_promoted_count}")
    console.print(f"  promoted: {report.promoted_count}")
    console.print(f"  ineligible: {report.ineligible_count}")
    for outcome in report.outcomes:
        console.print(f"  - {escape(outcome.candidate_id)}: {outcome.disposition}")


def _resolve_precedent_auto_demote_overrides_for_cli(
    stores_dir: Path | None,
) -> tuple[dict[str, bool | int | str], ConfigError | None]:
    """Load settings and resolve the three precedent-auto-demote knobs.

    Mirrors :func:`_resolve_candidate_auto_promote_overrides_for_cli`: a
    settings-store read failure degrades to the defaults alone — advisory,
    never fails the job over a catalog/settings-file problem.
    """
    try:
        settings = load_settings_values(stores_dir)
    except Exception:
        logger.warning("precedent_auto_demote_settings_read_failed", exc_info=True)
        settings = {}
    return _resolve_holding_a_refused_mode(
        resolve_precedent_auto_demote_overrides,
        settings,
        PRECEDENT_AUTO_DEMOTE_MODE_SETTING,
    )


def _build_precedent_auto_demote_policy(
    overrides: Mapping[str, bool | int | str],
    *,
    force_report_only: bool = False,
) -> PrecedentAutoDemotePolicy:
    """Construct the policy from already-resolved *overrides*.

    Unlike :func:`_build_candidate_auto_promote_policy`, ``mode="commit"``
    carries no construction-time gate here — demotion is itself the
    rollback a safe commit mode needs — so ``force_report_only``
    (``--dry-run``) only forces this run's own mode, the same way it does
    for the promotion command; it is applied before construction purely
    for symmetry with that command's convention, not because construction
    would otherwise fail.
    """
    mode = "report_only" if force_report_only else overrides["mode"]
    return PrecedentAutoDemotePolicy(
        enabled=bool(overrides["enabled"]),
        mode=mode,  # type: ignore[arg-type]
        max_demotions_per_run=int(overrides["max_demotions_per_run"]),
    )


@curate_app.command("evaluate-precedent-demotion")
def evaluate_precedent_demotion(
    min_unhelpful_citations: int = typer.Option(
        MIN_UNHELPFUL_CITATIONS,
        "--min-unhelpful-citations",
        min=1,
        help=(
            "Override for the per-precedent unhelpful-citation floor. "
            "Provisional: borrowed from trellis.classify.demotion_gate, "
            "measured for noise documents, not for precedents."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Force report_only for this run's effective_mode, applied "
            "before the policy is built. Without --dry-run, mode follows "
            "learning.precedent_auto_demote.mode (or its env override), "
            "which only takes effect once "
            "learning.precedent_auto_demote.enabled is also true — see "
            "effective_mode. It never changes what configured_mode "
            "reports: a stored commit still reports configured_mode: "
            "commit (effective_mode: report_only when disabled) in the "
            "event, the Loop Health row, and this command's own output."
        ),
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Evaluate current precedents for demotion — default-off, report-only.

    Judges every precedent with a successful ``precedent.promote`` event
    against the citations its own packs collected since it was promoted,
    joining ``PACK_ASSEMBLED.injected_items`` to ``FEEDBACK_RECORDED`` by
    pack id. A precedent demotes only on a floor of unhelpful citations —
    never on the absence of helpful ones — and any precedent cited helpful
    at least as often as unhelpful is refused regardless of the unhelpful
    count. An already-demoted or archived precedent, or one otherwise not
    in the current lifecycle state, is reported ``already_demoted`` or
    ``ineligible:<reason>`` without being judged.

    Only a precedent served through a flat ``get_context`` pack can
    accumulate evidence this way: ``get_lessons`` and sectioned packs emit
    no per-item attribution, so a precedent reachable only through those
    surfaces always refuses for want of evidence, regardless of how useful
    it actually is. See ``attribution_caveat`` in this command's own
    output (every format) for the same statement.

    ``learning.precedent_auto_demote.mode`` only commits once
    ``learning.precedent_auto_demote.enabled`` is also true
    (``effective_mode``); it ships ``enabled=False`` by default, so this
    command writes nothing — one ``precedent.demote`` per ``would_demote``
    verdict, capped by ``max_demotions_per_run``, through the same
    governed path ``trellis curate demote-precedent`` uses — only once an
    operator turns it on. Every run — including one that fails before
    evaluating a single precedent — emits at least one
    ``LEARNING_AUTO_DEMOTE_EVALUATED`` event.

    This command builds a real governed ``MutationExecutor`` via
    ``build_curate_executor`` on every run, report-only included: the
    executor is what stays fail-closed (Stage 2's policy gate, idempotency,
    audit emission) if ``effective_mode`` is ever wrong, not a missing
    executor. report_only means the policy never issues a
    ``precedent.demote`` command, not that the executor is skipped. See
    the "trellis curate evaluate-precedent-demotion" section in
    docs/agent-guide/operations.md.
    """
    registry = _get_registry()
    event_log = get_event_log()
    graph_store = get_graph_store()
    configured_mode: PrecedentAutoDemoteMode | None = None
    try:
        overrides, refusal = _resolve_precedent_auto_demote_overrides_for_cli(
            registry.stores_dir
        )
        configured_mode = overrides["mode"]  # type: ignore[assignment]
        _raise_held_refusal(refusal, dry_run=dry_run)
        policy = _build_precedent_auto_demote_policy(
            overrides, force_report_only=dry_run
        )

        report = run_precedent_auto_demotion(
            policy=policy,
            event_log=event_log,
            graph_store=graph_store,
            executor=build_curate_executor(registry),
            force_report_only=dry_run,
            configured_mode=configured_mode,
            min_unhelpful_citations=min_unhelpful_citations,
        )
    except ValueError as exc:
        # Same value-free-message convention as auto_promote_candidates:
        # never let str(exc) (which can embed the offending value) reach
        # stderr.
        emit_precedent_auto_demote_failure_event(
            event_log, error=exc, configured_mode=configured_mode
        )
        _emit_auto_job_validation_failure(
            output_format, "precedent auto-demote", type(exc).__name__
        )
        raise typer.Exit(code=EXIT_VALIDATION) from exc
    except Exception as exc:
        emit_precedent_auto_demote_failure_event(
            event_log, error=exc, configured_mode=configured_mode
        )
        raise

    if output_format == "json":
        emit_json(precedent_auto_demote_report_to_dict(report))
        return

    console.print(
        f"[bold]Evaluate Precedent Demotion[/bold] — mode={report.mode} "
        f"(configured={sanitize_precedent_auto_demote_mode(report.configured_mode)})"
    )
    if report.scan.truncated:
        # Same placement as `analyze token-usage`: before any count,
        # because a truncated discovery or join scan can under-count
        # unhelpful citations and read as "no evidence" rather than
        # "didn't look far enough".
        console.print(f"  [yellow]window[/yellow] {report.scan.note}")
    console.print(f"  {escape(report.attribution_caveat)}", soft_wrap=True)
    console.print(f"  would_demote: {report.would_demote_count}")
    console.print(f"  demoted: {report.demoted_count}")
    console.print(f"  skipped_rate_limited: {report.skipped_rate_limited_count}")
    console.print(f"  refused: {report.refused_count}")
    console.print(f"  already_demoted: {report.already_demoted_count}")
    console.print(
        f"  skipped_incomplete_evidence: {report.skipped_incomplete_evidence_count}"
    )
    console.print(f"  deduplicated: {report.deduplicated_count}")
    console.print(f"  ineligible: {report.ineligible_count}")
    for outcome in report.outcomes:
        detail = outcome.reason or outcome.disposition
        console.print(f"  - {escape(outcome.entity_id)}: {escape(detail)}")
