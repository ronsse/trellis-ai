"""Trellis CLI — trellis."""

from __future__ import annotations

import logging
import os
import sys

import structlog
import typer

from trellis_cli.admin import admin_app
from trellis_cli.analyze import analyze_app
from trellis_cli.curate import curate_app
from trellis_cli.demo import demo_app
from trellis_cli.ingest import ingest_app
from trellis_cli.metrics import metrics_app
from trellis_cli.policy import policy_app
from trellis_cli.retrieve import retrieve_app
from trellis_cli.serve import serve_app


def _configure_cli_logging() -> None:
    """Route structlog output to stderr so it can't corrupt ``--format json``.

    CLI subcommands write machine-readable payloads to stdout. structlog's
    default ``PrintLoggerFactory`` writes to ``sys.stdout``, which means
    a single store-init log line interleaves with the JSON output and
    breaks any caller piping the result into ``jq`` or ``json.loads``.
    Pinning the factory to ``sys.stderr`` keeps logs visible to operators
    while leaving stdout exclusively for the command's output.

    Honours ``TRELLIS_LOG_LEVEL`` (defaults to INFO) so noisy commands
    can be quieted without code changes. Mirrors the equivalent fix in
    ``trellis.mcp.server._configure_mcp_logging``.

    Wired as a Typer callback rather than a console-script wrapper so
    operators don't need to re-run ``pip install`` after upgrades — the
    callback fires inside ``app`` itself, which is the published
    entry point.
    """
    level_name = os.environ.get("TRELLIS_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


app = typer.Typer(
    name="trellis",
    help="Trellis — shared experience store for AI agents and teams.",
    no_args_is_help=True,
    callback=_configure_cli_logging,
)

# Register command groups
app.add_typer(admin_app, name="admin", help="Administration and setup")

worker_app = typer.Typer(help="Run curation workers", no_args_is_help=True)

app.add_typer(ingest_app, name="ingest")
app.add_typer(curate_app, name="curate")
app.add_typer(retrieve_app, name="retrieve")
app.add_typer(analyze_app, name="analyze")
app.add_typer(
    metrics_app,
    name="metrics",
    help="Feedback-driven parameter-tuning telemetry and promotion",
)
app.add_typer(policy_app, name="policy", help="Manage governance policies")
app.add_typer(demo_app, name="demo", help="Demo data and exploration")
app.add_typer(worker_app, name="worker")
app.add_typer(serve_app, name="serve", help="Run the Trellis REST API + UI")


if __name__ == "__main__":
    app()
