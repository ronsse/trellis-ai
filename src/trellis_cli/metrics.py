"""``trellis metrics`` — read-out surface for the parameter-tuning loop.

Subcommands let an operator inspect and act on the ops-plane stores
that RuleTuner writes to:

* ``outcomes`` — cell-level aggregates from OutcomeStore.
* ``proposals`` — pending / canary / promoted tuner proposals.
* ``versions`` — list ParameterSet snapshots for a scope.
* ``tune`` — run one RuleTuner pass (deterministic; safe to re-run).
* ``promote`` — route a specific proposal through the promotion
  pipeline.  Dry-run by default; requires ``--commit`` to actually
  write the new snapshot and emit the ``PARAMS_UPDATED`` event.

Every command honours ``--format json`` per CLAUDE.md's hard rule;
machine consumers should never parse the human table view.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from trellis.learning.tuners import (
    PromotionPolicy,
    RuleTuner,
    aggregate_outcomes,
    promote_proposal,
)
from trellis.schemas.parameters import ParameterScope
from trellis_cli.stores import (
    get_event_log,
    get_outcome_store,
    get_parameter_store,
    get_tuner_state_store,
)

metrics_app = typer.Typer(no_args_is_help=True)
console = Console()


def _emit_json(payload: Any) -> None:
    """Write JSON via ``typer.echo`` so Rich doesn't line-wrap long values."""
    typer.echo(json.dumps(payload))


# ---------------------------------------------------------------------------
# outcomes
# ---------------------------------------------------------------------------


@metrics_app.command("outcomes")
def outcomes_cmd(
    component_id: str | None = typer.Option(None, "--component-id"),
    domain: str | None = typer.Option(None, "--domain"),
    intent: str | None = typer.Option(None, "--intent"),
    phase: str | None = typer.Option(None, "--phase"),
    days: int = typer.Option(30, help="Sliding-window size in days."),
    limit: int = typer.Option(5000, help="Max outcomes to scan."),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Show per-cell outcome aggregates for the feedback-driven tuning loop."""
    store = get_outcome_store()
    since = datetime.now(UTC) - timedelta(days=days)
    outcomes = store.query(
        component_id=component_id,
        domain=domain,
        intent_family=intent,
        phase=phase,
        since=since,
        limit=limit,
    )
    aggs = aggregate_outcomes(outcomes)

    if output_format == "json":
        _emit_json(
            {
                "window_days": days,
                "outcomes_scanned": len(outcomes),
                "cells": [_agg_to_dict(a) for a in aggs],
            }
        )
        return

    console.print(
        f"[bold]Outcome aggregates[/bold] (last {days} days, "
        f"{len(outcomes)} outcomes → {len(aggs)} cells)"
    )
    if not aggs:
        console.print("  no matching outcomes")
        return

    table = Table()
    table.add_column("component_id")
    table.add_column("domain")
    table.add_column("intent_family")
    table.add_column("tool_name")
    table.add_column("count", justify="right")
    table.add_column("success_rate", justify="right")
    table.add_column("mean_latency_ms", justify="right")
    table.add_column("ref_rate", justify="right")
    for agg in sorted(aggs, key=lambda a: -a.count):
        table.add_row(
            agg.scope.component_id,
            agg.scope.domain or "-",
            agg.scope.intent_family or "-",
            agg.scope.tool_name or "-",
            str(agg.count),
            f"{agg.success_rate:.1%}",
            f"{agg.mean_latency_ms:.1f}",
            f"{agg.reference_rate:.1%}",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# proposals
# ---------------------------------------------------------------------------


@metrics_app.command("proposals")
def proposals_cmd(
    tuner: str | None = typer.Option(None, "--tuner"),
    status: str | None = typer.Option(None, "--status"),
    limit: int = typer.Option(100),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """List tuner proposals, optionally filtered by tuner / status."""
    store = get_tuner_state_store()
    proposals = store.list_proposals(tuner=tuner, status=status, limit=limit)

    if output_format == "json":
        _emit_json([p.model_dump(mode="json") for p in proposals])
        return

    console.print(
        f"[bold]Proposals[/bold] ({len(proposals)} matching; "
        f"tuner={tuner or 'any'} status={status or 'any'})"
    )
    if not proposals:
        return
    # Small summary: status histogram
    status_hist = Counter(p.status for p in proposals)
    console.print(f"  statuses: {dict(status_hist)}")
    table = Table()
    table.add_column("proposal_id")
    table.add_column("tuner")
    table.add_column("status")
    table.add_column("component_id")
    table.add_column("domain")
    table.add_column("proposed_values")
    table.add_column("sample_size", justify="right")
    for p in proposals:
        table.add_row(
            p.proposal_id[:18] + "…",
            p.tuner,
            p.status,
            p.scope.component_id,
            p.scope.domain or "-",
            json.dumps(p.proposed_values),
            str(p.sample_size),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------


@metrics_app.command("versions")
def versions_cmd(
    component_id: str = typer.Argument(..., help="Component id to list versions for."),
    domain: str | None = typer.Option(None, "--domain"),
    intent: str | None = typer.Option(None, "--intent"),
    tool: str | None = typer.Option(None, "--tool"),
    limit: int = typer.Option(20),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """List ParameterSet snapshots for a scope (most recent first)."""
    store = get_parameter_store()
    scope = ParameterScope(
        component_id=component_id,
        domain=domain,
        intent_family=intent,
        tool_name=tool,
    )
    history = store.list_versions(scope, limit=limit)
    active = store.get_active(scope)

    if output_format == "json":
        _emit_json(
            {
                "scope": list(scope.key()),
                "active_version": active.params_version if active else None,
                "versions": [s.model_dump(mode="json") for s in history],
            }
        )
        return

    console.print(
        f"[bold]Parameter history[/bold] scope={scope.key()}  ({len(history)} versions)"
    )
    if active is None:
        console.print("  [yellow]no active snapshot[/yellow]")
    else:
        console.print(f"  active: [green]{active.params_version}[/green]")

    if not history:
        return

    table = Table()
    table.add_column("params_version")
    table.add_column("created_at")
    table.add_column("source")
    table.add_column("values")
    for snap in history:
        marker = " *" if active and snap.params_version == active.params_version else ""
        table.add_row(
            snap.params_version + marker,
            snap.created_at.isoformat(),
            snap.source,
            json.dumps(snap.values),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# tune
# ---------------------------------------------------------------------------


@metrics_app.command("tune")
def tune_cmd(
    tuner_name: str = typer.Option("rule_tuner", "--tuner-name"),
    since_days: int | None = typer.Option(
        None, "--since-days", help="Force rescan of the last N days (ignores cursor)."
    ),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Run one RuleTuner pass — read outcomes, aggregate, emit proposals."""
    tuner = RuleTuner(
        get_outcome_store(),
        get_tuner_state_store(),
        tuner_name=tuner_name,
    )
    since = (
        datetime.now(UTC) - timedelta(days=since_days)
        if since_days is not None
        else None
    )
    proposals = tuner.run(since=since)

    if output_format == "json":
        _emit_json(
            {
                "tuner_name": tuner_name,
                "proposals_persisted": len(proposals),
                "proposals": [p.model_dump(mode="json") for p in proposals],
            }
        )
        return

    console.print(
        f"[bold]RuleTuner[/bold] tuner={tuner_name} "
        f"→ {len(proposals)} proposals persisted"
    )
    for p in proposals:
        console.print(
            f"  {p.proposal_id[:18]}…  "
            f"{p.scope.component_id} domain={p.scope.domain or '-'}  "
            f"{json.dumps(p.proposed_values)}  (n={p.sample_size})"
        )


# ---------------------------------------------------------------------------
# promote
# ---------------------------------------------------------------------------


@metrics_app.command("promote")
def promote_cmd(
    proposal_id: str = typer.Argument(..., help="Proposal id to promote."),
    commit: bool = typer.Option(
        False,
        "--commit",
        help="Actually write the snapshot; dry-run without this flag.",
    ),
    min_sample_size: int | None = typer.Option(None, "--min-sample-size"),
    min_effect_size: float | None = typer.Option(None, "--min-effect-size"),
    force: bool = typer.Option(False, "--force", help="Skip the policy gate."),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Route a proposal through the promotion pipeline.

    Dry-run by default — runs validate + policy gate, reports the
    decision, but does **not** mutate the stores or emit any event.
    Pass ``--commit`` to actually write the new ParameterSet.
    """
    tuner_state = get_tuner_state_store()
    params = get_parameter_store()
    events = get_event_log()

    policy_kwargs: dict[str, Any] = {}
    if min_sample_size is not None:
        policy_kwargs["min_sample_size"] = min_sample_size
    if min_effect_size is not None:
        policy_kwargs["min_effect_size"] = min_effect_size
    policy = PromotionPolicy(**policy_kwargs) if policy_kwargs else None

    if not commit:
        _dry_run_promote(
            proposal_id=proposal_id,
            tuner_state=tuner_state,
            params=params,
            policy=policy,
            force=force,
            output_format=output_format,
        )
        return

    result = promote_proposal(
        proposal_id,
        tuner_state=tuner_state,
        parameter_store=params,
        event_log=events,
        policy=policy,
        force=force,
        source="trellis.metrics.promote",
    )

    if output_format == "json":
        _emit_json(
            {
                "proposal_id": result.proposal_id,
                "status": result.status,
                "reason": result.reason,
                "params_version": result.params_version,
                "effect_size": result.effect_size,
            }
        )
        return

    color = (
        "green"
        if result.status == "promoted"
        else "red"
        if result.status == "rejected"
        else "yellow"
    )
    console.print(
        f"[{color}]{result.status.upper()}[/{color}] {result.proposal_id}: "
        f"{result.reason}"
    )
    if result.params_version:
        console.print(f"  new params_version: {result.params_version}")
    if result.effect_size is not None:
        console.print(f"  effect_size: {result.effect_size:.4f}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agg_to_dict(agg: Any) -> dict[str, Any]:
    """Serialise an AggregatedOutcomes for JSON output."""
    return {
        "scope": {
            "component_id": agg.scope.component_id,
            "domain": agg.scope.domain,
            "intent_family": agg.scope.intent_family,
            "tool_name": agg.scope.tool_name,
        },
        "count": agg.count,
        "success_count": agg.success_count,
        "success_rate": agg.success_rate,
        "mean_latency_ms": agg.mean_latency_ms,
        "items_served_total": agg.items_served_total,
        "items_referenced_total": agg.items_referenced_total,
        "reference_rate": agg.reference_rate,
        "metric_means": {k: agg.mean_metric(k) for k in agg.metric_counts},
    }


def _dry_run_promote(
    *,
    proposal_id: str,
    tuner_state: Any,
    params: Any,
    policy: PromotionPolicy | None,
    force: bool,
    output_format: str,
) -> None:
    """Report what ``promote --commit`` would do without mutating anything."""
    proposal = tuner_state.get_proposal(proposal_id)
    if proposal is None:
        msg = "proposal_not_found"
        payload = {
            "proposal_id": proposal_id,
            "status": "skipped",
            "reason": msg,
            "dry_run": True,
        }
        if output_format == "json":
            _emit_json(payload)
        else:
            console.print(f"[yellow]SKIPPED[/yellow] {proposal_id}: {msg}")
        return

    baseline = params.resolve(proposal.scope)
    baseline_values = baseline.values if baseline else None

    # Preview the policy decision by importing the helper.
    from trellis.learning.tuners.promotion import (  # noqa: PLC0415
        _apply_policy,
        _compute_effect_size,
    )

    effect, has_non_numeric = _compute_effect_size(
        proposal.proposed_values, baseline_values
    )
    effective_policy = policy or PromotionPolicy()
    reason = (
        None
        if force
        else _apply_policy(
            proposal=proposal,
            policy=effective_policy,
            baseline_values=baseline_values,
            effect=effect,
            has_non_numeric=has_non_numeric,
        )
    )
    predicted_status = "rejected" if reason else "promoted"

    if output_format == "json":
        _emit_json(
            {
                "proposal_id": proposal_id,
                "dry_run": True,
                "predicted_status": predicted_status,
                "reason": reason or "ok",
                "proposed_values": dict(proposal.proposed_values),
                "baseline_values": dict(baseline_values or {}),
                "effect_size": effect,
                "sample_size": proposal.sample_size,
            }
        )
        return

    color = "green" if predicted_status == "promoted" else "red"
    console.print("[dim](dry run — pass --commit to apply)[/dim]")
    console.print(
        f"[{color}]WOULD {predicted_status.upper()}[/{color}] {proposal_id}: "
        f"{reason or 'policy gate would pass'}"
    )
    console.print(f"  proposed: {json.dumps(proposal.proposed_values)}")
    console.print(f"  baseline: {json.dumps(baseline_values or {})}")
    if effect is not None:
        console.print(f"  effect_size: {effect:.4f}")
