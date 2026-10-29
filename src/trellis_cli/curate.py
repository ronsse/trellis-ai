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
from trellis.learning.review_ready import (
    approval_refusal,
    build_review_rows,
    make_promoted_probe,
)
from trellis.learning.scoring import (
    PromotionVerdict,
    preview_learning_promotion,
    promotion_already_promoted,
    promotion_landed,
    promotion_verdict,
    settle_promotion,
    unnamed_approved_count,
)
from trellis.mutate import (
    Command,
    CommandStatus,
    Operation,
    build_curate_executor,
)
from trellis.stores.settings_store import load_settings_values
from trellis_cli.curate_advisory import advisory_app
from trellis_cli.exit_codes import (
    EXIT_OK,
    EXIT_STORE,
    EXIT_VALIDATION,
    refusal_exit_code,
)
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import _get_registry, get_event_log, get_graph_store

curate_app = typer.Typer(no_args_is_help=True)
curate_app.add_typer(advisory_app, name="advisory")
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

    The exit code and the payload's ``status`` read the per-candidate
    outcomes, in either format: ``0`` / ``ok`` when every approved row
    landed (or none was approved), ``2`` / ``refused`` when any was refused
    or an approved row names no ``candidate_id`` (and no write failed),
    ``5`` / ``failed`` when a write did not land. A skipped blank-id row
    refuses the run even when other rows promoted; those writes still stand.

    A row whose precedent is already current, complete and unchanged is
    reported ``already_promoted`` and written not at all, so a decisions file
    can be run again after its refused row is fixed. It counts as satisfied
    for the verdict and is not in ``promoted_count``; ``already_promoted_count``
    carries it.

    ``--dry-run`` writes nothing and always exits ``0``, reporting ``ok``.
    It adds ``would_verdict`` and ``would_exit_code``: the verdict a real run
    would reach if every ready write landed. It sees a refusal the planner or
    an existing precedent's lifecycle state decides, a row that is
    ``already_promoted``, and a blank-id row, and cannot see a policy refusal
    or a failed write.
    """
    candidates_payload = json.loads(candidates.read_text(encoding="utf-8"))
    decisions_payload = json.loads(decisions.read_text(encoding="utf-8"))
    # Opened before the plan, not after it: the planner reads (never writes)
    # the document store so a precedent carries an excerpt of its source,
    # and ``--dry-run`` must show the payload a real run would submit.
    registry = _get_registry()
    plan = prepare_learning_promotions(
        candidates_payload=candidates_payload,
        decisions_payload=decisions_payload,
        document_store=registry.knowledge.document_store,
    )

    ready = [r for r in plan["results"] if r["status"] == "ready"]
    # Approved rows the planner skipped for want of an id: no outcome of their
    # own, but the verdict counts them (a refusal), so a run of only blank ids
    # no longer reads as a no-op.
    unnamed = unnamed_approved_count(plan)

    if dry_run:
        # Read-only, and never an exit code: the preview is named
        # ``would_*`` so it cannot be read as an outcome, and this command
        # returns ``0`` whatever it previews.
        previews = [
            _preview_promotion(
                entry,
                registry.knowledge.graph_store,
                registry.operational.event_log,
                registry.knowledge.document_store,
            )
            for entry in plan["results"]
        ]
        would_exit, would_status = _would_promotion_verdict(
            previews, unnamed_count=unnamed
        )
        if output_format == "json":
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "dry_run": True,
                        "approved_count": plan["approved_count"],
                        "named_count": plan["named_count"],
                        "ready_count": len(ready),
                        "would_verdict": would_status,
                        "would_exit_code": would_exit,
                        "results": [
                            _dry_run_row(entry, would)
                            for entry, would in zip(
                                plan["results"], previews, strict=True
                            )
                        ],
                    }
                )
            )
            return
        console.print(
            f"[bold]Dry run[/bold] — {plan['approved_count']} approved, "
            f"{len(ready)} ready to promote"
        )
        _print_unnamed_note(plan, dry_run=True)
        for entry, would in zip(plan["results"], previews, strict=True):
            note = "" if would == entry["status"] else f" (would be {escape(would)})"
            console.print(
                f"  - {escape(entry['candidate_id'])}: {entry['status']}{note}"
            )
        console.print(
            f"Would-be verdict: {would_status} (a real run would exit "
            f"{would_exit}; this dry run exits {EXIT_OK})"
        )
        return

    if not plan["results"]:
        exit_code = _report_run_without_results(
            plan, output_format, unnamed_count=unnamed
        )
        # Below the format branch, so both formats exit alike.
        if exit_code != EXIT_OK:
            raise typer.Exit(code=exit_code)
        return

    # Only a ready entry reaches the executor; an approved row the planner
    # could not ready (an unknown id, a non-promotable type) passes through
    # as its own outcome, so it is counted in the verdict and not dropped.
    executor = build_curate_executor(registry) if ready else None

    submission_results: list[dict[str, Any]] = []
    for entry in plan["results"]:
        if executor is None or entry["status"] != "ready":
            submission_results.append({"candidate_id": entry["candidate_id"], **entry})
            continue
        outcome = submit_learning_promotion(
            executor,
            entry["entity_payload"],
            entry["edge_payloads"],
            requested_by="cli:promote-learning",
            graph_store=registry.knowledge.graph_store,
            event_log=registry.operational.event_log,
            document_store=registry.knowledge.document_store,
        )
        submission_results.append(
            settle_promotion(
                {
                    "candidate_id": entry["candidate_id"],
                    "entity_id": entry["entity_id"],
                    **outcome,
                }
            )
        )

    promoted_count = sum(1 for r in submission_results if promotion_landed(r))
    already_promoted_count = sum(
        1 for r in submission_results if promotion_already_promoted(r)
    )
    # One reading of the outcomes, taken before either format renders, so the
    # exit code and the payload's ``status`` cannot depend on ``--format``
    # (tests/unit/test_format_exit_parity_rule.py).
    exit_code, status = _promotion_verdict(submission_results, unnamed_count=unnamed)

    if output_format == "json":
        print(
            json.dumps(
                {
                    "status": status,
                    "dry_run": False,
                    "approved_count": plan["approved_count"],
                    "named_count": plan["named_count"],
                    "ready_count": len(ready),
                    "promoted_count": promoted_count,
                    "already_promoted_count": already_promoted_count,
                    "results": submission_results,
                }
            )
        )
    else:
        # Over the rows that named a candidate, as the Review queue and the REST
        # response count them: a blank-id row never runs, and the note below
        # says it was skipped, so dividing by it read as a promotion that fell
        # short.
        console.print(
            f"[bold]Promote Learning[/bold] — {promoted_count}/"
            f"{plan['named_count']} approved candidates promoted"
            + (
                f", {already_promoted_count} already promoted"
                if already_promoted_count
                else ""
            )
        )
        _print_unnamed_note(plan)
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
            status_style = "green" if promotion_landed(entry) else "red"
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
        _print_verdict(status, exit_code)

    # Below the format branch, so both formats exit alike.
    if exit_code != EXIT_OK:
        raise typer.Exit(code=exit_code)


# ---------------------------------------------------------------------------
# review-ready — review and approve the promotion-ready learning candidates
# ---------------------------------------------------------------------------

#: Requester stamped on every promotion this command submits.
REVIEW_READY_REQUESTED_BY = "cli:review-ready"


#: The exit code each verdict maps to. A refusal is the operator's to fix
#: (``EXIT_VALIDATION``); a write that did not land is the deployment's
#: (``EXIT_STORE``).
_EXIT_BY_VERDICT: dict[PromotionVerdict, int] = {
    "ok": EXIT_OK,
    "refused": EXIT_VALIDATION,
    "failed": EXIT_STORE,
}


def _promotion_verdict(
    outcomes: list[dict[str, Any]], *, unnamed_count: int = 0
) -> tuple[int, str]:
    """``(exit_code, status)`` for a promotion run, from one reading of the outcomes.

    Shared by ``promote-learning`` and ``review-ready --approve``. The status
    is :func:`trellis.learning.scoring.promotion_verdict`, the same one the
    REST route states, so no surface has its own rule; this only maps it to an
    exit code. The exit code and the payload's ``status`` are derived together,
    before either format renders, so neither can depend on ``--format``
    (``tests/unit/test_format_exit_parity_rule.py``). ``unnamed_count`` is the
    planner's count of approved rows with a blank id, which only
    ``promote-learning`` can have; ``review-ready`` names its ids on the
    command line.
    """
    status = promotion_verdict(outcomes, unnamed_count=unnamed_count)
    return _EXIT_BY_VERDICT[status], status


def _print_verdict(status: str, exit_code: int) -> None:
    """State the run's verdict on the human surface, beside the exit code."""
    style = "green" if exit_code == EXIT_OK else "red"
    console.print(f"Verdict: [{style}]{status}[/{style}] (exit {exit_code})")


def _print_unnamed_note(plan: dict[str, Any], *, dry_run: bool = False) -> None:
    """Say so when approved rows named no candidate and so were never run.

    ``approved_count`` is what the operator marked; ``named_count`` is what
    the planner could act on. The difference is a row with a blank
    ``candidate_id``: it has no outcome, but it refuses the run, and the rows
    that did promote stay promoted. A dry run has run nothing, so its note is
    in the conditional and says what a real run would do.
    """
    unnamed = unnamed_approved_count(plan)
    if not unnamed:
        return
    if dry_run:
        console.print(
            f"[yellow]{unnamed} approved row(s) name no candidate_id and "
            "would be skipped. A real run would be refused for it; any row "
            "that promoted would stay promoted.[/yellow]"
        )
        return
    console.print(
        f"[yellow]{unnamed} approved row(s) name no candidate_id and "
        "were skipped. A skipped row makes the run refused; any row that "
        "promoted stays promoted.[/yellow]"
    )


def _report_run_without_results(
    plan: dict[str, Any], output_format: str, *, unnamed_count: int
) -> int:
    """Report a run the planner left with no result rows; return its exit code.

    Nothing was approved, or every approved row named no candidate. The first
    is a no-op (``0`` / ``ok``); the second is a refusal, because an approved
    row that names nothing is an error and not a skip. One reading either way,
    taken before the format branch, so the exit code the caller raises cannot
    depend on ``--format``.
    """
    exit_code, status = _promotion_verdict([], unnamed_count=unnamed_count)
    if output_format == "json":
        print(
            json.dumps(
                {
                    "status": status,
                    "dry_run": False,
                    "approved_count": plan["approved_count"],
                    "named_count": plan["named_count"],
                    "ready_count": 0,
                    "promoted_count": 0,
                    "already_promoted_count": 0,
                    "results": [],
                }
            )
        )
    elif plan["approved_count"]:
        # Approved rows exist and none names a candidate; "no approved
        # promotions" would contradict the note.
        _print_unnamed_note(plan)
        _print_verdict(status, exit_code)
    else:
        console.print(
            "[yellow]No approved promotions found.[/yellow] Edit the "
            "decisions file and set ``approved: true`` on the rows you "
            "want to promote."
        )
    return exit_code


def _preview_promotion(
    entry: dict[str, Any], graph_store: Any, event_log: Any, document_store: Any
) -> str:
    """The status a real run would give a planned row, if its writes landed.

    Read-only. A row the planner could not ready keeps its own status; a
    ready row is ``promoted`` unless the submission would answer before its
    first write: an existing precedent's lifecycle state would refuse it, or
    it is ``already_promoted``. Those are the two answers known before any
    write, and they come from the function the submission runs.
    """
    if entry["status"] != "ready":
        return str(entry["status"])
    early = preview_learning_promotion(
        entry["entity_payload"],
        entry["edge_payloads"],
        graph_store=graph_store,
        event_log=event_log,
        document_store=document_store,
    )
    return "promoted" if early is None else str(early["status"])


def _dry_run_row(entry: dict[str, Any], would: str) -> dict[str, Any]:
    """A planned row as ``--dry-run`` prints it, with its preview status.

    A row the submission would refuse is printed without its payload's
    ``name``: a refusal exists to keep that name out of the stores, and the
    name of a candidate refused after a redaction may be the text the
    redaction removed. The rest of the payload stays for the reviewer.
    """
    row = {**entry, "would_status": would}
    payload = entry.get("entity_payload")
    if would.startswith("refused") and isinstance(payload, dict):
        row["entity_payload"] = {k: v for k, v in payload.items() if k != "name"}
    return row


def _would_promotion_verdict(
    previews: list[str], *, unnamed_count: int = 0
) -> tuple[int, str]:
    """``(exit_code, status)`` a real run would reach, for ``--dry-run``'s preview.

    The same :func:`_promotion_verdict` a real run takes, over the rows the
    preview does not expect to land and the planner's blank-id count, so the
    two cannot drift. The caller reports it as ``would_*`` and never exits
    with it.
    """
    return _promotion_verdict(
        [{"status": p} for p in previews if p != "promoted"],
        unnamed_count=unnamed_count,
    )


def _approve_candidates(
    registry: Any,
    rows: list[dict[str, Any]],
    candidates: list[Any],
    approve: list[str],
    *,
    rationale: str,
    force_contradicted: bool,
) -> list[dict[str, Any]]:
    """Promote each named candidate through the governed path; one outcome per id."""
    rows_by_id = {row["candidate_id"]: row for row in rows}
    by_id = {str(c.get("candidate_id")): c for c in candidates if isinstance(c, dict)}
    executor = build_curate_executor(registry)
    outcomes: list[dict[str, Any]] = []
    for candidate_id in dict.fromkeys(approve):
        refusal = approval_refusal(
            candidate_id,
            rows_by_id.get(candidate_id),
            force_contradicted=force_contradicted,
        )
        if refusal is not None:
            outcomes.append(refusal)
            continue
        # Exactly what ``promote-learning`` does with an approved decision.
        plan = prepare_learning_promotions(
            candidates_payload={"candidates": [by_id[candidate_id]]},
            decisions_payload={
                "decisions": [
                    {
                        "candidate_id": candidate_id,
                        "approved": True,
                        "promotion_name": "",
                        "rationale": rationale,
                    }
                ]
            },
            document_store=registry.knowledge.document_store,
        )
        entry = plan["results"][0]
        outcome = submit_learning_promotion(
            executor,
            entry["entity_payload"],
            entry["edge_payloads"],
            requested_by=REVIEW_READY_REQUESTED_BY,
            graph_store=registry.knowledge.graph_store,
            event_log=registry.operational.event_log,
            document_store=registry.knowledge.document_store,
        )
        outcomes.append(
            settle_promotion(
                {
                    "candidate_id": candidate_id,
                    "entity_id": entry["entity_id"],
                    **outcome,
                }
            )
        )
    return outcomes


def _print_review_rows(rows: list[dict[str, Any]], *, ready_count: int) -> None:
    """Render the listing: one block per candidate, evidence beside the excerpt."""
    if not rows:
        console.print("[yellow]No promotion-ready candidates.[/yellow]")
        return
    console.print(
        f"[bold]Promotion-ready candidates[/bold] — "
        f"showing {len(rows)} of {ready_count}"
    )
    for number, row in enumerate(rows, start=1):
        sessions = row["distinct_helpful_sessions"]
        sessionless = row["sessionless_helpful_count"]
        console.print(
            f"\n{number}. [cyan]{escape(row['candidate_id'])}[/cyan]  "
            f"{escape(str(row['intent_family']))}",
            soft_wrap=True,
            highlight=False,
        )
        console.print(
            f"   source {escape(row['item_id'])}  "
            f"helpful {row['helpful_count']}, unhelpful {row['unhelpful_count']}, "
            f"sessions {'n/a' if sessions is None else sessions}, "
            f"sessionless {'n/a' if sessionless is None else sessionless}, "
            f"targets {row['target_entity_count']}",
            soft_wrap=True,
            highlight=False,
        )
        excerpt = row["source_excerpt"]
        console.print(
            f"   excerpt: {escape(excerpt)}"
            if excerpt is not None
            else f"   excerpt: none ({escape(row['source_excerpt_status'])})",
            soft_wrap=True,
            highlight=False,
        )
        if row["cross_family_noise"]:
            families = ", ".join(row["noise_intent_families"])
            console.print(
                f"   [red]contradicted[/red]: also an investigate_noise candidate "
                f"under {escape(families)}",
                soft_wrap=True,
                highlight=False,
            )
        if row["already_promoted"]:
            console.print("   [yellow]already promoted[/yellow]")
    console.print(
        "\n[dim]Approve with: trellis curate review-ready --approve <candidate id> "
        "--rationale TEXT[/dim]"
    )


def _print_approval_outcomes(outcomes: list[dict[str, Any]]) -> None:
    """Render one line per approved id."""
    for outcome in outcomes:
        style = "green" if promotion_landed(outcome) else "red"
        status = escape(str(outcome["status"]))
        message = outcome.get("message")
        tail = f" — {escape(str(message))}" if message else ""
        console.print(
            f"[{style}]{status}[/{style}] {escape(outcome['candidate_id'])}{tail}",
            soft_wrap=True,
            highlight=False,
        )


@curate_app.command("review-ready")
def review_ready(
    candidates_path: Path | None = typer.Option(  # noqa: B008 - typer option default
        None,
        "--candidates",
        help=(
            "Path to intent_learning_candidates.json. Defaults to the "
            "learning-artifacts directory the Review queue reads."
        ),
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        min=1,
        help="List at most N candidates. Does not limit --approve.",
    ),
    approve: list[str] | None = typer.Option(  # noqa: B008 - typer option default
        None,
        "--approve",
        help="Candidate id to promote; repeat the flag for several.",
    ),
    rationale: str = typer.Option(
        "", "--rationale", help="Why these are approved; stored on each precedent."
    ),
    force_contradicted: bool = typer.Option(
        False,
        "--force-contradicted",
        help=(
            "Allow approving a candidate whose item is an investigate_noise "
            "candidate under another intent family."
        ),
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Review the promotion-ready learning candidates and approve the ones you name.

    Without ``--approve`` this only reads: it lists the candidates the
    promotion-ready digest counts, each with a source excerpt, its citation
    counts and two flags, a cross-family noise contradiction and an existing
    precedent. With ``--approve ID`` it promotes each named candidate through
    the governed path, as ``promote-learning`` would, and refuses an id that
    is not ready, is already promoted, or is contradicted (unless
    ``--force-contradicted``). It exits non-zero when any named id did not
    land, in either format, and its text form ends with the same
    ``Verdict: <status> (exit N)`` line ``promote-learning`` prints. The
    listing prints no verdict and exits ``0``.
    """
    registry = _get_registry()
    candidates_path, candidates = _load_candidates_file(
        candidates_path, registry.stores_dir
    )
    rows = build_review_rows(
        candidates,
        document_store=registry.knowledge.document_store,
        is_promoted=make_promoted_probe(
            registry.operational.event_log, registry.knowledge.graph_store
        ),
    )
    payload: dict[str, Any] = {
        "candidates_path": str(candidates_path),
        "ready_count": len(rows),
    }
    exit_code = EXIT_OK
    outcomes: list[dict[str, Any]] = []
    if approve:
        outcomes = _approve_candidates(
            registry,
            rows,
            candidates,
            approve,
            rationale=rationale,
            force_contradicted=force_contradicted,
        )
        exit_code, status = _promotion_verdict(outcomes)
        payload["approved_count"] = len(outcomes)
        payload["promoted_count"] = sum(1 for o in outcomes if promotion_landed(o))
        payload["results"] = outcomes
    else:
        status = "ok"
        payload["shown_count"] = min(limit, len(rows))
        payload["candidates"] = rows[:limit]
    payload = {"status": status, **payload}

    if output_format == "json":
        emit_json(payload)
    elif approve:
        _print_approval_outcomes(outcomes)
        # The listing prints no verdict and exits 0; a promotion run states
        # the one ``promote-learning`` states, beside the same exit code.
        _print_verdict(status, exit_code)
    else:
        _print_review_rows(rows[:limit], ready_count=len(rows))

    # Below the format branch, so both formats exit alike
    # (tests/unit/test_format_exit_parity_rule.py).
    if exit_code != EXIT_OK:
        raise typer.Exit(code=exit_code)


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
            document_store=registry.knowledge.document_store,
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

    Only a precedent served through a ``get_context`` or sectioned pack
    can accumulate evidence this way: ``get_lessons`` emits no per-item
    attribution (nor does a sectioned pack assembled before #958), so a
    precedent reachable only through those surfaces always refuses for want
    of evidence, regardless of how useful it actually is. See
    ``attribution_caveat`` in this command's own output (every format) for
    the same statement.

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
