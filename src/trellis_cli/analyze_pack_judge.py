"""``trellis analyze pack-judge``: judge served items offline, then calibrate the judge.

Lives beside ``analyze.py`` rather than in it, the way ``admin backup`` lives
beside ``admin.py``: ``analyze.py`` calls :func:`register` once at its foot.
It sits under ``analyze`` because it is a read of the event log and the
document store that writes one operator-named file and nothing else. It is
measurement, not curation (``curate``) and not a worker, and nothing it
produces feeds serving, tags, demotion or promotion
(:mod:`trellis.learning.pack_judge`).

It records no meta-trace: that would be a write to a Trellis store, and the
shadow judge's one promise is that it makes none.

Two phases, chosen by which path is named. ``--out`` judges, ``--judged``
calibrates; naming both or neither is a validation error.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import structlog
import typer
from rich.markup import escape
from rich.table import Table

from trellis.learning.pack_judge import (
    MIN_GRADED_ITEMS,
    MIN_UNHELPFUL_GRADED,
    JudgeRunSummary,
    PackJudgeCalibration,
    PackJudgeError,
    calibrate_from_log,
    judge_window,
    load_window,
    parse_instant,
)
from trellis.llm.egress import EgressGate
from trellis.llm.routing import LLMConsumer
from trellis.stores.base.event_log import DEFAULT_SCAN_LIMIT
from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import _get_registry

if TYPE_CHECKING:
    from rich.console import Console

    from trellis.stores.base.event_log import EventLog
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

console: Console = build_console()

_UNKNOWN_MODEL = "unknown"


def _window(
    since: str | None, until: str | None, days: int
) -> tuple[datetime, datetime | None]:
    """The window the options name: ``--since`` wins over ``--days``."""
    start = parse_instant(since, option="--since")
    if start is None:
        start = datetime.now(tz=UTC) - timedelta(days=days)
    return start, parse_instant(until, option="--until")


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _render_judge(summary: JudgeRunSummary, out_path: Path) -> None:
    style = "green" if summary.status == "ok" else "yellow"
    console.print(
        f"[bold]Pack judge[/bold] [{style}]{escape(summary.status)}[/{style}] "
        f"(model {escape(summary.model)}, prompt {escape(summary.prompt_version)})"
    )
    console.print(
        f"  {summary.packs} pack(s), {summary.window_items} served item(s) in the "
        f"window; {summary.held_out_packs} held-out pack(s) dropped"
    )
    console.print(
        f"  {summary.already_judged} already judged, {summary.attempted} attempted, "
        f"{summary.rows_written} row(s) written to {escape(str(out_path))}"
    )
    table = Table(show_header=True, header_style="bold")
    table.add_column("label")
    table.add_column("rows", justify="right")
    for label in ("helpful", "unhelpful", "unsure", "unparsed"):
        table.add_row(label, str(getattr(summary, label)))
    for reason, count in summary.unjudgeable.items():
        table.add_row(f"unjudgeable: {escape(reason)}", str(count))
    console.print(table)
    if summary.llm_errors or summary.read_errors:
        console.print(
            f"  [yellow]{summary.llm_errors} model call(s) and "
            f"{summary.read_errors} document read(s) failed; nothing was written "
            "for them, so the next run asks again[/yellow]"
        )
    if summary.status == "aborted":
        console.print("  [red]stopped early: the model endpoint kept failing[/red]")
    if summary.limit_reached:
        console.print("  [dim]--limit reached; run again to continue[/dim]")
    if summary.scan.truncated:
        console.print(f"  [yellow]{escape(summary.scan.note)}[/yellow]")


def _render_calibration(report: PackJudgeCalibration) -> None:
    if report.status == "refused":
        console.print(
            f"[bold]Pack judge calibration[/bold] [yellow]refused[/yellow]: "
            f"{escape(report.refusal.value if report.refusal else '')}"
        )
        console.print(f"  {escape(report.message or '')}")
    else:
        console.print(
            f"[bold]Pack judge calibration[/bold] (model {escape(report.model or '')}, "
            f"prompt {escape(report.prompt_version)})"
        )
    matrix = report.matrix
    table = Table(show_header=True, header_style="bold", title="agent verdict vs judge")
    table.add_column("agent \\ judge")
    for column in ("helpful", "unhelpful", "unsure"):
        table.add_column(column, justify="right")
    for name, row in (
        ("helpful", matrix.agent_helpful),
        ("unhelpful", matrix.agent_unhelpful),
    ):
        table.add_row(name, str(row.helpful), str(row.unhelpful), str(row.unsure))
    console.print(table)
    console.print(
        f"  {report.graded_overlap} agent-graded item(s) answered by the judge "
        f"(minimum {report.min_graded}); {report.graded_unusable} graded item(s) "
        f"unparsed or unjudgeable"
    )
    if report.status == "ok":
        console.print(f"  agreement overall        {_pct(report.agreement)}")
        unhelpful = (
            _pct(report.unhelpful_agreement)
            if report.unhelpful_agreement_withheld is None
            else f"withheld ({escape(report.unhelpful_agreement_withheld)})"
        )
        console.print(
            f"  agreement, unhelpful side {unhelpful}  [dim](the harder target)[/dim]"
        )
        console.print(f"  agreement, helpful side   {_pct(report.helpful_agreement)}")
        kappa = (
            _num(report.kappa)
            if report.kappa_unavailable is None
            else f"undefined ({escape(report.kappa_unavailable)})"
        )
        console.print(
            f"  Cohen's kappa             {kappa}  "
            f"[dim](n={report.kappa_n}, unsure left out)[/dim]"
        )
    cover = report.coverage
    console.print(
        f"  coverage: {cover.judged_items}/{cover.injected_items} items, "
        f"{cover.judged_tokens}/{cover.injected_tokens} tokens "
        f"({_pct(cover.token_coverage)})"
    )
    useful = report.useful
    console.print(
        f"  useful tokens over the judged set: judge-implied "
        f"{_pct(useful.judge_useful_fraction)}, agent-graded lower bound "
        f"{_pct(useful.agent_lower_bound)} (agents graded "
        f"{_pct(useful.agent_graded_token_share)} of the judged tokens)"
    )
    if report.judged_rows_outside_window:
        console.print(
            f"  [dim]{report.judged_rows_outside_window} judged row(s) fall outside "
            "this window[/dim]"
        )
    for note in report.notes:
        console.print(f"  [dim]note: {escape(note)}[/dim]")


def pack_judge_command(
    out: Path | None = typer.Option(  # noqa: B008 - typer option default
        None,
        "--out",
        help=(
            "Judge phase: append one JSONL row per served (pack, item) here. "
            "Resumable: a pair already in the file for this prompt version and "
            "model is skipped."
        ),
    ),
    judged: Path | None = typer.Option(  # noqa: B008 - typer option default
        None,
        "--judged",
        help=(
            "Calibrate phase: the JSONL a judge run wrote; compare it to agent grades."
        ),
    ),
    since: str | None = typer.Option(
        None, "--since", help="Window start, an ISO-8601 date or time (UTC if naive)."
    ),
    until: str | None = typer.Option(
        None, "--until", help="Window end (exclusive), an ISO-8601 date or time."
    ),
    days: int = typer.Option(
        30, "--days", min=1, help="Window length when --since is not given."
    ),
    limit: int = typer.Option(
        0,
        "--limit",
        min=0,
        help=(
            "Judge phase: stop after attempting this many items (0 = no cap). "
            "Items an agent graded are judged first."
        ),
    ),
    scan_limit: int = typer.Option(
        DEFAULT_SCAN_LIMIT,
        "--scan-limit",
        min=1,
        help="Max events to scan per event type; the newest are kept.",
    ),
    model_id: str = typer.Option(
        "",
        "--model-id",
        help=(
            "Label recorded on each judged row (default: the route's configured "
            "model). In the calibrate phase, selects which judge's rows to use."
        ),
    ),
    min_graded: int = typer.Option(
        MIN_GRADED_ITEMS,
        "--min-graded",
        min=1,
        help="Calibrate phase: refuse below this many agent-graded items.",
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Judge served pack items offline with a model, or measure that judge.

    Shadow only. Nothing here feeds serving, tags, demotion or promotion: a
    judge's label is not evidence of unhelpfulness until a measured agreement
    floor holds, and the calibrate phase is how that floor is measured. The
    judge writes one operator-named JSONL file (ids, label, model, prompt
    version, timestamp, token count; no intent or item text) and nothing to any
    Trellis store.

    Judge: ``--out judged.jsonl`` reads the served packs in the window, drops
    held-out ones, and asks the model whether each item helps its pack's intent.
    The model comes from the ``llm:`` block of config.yaml, route
    ``pack_judge``, and may be any OpenAI-compatible endpoint (``base_url``).

    Calibrate: ``--judged judged.jsonl`` joins those rows to agent feedback and
    prints a confusion matrix, agreement (overall and on the unhelpful side),
    Cohen's kappa, coverage, and the judge-implied useful-token fraction beside
    the agent-graded lower bound. Refused below ``--min-graded`` graded items.
    """
    if (out is None) == (judged is None):
        message = (
            "Name exactly one phase: --out <file> to judge, or --judged <file> "
            "to calibrate."
        )
        raise PackJudgeError(message)
    start, end = _window(since, until, days)
    registry = _get_registry()
    if judged is not None:
        _calibrate_phase(
            registry.operational.event_log,
            judged,
            start=start,
            end=end,
            scan_limit=scan_limit,
            model=model_id or None,
            min_graded=min_graded,
            output_format=output_format,
        )
    if out is not None:
        _judge_phase(
            registry,
            out,
            start=start,
            end=end,
            scan_limit=scan_limit,
            limit=limit,
            model_id=model_id,
            output_format=output_format,
        )


def _calibrate_phase(
    event_log: EventLog,
    judged: Path,
    *,
    start: datetime,
    end: datetime | None,
    scan_limit: int,
    model: str | None,
    min_graded: int,
    output_format: str,
) -> NoReturn:
    """Print the calibration and exit; a refusal exits like a bad input."""
    report = calibrate_from_log(
        event_log,
        judged,
        since=start,
        until=end,
        scan_limit=scan_limit,
        model=model,
        min_graded=min_graded,
        min_unhelpful=MIN_UNHELPFUL_GRADED,
    )
    # The exit sits below the format branch (test_format_exit_parity_rule).
    exit_code = EXIT_OK if report.status == "ok" else EXIT_VALIDATION
    if output_format == "json":
        emit_json(report.model_dump(mode="json"))
    else:
        _render_calibration(report)
    raise typer.Exit(code=exit_code)


def _judge_phase(
    registry: StoreRegistry,
    out: Path,
    *,
    start: datetime,
    end: datetime | None,
    scan_limit: int,
    limit: int,
    model_id: str,
    output_format: str,
) -> NoReturn:
    """Judge the window into ``out`` and exit; any failed call exits non-zero."""
    from trellis_cli.worker import _require_llm_client_or_exit  # noqa: PLC0415

    client = _require_llm_client_or_exit(
        LLMConsumer.PACK_JUDGE, command="analyze pack-judge"
    )
    document_store = registry.knowledge.document_store
    label = (
        model_id or registry.llm_route(LLMConsumer.PACK_JUDGE).model or _UNKNOWN_MODEL
    )
    window = load_window(
        registry.operational.event_log, since=start, until=end, scan_limit=scan_limit
    )
    summary = asyncio.run(
        judge_window(
            window,
            document_store=document_store,
            client=client,
            gate=EgressGate.for_route(registry, LLMConsumer.PACK_JUDGE, document_store),
            model=label,
            out_path=out,
            limit=limit,
        )
    )
    # A failed model call or document read wrote nothing for its item, so a
    # non-zero exit tells a retrying script the rerun has work to do.
    exit_code = EXIT_OK if summary.status == "ok" else EXIT_STORE
    if output_format == "json":
        emit_json(summary.model_dump(mode="json"))
    else:
        _render_judge(summary, out)
    raise typer.Exit(code=exit_code)


def register(analyze_app: typer.Typer) -> None:
    """Mount ``pack-judge`` onto the ``analyze`` Typer app."""
    analyze_app.command("pack-judge")(pack_judge_command)


__all__ = ["pack_judge_command", "register"]
