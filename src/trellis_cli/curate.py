"""Curate commands — promote, link, label, feedback, entity, promote-learning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from trellis.learning import prepare_learning_promotions
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.handlers import create_curate_handlers
from trellis_cli.stores import _get_registry

curate_app = typer.Typer(no_args_is_help=True)
console = Console()


def _execute_command(cmd: Command, output_format: str) -> None:
    """Submit a command to the MutationExecutor and display the result."""
    registry = _get_registry()
    handlers = create_curate_handlers(registry)
    executor = MutationExecutor(
        event_log=registry.operational.event_log, handlers=handlers
    )
    result = executor.execute(cmd)

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
    registry = _get_registry()
    handlers = create_curate_handlers(registry)
    executor = MutationExecutor(
        event_log=registry.operational.event_log, handlers=handlers
    )
    result = executor.execute(cmd)

    if result.status == CommandStatus.FAILED:
        if output_format == "json":
            console.print(json.dumps({"status": "error", "message": result.message}))
        else:
            console.print(f"[red]{result.message}[/red]")
        raise typer.Exit(code=1)

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
            raise typer.Exit(code=1) from exc

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
    registry = _get_registry()
    handlers = create_curate_handlers(registry)
    executor = MutationExecutor(
        event_log=registry.operational.event_log, handlers=handlers
    )
    result = executor.execute(cmd)

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
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Record feedback on a trace or precedent."""
    args: dict[str, object] = {"target_id": target_id, "rating": rating}
    if comment:
        args["comment"] = comment
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


def _submit_promotion(
    executor: MutationExecutor,
    entity_payload: dict[str, Any],
    edge_payloads: list[dict[str, Any]],
) -> dict[str, Any]:
    """Submit one approved promotion. A failed entity short-circuits the edges.

    ``entity_payload`` and ``edge_payloads`` come from
    :func:`trellis.learning.scoring.build_learning_promotion_payloads`,
    which always sets ``entity_id`` and a non-empty ``properties`` dict
    on both — this function trusts that contract rather than re-guarding.
    """
    entity_cmd = Command(
        operation=Operation.ENTITY_CREATE,
        args={
            "entity_type": entity_payload["entity_type"],
            "entity_id": entity_payload["entity_id"],
            "name": entity_payload["name"],
            "properties": dict(entity_payload["properties"]),
        },
        target_type="entity",
        requested_by="cli:promote-learning",
    )
    entity_result = executor.execute(entity_cmd)
    if entity_result.status != CommandStatus.SUCCESS:
        return {
            "status": "entity_failed",
            "entity_status": entity_result.status.value,
            "message": entity_result.message,
        }

    edge_outcomes = []
    for edge in edge_payloads:
        edge_cmd = Command(
            operation=Operation.LINK_CREATE,
            args={
                "source_id": edge["source_id"],
                "target_id": edge["target_id"],
                "edge_kind": edge["edge_kind"],
                "properties": dict(edge["properties"]),
            },
            target_id=edge["source_id"],
            target_type="entity",
            requested_by="cli:promote-learning",
        )
        edge_result = executor.execute(edge_cmd)
        edge_outcomes.append(
            {
                "edge_kind": edge["edge_kind"],
                "target_id": edge["target_id"],
                "status": edge_result.status.value,
            }
        )
    return {
        "status": "promoted",
        "node_id": entity_result.created_id,
        "edges": edge_outcomes,
    }


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

    registry = _get_registry()
    handlers = create_curate_handlers(registry)
    executor = MutationExecutor(
        event_log=registry.operational.event_log, handlers=handlers
    )

    submission_results: list[dict[str, Any]] = []
    for entry in plan["results"]:
        if entry["status"] != "ready":
            submission_results.append({"candidate_id": entry["candidate_id"], **entry})
            continue
        outcome = _submit_promotion(
            executor,
            entry["entity_payload"],
            entry["edge_payloads"],
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
