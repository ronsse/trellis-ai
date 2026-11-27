"""``trellis curate corrections``: review the corrections agents proposed.

MCP ``propose_correction`` records a proposal and changes nothing served
(:mod:`trellis.feedback.corrections`). This group is where a person decides:

* ``list`` and ``show`` read the ``CORRECTION_PROPOSED`` events.
* ``apply`` re-runs every check the tool ran, stores the corrected text as a
  new memory through the governed ``evidence.ingest`` path, stamps the target
  superseded by it with :func:`trellis.mcp.supersession.supersede_document`
  (the path ``save_memory(supersedes=...)`` uses), then emits
  ``CORRECTION_APPLIED``. The target is then withheld only by the pairwise
  supersession rule: while its successor is a candidate in the same pack.
* ``reject`` emits ``CORRECTION_REJECTED`` and writes nothing else.

The operator CLI reads at ``restricted`` clearance. Exit codes do not depend on
``--format``.
"""

from __future__ import annotations

from typing import Any

import structlog
import typer
from rich.markup import escape

from trellis.feedback.corrections import (
    CORRECTION_STATUSES,
    CORRECTIONS_CLI_SURFACE,
    CorrectionProposal,
    CorrectionStatus,
    correction_refusal,
    find_proposal,
    list_proposals,
)
from trellis.mutate import (
    Command,
    CommandResult,
    CommandStatus,
    Operation,
    build_correction_executor,
    build_curate_executor,
    build_evidence_ingest_command,
)
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY, Sensitivity
from trellis.stores.registry import StoreRegistry
from trellis_cli.exit_codes import (
    EXIT_IDEMPOTENCY,
    EXIT_OK,
    EXIT_POLICY,
    EXIT_STORE,
    EXIT_VALIDATION,
)
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import _get_registry

console = build_console()
logger = structlog.get_logger(__name__)

corrections_app = typer.Typer(
    no_args_is_help=True,
    help="Review corrections agents proposed with propose_correction.",
)

#: The operator CLI is trusted to read every label.
_CLI_CLEARANCE: Sensitivity = "restricted"

_EXIT_BY_STATUS: dict[CommandStatus, int] = {
    CommandStatus.REJECTED: EXIT_POLICY,
    CommandStatus.DUPLICATE: EXIT_IDEMPOTENCY,
    CommandStatus.FAILED: EXIT_STORE,
}


class _RefusedError(Exception):
    """A review command that cannot proceed; nothing more is written."""

    def __init__(self, message: str, code: int, **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra


def _finish(
    output_format: str,
    *,
    code: int,
    payload: dict[str, Any],
    text: list[str],
) -> None:
    """Print in the asked format, then exit with ``code`` whichever it was."""
    if output_format == "json":
        emit_json({"status": "ok" if code == EXIT_OK else "error", **payload})
    else:
        for line in text:
            console.print(line, soft_wrap=True)
    # Below the format branch: both formats exit alike
    # (tests/unit/test_format_exit_parity_rule.py).
    if code != EXIT_OK:
        raise typer.Exit(code=code)


def _refused_output(output_format: str, exc: _RefusedError) -> None:
    _finish(
        output_format,
        code=exc.code,
        payload={"message": exc.message, **exc.extra},
        text=[f"[red]{escape(exc.message)}[/red]"],
    )


def _require_proposal(registry: StoreRegistry, proposal_id: str) -> CorrectionProposal:
    proposal = find_proposal(registry.operational.event_log, proposal_id)
    if proposal is None:
        msg = f"no correction proposal {proposal_id}"
        raise _RefusedError(msg, EXIT_VALIDATION)
    return proposal


def _require_pending(proposal: CorrectionProposal) -> None:
    if proposal.status != "pending":
        msg = f"correction {proposal.proposal_id} is already {proposal.status}"
        raise _RefusedError(msg, EXIT_IDEMPOTENCY, proposal_status=proposal.status)


def _resolve(
    registry: StoreRegistry,
    proposal: CorrectionProposal,
    *,
    decision: str,
    reason: str,
    successor_id: str | None = None,
) -> CommandResult:
    args: dict[str, Any] = {"decision": decision, "reason": reason}
    if successor_id is not None:
        args["successor_id"] = successor_id
    command = Command(
        operation=Operation.CORRECTION_RESOLVE,
        target_id=proposal.proposal_id,
        target_type="correction",
        args=args,
        requested_by=CORRECTIONS_CLI_SURFACE,
    )
    return build_correction_executor(registry).execute(command)


def _require_success(result: CommandResult, what: str) -> None:
    if result.status is not CommandStatus.SUCCESS:
        msg = f"{what} was not recorded ({result.status.value}): {result.message}"
        raise _RefusedError(msg, _EXIT_BY_STATUS.get(result.status, EXIT_STORE))


def apply_correction(
    registry: StoreRegistry, proposal_id: str, *, reason: str
) -> dict[str, Any]:
    """Apply a pending proposal; return what was written. Raises ``_RefusedError``."""
    proposal = _require_proposal(registry, proposal_id)
    _require_pending(proposal)
    refusal, target = correction_refusal(
        registry,
        item_id=proposal.target_id,
        corrected_content=proposal.corrected_content,
        reason=proposal.reason,
        clearance=_CLI_CLEARANCE,
    )
    if refusal is not None or target is None:
        message = refusal.message if refusal is not None else "target missing"
        msg = f"cannot apply {proposal_id}: {message}"
        raise _RefusedError(
            msg,
            EXIT_VALIDATION,
            refusal=refusal.reason if refusal is not None else "target_missing",
        )

    document_store = registry.knowledge.document_store
    existing = document_store.get_by_hash(proposal.content_digest)
    if existing is not None:
        successor_id = str(existing["doc_id"])
    else:
        metadata: dict[str, Any] = {
            "correction_of": proposal.target_id,
            "correction_proposal_id": proposal_id,
        }
        label = (target.get("metadata") or {}).get(DATA_CLASSIFICATION_KEY)
        if label is not None:
            metadata[DATA_CLASSIFICATION_KEY] = label
        command = build_evidence_ingest_command(
            content=proposal.corrected_content,
            metadata=metadata,
            requested_by=CORRECTIONS_CLI_SURFACE,
            derive_idempotency=False,
        )
        result = build_curate_executor(registry, clearance=_CLI_CLEARANCE).execute(
            command
        )
        _require_success(result, "the corrected memory")
        if result.created_id is None:
            msg = "the corrected memory was stored without an id"
            raise _RefusedError(msg, EXIT_STORE)
        successor_id = result.created_id

    from trellis.mcp.supersession import supersede_document  # noqa: PLC0415

    error = supersede_document(
        registry,
        doc_id=proposal.target_id,
        successor=successor_id,
        requested_by=CORRECTIONS_CLI_SURFACE,
        clearance=_CLI_CLEARANCE,
    )
    if error is not None:
        msg = (
            f"stored {successor_id} but could not mark {proposal.target_id} "
            f"superseded: {error}; the proposal stays pending"
        )
        raise _RefusedError(msg, EXIT_STORE, successor_id=successor_id)
    _require_success(
        _resolve(
            registry,
            proposal,
            decision="applied",
            reason=reason,
            successor_id=successor_id,
        ),
        "the applied verdict",
    )
    return {
        "proposal_id": proposal_id,
        "target_id": proposal.target_id,
        "successor_id": successor_id,
        "proposal_status": "applied",
    }


def reject_correction(
    registry: StoreRegistry, proposal_id: str, *, reason: str
) -> dict[str, Any]:
    """Reject a pending proposal; nothing but the verdict event is written."""
    proposal = _require_proposal(registry, proposal_id)
    _require_pending(proposal)
    if not reason.strip():
        msg = "--reason must not be blank"
        raise _RefusedError(msg, EXIT_VALIDATION)
    _require_success(
        _resolve(registry, proposal, decision="rejected", reason=reason),
        "the rejected verdict",
    )
    return {
        "proposal_id": proposal_id,
        "target_id": proposal.target_id,
        "proposal_status": "rejected",
    }


@corrections_app.command("list")
def list_command(
    status: str | None = typer.Option(
        None,
        "--status",
        help="Only proposals with this status: pending|applied|rejected.",
    ),
    limit: int = typer.Option(1000, "--limit", min=1, help="Most proposals read."),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """List correction proposals, newest first, without their proposed text."""
    if status is not None and status not in CORRECTION_STATUSES:
        _refused_output(
            output_format,
            _RefusedError(
                f"--status must be one of {', '.join(CORRECTION_STATUSES)}",
                EXIT_VALIDATION,
            ),
        )
        return
    wanted: CorrectionStatus | None = status  # type: ignore[assignment]
    rows = list_proposals(
        _get_registry().operational.event_log, status=wanted, limit=limit
    )
    text = [
        f"{escape(p.proposal_id)}  {p.status:<8}  target {escape(p.target_id)}  "
        f"{p.content_length} chars  {escape(p.reason)}"
        for p in rows
    ] or ["No correction proposals."]
    _finish(
        output_format,
        code=EXIT_OK,
        payload={"count": len(rows), "proposals": [p.summary() for p in rows]},
        text=text,
    )


@corrections_app.command("show")
def show_command(
    proposal_id: str = typer.Argument(..., help="The proposal id."),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Show one proposal, its proposed text and any verdict."""
    try:
        proposal = _require_proposal(_get_registry(), proposal_id)
    except _RefusedError as exc:
        _refused_output(output_format, exc)
        return
    detail = proposal.detail()
    text = [
        f"{key}: {escape(str(value))}"
        for key, value in detail.items()
        if key != "corrected_content"
    ]
    text += ["", "corrected_content:", escape(proposal.corrected_content)]
    _finish(output_format, code=EXIT_OK, payload={"proposal": detail}, text=text)


@corrections_app.command("apply")
def apply_command(
    proposal_id: str = typer.Argument(..., help="The proposal id."),
    reason: str = typer.Option(
        "applied by reviewer", "--reason", help="Recorded on the verdict event."
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Store the corrected text as a new memory superseding the target."""
    try:
        written = apply_correction(_get_registry(), proposal_id, reason=reason)
    except _RefusedError as exc:
        _refused_output(output_format, exc)
        return
    _finish(
        output_format,
        code=EXIT_OK,
        payload=written,
        text=[
            (
                f"Applied {escape(proposal_id)}: {escape(written['successor_id'])} "
                f"supersedes {escape(written['target_id'])}."
            )
        ],
    )


@corrections_app.command("reject")
def reject_command(
    proposal_id: str = typer.Argument(..., help="The proposal id."),
    reason: str = typer.Option(..., "--reason", help="Why the proposal is rejected."),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Reject a proposal; nothing served changes."""
    try:
        written = reject_correction(_get_registry(), proposal_id, reason=reason)
    except _RefusedError as exc:
        _refused_output(output_format, exc)
        return
    _finish(
        output_format,
        code=EXIT_OK,
        payload=written,
        text=[f"Rejected {escape(proposal_id)}; nothing served changed."],
    )


def register(curate_app: typer.Typer) -> None:
    """Mount ``corrections`` onto the ``curate`` Typer app."""
    curate_app.add_typer(corrections_app, name="corrections")


__all__ = [
    "apply_correction",
    "corrections_app",
    "register",
    "reject_correction",
]
