"""CLI entry for the capture sweep — ``python -m trellis_workers.session_capture``.

Also installed as the ``trellis-session-capture`` console script and reachable
as ``trellis worker capture-sessions``; all three run :func:`run_sweep`, the
single machine-side wrapper here. It builds the store registry from the
operator's Trellis config, builds the local distillation model client from that
config, runs one sweep, and writes the JSON report to stdout. The systemd timer
that schedules this, and the env flags it reads, are documented in
``docs/agent-guide/session-auto-capture.md``.

**The judge is not optional.** ``distill_session`` fail-closes on a missing
client, so a sweep without one triggers nothing, writes nothing, and advances
no watermark — a perfectly clean-looking no-op. Both the missing-client case
(:class:`CaptureJudgeUnavailableError`) and the per-session case
(:func:`judge_unavailable_sessions`) therefore end in a non-zero exit rather
than a warning nobody reads.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from trellis.logging import configure_stderr_logging
from trellis.stores.registry import StoreRegistry
from trellis_workers.session_capture.capture import (
    DEFAULT_SAMPLE_DENOMINATOR,
    DEFAULT_SOURCE_SYSTEM,
    run_capture,
)
from trellis_workers.session_capture.distill import DEFAULT_DISTILL_MODEL

if TYPE_CHECKING:
    from trellis.llm import LLMClient
    from trellis_workers.session_capture.models import CaptureReport

logger = structlog.get_logger(__name__)

_ENV_ROOT = "TRELLIS_CAPTURE_TRANSCRIPTS_ROOT"
_ENV_WATERMARK = "TRELLIS_CAPTURE_WATERMARK"
_ENV_SAMPLE = "TRELLIS_CAPTURE_SAMPLE_DENOMINATOR"
_ENV_SOURCE_SYSTEM = "TRELLIS_CAPTURE_SOURCE_SYSTEM"
_ENV_MODEL = "TRELLIS_DISTILL_MODEL"

#: Exit codes. Anything non-zero means "this sweep did not judge everything it
#: saw" — the systemd unit surfaces that instead of logging a clean success.
EXIT_OK = 0
EXIT_JUDGE_UNAVAILABLE = 1

#: Actionable remediation appended to every judge-unavailable message.
JUDGE_REMEDIATION = (
    "Configure an 'llm:' block in config.yaml (provider, api_key_env, model, "
    "base_url for a local endpoint) and install the matching extra "
    "([llm-openai] / [llm-anthropic])."
)


class CaptureJudgeUnavailableError(RuntimeError):
    """No distillation judge could be built, so the sweep would be a no-op."""


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


def _build_llm_client(registry: StoreRegistry) -> LLMClient:
    """Build the local judge client, or raise loudly.

    This used to swallow every failure and return ``None``, which combined
    with ``distill_session``'s fail-closed contract to turn a misconfigured
    ``llm:`` block into a silent no-op sweep. Raising keeps the fail-closed
    behaviour (nothing is captured without a judge) while making the cause
    reach the operator.
    """
    try:
        client = registry.build_llm_client()
    except Exception as exc:
        msg = f"could not build the distillation judge: {exc}. {JUDGE_REMEDIATION}"
        raise CaptureJudgeUnavailableError(msg) from exc
    if client is None:
        msg = f"no distillation judge is configured. {JUDGE_REMEDIATION}"
        raise CaptureJudgeUnavailableError(msg)
    return client


def judge_unavailable_sessions(report: CaptureReport) -> int:
    """Sessions the judge could not adjudicate during a sweep.

    These are left un-watermarked for a later retry, so the run is a partial
    failure — an explicit count rather than a warning buried in the log.
    """
    return sum(
        1 for warning in report.warnings if warning.get("kind") == "distill_unavailable"
    )


def run_sweep(
    *,
    registry: StoreRegistry | None = None,
    dry_run: bool = False,
) -> CaptureReport:
    """Run one capture sweep against the operator's configured stores.

    Args:
        registry: Store registry to sweep into. ``None`` builds one from the
            config dir — the standalone entry point's behaviour. Callers that
            already own a registry (the ``trellis worker`` front door) pass
            theirs so both paths honour the same config resolution.
        dry_run: Plan the sweep without writing memories or advancing the
            watermark.

    Raises:
        CaptureJudgeUnavailableError: no distillation judge could be built.
    """
    root = Path(os.environ.get(_ENV_ROOT, str(_default_root())))
    watermark = Path(os.environ.get(_ENV_WATERMARK, str(_default_watermark())))
    source_system = os.environ.get(_ENV_SOURCE_SYSTEM, DEFAULT_SOURCE_SYSTEM)
    model_id = os.environ.get(_ENV_MODEL, DEFAULT_DISTILL_MODEL)

    store_registry = registry
    if store_registry is None:
        store_registry = StoreRegistry.from_config_dir()
    client = _build_llm_client(store_registry)

    return run_capture(
        store_registry,
        transcripts_root=root,
        watermark_path=watermark,
        llm_client=client,
        source_system=source_system,
        sample_denominator=_sample_denominator(),
        distill_model_id=model_id,
        dry_run=dry_run,
    )


def main(argv: list[str] | None = None) -> int:
    """Run one capture sweep; return a process exit code."""
    parser = argparse.ArgumentParser(prog="trellis-session-capture")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan the sweep without writing memories or advancing the watermark.",
    )
    args = parser.parse_args(argv)

    # stdout is the report channel; structlog's unconfigured default also
    # writes there, so pin it to stderr before anything can log.
    configure_stderr_logging()

    try:
        report = run_sweep(dry_run=args.dry_run)
    except CaptureJudgeUnavailableError as exc:
        # A misconfigured `llm:` block is an operator error, not a crash: the
        # message already names the fix, and a stack trace would bury it.
        logger.error("capture_judge_unavailable", error=str(exc))  # noqa: TRY400
        sys.stderr.write(f"trellis-session-capture: {exc}\n")
        return EXIT_JUDGE_UNAVAILABLE

    payload = report.to_payload()
    unjudged = judge_unavailable_sessions(report)
    payload["sessions_judge_unavailable"] = unjudged
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    if unjudged:
        sys.stderr.write(
            f"trellis-session-capture: {unjudged} session(s) left unjudged — "
            f"the judge was unreachable. They stay un-watermarked for retry.\n"
        )
        return EXIT_JUDGE_UNAVAILABLE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
