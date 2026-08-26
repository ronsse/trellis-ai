"""Curate commands — promote, link, label, feedback, entity, promote-learning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from trellis.learning import prepare_learning_promotions, submit_learning_promotion
from trellis.mutate import (
    Command,
    CommandStatus,
    Operation,
    build_curate_executor,
)
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.output import emit_json
from trellis_cli.stores import _get_registry

curate_app = typer.Typer(no_args_is_help=True)
console = Console()


def _execute_command(cmd: Command, output_format: str) -> None:
    """Submit a command to the MutationExecutor and display the result."""
    result = build_curate_executor(_get_registry()).execute(cmd)

    if output_format == "json":
        console.print(
            json.dumps(
                {
                    "status": result.status.value,
                    "command_id": result.command_id,
                    "operation": result.operation,
                    "message": result.message,
                    "created_id": result.created_id,
                }
            )
        )
    else:
        if result.status == CommandStatus.SUCCESS:
            console.print(f"[green]\u2713 Command executed[/green]: {result.operation}")
        else:
            console.print(
                f"[red]\u2717 Command {result.status}[/red]: {result.operation}"
            )
        console.print(f"  ID: {result.command_id}")
        console.print(f"  Message: {result.message}")


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
            console.print(json.dumps({"status": "error", "message": result.message}))
        else:
            console.print(f"[red]{result.message}[/red]")
        raise typer.Exit(code=EXIT_INTERNAL)

    if output_format == "json":
        console.print(
            json.dumps(
                {
                    "status": "ok",
                    "edge_id": result.created_id,
                    "source_id": source_id,
                    "target_id": target_id,
                    "edge_kind": edge_kind,
                }
            )
        )
    else:
        console.print(f"[green]\u2713 Link created[/green]: {result.created_id}")
        console.print(f"  {source_id} --[{edge_kind}]--> {target_id}")


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
        console.print(
            "[red]No criteria selected — a prune must say what it is pruning. "
            "Pass at least one of --noise-documents / --unconfirmed-mints / "
            "--lifecycle-state.[/red]"
        )
        raise typer.Exit(code=EXIT_VALIDATION)

    cmd = Command(
        operation=Operation.RETENTION_PRUNE,
        args={"criteria": criteria, "reason": reason, "dry_run": not apply},
        target_type="retention_run",
        requested_by=requested_by,
    )
    result = build_curate_executor(_get_registry()).execute(cmd)

    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        exit_code = (
            EXIT_VALIDATION if result.status == CommandStatus.REJECTED else EXIT_STORE
        )
        if output_format == "json":
            emit_json(
                {
                    "status": result.status.value,
                    "command_id": result.command_id,
                    "message": result.message,
                }
            )
        else:
            console.print(f"[red]{escape(result.message)}[/red]")
        raise typer.Exit(code=exit_code)

    if output_format == "json":
        emit_json(
            {
                "status": result.status.value,
                "command_id": result.command_id,
                "dry_run": not apply,
                "message": result.message,
            }
        )
    else:
        console.print(f"[green]✓[/green] {escape(result.message)}")
        if not apply:
            console.print(
                "[yellow]Dry run — nothing was written. Re-run with --apply "
                "to archive.[/yellow]"
            )


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
    """Return archived items to ``Lifecycle.state="current"``.

    The governed inverse of ``curate prune``. Ids that are not currently
    archived are skipped, not treated as errors — a corrective batch built
    from an audit payload will legitimately name items already restored.
    """
    ids = list(item_id)
    if from_file:
        ids.extend(
            line.strip()
            for line in Path(from_file).read_text().splitlines()
            if line.strip()
        )
    if not ids:
        console.print(
            "[red]No ids supplied — pass --item-id (repeatable) or --from-file.[/red]"
        )
        raise typer.Exit(code=EXIT_VALIDATION)

    cmd = Command(
        operation=Operation.RETENTION_RESTORE,
        args={"item_ids": ids, "reason": reason},
        target_type="retention_restore",
        requested_by=requested_by,
    )
    result = build_curate_executor(_get_registry()).execute(cmd)

    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        exit_code = (
            EXIT_VALIDATION if result.status == CommandStatus.REJECTED else EXIT_STORE
        )
        if output_format == "json":
            emit_json(
                {
                    "status": result.status.value,
                    "command_id": result.command_id,
                    "message": result.message,
                }
            )
        else:
            console.print(f"[red]{escape(result.message)}[/red]")
        raise typer.Exit(code=exit_code)

    if output_format == "json":
        emit_json(
            {
                "status": result.status.value,
                "command_id": result.command_id,
                "message": result.message,
            }
        )
    else:
        console.print(f"[green]✓[/green] {escape(result.message)}")


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
        # REJECTED is a validation/policy outcome (EXIT_VALIDATION),
        # FAILED on this path is a store outcome such as target-not-found
        # (EXIT_STORE). ``command_id`` rides the JSON so a failed attempt
        # still joins to its MUTATION_REJECTED audit event.
        exit_code = (
            EXIT_VALIDATION if result.status == CommandStatus.REJECTED else EXIT_STORE
        )
        if output_format == "json":
            emit_json(
                {
                    "status": result.status.value,
                    "command_id": result.command_id,
                    "message": result.message,
                }
            )
        else:
            console.print(f"[red]{escape(result.message)}[/red]")
        raise typer.Exit(code=exit_code)

    if output_format == "json":
        emit_json(
            {
                "status": result.status.value,
                "command_id": result.command_id,
                "target_id": target_id,
                "message": result.message,
            }
        )
    else:
        console.print(f"[green]✓[/green] {escape(result.message)}")


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
                console.print(
                    json.dumps(
                        {
                            "status": "error",
                            "message": f"Invalid JSON for --properties: {exc}",
                        }
                    )
                )
            else:
                console.print(f"[red]Invalid JSON for --properties[/red]: {exc}")
            raise typer.Exit(code=EXIT_INTERNAL) from exc

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

    if output_format == "json":
        console.print(
            json.dumps(
                {
                    "status": "ok",
                    "node_id": result.created_id,
                    "entity_type": entity_type,
                    "name": name,
                    "properties": {**props, "name": name},
                }
            )
        )
    else:
        console.print(f"[green]\u2713 Entity created[/green]: {result.created_id}")
        console.print(f"  Type: {entity_type}")
        console.print(f"  Name: {name}")
        if properties:
            console.print(f"  Properties: {props}")


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
    """
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
            console.print(f"  - {entry['candidate_id']}: {entry['status']}")
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

    executor = build_curate_executor(_get_registry())

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
            entry["candidate_id"],
            f"[{status_style}]{entry['status']}[/{status_style}]",
            entry.get("node_id", "-"),
            edge_summary,
        )
    console.print(table)
