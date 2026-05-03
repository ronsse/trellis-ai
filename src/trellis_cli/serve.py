"""``trellis serve`` — run the Trellis REST API + UI.

Thin wrapper around ``trellis_api.app.main`` that exposes deployment-time
flags (``--host``, ``--port``, ``--config-dir``) suitable for container
ENTRYPOINTs. Structured logging is configured before uvicorn starts so
startup messages land in the same JSON stream as request logs.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from trellis_api.app import DEFAULT_HOST, DEFAULT_PORT

serve_app = typer.Typer(
    help="Run the Trellis REST API and UI.",
    invoke_without_command=True,
    no_args_is_help=False,
)


@serve_app.callback()
def serve(
    host: str = typer.Option(
        DEFAULT_HOST,
        "--host",
        help=(
            "Bind address. Defaults to 127.0.0.1 (loopback-only) so the "
            "unauthenticated API isn't exposed on a fresh install. Set "
            "TRELLIS_API_HOST=0.0.0.0 (or pass --host 0.0.0.0) for "
            "container deployments that need to listen on the pod IP."
        ),
    ),
    port: int = typer.Option(
        DEFAULT_PORT,
        "--port",
        help=f"TCP port to listen on (default {DEFAULT_PORT}).",
    ),
    config_dir: str = typer.Option(
        "",
        "--config-dir",
        help=(
            "Trellis config directory (contains config.yaml). Overrides "
            "TRELLIS_CONFIG_DIR. Defaults to ~/.trellis."
        ),
    ),
) -> None:
    if config_dir:
        os.environ["TRELLIS_CONFIG_DIR"] = str(Path(config_dir).expanduser().resolve())

    from trellis_api.app import main  # noqa: PLC0415

    main(host=host, port=port)
