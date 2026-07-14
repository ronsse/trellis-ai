"""CLI entry for the capture sweep — ``python -m trellis_workers.session_capture``.

Thin machine-side wrapper: builds the store registry from the operator's
Trellis config, builds the local distillation model client from that config,
runs one sweep, and writes the JSON report to stdout. The systemd timer that
schedules this, and the env flags it reads, are documented in
``docs/agent-guide/session-auto-capture.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import structlog

from trellis.stores.registry import StoreRegistry
from trellis_workers.session_capture.capture import (
    DEFAULT_SAMPLE_DENOMINATOR,
    DEFAULT_SOURCE_SYSTEM,
    run_capture,
)
from trellis_workers.session_capture.distill import DEFAULT_DISTILL_MODEL

logger = structlog.get_logger(__name__)

_ENV_ROOT = "TRELLIS_CAPTURE_TRANSCRIPTS_ROOT"
_ENV_WATERMARK = "TRELLIS_CAPTURE_WATERMARK"
_ENV_SAMPLE = "TRELLIS_CAPTURE_SAMPLE_DENOMINATOR"
_ENV_SOURCE_SYSTEM = "TRELLIS_CAPTURE_SOURCE_SYSTEM"
_ENV_MODEL = "TRELLIS_DISTILL_MODEL"


def _config_dir() -> Path:
    return Path(os.environ.get("TRELLIS_CONFIG_DIR", str(Path.home() / ".trellis")))


def _default_root() -> Path:
    return Path.home() / ".claude" / "projects"


def _default_watermark() -> Path:
    return _config_dir() / "capture-watermark.json"


def _sample_denominator() -> int:
    raw = os.environ.get(_ENV_SAMPLE, "").strip()
    if not raw:
        return DEFAULT_SAMPLE_DENOMINATOR
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SAMPLE_DENOMINATOR
    return value if value >= 1 else DEFAULT_SAMPLE_DENOMINATOR


def _build_llm_client(registry: StoreRegistry) -> object | None:
    """Best-effort local model client from the registry config."""
    try:
        return registry.build_llm_client()
    except Exception:
        logger.warning("capture_llm_client_unavailable")
        return None


def main(argv: list[str] | None = None) -> int:
    """Run one capture sweep; return a process exit code."""
    parser = argparse.ArgumentParser(prog="trellis-session-capture")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan the sweep without writing memories or advancing the watermark.",
    )
    args = parser.parse_args(argv)

    root = Path(os.environ.get(_ENV_ROOT, str(_default_root())))
    watermark = Path(os.environ.get(_ENV_WATERMARK, str(_default_watermark())))
    source_system = os.environ.get(_ENV_SOURCE_SYSTEM, DEFAULT_SOURCE_SYSTEM)
    model_id = os.environ.get(_ENV_MODEL, DEFAULT_DISTILL_MODEL)

    registry = StoreRegistry.from_config_dir()
    client = _build_llm_client(registry)

    report = run_capture(
        registry,
        transcripts_root=root,
        watermark_path=watermark,
        llm_client=client,  # type: ignore[arg-type]
        source_system=source_system,
        sample_denominator=_sample_denominator(),
        distill_model_id=model_id,
        dry_run=args.dry_run,
    )
    sys.stdout.write(json.dumps(report.to_payload(), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
