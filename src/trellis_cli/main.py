"""Trellis CLI — trellis."""

from __future__ import annotations

import os

import typer

from trellis.logging import configure_stderr_logging
from trellis_cli.admin import admin_app
from trellis_cli.analyze import analyze_app
from trellis_cli.curate import curate_app
from trellis_cli.demo import demo_app
from trellis_cli.extract_refresh import extract_app
from trellis_cli.ingest import ingest_app
from trellis_cli.metrics import metrics_app
from trellis_cli.policy import policy_app
from trellis_cli.retrieve import retrieve_app
from trellis_cli.serve import serve_app
from trellis_cli.worker import worker_app

app = typer.Typer(
    name="trellis",
    help="Trellis — shared experience store for AI agents and teams.",
    no_args_is_help=True,
)


@app.callback()
def _root(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show INFO-level logs (default: WARNING)."
    ),
    debug: bool = typer.Option(False, "--debug", help="Show DEBUG-level logs."),
) -> None:
    # CLI defaults to WARNING so per-command stderr stays quiet next to
    # the friendly Rich output. ``TRELLIS_LOG_LEVEL`` always wins so
    # operators can pin a level globally; the flags only set defaults
    # when the env var is absent. Routing structlog to stderr keeps
    # ``--format json`` stdout parseable.
    if "TRELLIS_LOG_LEVEL" not in os.environ:
        if debug:
            os.environ["TRELLIS_LOG_LEVEL"] = "DEBUG"
        elif verbose:
            os.environ["TRELLIS_LOG_LEVEL"] = "INFO"
        else:
            os.environ["TRELLIS_LOG_LEVEL"] = "WARNING"
    configure_stderr_logging()


# Register command groups
app.add_typer(admin_app, name="admin", help="Administration and setup")

app.add_typer(ingest_app, name="ingest")
app.add_typer(
    extract_app,
    name="extract",
    help="Re-run extractors and emit structural diffs",
)
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
