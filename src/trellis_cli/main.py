"""Trellis CLI — trellis."""

from __future__ import annotations

import typer

from trellis_cli.admin import admin_app
from trellis_cli.analyze import analyze_app
from trellis_cli.curate import curate_app
from trellis_cli.demo import demo_app
from trellis_cli.ingest import ingest_app
from trellis_cli.metrics import metrics_app
from trellis_cli.policy import policy_app
from trellis_cli.retrieve import retrieve_app

app = typer.Typer(
    name="trellis",
    help="Trellis — shared experience store for AI agents and teams.",
    no_args_is_help=True,
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


if __name__ == "__main__":
    app()
